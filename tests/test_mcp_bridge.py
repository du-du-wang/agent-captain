import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_captain.integrations import mcp_bridge
class FakeService:
    def __init__(self, token="private-secret", result=None, status=200, redirect=None):
        self.token = token
        self.result = result if result is not None else {"agents": [{"agent_id": "local::terminal-1", "title": "PPT 汇报", "source": "terminal"}]}
        self.status = status
        self.redirect = redirect
        self.calls = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.calls.append({"path": self.path, "headers": dict(self.headers), "body": request})
                self.send_response(owner.status)
                self.send_header("Content-Type", "application/json")
                if owner.redirect:
                    self.send_header("Location", owner.redirect)
                self.end_headers()
                self.wfile.write(json.dumps(owner.result, ensure_ascii=False).encode("utf-8"))

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class McpBridgeTests(unittest.TestCase):
    def test_public_management_policy_is_local_read_only_and_not_a_grant(self):
        bridge = mcp_bridge.McpBridge()
        response = self.initialize(bridge)
        self.assertEqual(response["result"]["instructions"], mcp_bridge.MCP_INSTRUCTIONS)
        with patch.object(mcp_bridge, "call_service", side_effect=AssertionError("policy must not call HTTP")), \
             patch.object(mcp_bridge, "read_descriptor", side_effect=AssertionError("policy must not read credentials")):
            result = self.call(bridge, "get_management_policy")["result"]
            self.assertFalse(result["isError"])
            policy = result["structuredContent"]
            self.assertEqual(policy["execution_policy"], "delegate_first")
            self.assertEqual(policy["decision_owner"], "human_user")
            self.assertFalse(policy["capability_grant"])
            policy["direct_execution_exceptions"].append("forged_grant")
            fresh = self.call(bridge, "get_management_policy")["result"]["structuredContent"]
            self.assertNotIn("forged_grant", fresh["direct_execution_exceptions"])
            for arguments in [{"controller_id": "forged"}, {"grant_all": True}, {"agent_id": "spark::worker"}]:
                self.assertTrue(self.call(bridge, "get_management_policy", arguments)["result"]["isError"])
        self.assertTrue(mcp_bridge.TOOLS_BY_NAME["get_management_policy"]["annotations"]["readOnlyHint"])

    def test_public_management_policy_requires_the_protocol_handshake(self):
        bridge = mcp_bridge.McpBridge()
        self.assertEqual(self.call(bridge, "get_management_policy")["error"]["code"], -32002)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.descriptor = Path(self.temp.name) / "service.json"
        self.env = patch.dict(os.environ, {"AGENTCAPTAIN_CONNECTION_FILE": "", "PRON_HUB_CONNECTION_FILE": "",
                                           "AGENTCAPTAIN_CONTROLLER_ID": "", "PRON_HUB_CONTROLLER_ID": "",
                                           "CEO_HUB_CONNECTION_FILE": str(self.descriptor),
                                           "HTTP_PROXY": "http://127.0.0.1:1"})
        self.env.start()
        self.services = []

    def tearDown(self):
        for service in self.services:
            service.close()
        self.env.stop()
        self.temp.cleanup()

    def service(self, **kwargs):
        service = FakeService(**kwargs)
        self.services.append(service)
        self.descriptor.write_text(json.dumps({"url": service.url, "token": service.token, "version": "0.3.0"}), encoding="utf-8")
        return service

    @staticmethod
    def initialize(bridge, version="2025-11-25"):
        response = bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
        bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return response

    @staticmethod
    def call(bridge, name="list_active_agents", arguments=None, request_id=2):
        return bridge.handle({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                              "params": {"name": name, "arguments": arguments or {}}})

    def test_subprocess_stdio_handshake_has_only_protocol_output(self):
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "Codex", "version": "test"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "ping"}]
        process = subprocess.run([sys.executable, "-u", str(Path(__file__).resolve().parents[1] / "scripts/captain.py"), "mcp"],
                                 input="".join(json.dumps(m) + "\n" for m in messages),
                                 capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(process.stderr, "")
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([item["id"] for item in responses], [1, 2, 3])
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(responses[0]["result"]["serverInfo"]["version"], mcp_bridge.VERSION)
        self.assertEqual(responses[2]["result"], {})
        names = {item["name"] for item in responses[1]["result"]["tools"]}
        self.assertEqual(names, {"list_active_agents", "get_agent", "read_agent", "update_agent", "reconnect_agent", "set_auto_reconnect", "reopen_agent", "send_agent_message", "send_terminal_input", "create_agent", "poll_events", "get_delivery", "get_management_policy"})
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "ceo-hub")
        self.assertEqual(responses[0]["result"]["serverInfo"]["version"], "0.7.0")
        self.assertNotIn("private-secret", process.stdout)

    def test_auto_reconnect_setting_preserves_explicit_choice_and_exact_generation(self):
        bridge = mcp_bridge.McpBridge()
        self.initialize(bridge)
        service = self.service(result={"auto_reconnect": {"enabled": True, "interval_seconds": 600}})
        arguments = {"agent_id": "remote::worker", "enabled": True,
                     "expected_terminal_id": "ceohub-0123456789abcdef", "request_key": "auto-setting-1"}
        result = self.call(bridge, "set_auto_reconnect", arguments)["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(service.calls[0]["body"], {"tool": "set_auto_reconnect", "arguments": arguments})
        for changes in ({"enabled": 1}, {"enabled": "true"}, {"expected_terminal_id": ""},
                        {"controller_id": "forged"}, {"interval_seconds": 1}, {"command": "codex"}):
            with self.subTest(changes=changes):
                self.assertTrue(self.call(bridge, "set_auto_reconnect", {**arguments, **changes})["result"]["isError"])
        for missing in ("enabled", "expected_terminal_id", "request_key"):
            self.assertTrue(self.call(bridge, "set_auto_reconnect", {k: v for k, v in arguments.items() if k != missing})["result"]["isError"])

    def test_negotiation_and_required_initialization(self):
        bridge = mcp_bridge.McpBridge()
        self.assertEqual(bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["error"]["code"], -32002)
        response = self.initialize(bridge, "2099-01-01")
        self.assertEqual(response["result"]["protocolVersion"], mcp_bridge.PROTOCOL_VERSIONS[-1])
        self.assertEqual(bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "unavailable"})["error"]["code"], -32601)
        self.assertIsNone(bridge.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 2}}))

    def test_read_output_arguments_support_cursor_or_after_and_boolean_tools(self):
        mcp_bridge.validate_arguments("read_agent", {"agent_id": "local::terminal", "cursor": "older", "include_tools": True})
        mcp_bridge.validate_arguments("read_agent", {"agent_id": "local::terminal", "after_cursor": "new", "include_tools": False})
        with self.assertRaises(mcp_bridge.BridgeError):
            mcp_bridge.validate_arguments("read_agent", {"agent_id": "local::terminal", "cursor": "a", "after_cursor": "b"})
        with self.assertRaises(mcp_bridge.BridgeError):
            mcp_bridge.validate_arguments("read_agent", {"agent_id": "local::terminal", "include_tools": 1})

    def test_read_call_uses_private_auth_and_no_browser_headers(self):
        service = self.service(result={"source": "terminal", "screen": "已完成 PPT\n❯", "command": "codex", "history_complete": False})
        bridge = mcp_bridge.McpBridge()
        self.initialize(bridge)
        response = self.call(bridge, "read_agent", {"agent_id": "local::terminal-1", "limit": 20})
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"]["screen"], "已完成 PPT\n❯")
        self.assertFalse(response["result"]["structuredContent"]["history_complete"])
        self.assertEqual(response["result"]["structuredContent"]["command"], "codex")
        sent = service.calls[0]
        self.assertEqual(sent["path"], "/api/plugin")
        self.assertEqual(sent["body"], {"tool": "read_agent", "arguments": {"agent_id": "local::terminal-1", "limit": 20}})
        self.assertEqual(sent["headers"]["Authorization"], "Bearer private-secret")
        self.assertNotIn("Origin", sent["headers"])
        self.assertNotIn("Cookie", sent["headers"])

    def test_descriptor_is_reloaded_after_service_restart(self):
        first = self.service(token="first-private-token", result={"generation": 1})
        bridge = mcp_bridge.McpBridge()
        self.initialize(bridge)
        self.assertEqual(self.call(bridge)["result"]["structuredContent"]["generation"], 1)
        second = self.service(token="second-private-token", result={"generation": 2})
        self.assertEqual(self.call(bridge)["result"]["structuredContent"]["generation"], 2)
        self.assertEqual(len(first.calls), 1)
        self.assertEqual(second.calls[0]["headers"]["Authorization"], "Bearer second-private-token")

    def test_credentials_are_redacted_even_in_error_echoes_and_nested_content(self):
        service = self.service(token='secret"with\\escape', result={"nested": ["secret\"with\\escape"], 'secret"with\\escape': "value"})
        bridge = mcp_bridge.McpBridge()
        self.initialize(bridge)
        result = self.call(bridge)["result"]
        self.assertEqual(result["structuredContent"], {"nested": ["[redacted]"], "[redacted]": "value"})
        service.status = 403
        service.result = {"error": "Rejected bearer secret\"with\\escape"}
        result = self.call(bridge)["result"]
        self.assertTrue(result["isError"])
        self.assertIn("[redacted]", result["content"][0]["text"])
        self.assertNotIn(service.token, json.dumps(result))

    def test_remote_and_credential_urls_are_refused_without_contacting_service(self):
        service = self.service()
        bridge = mcp_bridge.McpBridge()
        self.initialize(bridge)
        for url in ("https://127.0.0.1", "http://localhost", "http://example.org", "http://user@127.0.0.1", service.url + "#", service.url + "?", service.url + "/other"):
            self.descriptor.write_text(json.dumps({"url": url, "token": "private-secret"}), encoding="utf-8")
            with self.subTest(url=url):
                result = self.call(bridge)["result"]
                self.assertTrue(result["isError"])
                self.assertIn("Refused", result["content"][0]["text"])
        self.assertEqual(service.calls, [])

    def test_redirect_never_receives_bearer_token(self):
        other = FakeService()
        self.services.append(other)
        source = self.service(status=307, redirect=other.url + "/api/plugin")
        bridge = mcp_bridge.McpBridge()
        self.initialize(bridge)
        result = self.call(bridge)["result"]
        self.assertTrue(result["isError"])
        self.assertIn("redirect was refused", result["content"][0]["text"])
        self.assertEqual(len(source.calls), 1)
        self.assertEqual(other.calls, [])

    def test_write_requires_key_and_preserves_plain_user_message(self):
        service = self.service(result={"request_id": "delivery-1", "delivery": "submitted"})
        bridge = mcp_bridge.McpBridge()
        self.initialize(bridge)
        arguments = {"agent_id": "316::terminal-1", "text": "请继续修这个 bug。"}
        self.assertTrue(self.call(bridge, "send_agent_message", arguments)["result"]["isError"])
        self.assertEqual(service.calls, [])
        arguments["request_key"] = "stable-user-action-1"
        result = self.call(bridge, "send_agent_message", arguments)["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(service.calls[0]["body"]["arguments"], arguments)

    def test_metadata_update_passes_only_explicit_alias_pin_and_stable_key(self):
        service = self.service(result={"updated": True, "agent_id": "316::terminal-1", "alias": "电路项目", "pinned": True,
                                       "active": True, "scope_changed": False})
        bridge = mcp_bridge.McpBridge()
        self.initialize(bridge)
        arguments = {"agent_id": "316::terminal-1", "alias": "电路项目", "pinned": True, "request_key": "metadata-once"}
        response = self.call(bridge, "update_agent", arguments)["result"]
        self.assertFalse(response["isError"])
        self.assertEqual(response["structuredContent"]["scope_changed"], False)
        self.assertEqual(service.calls[0]["body"], {"tool": "update_agent", "arguments": arguments})
        self.assertNotIn("controller_id", service.calls[0]["body"]["arguments"])
        for field in ("host", "command", "text", "active", "grant"):
            self.assertNotIn(field, service.calls[0]["body"]["arguments"])

    def test_metadata_update_invalid_or_empty_changes_do_not_contact_service(self):
        service = self.service()
        bridge = mcp_bridge.McpBridge()
        self.initialize(bridge)
        base = {"agent_id": "local::terminal-1", "request_key": "metadata-key"}
        invalid = [base, {**base, "alias": ""}, {**base, "alias": "   "},
                   {**base, "alias": "中" * 201}, {**base, "alias": "title\x1b[31m"},
                   {**base, "alias": "title\n"}, {**base, "alias": "title\x85"},
                   {**base, "pinned": 1}, {**base, "pinned": "true"},
                   {"agent_id": "local::terminal-1", "pinned": True},
                   {**base, "pinned": True, "active": True},
                   {**base, "pinned": True, "controller_id": "different-controller"}]
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                self.assertTrue(self.call(bridge, "update_agent", arguments)["result"]["isError"])
        self.assertEqual(service.calls, [])
        mcp_bridge.validate_arguments("update_agent", {**base, "pinned": False})
        mcp_bridge.validate_arguments("update_agent", {**base, "alias": "中" * 200})

    def test_reconnect_passes_exact_existing_agent_and_key_without_startup_or_transport_arguments(self):
        service = self.service(result={"reconnected": True, "state": "connected", "agent_id": "316::terminal-1"})
        bridge = mcp_bridge.McpBridge()
        self.initialize(bridge)
        arguments = {"agent_id": "316::terminal-1", "request_key": "reattach-once"}
        result = self.call(bridge, "reconnect_agent", arguments)["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(service.calls[0]["body"], {"tool": "reconnect_agent", "arguments": arguments})
        for extra in ({"command": "codex resume"}, {"force": True}, {"create": True}, {"host": "other"},
                      {"target": "other-host"}, {"transport": "tunnel"}, {"controller_id": "different"}, {"text": "task"}):
            self.assertTrue(self.call(bridge, "reconnect_agent", {**arguments, **extra})["result"]["isError"])
        self.assertTrue(self.call(bridge, "reconnect_agent", {"agent_id": "316::terminal-1"})["result"]["isError"])
        self.assertEqual(len(service.calls), 1)

    def test_reopen_schema_is_destructive_explicit_and_closed_to_startup_arguments(self):
        definition = mcp_bridge.TOOLS_BY_NAME["reopen_agent"]
        self.assertFalse(definition["annotations"]["readOnlyHint"])
        self.assertTrue(definition["annotations"]["destructiveHint"])
        self.assertTrue(definition["annotations"]["idempotentHint"])
        self.assertFalse(definition["inputSchema"]["additionalProperties"])
        self.assertEqual(set(definition["inputSchema"]["required"]),
            {"agent_id", "expected_terminal_id", "confirmed", "request_key"})
        self.assertEqual(set(definition["inputSchema"]["properties"]),
            {"agent_id", "expected_terminal_id", "confirmed", "request_key"})
        self.assertIn("Destructive", definition["description"])
        self.assertIn("does not start/resume Codex", definition["description"])

    def test_reopen_invalid_confirmation_missing_required_fields_or_extra_arguments_never_contact_http(self):
        service = self.service()
        bridge = mcp_bridge.McpBridge()
        self.initialize(bridge)
        base = {"agent_id": "316::terminal-1", "expected_terminal_id": "ceohub-" + "a" * 32,
            "confirmed": True, "request_key": "one-destructive-intent"}
        invalid = [{key: value for key, value in base.items() if key != field} for field in base]
        invalid += [{**base, "confirmed": value} for value in (False, 1, None, "true")]
        invalid += [{**base, "expected_terminal_id": ""}, {**base, "expected_terminal_id": "a" * 129}]
        invalid += [{**base, **extra} for extra in ({"command": "codex resume"}, {"resume": True},
            {"force": True}, {"host": "another-host"}, {"target": "another-ssh"}, {"transport": "ssh"},
            {"controller_id": "forged-controller"}, {"text": "new task"}, {"data": "yes\r"})]
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                response = self.call(bridge, "reopen_agent", arguments)["result"]
                self.assertTrue(response["isError"])
        self.assertEqual(service.calls, [])

    def test_reopen_preserves_exact_identity_key_and_lifecycle_evidence_without_resume(self):
        service = self.service()
        bridge = mcp_bridge.McpBridge()
        self.initialize(bridge)
        arguments = {"agent_id": "316::terminal-1", "expected_terminal_id": "ceohub-" + "a" * 32,
            "confirmed": True, "request_key": "same-reopen-operation"}
        for state, code in (("blocked", "verified_close_unavailable"), ("unknown", "reopen_close_unknown"), ("done", "reopened")):
            service.result = {"agent_id": arguments["agent_id"], "operation": "reopen_terminal",
                "operation_id": arguments["request_key"], "operation_state": state,
                "previous_terminal_id": arguments["expected_terminal_id"], "reopened": state == "done",
                "model_started": False, "recovery": {"code": code, "message": "Only this confirmed terminal; no model command."}}
            with self.subTest(state=state):
                response = self.call(bridge, "reopen_agent", arguments)["result"]
                self.assertFalse(response["isError"])
                self.assertEqual(response["structuredContent"], service.result)
                self.assertEqual(service.calls[-1]["body"], {"tool": "reopen_agent", "arguments": arguments})
        self.assertEqual(len(service.calls), 3, "Only the explicitly issued fake-service requests may be made")

    def test_reopen_timeout_uses_dedicated_budget_keeps_original_key_and_never_retries(self):
        service = self.service()
        bridge = mcp_bridge.McpBridge()
        self.initialize(bridge)
        arguments = {"agent_id": "316::terminal-1", "expected_terminal_id": "ceohub-" + "a" * 32,
            "confirmed": True, "request_key": "same-destructive-operation"}
        for failure in (mcp_bridge.URLError("fixture response lost"), TimeoutError("fixture timeout")):
            with self.subTest(failure=type(failure).__name__), patch.object(mcp_bridge, "build_opener") as opener:
                opener.return_value.open.side_effect = failure
                response = self.call(bridge, "reopen_agent", arguments)["result"]
                self.assertTrue(response["isError"])
                message = response["content"][0]["text"]
                self.assertIn("outcome is unknown", message)
                self.assertIn("SAME request_key", message)
                self.assertIn("do not submit a new key", message)
                opener.return_value.open.assert_called_once()
                self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], 210)
                forwarded = json.loads(opener.return_value.open.call_args.args[0].data)
                self.assertEqual(forwarded, {"tool": "reopen_agent", "arguments": arguments})
        self.assertEqual(service.calls, [])

    def test_management_action_timeout_preserves_unknown_same_key_guidance_without_retry(self):
        self.service()
        bridge = mcp_bridge.McpBridge()
        self.initialize(bridge)
        for name in ("update_agent", "reconnect_agent"):
            with self.subTest(tool=name), patch.object(mcp_bridge, "build_opener") as opener:
                opener.return_value.open.side_effect = mcp_bridge.URLError("fixture connection lost")
                arguments = {"agent_id": "316::terminal-1", "request_key": "same-management-action"}
                if name == "update_agent":
                    arguments["pinned"] = True
                response = self.call(bridge, name, arguments)["result"]
                self.assertTrue(response["isError"])
                self.assertIn("outcome is unknown", response["content"][0]["text"])
                self.assertIn("SAME request_key", response["content"][0]["text"])
                self.assertEqual(opener.return_value.open.call_count, 1)

    def test_reconnect_operation_evidence_preserves_blocked_unknown_and_done_outcomes(self):
        service = self.service()
        bridge = mcp_bridge.McpBridge()
        self.initialize(bridge)
        arguments = {"agent_id": "316::terminal-1", "request_key": "same-reconnect-operation"}
        for state, code in (("blocked", "original_session_missing"), ("unknown", "reconnect_unknown"),
                            ("done", "already_connected")):
            service.result = {"agent_id": "316::terminal-1", "connected": state == "done",
                              "operation": "reconnect_agent", "operation_state": state,
                              "operation_id": "same-reconnect-operation",
                              "recovery": {"code": code, "message": "保留原对象；没有输入或新建。", "next_action": "inspect_connection"}}
            with self.subTest(state=state):
                response = self.call(bridge, "reconnect_agent", arguments)["result"]
                self.assertFalse(response["isError"])
                self.assertEqual(response["structuredContent"], service.result)
                self.assertEqual(service.calls[-1]["body"]["arguments"], arguments)
        self.assertEqual(len(service.calls), 3, "only the three explicit test calls may contact the fake service")

    def test_cached_overview_preserves_freshness_evidence_instead_of_claiming_live_connectivity(self):
        cached = {"agents": [{"agent_id": "316::terminal-1", "pinned": True, "host_label": "远端开发机",
                              "management": {"status": "display_disconnected", "label": "显示连接已断开",
                                             "tone": "muted", "primary_action": "reconnect", "needs_attention": True}, "connected": False,
                              "live_verified": False, "status_checked_at": "fixture-old-time"}], "status_source": "cache"}
        service = self.service(result=cached)
        bridge = mcp_bridge.McpBridge()
        self.initialize(bridge)
        result = self.call(bridge)["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"], cached)
        self.assertEqual(service.calls[0]["body"], {"tool": "list_active_agents", "arguments": {}})
        self.assertEqual(len(service.calls), 1)

    def test_new_management_tools_have_safe_mutation_annotations_and_closed_schemas(self):
        for name in ("update_agent", "reconnect_agent"):
            definition = mcp_bridge.TOOLS_BY_NAME[name]
            self.assertFalse(definition["annotations"]["readOnlyHint"])
            self.assertFalse(definition["annotations"]["destructiveHint"])
            self.assertTrue(definition["annotations"]["idempotentHint"])
            self.assertFalse(definition["inputSchema"]["additionalProperties"])
            self.assertIn("request_key", definition["inputSchema"]["required"])

    def test_raw_terminal_input_preserves_keys_and_requires_stable_request_key(self):
        service = self.service(result={"request_id": "input-1", "delivery": "submitted"})
        bridge = mcp_bridge.McpBridge()
        self.initialize(bridge)
        arguments = {"agent_id": "316::terminal-1", "data": "\u0003codex resume 会话-1\r"}
        self.assertTrue(self.call(bridge, "send_terminal_input", arguments)["result"]["isError"])
        self.assertEqual(service.calls, [])
        arguments["request_key"] = "same-terminal-action"
        result = self.call(bridge, "send_terminal_input", arguments)["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(service.calls[0]["body"], {"tool": "send_terminal_input", "arguments": arguments})

    def test_open_shell_with_task_text_is_not_rewritten_as_startup_command(self):
        service = self.service(result={"agent_id": "local::terminal-2", "delivery": "waiting_for_agent", "source": "terminal"})
        bridge = mcp_bridge.McpBridge()
        self.initialize(bridge)
        arguments = {"host": "local", "cwd": "D:\\workspace\\PPT", "title": "PPT", "text": "继续做汇报 PPT", "request_key": "new-shell-1"}
        result = self.call(bridge, "create_agent", arguments)["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["delivery"], "waiting_for_agent")
        self.assertEqual(service.calls[0]["body"]["arguments"], arguments)
        self.assertNotIn("command", service.calls[0]["body"]["arguments"])

    def test_user_startup_command_passes_through_without_provider_parameters(self):
        service = self.service(result={"agent_id": "316::terminal-2", "delivery": "submitted"})
        bridge = mcp_bridge.McpBridge()
        self.initialize(bridge)
        arguments = {"host": "316", "cwd": "/home/user/project", "title": "工作", "command": "codex-switch", "request_key": "new-shell-2"}
        self.assertFalse(self.call(bridge, "create_agent", arguments)["result"]["isError"])
        self.assertEqual(service.calls[0]["body"]["arguments"], arguments)
        arguments["model_provider"] = "janus"
        self.assertTrue(self.call(bridge, "create_agent", arguments)["result"]["isError"])
        self.assertEqual(len(service.calls), 1)

    def test_busy_delivery_is_not_relabelled_as_submitted(self):
        service = self.service(result={"request_id": "input-3", "delivery": "human_busy"})
        bridge = mcp_bridge.McpBridge()
        self.initialize(bridge)
        arguments = {"agent_id": "local::terminal-1", "text": "请继续", "request_key": "message-3"}
        result = self.call(bridge, "send_agent_message", arguments)["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["delivery"], "human_busy")

    def test_durable_error_diagnostics_preserve_delivery_identity_and_retry_state(self):
        service = self.service(result={"request_id": "raw-1", "state": "human_busy", "delivery": "human_busy", "error": "User has an unfinished draft"})
        bridge = mcp_bridge.McpBridge()
        self.initialize(bridge)
        arguments = {"agent_id": "local::terminal-1", "data": "codex\r", "request_key": "raw-once"}
        result = self.call(bridge, "send_terminal_input", arguments)["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["state"], "human_busy")
        self.assertEqual(result["structuredContent"]["request_id"], "raw-1")
        service.result.update(state="unknown", delivery="unknown", error="Reply lost; inspect the terminal")
        result = self.call(bridge, "send_terminal_input", arguments)["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["state"], "unknown")

    def test_terminal_payload_size_limits_prevent_contacting_service(self):
        service = self.service()
        bridge = mcp_bridge.McpBridge()
        self.initialize(bridge)
        for name, field in (("send_agent_message", "text"), ("send_terminal_input", "data")):
            with self.subTest(tool=name):
                arguments = {"agent_id": "local::terminal-1", field: "中" * 30001, "request_key": "oversized-action"}
                self.assertTrue(self.call(bridge, name, arguments)["result"]["isError"])
        self.assertEqual(service.calls, [])

    def test_raw_input_connection_failure_reports_unknown_without_retry(self):
        service = self.service()
        bridge = mcp_bridge.McpBridge()
        self.initialize(bridge)
        with patch.object(mcp_bridge, "build_opener") as opener:
            opener.return_value.open.side_effect = mcp_bridge.URLError("connection interrupted")
            arguments = {"agent_id": "local::terminal-1", "data": "codex\r", "request_key": "startup-once"}
            result = self.call(bridge, "send_terminal_input", arguments)["result"]
        self.assertTrue(result["isError"])
        self.assertIn("delivery is unknown", result["content"][0]["text"])
        self.assertIn("SAME request_key", result["content"][0]["text"])
        self.assertEqual(opener.return_value.open.call_count, 1)
        self.assertEqual(service.calls, [])

    def test_missing_service_is_actionable_not_a_traceback(self):
        bridge = mcp_bridge.McpBridge()
        self.initialize(bridge)
        result = self.call(bridge)["result"]
        self.assertTrue(result["isError"])
        self.assertIn("run_agentcaptain.cmd", result["content"][0]["text"])
        self.assertIn("AgentCaptain", result["content"][0]["text"])
        self.assertNotIn("Traceback", result["content"][0]["text"])

    def test_long_poll_does_not_block_other_calls(self):
        poll_started = threading.Event()
        read_called = threading.Event()

        def fake_call(name, arguments):
            if name == "poll_events":
                poll_started.set()
                if not read_called.wait(2):
                    raise AssertionError("poll blocked the read")
            else:
                self.assertTrue(poll_started.wait(2))
                read_called.set()
            return {"name": name}

        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {}}},
                 {"jsonrpc": "2.0", "method": "notifications/initialized"},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "poll_events", "arguments": {"timeout_seconds": 30}}},
                 {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_active_agents", "arguments": {}}}]
        stdin = io.StringIO("".join(json.dumps(line) + "\n" for line in lines))
        stdout = io.StringIO()
        with patch.object(mcp_bridge, "call_service", side_effect=fake_call):
            mcp_bridge.serve(stdin, stdout)
        responses = {row["id"]: row for row in map(json.loads, stdout.getvalue().splitlines())}
        self.assertFalse(responses[2]["result"]["isError"])
        self.assertFalse(responses[3]["result"]["isError"])

    def test_invalid_framing_is_a_protocol_error_and_recovers(self):
        output = io.StringIO()
        mcp_bridge.serve(io.StringIO("not-json\n[]\n{\"jsonrpc\":\"2.0\",\"id\":4,\"method\":\"ping\"}\n"), output)
        rows = list(map(json.loads, output.getvalue().splitlines()))
        self.assertEqual(rows[0]["error"]["code"], -32700)
        self.assertEqual(rows[1]["error"]["code"], -32600)
        self.assertEqual(rows[2]["result"], {})


if __name__ == "__main__":
    unittest.main()
