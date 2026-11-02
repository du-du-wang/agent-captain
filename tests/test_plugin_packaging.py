"""Isolated AgentCaptain plugin generation/cache tests; no real Hub or Codex config."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("configure_agent_captain_plugin", ROOT / "scripts" / "configure_plugin.py")
configure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure)


class PluginPackagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-captain-plugin-")
        self.base = Path(self.temp.name).resolve()
        self.clone = self.base / "clone with spaces"
        self.clone.mkdir()
        self.source_plugin = self.clone / "plugins" / "agent-captain"
        shutil.copytree(ROOT / "plugins" / "agent-captain", self.source_plugin)
        marketplace = self.clone / ".agents" / "plugins"
        marketplace.mkdir(parents=True)
        shutil.copyfile(ROOT / ".agents" / "plugins" / "marketplace.json", marketplace / "marketplace.json")
        self.package_source = self.clone / "src" / "agent_captain"
        for relative in ("integrations/mcp_bridge.py", "core/management_policy.py"):
            target = self.package_source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / "src" / "agent_captain" / relative, target)
        self.output = self.base / "retained local marketplace"

    def tearDown(self):
        self.temp.cleanup()

    def generate(self, output=None):
        return configure.configure_plugin(output or self.output, repo_root=self.clone)

    @staticmethod
    def load(path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def test_source_identity_and_machine_independent_template(self):
        manifest = self.load(self.source_plugin / ".codex-plugin" / "plugin.json")
        self.assertEqual(manifest["name"], "agent-captain")
        self.assertEqual(manifest["version"], "0.7.0")
        self.assertEqual(manifest["interface"]["displayName"], "AgentCaptain")
        self.assertEqual(self.load(self.source_plugin / ".mcp.json"), {"mcpServers": {}})
        catalog = self.load(self.clone / ".agents" / "plugins" / "marketplace.json")
        self.assertEqual(catalog["name"], "agent-captain-source")
        self.assertEqual(catalog["plugins"][0]["source"]["path"], "./plugins/agent-captain")
        skill = (self.source_plugin / "skills" / "agent-captain" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: agent-captain", skill)
        self.assertIn("当前 Codex 对话里的模型就是主控", skill)
        self.assertNotIn("\u961f\u957f", skill)
        self.assertNotIn("\u961f\u957f", json.dumps(manifest, ensure_ascii=False))
        self.assertIn("用户保留最终决定权", skill)
        self.assertIn("content_complete", skill)

    def test_generated_command_uses_running_python_and_retained_bridge(self):
        result = self.generate()
        plugin = Path(result["plugin_root"])
        server = self.load(plugin / ".mcp.json")["mcpServers"]["agent_captain"]
        self.assertEqual(server["command"], str(Path(sys.executable).resolve()))
        self.assertEqual(server["args"], ["-u", str(plugin / "mcp_bridge.py")])
        self.assertEqual(server["env"], {"AGENTCAPTAIN_CONNECTION_FILE": str(self.clone / ".runtime" / "service.json")})
        self.assertEqual((plugin / "mcp_bridge.py").read_bytes(),
                         (self.package_source / "integrations" / "mcp_bridge.py").read_bytes())
        self.assertEqual((plugin / "management_policy.py").read_bytes(),
                         (self.package_source / "core" / "management_policy.py").read_bytes())
        self.assertEqual(result["plugin"], "agent-captain@agent-captain-local")
        self.assertTrue(result["retain_generated_directory"])
        self.assertEqual(result["commands"][1], "codex plugin add agent-captain@agent-captain-local")
        generated_catalog = self.load(self.output / ".agents" / "plugins" / "marketplace.json")
        self.assertEqual(generated_catalog["name"], "agent-captain-local")
        self.assertNotEqual(generated_catalog["name"],
                            self.load(self.clone / ".agents" / "plugins" / "marketplace.json")["name"])
        self.assertNotIn("${PLUGIN_ROOT}", json.dumps(server))

    def test_default_output_is_checkout_runtime(self):
        result = configure.configure_plugin(repo_root=self.clone)
        self.assertEqual(Path(result["marketplace_root"]), self.clone / ".runtime" / "plugin-marketplace")

    def test_ignored_legacy_launchers_are_not_plugin_source(self):
        # Old installations can keep local compatibility entries at these
        # paths. A plugin must contain the canonical standalone implementation,
        # never a checkout bootstrap or any unrelated local additions.
        for name in ("mcp_bridge.py", "management_policy.py"):
            (self.clone / name).write_text("legacy-bootstrap-not-for-bundling", encoding="utf-8")
        result = self.generate()
        plugin = Path(result["plugin_root"])
        for name, relative in (("mcp_bridge.py", "integrations/mcp_bridge.py"),
                               ("management_policy.py", "core/management_policy.py")):
            self.assertEqual((plugin / name).read_bytes(),
                             (self.package_source / relative).read_bytes())
            self.assertNotIn(b"legacy-bootstrap-not-for-bundling", (plugin / name).read_bytes())

    def test_generated_readme_links_to_repository_documentation(self):
        result = self.generate()
        readme = (Path(result["plugin_root"]) / "README.md").read_text(encoding="utf-8")
        self.assertIn(
            "https://github.com/du-du-wang/agent-captain/blob/main/docs/management-session-product.md",
            readme,
        )
        self.assertNotIn("../../docs/", readme)

    def test_only_allowlisted_code_is_copied_no_live_descriptor_or_auth(self):
        # Fake fixtures, not actual user secrets. They must never enter a plugin.
        (self.source_plugin / "auth.json").write_text('{"token":"fixture-not-a-credential"}', encoding="utf-8")
        (self.clone / "service.json").write_text('{"token":"fixture-not-a-credential"}', encoding="utf-8")
        (self.clone / ".runtime").mkdir()
        (self.clone / ".runtime" / "terminal-broker.json").write_text("fixture-state", encoding="utf-8")
        result = self.generate()
        all_files = {path.relative_to(self.output).as_posix() for path in self.output.rglob("*") if path.is_file()}
        self.assertEqual(all_files, {
            ".agent-captain-generated.json", ".agents/plugins/marketplace.json",
            "plugins/agent-captain/.codex-plugin/plugin.json", "plugins/agent-captain/.mcp.json",
            "plugins/agent-captain/skills/agent-captain/SKILL.md", "plugins/agent-captain/README.md",
            "plugins/agent-captain/mcp_bridge.py",
            "plugins/agent-captain/management_policy.py",
        })
        generated_config = self.load(Path(result["plugin_root"]) / ".mcp.json")
        self.assertEqual(generated_config["mcpServers"]["agent_captain"]["env"],
                         {"AGENTCAPTAIN_CONNECTION_FILE": str(self.clone / ".runtime" / "service.json")})
        self.assertNotIn("fixture-not-a-credential", json.dumps(generated_config))
        self.assertEqual(self.load(self.output / ".agent-captain-generated.json")["generator"], "agent-captain-configure-plugin")

    def test_existing_unknown_directory_preserved(self):
        self.output.mkdir()
        evidence = self.output / "user-data.txt"
        evidence.write_text("preserve me", encoding="utf-8")
        with self.assertRaises(configure.ConfigurationError):
            self.generate()
        self.assertEqual(evidence.read_text(encoding="utf-8"), "preserve me")
        self.assertEqual(list(self.output.iterdir()), [evidence])

    def test_existing_empty_directory_refused(self):
        self.output.mkdir()
        with self.assertRaises(configure.ConfigurationError):
            self.generate()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_existing_file_preserved(self):
        self.output.write_text("original file", encoding="utf-8")
        with self.assertRaises(configure.ConfigurationError):
            self.generate()
        self.assertEqual(self.output.read_text(encoding="utf-8"), "original file")

    def test_repeat_generation_never_overwrites_known_output(self):
        self.generate()
        before = {path.relative_to(self.output): path.read_bytes() for path in self.output.rglob("*") if path.is_file()}
        with self.assertRaises(configure.ConfigurationError):
            self.generate()
        after = {path.relative_to(self.output): path.read_bytes() for path in self.output.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_new_output_supports_safe_update(self):
        self.generate()
        another = self.base / "marketplace-v2"
        result = self.generate(another)
        self.assertEqual(Path(result["marketplace_root"]), another)
        self.assertTrue(self.output.is_dir())

    def test_legacy_generation_preserves_ceo_identity_and_new_display_without_reading_live_state(self):
        source_before = {path.relative_to(self.source_plugin): path.read_bytes()
                         for path in self.source_plugin.rglob("*") if path.is_file()}
        result = configure.configure_plugin(self.output, repo_root=self.clone, legacy_ceo_identity=True)
        plugin = Path(result["plugin_root"])
        manifest = self.load(plugin / ".codex-plugin" / "plugin.json")
        self.assertEqual(result["plugin"], "ceo-hub@ceo-hub-local")
        self.assertEqual(result["version"], "0.7.0")
        self.assertTrue(result["legacy_ceo_identity"])
        self.assertEqual(plugin, self.output / "plugins" / "ceo-hub")
        self.assertEqual(manifest["name"], "ceo-hub")
        self.assertEqual(manifest["version"], "0.7.0")
        self.assertEqual(manifest["interface"]["displayName"], "AgentCaptain")
        skill = (plugin / "skills" / "ceo-hub" / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(skill.startswith("---\nname: ceo-hub\n"))
        self.assertFalse((plugin / "skills" / "agent-captain").exists())
        self.assertIn("`ceo-hub` compatibility skill", (plugin / "README.md").read_text(encoding="utf-8"))
        catalog = self.load(self.output / ".agents" / "plugins" / "marketplace.json")
        self.assertEqual(catalog["name"], "ceo-hub-local")
        self.assertEqual(catalog["plugins"][0]["name"], "ceo-hub")
        self.assertEqual(catalog["plugins"][0]["source"], {"source": "local", "path": "./plugins/ceo-hub"})
        servers = self.load(plugin / ".mcp.json")["mcpServers"]
        self.assertEqual(set(servers), {"ceo_hub"})
        self.assertEqual(servers["ceo_hub"]["args"], ["-u", str(plugin / "mcp_bridge.py")])
        self.assertEqual(servers["ceo_hub"]["env"], {"CEO_HUB_CONNECTION_FILE": str(self.clone / ".runtime" / "service.json")})
        self.assertEqual(result["commands"][1], "codex plugin add ceo-hub@ceo-hub-local")
        marker = self.load(self.output / ".agent-captain-generated.json")
        self.assertTrue(marker["legacy_ceo_identity"])
        self.assertEqual(marker["plugin"], "ceo-hub")
        source_after = {path.relative_to(self.source_plugin): path.read_bytes()
                        for path in self.source_plugin.rglob("*") if path.is_file()}
        self.assertEqual(source_before, source_after)
        self.assertFalse((self.output / "plugins" / "agent-captain").exists())

    def test_pron_compatibility_keeps_only_technical_identifiers_and_allowlisted_files(self):
        source_before = {path.relative_to(self.source_plugin): path.read_bytes()
                         for path in self.source_plugin.rglob("*") if path.is_file()}
        (self.clone / ".runtime").mkdir()
        (self.clone / ".runtime" / "service.json").write_text(
            'fake-descriptor-must-not-be-read', encoding="utf-8")
        result = configure.configure_plugin(self.output, repo_root=self.clone, legacy_pron_identity=True)
        plugin = Path(result["plugin_root"])
        self.assertEqual(result["plugin"], "pron-hub@pron-hub-local")
        self.assertFalse(result["legacy_ceo_identity"])
        self.assertTrue(result["legacy_pron_identity"])
        manifest = self.load(plugin / ".codex-plugin" / "plugin.json")
        self.assertEqual(manifest["name"], "pron-hub")
        self.assertEqual(manifest["interface"]["displayName"], "AgentCaptain")
        skill = (plugin / "skills" / "pron-hub" / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(skill.startswith("---\nname: pron-hub\n"))
        self.assertIn("当前 Codex 对话里的模型就是主控", skill)
        self.assertNotIn("\u961f\u957f", skill)
        self.assertNotIn("PRON Hub", skill)
        self.assertNotIn("CEO", skill)
        self.assertFalse((plugin / "skills" / "agent-captain").exists())
        servers = self.load(plugin / ".mcp.json")["mcpServers"]
        self.assertEqual(set(servers), {"pron_hub"})
        self.assertEqual(servers["pron_hub"]["env"], {
            "PRON_HUB_CONNECTION_FILE": str(self.clone / ".runtime" / "service.json")})
        catalog = self.load(self.output / ".agents" / "plugins" / "marketplace.json")
        self.assertEqual(catalog["name"], "pron-hub-local")
        self.assertEqual(catalog["interface"]["displayName"], "AgentCaptain local tools")
        self.assertEqual(catalog["plugins"][0]["source"]["path"], "./plugins/pron-hub")
        generated = {path.relative_to(self.output).as_posix(): path.read_bytes()
                     for path in self.output.rglob("*") if path.is_file()}
        self.assertEqual(set(generated), {
            ".agent-captain-generated.json", ".agents/plugins/marketplace.json",
            "plugins/pron-hub/.codex-plugin/plugin.json", "plugins/pron-hub/.mcp.json",
            "plugins/pron-hub/skills/pron-hub/SKILL.md", "plugins/pron-hub/README.md",
            "plugins/pron-hub/mcp_bridge.py", "plugins/pron-hub/management_policy.py",
        })
        self.assertTrue(self.load(self.output / ".agent-captain-generated.json")["legacy_pron_identity"])
        for contents in generated.values():
            self.assertNotIn(b'fake-descriptor-must-not-be-read', contents)
        source_after = {path.relative_to(self.source_plugin): path.read_bytes()
                        for path in self.source_plugin.rglob("*") if path.is_file()}
        self.assertEqual(source_before, source_after)

    def test_compatibility_identities_are_mutually_exclusive_before_output_creation(self):
        with self.assertRaises(configure.ConfigurationError):
            configure.configure_plugin(self.output, repo_root=self.clone,
                                       legacy_ceo_identity=True, legacy_pron_identity=True)
        self.assertFalse(self.output.exists())

    def test_legacy_generation_remains_allowlisted_and_does_not_copy_fake_credentials(self):
        (self.source_plugin / "auth.json").write_text('fake-credential-must-not-be-copied', encoding="utf-8")
        runtime = self.clone / ".runtime"
        runtime.mkdir()
        (runtime / "service.json").write_text('fake-descriptor-must-not-be-read', encoding="utf-8")
        result = configure.configure_plugin(self.output, repo_root=self.clone, legacy_ceo_identity=True)
        generated = {path.relative_to(self.output).as_posix(): path.read_bytes()
                     for path in self.output.rglob("*") if path.is_file()}
        self.assertEqual(set(generated), {
            ".agent-captain-generated.json", ".agents/plugins/marketplace.json",
            "plugins/ceo-hub/.codex-plugin/plugin.json", "plugins/ceo-hub/.mcp.json",
            "plugins/ceo-hub/skills/ceo-hub/SKILL.md", "plugins/ceo-hub/README.md",
            "plugins/ceo-hub/mcp_bridge.py",
            "plugins/ceo-hub/management_policy.py",
        })
        for contents in generated.values():
            self.assertNotIn(b'fake-credential-must-not-be-copied', contents)
            self.assertNotIn(b'fake-descriptor-must-not-be-read', contents)
        self.assertTrue(result["retain_generated_directory"])

    def test_invalid_legacy_skill_conversion_fails_before_creating_output(self):
        skill = self.source_plugin / "skills" / "agent-captain" / "SKILL.md"
        skill.write_text('---\nname: unexpected-identity\n---\nKeep unchanged.', encoding="utf-8")
        with self.assertRaises(configure.ConfigurationError):
            configure.configure_plugin(self.output, repo_root=self.clone, legacy_ceo_identity=True)
        self.assertFalse(self.output.exists())

    def test_invalid_legacy_source_encoding_is_safe_and_does_not_create_output(self):
        skill = self.source_plugin / "skills" / "agent-captain" / "SKILL.md"
        skill.write_bytes(b'\xffinvalid-utf8-fixture')
        with self.assertRaises(configure.ConfigurationError):
            configure.configure_plugin(self.output, repo_root=self.clone, legacy_ceo_identity=True)
        self.assertFalse(self.output.exists())

    def test_machine_specific_source_mcp_refused_before_output(self):
        (self.source_plugin / ".mcp.json").write_text(json.dumps({"mcpServers": {
            "unreviewed": {"command": "/some/private/path", "env": {"secret": "fixture"}}
        }}), encoding="utf-8")
        with self.assertRaises(configure.ConfigurationError):
            self.generate()
        self.assertFalse(self.output.exists())

    def test_invalid_manifest_refused_before_output(self):
        manifest = self.source_plugin / ".codex-plugin" / "plugin.json"
        data = self.load(manifest)
        data["mcpServers"] = "../outside.json"
        manifest.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(configure.ConfigurationError):
            self.generate()
        self.assertFalse(self.output.exists())

    def test_invalid_marketplace_entry_refused_before_output(self):
        path = self.clone / ".agents" / "plugins" / "marketplace.json"
        path.write_text('{"name":"agent-captain-source","plugins":[null]}', encoding="utf-8")
        with self.assertRaises(configure.ConfigurationError):
            self.generate()
        self.assertFalse(self.output.exists())

    def test_missing_bridge_refused_before_output(self):
        (self.package_source / "integrations" / "mcp_bridge.py").unlink()
        with self.assertRaises(configure.ConfigurationError):
            self.generate()
        self.assertFalse(self.output.exists())

    def test_missing_management_policy_refused_before_output(self):
        (self.package_source / "core" / "management_policy.py").unlink()
        with self.assertRaises(configure.ConfigurationError):
            self.generate()
        self.assertFalse(self.output.exists())

    def test_unavailable_interpreter_refused_before_output(self):
        with self.assertRaises(configure.ConfigurationError):
            configure.configure_plugin(self.output, repo_root=self.clone,
                                       python_executable=self.base / "missing-python")
        self.assertFalse(self.output.exists())

    def test_dangling_output_symlink_refused(self):
        try:
            self.output.symlink_to(self.base / "missing-output", target_is_directory=True)
        except OSError:
            self.skipTest("Platform does not permit test symlinks")
        with self.assertRaises(configure.ConfigurationError):
            self.generate()
        self.assertTrue(self.output.is_symlink())
        self.assertFalse((self.base / "missing-output").exists())

    def test_cached_plugin_can_initialize_and_list_tools_from_unrelated_cwd(self):
        result = self.generate()
        # Mimic a host copying this package into an installation cache. The
        # absolute bridge reference remains valid without plugin-root expansion.
        cache = self.base / "isolated codex cache" / "agent-captain-local" / "agent-captain" / "0.7.0"
        shutil.copytree(result["plugin_root"], cache)
        cwd = self.base / "unrelated working directory"
        cwd.mkdir()
        # Demonstrate that no imports/resources rely on the original clone.
        moved_clone = self.base / "moved original clone"
        self.clone.rename(moved_clone)
        server = self.load(cache / ".mcp.json")["mcpServers"]["agent_captain"]
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "packaging-test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        env = dict(os.environ)
        env.pop("PRON_HUB_CONTROLLER_ID", None)
        env.pop("AGENTCAPTAIN_CONTROLLER_ID", None)
        env.pop("CEO_HUB_CONTROLLER_ID", None)
        env["CEO_HUB_CONNECTION_FILE"] = str(self.base / "deliberately-missing-service.json")
        env["PRON_HUB_CONNECTION_FILE"] = str(self.base / "deliberately-missing-service.json")
        env["AGENTCAPTAIN_CONNECTION_FILE"] = str(self.base / "deliberately-missing-service.json")
        process = subprocess.run([server["command"], *server["args"]], cwd=cwd, env=env,
                                 input="\n".join(json.dumps(message) for message in messages) + "\n",
                                 text=True, encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, "")
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([response["id"] for response in responses], [1, 2])
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(responses[0]["result"]["serverInfo"]["version"], "0.7.0")
        names = {tool["name"] for tool in responses[1]["result"]["tools"]}
        self.assertEqual(names, {"list_active_agents", "get_agent", "read_agent", "send_agent_message",
                                 "create_agent", "send_terminal_input", "poll_events", "get_delivery",
                                 "update_agent", "reconnect_agent", "reopen_agent", "get_management_policy"})
        self.assertFalse((self.base / "deliberately-missing-service.json").exists())
        self.assertTrue(Path(server["args"][1]).is_file())

    def test_legacy_cached_package_advertises_new_tools_and_compatible_server_identity(self):
        result = configure.configure_plugin(self.output, repo_root=self.clone, legacy_ceo_identity=True)
        cache = self.base / "isolated legacy cache" / "ceo-hub-local" / "ceo-hub" / "0.7.0"
        shutil.copytree(result["plugin_root"], cache)
        server = self.load(cache / ".mcp.json")["mcpServers"]["ceo_hub"]
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "legacy-cache-test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        env = dict(os.environ)
        env.pop("PRON_HUB_CONTROLLER_ID", None)
        env.pop("AGENTCAPTAIN_CONTROLLER_ID", None)
        env.pop("CEO_HUB_CONTROLLER_ID", None)
        missing = self.base / "missing-no-auth-service.json"
        env.update(CEO_HUB_CONNECTION_FILE=str(missing), PRON_HUB_CONNECTION_FILE=str(missing),
                   AGENTCAPTAIN_CONNECTION_FILE=str(missing))
        process = subprocess.run([server["command"], *server["args"]], cwd=self.base, env=env,
                                 input="\n".join(json.dumps(message) for message in messages) + "\n",
                                 text=True, encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, "")
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        info = responses[0]["result"]["serverInfo"]
        self.assertEqual(info, {"name": "ceo-hub", "title": "AgentCaptain", "version": "0.7.0"})
        names = {item["name"] for item in responses[1]["result"]["tools"]}
        self.assertIn("update_agent", names)
        self.assertIn("reconnect_agent", names)
        self.assertIn("reopen_agent", names)
        self.assertIn("get_management_policy", names)
        self.assertEqual(len(names), 12)
        self.assertFalse(missing.exists())

    def test_cli_generates_json_without_registering_or_installing(self):
        target = self.base / "cli marketplace"
        process = subprocess.run([sys.executable, str(ROOT / "scripts" / "configure_plugin.py"),
                                  "--output", str(target), "--json"], text=True, encoding="utf-8",
                                 capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, "")
        result = json.loads(process.stdout)
        self.assertEqual(Path(result["marketplace_root"]), target)
        self.assertEqual(result["plugin"], "agent-captain@agent-captain-local")
        self.assertTrue(result["retain_generated_directory"])

    def test_cli_explicit_legacy_flag_generates_only_compatible_new_output(self):
        target = self.base / "legacy cli marketplace"
        process = subprocess.run([sys.executable, str(ROOT / "scripts" / "configure_plugin.py"),
                                  "--output", str(target), "--legacy-ceo-identity", "--json"], text=True,
                                 encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, "")
        result = json.loads(process.stdout)
        self.assertEqual(result["plugin"], "ceo-hub@ceo-hub-local")
        self.assertEqual(result["version"], "0.7.0")
        self.assertTrue(result["legacy_ceo_identity"])
        self.assertTrue((target / "plugins" / "ceo-hub" / "skills" / "ceo-hub" / "SKILL.md").is_file())
        self.assertFalse((target / "plugins" / "agent-captain").exists())

    def test_cli_refuses_existing_target_with_nonzero_exit(self):
        self.output.mkdir()
        process = subprocess.run([sys.executable, str(ROOT / "scripts" / "configure_plugin.py"),
                                  "--output", str(self.output)], text=True, encoding="utf-8",
                                 capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(process.stdout, "")
        self.assertIn("Output already exists", process.stderr)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_cli_pron_compatibility_and_conflicting_options(self):
        target = self.base / "pron compatibility marketplace"
        script = str(ROOT / "scripts" / "configure_plugin.py")
        process = subprocess.run([sys.executable, script, "--output", str(target),
                                  "--legacy-pron-identity", "--json"], text=True, encoding="utf-8",
                                 capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result["plugin"], "pron-hub@pron-hub-local")
        self.assertTrue(result["legacy_pron_identity"])
        conflict = self.base / "must not appear"
        process = subprocess.run([sys.executable, script, "--output", str(conflict),
                                  "--legacy-pron-identity", "--legacy-ceo-identity"],
                                 text=True, encoding="utf-8", capture_output=True, timeout=10)
        self.assertNotEqual(process.returncode, 0)
        self.assertFalse(conflict.exists())


if __name__ == "__main__":
    unittest.main()
