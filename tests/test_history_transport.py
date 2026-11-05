"""Read-only SSH failures never become worker-offline or leak diagnostics."""
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_captain.core.terminal_hub import Hub, HistoryReadError
from test_terminal_hub import FakeTerminals
from tests.test_ssh_source import unpack_source

SESSION = "11111111-1111-4111-8111-111111111111"
TERMINAL = "ceohub-0123456789abcdef01234567"
SECRET = "PRIVATE_PROVIDER_TOKEN_AND_COMMAND"


def reply(result=None, *, returncode=0, stderr="", stdout=None):
    if stdout is None:
        stdout = json.dumps({"ok": True, "result": result or {"home": "/home/worker"}})
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class HistoryTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.terminals = FakeTerminals()
        self.hub = Hub(self.temp.name, terminals=self.terminals)
        self.hub.hosts = {"remote": {"transport": "ssh", "label": "Remote", "target": "test-ssh-alias"}}
        self.terminals.add(TERMINAL, host="remote", state="agent", screen="VISIBLE REPORT TAIL")
        self.hub.store.remember("remote", "worker", "Work", mode="terminal", terminal_id=TERMINAL)
        self.binding = {"state": "bound", "binding_evidence": "held_rollout", "session_id": SESSION,
            "rollout_path": "/home/worker/.codex/sessions/exact.jsonl", "codex_home": "/home/worker/.codex",
            "process_identity": {"pid": 4242, "start_ticks": 100}}

    def tearDown(self):
        self.hub.close()
        self.temp.cleanup()

    def history(self):
        return self.hub.history("remote", {"op": "probe"})

    def test_normal_read_only_history_disables_connection_multiplexing(self):
        with patch("agent_captain.core.terminal_hub.subprocess.run", return_value=reply()) as run:
            self.assertEqual(self.history(), {"home": "/home/worker"})
        args = run.call_args.args[0]
        self.assertIn("BatchMode=yes", args)
        self.assertIn("ControlMaster=no", args)
        self.assertIn("ControlPersist=no", args)
        self.assertIn("ConnectTimeout=30", args)
        self.assertEqual(run.call_args.kwargs["timeout"], 45)
        self.assertEqual(args[-2], "test-ssh-alias")
        self.assertIn("def main(request)", unpack_source(run.call_args.kwargs["input"]))
        self.assertEqual(self.terminals.writes, [])

    def test_original_pane_probe_has_bounded_slow_ssh_budget(self):
        from agent_captain.transport.terminal_client import SSH_CONTROL_CONNECT_TIMEOUT, SSH_CONTROL_TIMEOUT
        probe = {"ok": True, "exists": False, "reason": "session_missing"}
        response = reply(stdout="CEO_HUB_PTY_RESULT=" + json.dumps(probe))
        with patch("agent_captain.core.terminal_hub.subprocess.run", return_value=response) as run:
            self.assertEqual(self.hub._probe_remote_terminal("remote", TERMINAL), probe)
        self.assertIn(f"ConnectTimeout={SSH_CONTROL_CONNECT_TIMEOUT}", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["timeout"], SSH_CONTROL_TIMEOUT)
        self.assertEqual(run.call_args.args[0][-2], "test-ssh-alias")
        self.assertEqual(run.call_args.args[0][-1], "python3 - probe --session " + TERMINAL)
        self.assertIn("def probe_existing_session", unpack_source(run.call_args.kwargs["input"]))
        self.assertEqual(self.terminals.writes, [])
        self.assertFalse(any(call[0] == "create" for call in self.terminals.calls))

    def test_fixed_ssh_failure_classifications_do_not_export_raw_streams(self):
        cases = (
            ("Connection timed out during banner exchange", "ssh_handshake_failed"),
            ("kex_exchange_identification: Connection closed by remote host", "ssh_handshake_failed"),
            ("Permission denied (publickey).", "ssh_authentication_failed"),
            ("Too many authentication failures", "ssh_authentication_failed"),
            ("Host key verification failed.", "ssh_host_key_failed"),
            ("WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!", "ssh_host_key_failed"),
            ("ssh: connect to host example port 22: Connection timed out", "ssh_timeout"),
            ("ssh: connect to host example port 22: Connection refused", "ssh_transport_unavailable"),
        )
        for diagnostic, code in cases:
            with self.subTest(code=code, diagnostic=diagnostic), patch("agent_captain.core.terminal_hub.subprocess.run",
                    return_value=reply(returncode=255, stdout=SECRET, stderr=diagnostic + "\n" + SECRET)):
                with self.assertRaises(HistoryReadError) as caught:
                    self.history()
            self.assertEqual(caught.exception.reason_code, code)
            self.assertNotIn(SECRET, str(caught.exception))
            self.assertNotIn(diagnostic, str(caught.exception))

    def test_spawn_and_deadline_errors_never_export_command_or_exception_text(self):
        failures = (
            (FileNotFoundError(SECRET), "ssh_transport_unavailable"),
            (BrokenPipeError(SECRET), "ssh_transport_unavailable"),
            (subprocess.TimeoutExpired(SECRET, 30, output=SECRET, stderr=SECRET), "ssh_timeout"),
        )
        for failure, code in failures:
            with self.subTest(code=code), patch("agent_captain.core.terminal_hub.subprocess.run", side_effect=failure):
                with self.assertRaises(HistoryReadError) as caught:
                    self.history()
            self.assertEqual(caught.exception.reason_code, code)
            self.assertNotIn(SECRET, str(caught.exception))

    def test_nontransport_remote_exit_has_separate_safe_classification(self):
        with patch("agent_captain.core.terminal_hub.subprocess.run", return_value=reply(returncode=127, stdout=SECRET,
                stderr="python3: command not found " + SECRET)):
            with self.assertRaises(HistoryReadError) as caught:
                self.history()
        self.assertEqual(caught.exception.reason_code, "remote_read_failed")
        self.assertNotIn(SECRET, str(caught.exception))

    def test_protocol_errors_do_not_export_stdout_or_remote_error(self):
        invalid = (
            SECRET, "{bad " + SECRET, "[]", "null", "{}",
            json.dumps({"ok": "true", "result": {"private": SECRET}}),
            json.dumps({"ok": True, "result": SECRET}),
        )
        for stdout in invalid:
            with self.subTest(stdout=stdout), patch("agent_captain.core.terminal_hub.subprocess.run", return_value=reply(stdout=stdout)):
                with self.assertRaises(HistoryReadError) as caught:
                    self.history()
            self.assertEqual(caught.exception.reason_code, "protocol_invalid")
            self.assertNotIn(SECRET, str(caught.exception))
        with patch("agent_captain.core.terminal_hub.subprocess.run", return_value=reply(stdout=json.dumps({"ok": False, "error": SECRET}))):
            with self.assertRaises(HistoryReadError) as caught:
                self.history()
        self.assertEqual(caught.exception.reason_code, "remote_read_failed")
        self.assertNotIn(SECRET, str(caught.exception))

    def test_display_can_remain_connected_when_independent_history_handshake_fails(self):
        with patch("agent_captain.core.terminal_hub.subprocess.run", return_value=reply(returncode=255,
                stdout=SECRET, stderr="kex_exchange_identification: Connection reset\n" + SECRET)):
            result = self.hub.read_agent("remote", "worker")
        self.assertTrue(result["connected"])
        self.assertEqual(result["status"], "agent")
        self.assertEqual(result["source"], "terminal")
        self.assertEqual(result["screen"], "VISIBLE REPORT TAIL")
        self.assertEqual(result["messages"], [])
        self.assertFalse(result["content_complete"])
        self.assertFalse(result["history_complete"])
        self.assertEqual(result["output_binding"]["state"], "unavailable")
        self.assertEqual(result["output_binding"]["reason_code"], "ssh_handshake_failed")
        self.assertEqual(result["read_transport"]["attempts"], 2)
        self.assertEqual([row["reason_code"] for row in result["read_transport"]["attempt_trace"]],
                         ["ssh_handshake_failed", "ssh_handshake_failed"])
        self.assertTrue(all(type(row["elapsed_ms"]) is int and row["elapsed_ms"] >= 0
                            for row in result["read_transport"]["attempt_trace"]))
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(self.terminals.writes, [])

    def test_post_read_verification_failure_withholds_all_semantic_output(self):
        failure = {"state": "unavailable", "reason_code": "session_identity_changed"}
        with patch("agent_captain.core.terminal_hub.subprocess.run", return_value=reply(failure)) as run:
            result = self.hub.read_agent("remote", "worker")
        self.assertEqual(run.call_count, 1)
        self.assertTrue(result["connected"])
        self.assertEqual(result["source"], "terminal")
        self.assertEqual(result["messages"], [])
        self.assertFalse(result["content_complete"])
        self.assertNotIn("REAL BODY", json.dumps(result))
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(self.terminals.writes, [])

    def test_normal_exact_session_read_keeps_original_verification_flow(self):
        output = {"id": SESSION, "source": "codex_rollout", "messages": [{"role": "assistant", "text": "BEGIN\nFULL BODY\nEND"}],
                  "content_complete": True, "history_complete": True, "status": "completed_in_history"}
        binding = {**self.binding, "terminal_id": TERMINAL, "historical": False, "live_session_verified": True}
        with patch("agent_captain.core.terminal_hub.subprocess.run", return_value=reply(
                {"state": "bound", "binding": binding, "output": output})) as run:
            result = self.hub.read_agent("remote", "worker")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs["timeout"], 75,
                         "Both identity checks and semantic output share one SSH transaction")
        self.assertTrue(all("ConnectTimeout=30" in call.args[0] for call in run.call_args_list))
        self.assertEqual(result["source"], "codex_rollout")
        self.assertEqual(result["messages"], output["messages"])
        self.assertEqual(result["screen"], "VISIBLE REPORT TAIL")
        self.assertTrue(result["output_binding"]["live_session_verified"])
        self.assertEqual(result["output_binding"]["session_id"], SESSION)
        self.assertEqual(result["conversation_status"], "completed_in_history")
        self.assertEqual(result["read_transport"]["attempts"], 1)
        self.assertTrue(result["read_transport"]["batched_identity_checks"])
        self.assertEqual(self.terminals.writes, [])

    def test_pagination_failure_is_safe_error_not_screen_output(self):
        for field in ("cursor", "after_cursor"):
            with self.subTest(field=field), patch("agent_captain.core.terminal_hub.subprocess.run", return_value=reply(returncode=255,
                    stdout=SECRET, stderr="Host key verification failed. " + SECRET)):
                with self.assertRaises(ValueError) as caught:
                    self.hub.read_agent("remote", "worker", **{field: "same-session-page"})
            self.assertIn("原游标未被判为失效", str(caught.exception))
            self.assertNotIn("请重新读取最新", str(caught.exception))
            self.assertIn("ssh_host_key_failed", str(caught.exception))
            self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(self.terminals.writes, [])

    def test_unknown_output_exception_is_not_exported(self):
        with patch.object(self.hub, "history", side_effect=RuntimeError(SECRET)):
            result = self.hub.read_agent("remote", "worker")
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["output_binding"]["reason_code"], "output_read_failed")
        self.assertNotIn(SECRET, json.dumps(result))

    def test_retry_success_retains_sanitized_attempt_trace(self):
        output = {"id": SESSION, "source": "codex_rollout", "messages": [], "content_complete": True}
        binding = {**self.binding, "terminal_id": TERMINAL, "historical": False, "live_session_verified": True}
        with patch("agent_captain.core.terminal_hub.subprocess.run", side_effect=[
                reply(returncode=255, stderr="kex_exchange_identification: Connection closed " + SECRET),
                reply({"state": "bound", "binding": binding, "output": output})]):
            result = self.hub.read_agent("remote", "worker")
        self.assertEqual(result["source"], "codex_rollout")
        trace = result["read_transport"]["attempt_trace"]
        self.assertEqual([item["reason_code"] for item in trace], ["ssh_handshake_failed", "ok"])
        self.assertEqual([item["attempt"] for item in trace], [1, 2])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(self.terminals.writes, [])


if __name__ == "__main__":
    unittest.main()
