"""Bounded slow-SSH reads and explicit pre-attachment retry, without live SSH."""
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from agent_captain.integrations import mcp_bridge
from agent_captain.core.terminal_hub import Hub
from tests.test_terminal_hub import FakeTerminals
from tests.test_agent_management import TERMINAL, IDENTITY


class SlowSSHReconnectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.terminals = FakeTerminals()
        self.hub = Hub(self.temp.name, terminals=self.terminals)
        self.hub.hosts = {"remote": {"transport": "ssh", "target": "fake-ssh-target", "label": "Remote"}}
        self.terminals.add(TERMINAL, "remote")
        self.hub.store.remember("remote", "worker", "Worker", mode="terminal", terminal_id=TERMINAL, cwd="/work/project")
        self.terminals.records[TERMINAL].update(connected=False, state="exited", agent_ready=False)
        self.probes = []
        self.answer = {"ok": True, "exists": True, "identity": IDENTITY}
        self.hub._probe_remote_terminal = self.probe

    def tearDown(self):
        self.hub.close()
        self.temp.cleanup()

    def probe(self, host, terminal):
        self.probes.append((host, terminal))
        return self.answer

    def creates(self):
        return [call for call in self.terminals.calls if call[0] == "create"]

    def test_explicit_same_key_pre_attach_route_retry_remains_idempotent_after_success(self):
        self.answer = {"ok": False, "exists": False, "reason": "route_unavailable"}
        first = self.hub.reconnect_agent("remote", "worker", request_key="route")
        self.assertEqual(first["operation_state"], "blocked")
        self.assertTrue(first["recovery"]["retryable"])
        self.assertEqual(self.creates(), [])
        self.answer = {"ok": True, "exists": True, "identity": IDENTITY}
        recovered = self.hub.reconnect_agent("remote", "worker", request_key="route")
        self.assertEqual(recovered["operation_state"], "done")
        self.assertEqual(recovered["recovery"]["code"], "awaiting_foreground")
        self.assertEqual(len(self.probes), 3)
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(self.terminals.writes, [])
        self.hub.reconnect_agent("remote", "worker", request_key="route")
        self.assertEqual(len(self.probes), 3)
        self.assertEqual(len(self.creates()), 1)

    def test_known_legacy_pre_attach_route_receipt_can_retry_same_key(self):
        self.answer = {"ok": False, "exists": False, "reason": "route_unavailable"}
        first = self.hub.reconnect_agent("remote", "worker", request_key="legacy-route")
        recovery = dict(first["recovery"])
        recovery.pop("retryable")
        self.hub.store.finish_operation("agent-reconnect:legacy-route", {"state": "blocked", "recovery": recovery})
        self.answer = {"ok": True, "exists": True, "identity": IDENTITY}
        recovered = self.hub.reconnect_agent("remote", "worker", request_key="legacy-route")
        self.assertEqual(recovered["operation_state"], "done")
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(self.terminals.writes, [])

    def test_post_attach_route_failure_does_not_retry_same_key(self):
        with patch.object(self.terminals, "snapshot", return_value=dict(self.terminals.records[TERMINAL])):
            result = self.hub.reconnect_agent("remote", "worker", request_key="post-attach-route")
        self.assertEqual(result["operation_state"], "blocked")
        self.assertEqual(result["recovery"]["code"], "route_unavailable")
        count = len(self.probes)
        repeated = self.hub.reconnect_agent("remote", "worker", request_key="post-attach-route")
        self.assertEqual(repeated["operation_state"], "blocked")
        self.assertEqual(len(self.probes), count)
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(self.terminals.writes, [])

    def test_pre_attach_auth_or_hostkey_blocked_receipt_never_retries(self):
        for reason in ("ssh_auth_required", "ssh_host_key_required"):
            with self.subTest(reason=reason):
                self.answer = {"ok": False, "exists": False, "reason": reason}
                self.hub.reconnect_agent("remote", "worker", request_key=reason)
                count = len(self.probes)
                repeated = self.hub.reconnect_agent("remote", "worker", request_key=reason)
                self.assertEqual(repeated["recovery"]["code"], reason)
                self.assertEqual(len(self.probes), count)
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.terminals.writes, [])

    def test_route_code_without_exact_pre_attach_receipt_never_retries(self):
        fingerprint = self.hub._reconnect_fingerprint("remote", "worker", TERMINAL, "ui", None)
        self.hub.store.begin_operation("agent-reconnect:unproven-route", fingerprint)
        self.hub.store.finish_operation("agent-reconnect:unproven-route", {"state": "blocked", "recovery": {
            "code": "route_unavailable", "message": "Unknown attachment stage", "next_action": "check_connection",
            "retryable": True, "request_key": "unproven-route", "request_origin": "ui"}})
        repeated = self.hub.reconnect_agent("remote", "worker", request_key="unproven-route")
        self.assertEqual(repeated["operation_state"], "blocked")
        self.assertEqual(self.probes, [])
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.terminals.writes, [])

    def test_unknown_post_attach_probe_failure_never_replays_same_key(self):
        with patch.object(self.hub, "_probe_remote_terminal", side_effect=[self.answer,
                {"ok": False, "exists": False, "reason": "route_unavailable"}]) as probe:
            first = self.hub.reconnect_agent("remote", "worker", request_key="unknown-route")
            self.assertEqual(first["operation_state"], "unknown")
            repeated = self.hub.reconnect_agent("remote", "worker", request_key="unknown-route")
            self.assertEqual(repeated["operation_state"], "unknown")
            self.assertEqual(probe.call_count, 2)
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(self.terminals.writes, [])

    def test_pre_attach_receipt_must_match_same_key_and_origin(self):
        for key, receipt_key, origin in (("wrong-receipt-key", "another-key", "ui"),
                                         ("wrong-receipt-origin", "wrong-receipt-origin", "plugin")):
            with self.subTest(key=key):
                fingerprint = self.hub._reconnect_fingerprint("remote", "worker", TERMINAL, "ui", None)
                self.hub.store.begin_operation("agent-reconnect:" + key, fingerprint)
                self.hub.store.finish_operation("agent-reconnect:" + key, {"state": "blocked", "recovery": {
                    "code": "route_unavailable", "message": "SSH 路由暂不可达；请检查机器或恢复 SSH 上游/隧道路由，再由用户重试。不会自动重启服务。",
                    "next_action": "check_connection", "retryable": True,
                    "request_key": receipt_key, "request_origin": origin}})
                result = self.hub.reconnect_agent("remote", "worker", request_key=key)
                self.assertEqual(result["operation_state"], "blocked")
                self.assertEqual(self.probes, [])
                self.assertEqual(self.creates(), [])
                self.assertEqual(self.terminals.writes, [])

    def test_original_pane_check_allows_slow_authentication_without_changing_its_identity_protocol(self):
        reply = subprocess.CompletedProcess([], 0,
            'CEO_HUB_PTY_RESULT=' + json.dumps(self.answer) + '\n', '')
        with patch('agent_captain.core.terminal_hub.subprocess.run', return_value=reply) as run:
            value = Hub._probe_remote_terminal(self.hub, 'remote', TERMINAL)
        self.assertEqual(value, self.answer)
        self.assertEqual(run.call_args.kwargs['timeout'], 150)
        self.assertIn('ConnectTimeout=60', run.call_args.args[0])
        self.assertEqual(run.call_args.args[0][-1], 'python3 - probe --session ' + TERMINAL)


class SlowSSHMCPBudgetTests(unittest.TestCase):
    def service_timeout(self, name, arguments=None, bridge=mcp_bridge):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{"ok":true}'
        with patch.object(bridge, "read_connection", return_value=("http://127.0.0.1:39001", "fixture-capability-not-real")), \
                patch.object(bridge, "build_opener", return_value=opener):
            result = bridge.call_service(name, arguments or {})
        self.assertEqual(result, {"ok": True})
        opener.open.assert_called_once()
        return opener.open.call_args.kwargs["timeout"]

    def test_read_and_reconnect_have_distinct_bounded_ssh_budgets(self):
        for name, timeout in (("read_agent", 330), ("reconnect_agent", 600)):
            with self.subTest(tool=name):
                self.assertEqual(self.service_timeout(name, {"agent_id": "remote::worker", "request_key": "same-key"}), timeout)

    def test_reconnect_budget_covers_original_pane_checks_optional_continuity_reads_and_display_rpc(self):
        from agent_captain.transport.terminal_client import FOREGROUND_RPC_TIMEOUT, SSH_CONTROL_TIMEOUT
        checks = 2 * SSH_CONTROL_TIMEOUT + 2 * 45
        display_create_and_registry = 20 + 2 * 3
        self.assertLess(checks + FOREGROUND_RPC_TIMEOUT + display_create_and_registry,
                        self.service_timeout("reconnect_agent"))

    def test_generated_plugin_uses_matching_budgets_without_reading_credentials(self):
        root = Path(__file__).resolve().parents[1]
        generator_spec = importlib.util.spec_from_file_location("slow_ssh_plugin_generator", root / "scripts/configure_plugin.py")
        generator = importlib.util.module_from_spec(generator_spec)
        generator_spec.loader.exec_module(generator)
        with tempfile.TemporaryDirectory() as temporary:
            result = generator.configure_plugin(Path(temporary) / "plugin", repo_root=root)
            filename = Path(result["plugin_root"]) / "mcp_bridge.py"
            spec = importlib.util.spec_from_file_location("slow_ssh_generated_plugin_fixture", filename)
            generated = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules), patch.object(sys, "path", [str(filename.parent), *sys.path]):
                spec.loader.exec_module(generated)
            for name in ("read_agent", "reconnect_agent", "send_agent_message", "send_terminal_input", "create_agent", "update_agent", "poll_events"):
                with self.subTest(tool=name):
                    self.assertEqual(self.service_timeout(name, bridge=generated), self.service_timeout(name))

    def test_read_budget_covers_snapshot_and_existing_bounded_history_policy(self):
        from agent_captain.transport.terminal_client import FOREGROUND_RPC_TIMEOUT
        self.assertLess(FOREGROUND_RPC_TIMEOUT + 75 + 45 + 2 * 3, self.service_timeout("read_agent"))

    def test_prompt_budget_covers_in_flight_probes_and_both_fresh_identity_checks(self):
        from agent_captain.transport.terminal_client import FOREGROUND_RPC_TIMEOUT, PROMPT_RPC_TIMEOUT, SSH_CONTROL_TIMEOUT
        self.assertGreater(PROMPT_RPC_TIMEOUT, 4 * SSH_CONTROL_TIMEOUT)
        self.assertGreater(self.service_timeout("send_agent_message"), FOREGROUND_RPC_TIMEOUT + PROMPT_RPC_TIMEOUT)

    def test_real_broker_deadlines_fit_inside_client_and_mcp_waits(self):
        """Cross-language guard: raising a broker limit must not orphan callers."""
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node.js is required to inspect the broker control budget')
        root = Path(__file__).resolve().parents[1]
        raw = subprocess.check_output([node, '-e',
            "process.stdout.write(JSON.stringify(require('./terminal/terminal_broker.cjs').SSH_CONTROL_BUDGET))"],
            cwd=root, timeout=10, text=True, encoding='utf-8')
        budget = json.loads(raw)
        self.assertGreaterEqual(budget['commandMs'], 150000, 'Allow a complete slow SSH authentication/control exchange')
        self.assertLess(budget['commandMs'], 180000, 'The auxiliary command remains bounded')
        self.assertLess(budget['connectSeconds'], budget['commandMs'] / 1000)
        from agent_captain.transport.terminal_client import TerminalClient
        client = TerminalClient()
        with patch.object(client, 'request', return_value={}) as request:
            client.snapshot('ceohub-0123456789abcdef', 1)
            foreground_rpc = request.call_args.kwargs['timeout']
            self.assertGreater(foreground_rpc, budget['commandMs'] / 1000)
            client.write('ceohub-0123456789abcdef', 'resize', {'cols': 90, 'rows': 28})
            self.assertEqual(request.call_args.kwargs['timeout'], foreground_rpc)
            client.write('ceohub-0123456789abcdef', 'prompt', {'text': 'fixture'})
            prompt_rpc = request.call_args.kwargs['timeout']
        self.assertGreater(prompt_rpc, 4 * budget['commandMs'] / 1000)
        self.assertGreater(self.service_timeout('send_agent_message'), foreground_rpc + prompt_rpc)
        self.assertGreater(self.service_timeout('send_terminal_input'), foreground_rpc)
        self.assertGreater(self.service_timeout('read_agent'), foreground_rpc + 75 + 45)
        self.assertGreater(self.service_timeout('create_agent'), 20 + foreground_rpc + prompt_rpc,
                           'Creation may include authorized startup input followed by one prompt')

    def test_deliveries_input_and_other_tools_keep_original_120_second_budget(self):
        for name in ("update_agent", "get_agent", "list_active_agents",
                     "create_terminal", "read_terminal_history", "get_delivery"):
            with self.subTest(tool=name):
                self.assertEqual(self.service_timeout(name), 120)

    def test_event_poll_keeps_its_original_distinct_budget(self):
        self.assertEqual(self.service_timeout("poll_events"), 65)
        self.assertEqual(self.service_timeout("poll_events", {"timeout_seconds": 30}), 65)


if __name__ == "__main__":
    unittest.main()
