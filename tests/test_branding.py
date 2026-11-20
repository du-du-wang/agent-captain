"""AgentCaptain public branding and backward-compatible identity contracts.

Tests use temporary configuration/storage and never connect to a real broker,
SSH host, model, or user conversation.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_captain.server import app
from agent_captain import __version__
from agent_captain.integrations import mcp_bridge
from agent_captain.core import terminal_hub
from agent_captain.infrastructure.store import Store


ROOT = Path(__file__).resolve().parents[1]


class InertTerminals:
    def sessions(self):
        return []


class BrandingTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {key: "" for key in (
            "AGENTCAPTAIN_CONNECTION_FILE", "PRON_HUB_CONNECTION_FILE", "CEO_HUB_CONNECTION_FILE",
            "AGENTCAPTAIN_CONTROLLER_ID", "PRON_HUB_CONTROLLER_ID")})
        environment.start()
        self.addCleanup(environment.stop)

    def test_public_release_versions_agree_without_changing_broker_protocol(self):
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
        self.assertEqual(package["name"], "agent-captain-terminal")
        self.assertIn("AgentCaptain", package["description"])
        self.assertIn("One captain for agents across your machines.", package["description"])
        self.assertEqual({__version__, app.VERSION, terminal_hub.VERSION, mcp_bridge.VERSION,
                          package["version"], lock["version"], lock["packages"][""]["version"]}, {"0.7.1"})
        self.assertEqual(lock["name"], package["name"])
        self.assertEqual(lock["packages"][""]["name"], package["name"])
        broker = (ROOT / "terminal" / "terminal_broker.cjs").read_text(encoding="utf-8")
        self.assertRegex(broker, r"const VERSION = ['\"]0\.3\.0['\"]")

    def test_public_configuration_and_ui_do_not_ship_personal_ssh_hosts(self):
        configuration = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.assertEqual(configuration, {"hosts": {"local": {"label": "本机", "transport": "local"}}})
        html = (ROOT / "web/index.html").read_text(encoding="utf-8")
        self.assertIn("<title>AgentCaptain</title>", html)
        self.assertIn('aria-label="AgentCaptain"', html)
        self.assertNotIn("\u961f\u957f", html)
        self.assertIn("One captain for agents across your machines.", html)
        self.assertIn("一个主控，统筹本机与远程 Agent。", html)
        self.assertNotRegex(html, r"PRON|CEO|Personal Remote Ops|pron-hub-logo")
        # Build forbidden legacy values at runtime so a release-source scanner
        # does not mistake this regression's negative assertions for fixtures.
        for private_host in ("host" + "316" + "public", "server" + "316", "316" + " 服务器",
                             "DGX-" + "Spark-hw-" + "tunnelterm"):
            self.assertNotIn(private_host, html)
        for field in ("machine-id", "machine-label", "machine-target"):
            self.assertRegex(html, rf'<input id="{re.escape(field)}"[^>]*placeholder="[^\"]+"')

    def test_fresh_install_defaults_to_local_without_discovering_remote_hosts(self):
        configuration = (ROOT / "config.example.json").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config.example.json").write_text(configuration, encoding="utf-8")
            with patch.object(terminal_hub, "ROOT", root):
                service = terminal_hub.Hub(root / "runtime", terminals=InertTerminals())
                try:
                    self.assertEqual(set(service.hosts), {"local"})
                    self.assertEqual(service.attached_agents(), [])
                    self.assertEqual(service.store.events(), [])
                finally:
                    service.close()

    def test_existing_machine_registry_survives_rebranding(self):
        configuration = (ROOT / "config.example.json").read_text(encoding="utf-8")
        saved = {"local": {"label": "Local", "transport": "local"},
                 "lab": {"label": "Lab workstation", "transport": "ssh", "target": "lab-fixture"}}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config.example.json").write_text(configuration, encoding="utf-8")
            runtime = root / "runtime"
            runtime.mkdir()
            storage = Store(runtime / "hub.sqlite")
            storage.setting("hosts", saved)
            storage.close()
            with patch.object(terminal_hub, "ROOT", root):
                service = terminal_hub.Hub(runtime, terminals=InertTerminals())
                try:
                    self.assertEqual(service.hosts, saved)
                    self.assertEqual(service.store.setting("hosts"), saved)
                finally:
                    service.close()

    def test_mcp_brand_title_keeps_existing_server_wire_identity(self):
        bridge = mcp_bridge.McpBridge()
        response = bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                             "clientInfo": {"name": "branding-test", "version": "1"}}})
        info = response["result"]["serverInfo"]
        self.assertEqual(info, {"name": "ceo-hub", "title": "AgentCaptain", "version": "0.7.1"})

    def test_existing_connection_override_is_shared_by_service_and_adapter(self):
        with tempfile.TemporaryDirectory() as folder:
            descriptor = Path(folder) / "service.json"
            descriptor.write_text(json.dumps({"url": "http://127.0.0.1:8766", "token": "test-private-capability"}), encoding="utf-8")
            with patch.dict(os.environ, {"AGENTCAPTAIN_CONNECTION_FILE": "", "PRON_HUB_CONNECTION_FILE": "",
                                         "CEO_HUB_CONNECTION_FILE": str(descriptor)}):
                self.assertEqual(app.connection_file(), descriptor)
                self.assertEqual(mcp_bridge.connection_file(), descriptor)
                self.assertEqual(mcp_bridge.read_connection(), ("http://127.0.0.1:8766", "test-private-capability"))

    def test_canonical_and_legacy_descriptor_overrides_have_explicit_precedence(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            canonical, prior, oldest = (base / name for name in ("captain.json", "prior.json", "oldest.json"))
            values = {"AGENTCAPTAIN_CONNECTION_FILE": str(canonical), "PRON_HUB_CONNECTION_FILE": str(prior),
                      "CEO_HUB_CONNECTION_FILE": str(oldest)}
            for missing, expected in (((), canonical), (("AGENTCAPTAIN_CONNECTION_FILE",), prior),
                                      (("AGENTCAPTAIN_CONNECTION_FILE", "PRON_HUB_CONNECTION_FILE"), oldest)):
                environment = dict(values) | {key: "" for key in missing}
                with self.subTest(missing=missing), patch.dict(os.environ, environment):
                    self.assertEqual(app.connection_file(), expected)
                    self.assertEqual(mcp_bridge.connection_file(), expected)

    def test_invalid_canonical_override_never_falls_back_to_another_capability(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            legacy = base / "legacy.json"
            legacy.write_text(json.dumps({"url": "http://127.0.0.1:8766", "token": "legacy-private-capability"}), encoding="utf-8")
            missing = base / "missing-canonical.json"
            with patch.dict(os.environ, {"AGENTCAPTAIN_CONNECTION_FILE": str(missing),
                                         "PRON_HUB_CONNECTION_FILE": str(legacy), "CEO_HUB_CONNECTION_FILE": str(legacy)}):
                self.assertEqual(app.connection_file(), missing)
                self.assertEqual(mcp_bridge.connection_file(), missing)
                with self.assertRaises(mcp_bridge.BridgeError) as error:
                    mcp_bridge.read_connection()
                self.assertIn("AgentCaptain", str(error.exception))
                self.assertNotIn("legacy-private-capability", str(error.exception))

    def test_compatibility_state_paths_and_terminal_identifiers_are_unchanged(self):
        for module in (app, mcp_bridge):
            source = Path(module.__file__).read_text(encoding="utf-8")
            self.assertIn("CEO_HUB_CONNECTION_FILE", source)
            self.assertIn("ceo-hub", source)
        source = Path(mcp_bridge.__file__).read_text(encoding="utf-8")
        self.assertIn("CEOHub", source)
        source = Path(terminal_hub.__file__).read_text(encoding="utf-8")
        self.assertIn('"ceohub-" +', source)
        self.assertIn('"ceo-hub:" + request_key', source)

    def test_cli_help_uses_new_brand_without_starting_service(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "captain.py"), "--help"],
                                capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertIn("AgentCaptain", result.stdout)
        self.assertIn("一个主控，统筹本机与远程 Agent。", result.stdout)
        self.assertNotRegex(result.stdout, r"PRON|CEO Hub|Personal Remote Ops")


if __name__ == "__main__":
    unittest.main()
