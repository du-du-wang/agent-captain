"""Opt-in HTTP/MCP integration with the real Hub and an isolated fake terminal."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from agent_captain.core.terminal_hub import Hub
from agent_captain.server.app import create_server
from tests.test_controller_scope import Controllers
from tests.test_terminal_hub import FakeTerminals


TID = "ceohub-0123456789abcdef01234567"
IDENTITY = {"name": TID, "session": "$7", "created": 1234, "pane": "%8",
            "pane_pid": 987654, "pane_start_ticks": 123456}


class AutoReconnectApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.terminals = FakeTerminals()
        self.terminals.request = lambda path, timeout=3: {"sessions": self.terminals.sessions()}
        self.hub = Hub(self.temp.name, terminals=self.terminals)
        self.hub.hosts = {"remote": {"transport": "ssh", "target": "fixture-host", "label": "Remote"},
                          "local": {"transport": "local", "label": "Local"}}
        self.terminals.add(TID, "remote")
        self.hub.store.remember("remote", "worker", "Worker", active=True, mode="terminal",
                                terminal_id=TID, cwd="/work/project")
        self.now = 1000
        self.hub.auto_reconnect.clock = lambda: self.now
        self.hub._probe_remote_terminal = lambda host, tid: {"ok": True, "exists": True, "identity": IDENTITY}
        self.server = create_server(self.hub, port=0, token="fixture-cookie")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.hub.close()
        self.temp.cleanup()

    def configure(self, enabled=True, request_key="toggle-1", **changes):
        body = {"host": "remote", "thread_id": "worker", "enabled": enabled,
                "expected_terminal_id": TID, "request_key": request_key, **changes}
        request = Request(self.server.origin + "/api/agents/auto-reconnect", data=json.dumps(body).encode(),
                          headers={"Cookie": "ceo_session=fixture-cookie", "Origin": self.server.origin,
                                   "Content-Type": "application/json"}, method="POST")
        try:
            with self.opener.open(request, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_default_off_and_read_only_hub_does_not_start_scheduler(self):
        self.assertIsNone(self.hub._auto_reconnect_thread)
        value = self.hub.get_agent("remote", "worker")
        self.assertFalse(value["auto_reconnect"]["enabled"])
        self.assertIsNone(value["auto_reconnect"]["next_attempt_at"])
        self.assertEqual(self.terminals.writes, [])

    def test_opt_in_persists_without_browser_or_background_model_input(self):
        status, value = self.configure()
        self.assertEqual(status, 200)
        self.assertEqual(value["auto_reconnect"]["status"], "connected")
        self.assertEqual(value["auto_reconnect"]["interval_seconds"], 600)
        other = Hub(self.temp.name, terminals=self.terminals)
        try:
            other.hosts = self.hub.hosts
            self.assertTrue(other.get_agent("remote", "worker")["auto_reconnect"]["enabled"])
            self.assertIsNone(other._auto_reconnect_thread)
        finally:
            other.close()
        self.assertEqual(self.terminals.writes, [])
        self.assertFalse(any(call[0] == "create" for call in self.terminals.calls))

    def test_disconnection_waits_ten_minutes_and_attaches_only_original_terminal(self):
        self.terminals.records[TID].update(connected=False, state="exited", agent_ready=False)
        status, value = self.configure()
        self.assertEqual(status, 200)
        self.assertEqual(value["auto_reconnect"]["next_attempt_at"], 1600)
        self.now = 1599.99
        self.hub.auto_reconnect.tick()
        self.assertFalse(any(call[0] == "create" for call in self.terminals.calls))
        self.now = 1600
        self.hub.auto_reconnect.tick()
        created = [call for call in self.terminals.calls if call[0] == "create"]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0][1]["session_id"], TID)
        self.assertTrue(created[0][1]["cwd"].startswith("/.__pronhub_existing__/" + TID + "/"))
        self.assertEqual(self.hub.get_agent("remote", "worker")["auto_reconnect"]["status"], "connected")
        self.assertEqual(self.terminals.writes, [])

    def test_stable_configuration_replay_does_not_reenable_after_later_disable(self):
        self.assertEqual(self.configure()[0], 200)
        self.assertEqual(self.configure(False, "toggle-2")[0], 200)
        status, repeated = self.configure()
        self.assertEqual(status, 200)
        self.assertFalse(repeated["auto_reconnect"]["enabled"])
        self.assertEqual(self.configure(False, "toggle-1")[0], 400)

    def test_stale_generation_types_and_closed_worker_are_rejected(self):
        self.assertEqual(self.configure(expected_terminal_id="ceohub-ffffffffffffffff")[0], 400)
        for enabled in (1, "true", None):
            self.assertEqual(self.configure(enabled)[0], 400)
        self.hub.store.remember("remote", "worker", "Worker", mode="closed")
        self.assertEqual(self.configure()[0], 400)

    def test_endpoint_requires_same_origin_cookie(self):
        request = Request(self.server.origin + "/api/agents/auto-reconnect", data=b"{}",
                          headers={"Cookie": "ceo_session=fixture-cookie", "Origin": "https://example.invalid"}, method="POST")
        with self.assertRaises(HTTPError) as caught:
            self.opener.open(request, timeout=5)
        self.assertEqual(caught.exception.code, 403)
        self.assertFalse(self.hub.get_agent("remote", "worker")["auto_reconnect"]["enabled"])

    def test_controller_plugin_setting_uses_authenticated_worker_access_and_generation(self):
        self.hub.controllers.shutdown()
        self.hub.controllers = Controllers(self.hub)
        self.hub.controllers.grants["a"] = set()
        arguments = {"agent_id": "remote::worker", "enabled": True,
                     "expected_terminal_id": TID, "request_key": "plugin-toggle"}
        result = self.hub.plugin_call("set_auto_reconnect", arguments, controller_id="a")
        self.assertTrue(result["auto_reconnect"]["enabled"])
        self.hub.controllers.active.remove("a")
        with self.assertRaises(PermissionError):
            self.hub.plugin_call("set_auto_reconnect", {**arguments, "enabled": False, "request_key": "revoked"}, controller_id="a")

    def test_owner_starts_only_one_scheduler_and_stops_on_close(self):
        self.hub.start_auto_reconnect()
        first = self.hub._auto_reconnect_thread
        self.hub.start_auto_reconnect()
        self.assertIs(self.hub._auto_reconnect_thread, first)
        self.assertTrue(first.is_alive())
        self.hub.close()
        self.assertFalse(first.is_alive())

    def test_state_and_disabling_remain_responsive_during_slow_reconnect(self):
        self.assertEqual(self.configure()[0], 200)
        locked, release, completed = threading.Event(), threading.Event(), threading.Event()
        result = {}
        def network_action():
            with self.hub.registry_lock:
                locked.set()
                release.wait(5)
        def observe_and_disable():
            result["state"] = self.hub.state()
            result["disabled"] = self.configure(False, "disable-while-reconnecting")
            completed.set()
        worker = threading.Thread(target=network_action)
        reader = threading.Thread(target=observe_and_disable)
        worker.start()
        self.assertTrue(locked.wait(1))
        reader.start()
        try:
            self.assertTrue(completed.wait(2), "A slow background attach must not freeze state or the off switch")
        finally:
            release.set()
            worker.join(timeout=3)
            reader.join(timeout=3)
        self.assertEqual(result["disabled"][0], 200)
        self.assertFalse(result["disabled"][1]["auto_reconnect"]["enabled"])


if __name__ == "__main__":
    unittest.main()
