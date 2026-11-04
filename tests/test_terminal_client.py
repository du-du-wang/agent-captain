"""The local terminal capability cannot be redirected or used to start scans."""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from urllib.error import HTTPError

from agent_captain.transport.terminal_client import BrokerError, TerminalClient


class TerminalClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "terminal.json"
        self.env = patch.dict(os.environ, {"CEO_HUB_TERMINAL_FILE": str(self.path)})
        self.env.start()
        self.client = TerminalClient()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def descriptor(self, url="http://127.0.0.1:39001", token="test-capability-0123456789", pid=123):
        self.path.write_text(json.dumps({"url": url, "token": token, "pid": pid, "version": "0.3.0"}), encoding="utf-8")

    def test_only_literal_loopback_without_credentials_paths_or_queries_is_accepted(self):
        for url in ("https://127.0.0.1:3", "http://localhost:3", "http://example.com:3", "http://user@127.0.0.1:3",
                    "http://127.0.0.1:3/private", "http://127.0.0.1:3?token=bad", "http://127.0.0.1:3#bad"):
            with self.subTest(url=url):
                self.descriptor(url)
                with patch.object(self.client.opener, "open", side_effect=AssertionError("no network")), self.assertRaises(ValueError):
                    self.client.request("/sessions")

    def test_descriptor_reloads_for_every_request_after_restart(self):
        self.descriptor()
        self.assertEqual(self.client.descriptor()["pid"], 123)
        self.descriptor(pid=456)
        self.assertEqual(self.client.descriptor()["pid"], 456)

    def test_absent_broker_registry_read_never_starts_a_process(self):
        with patch.object(self.client, "ensure_started", side_effect=AssertionError("no launch")):
            self.assertEqual(self.client.sessions(), [])

    def test_pid_and_version_must_match_to_reuse_a_broker(self):
        self.descriptor()
        with patch.object(self.client, "request", return_value={"pid": 456, "version": "0.3.0"}):
            self.assertFalse(self.client.running())
        with patch.object(self.client, "request", return_value={"pid": 123, "version": "0.1.0"}):
            self.assertFalse(self.client.running())
        with patch.object(self.client, "request", return_value={"pid": 123, "version": "0.3.0"}):
            self.assertTrue(self.client.running())

    def test_error_codes_and_messages_preserve_known_non_delivery(self):
        self.descriptor()
        error = HTTPError("http://127.0.0.1:39001", 409, "Conflict", {}, io.BytesIO(b'{"error":"human_busy","message":"User has a draft"}'))
        with patch.object(self.client.opener, "open", side_effect=error):
            with self.assertRaises(BrokerError) as result:
                self.client.request("/sessions/x/prompt", {"text": "hello"})
        self.assertEqual(result.exception.code, "human_busy")
        self.assertEqual(str(result.exception), "User has a draft")

    def test_browser_initial_output_restores_atomic_screen_dimensions_and_sequence(self):
        view = {"ansi": "\x1b[32mShared screen", "current_seq": 42, "connected": True, "state": "shell", "cols": 93, "rows": 27}
        with patch.object(self.client, "snapshot", return_value=view) as snapshot:
            result = self.client.output("ceohub-0123456789abcdef", after=0)
        self.assertTrue(result["reset"])
        self.assertEqual((result["seq"], result["cols"], result["rows"]), (42, 93, 27))
        self.assertEqual(result["ansi"], view["ansi"])
        snapshot.assert_called_once_with("ceohub-0123456789abcdef", 5000)

    def test_slow_foreground_checks_finish_within_loopback_delivery_budget(self):
        self.descriptor()
        terminal = "ceohub-0123456789abcdef"
        cases = (
            (lambda: self.client.snapshot(terminal, 1), 150),
            (lambda: self.client.output(terminal, after=1), 150),
            (lambda: self.client.write(terminal, "input", {"data": "fixture"}), 150),
            (lambda: self.client.write(terminal, "resize", {"cols": 90, "rows": 28}), 150),
            (lambda: self.client.write(terminal, "prompt", {"text": "fixture"}), 600),
        )
        for call, checks_budget in cases:
            with self.subTest(checks_budget=checks_budget), patch.object(self.client.opener, "open") as opened:
                opened.return_value.__enter__.return_value.read.return_value = b"{}"
                call()
                timeout = opened.call_args.kwargs["timeout"]
                self.assertGreaterEqual(timeout, checks_budget + 5)
                mcp_budget = 840 if checks_budget == 600 else 200
                self.assertLess(timeout, mcp_budget, "The MCP request must outlast broker delivery checks")
        for operation in ("close",):
            with self.subTest(operation=operation), patch.object(self.client.opener, "open") as opened:
                opened.return_value.__enter__.return_value.read.return_value = b"{}"
                self.client.write(terminal, operation, {})
                self.assertEqual(opened.call_args.kwargs["timeout"], 20)

    def test_manager_launch_environment_is_lazy_and_only_copied_into_owned_child(self):
        inherited = {"KEEP": "yes", "HTTP_PROXY": "http://127.0.0.1:7890"}
        callback = Mock(return_value=inherited)
        client = TerminalClient(descriptor_file=self.path, runtime=Path(self.temp.name) / "logs", launch_env=callback)
        self.assertEqual(client.sessions(), [])
        callback.assert_not_called()
        process = Mock()
        process.poll.return_value = None
        with patch.object(client, "running", side_effect=[False, True]), patch("agent_captain.transport.terminal_client.shutil.which", return_value="fake-node"), \
                patch("agent_captain.transport.terminal_client.Path.exists", return_value=True), patch("agent_captain.transport.terminal_client.launch_background", return_value=process) as spawn:
            client.ensure_started()
        callback.assert_called_once_with()
        child_env = spawn.call_args.kwargs["env_overrides"]
        self.assertEqual(child_env["HTTP_PROXY"], inherited["HTTP_PROXY"])
        self.assertNotIn("KEEP", child_env)
        self.assertEqual(child_env["CEO_HUB_TERMINAL_FILE"], str(self.path.resolve()))
        self.assertEqual(inherited, {"KEEP": "yes", "HTTP_PROXY": "http://127.0.0.1:7890"})

    def test_existing_manager_and_default_worker_do_not_infer_or_change_proxy(self):
        callback = Mock(side_effect=AssertionError("a connected broker never rereads launch settings"))
        client = TerminalClient(descriptor_file=self.path, runtime=self.temp.name, launch_env=callback)
        with patch.object(client, "running", return_value=True):
            client.ensure_started()
        callback.assert_not_called()
        worker = TerminalClient(runtime=Path(self.temp.name) / "worker-logs")
        process = Mock()
        process.poll.return_value = None
        supplied = {"CEO_HUB_TERMINAL_FILE": str(self.path), "KEEP": "yes", "HTTP_PROXY": "http://existing-user-setting:1234"}
        with patch.dict(os.environ, supplied, clear=True), patch.object(worker, "running", side_effect=[False, True]), \
                patch("agent_captain.transport.terminal_client.shutil.which", return_value="fake-node"), patch("agent_captain.transport.terminal_client.Path.exists", return_value=True), \
                patch("agent_captain.transport.terminal_client.launch_background", return_value=process) as spawn:
            worker.ensure_started()
            self.assertEqual(dict(os.environ), supplied)
        self.assertEqual(spawn.call_args.kwargs["env_overrides"]["HTTP_PROXY"], supplied["HTTP_PROXY"])
        self.assertIsNone(worker.launch_env)


if __name__ == "__main__":
    unittest.main()
