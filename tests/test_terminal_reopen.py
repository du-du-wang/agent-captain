"""Explicit terminal-generation replacement; isolated broker/SSH fixtures only."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_captain.core.terminal_hub import Hub, RETRYABLE
from agent_captain.transport.terminal_client import BrokerError
from agent_captain.transport.terminal_client import TerminalClient
from agent_captain.transport import remote_terminal_close


OLD = "ceohub-0123456789abcdef0123456789abcdef"
OTHER = "ceohub-fedcba9876543210fedcba9876543210"
THREAD = "fixture-worker"
OWNER = {"pid": 4242, "recovery_id": "a" * 32}
IDENTITY = {"name": OLD, "session": "$1", "created": 100, "pane": "%2", "pane_pid": 500,
    "pane_start_ticks": 600, "boot_id": "00000000-0000-4000-8000-000000000000", "server_pid": 700}


class ReopenBroker:
    def __init__(self):
        self.lock = threading.RLock()
        self.records = {}
        self.requests = []
        self.actions = []
        self.owner = dict(OWNER)
        self.features = ["verified_close"]
        self.close_error = None
        self.create_error = None
        self.legacy_close = False
        self.owner_flip_at = None
        self.health_count = 0
        self.before_close = None
        self.missing_record = False

    def add(self, tid=OLD, host="316", cwd="/workspace/project"):
        row = {"id": tid, "host": host, "transport": "local" if host == "local" else "ssh",
            "target": "" if host == "local" else "fake316", "cwd": cwd, "title": "Work", "connected": True,
            "state": "agent", "agent_ready": True, "human_busy": False, "command": "codex", "cols": 100, "rows": 30}
        self.records[tid] = row
        return row

    def sessions(self):
        with self.lock:
            return copy.deepcopy(list(self.records.values()))

    def request(self, path, body=None, timeout=20):
        self.requests.append((path, copy.deepcopy(body)))
        if path == "/health":
            self.health_count += 1
            if self.owner_flip_at == self.health_count:
                self.owner = {"pid": 4243, "recovery_id": "b" * 32}
            return {"ok": True, "version": "0.3.0", **self.owner, "features": list(self.features)}
        if path == "/sessions/create":
            self.assert_owner(body)
            row = self.add(body["session_id"], body["host"], body["cwd"])
            row.update({key: body[key] for key in ("title", "target", "transport", "cols", "rows")})
            row.update(state="shell", agent_ready=False, command="bash")
            if self.create_error:
                raise self.create_error
            return copy.deepcopy(row)
        raise AssertionError("Unexpected broker request")

    def assert_owner(self, body):
        if body.get("expected_owner") != self.owner:
            raise BrokerError("Owner changed", "terminal_owner_changed", 409)

    def write(self, tid, action, body):
        self.actions.append((tid, action, copy.deepcopy(body)))
        if action == "close" and body.get("require_verified"):
            self.assert_owner(body)
            if self.before_close:
                self.before_close()
            if self.close_error:
                raise self.close_error
            if self.legacy_close:
                return {"id": tid, "closed": True}
            self.records.pop(tid, None)
            return {"id": tid, "closed": True, "closed_verified": True, "record_removed": True,
                "remote_closed_verified": body.get("expected_transport") == "ssh",
                "local_display_exit_verified": not self.missing_record, "missing_record": self.missing_record}
        return {"id": tid, "submitted": True, "state": "submitted"}

    def close(self):
        pass


class TerminalReopenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.broker = ReopenBroker()
        self.hub = self.make_hub()
        self.probe = patch.object(self.hub, "_probe_reopen_terminal", return_value={"ok": True, "terminal_id": OLD,
            "exists": True, "identity": copy.deepcopy(IDENTITY)})
        self.probe_mock = self.probe.start()
        self.add_worker()

    def make_hub(self):
        hub = Hub(self.temp.name, terminals=self.broker)
        hub.hosts = {"316": {"transport": "ssh", "target": "fake316", "label": "316"},
                     "local": {"transport": "local", "label": "Local"}}
        return hub

    def add_worker(self, tid=OLD, thread=THREAD, host="316", cwd=None):
        cwd = cwd or ("C:/workspace/project" if host == "local" else "/workspace/project")
        self.broker.add(tid, host, cwd)
        self.hub.store.remember(host, thread, "My worker", mode="terminal", terminal_id=tid, cwd=cwd, active=True)
        self.hub.store.update_agent_metadata(host, thread, pinned=True)

    def tearDown(self):
        self.probe.stop()
        self.hub.close()
        self.temp.cleanup()

    def reopen(self, **changes):
        args = {"host": "316", "thread_id": THREAD, "request_key": "reopen-1", "expected_terminal_id": OLD, "confirm": True}
        return self.hub.reopen_terminal(**(args | changes))

    def creates(self):
        return [body for path, body in self.broker.requests if path == "/sessions/create"]

    def test_explicit_boolean_confirmation_and_stable_expected_identity_are_required(self):
        for confirm in (False, None, 1, "true"):
            with self.subTest(confirm=confirm), self.assertRaises(ValueError):
                self.reopen(confirm=confirm)
        for value in (None, "", "not-a-terminal"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.reopen(expected_terminal_id=value)
        with self.assertRaises(ValueError):
            self.reopen(request_key=None)
        self.assertEqual(self.broker.actions, [])
        self.assertEqual(self.broker.requests, [])

    def test_success_replaces_exact_terminal_and_preserves_single_worker_metadata(self):
        self.add_worker(OTHER, "unrelated")
        result = self.reopen()
        new = result["terminal_id"]
        self.assertNotEqual(new, OLD)
        self.assertTrue(remote_terminal_close.SESSION.fullmatch(new))
        self.assertEqual((result["operation_state"], result["reopened"], result["previous_terminal_id"]), ("done", True, OLD))
        self.assertEqual((result["agent_id"], result["thread_id"], result["alias"], result["cwd"], result["pinned"]),
            ("316::" + THREAD, THREAD, "My worker", "/workspace/project", True))
        self.assertEqual(len(self.hub.store.agents()), 2)
        self.assertNotIn(OLD, self.broker.records)
        self.assertTrue(self.broker.records[OTHER]["connected"])
        self.assertEqual([(tid, action) for tid, action, _ in self.broker.actions], [(OLD, "close")])
        close = self.broker.actions[0][2]
        self.assertEqual(close["expected_remote"], {"exists": True, "identity": IDENTITY})
        self.assertEqual(close["expected_owner"], OWNER)
        self.assertEqual(close["expected_target"], "fake316")
        create = self.creates()[0]
        self.assertEqual(create["expected_owner"], OWNER)
        self.assertIs(create["require_verified"], True)
        self.assertFalse(any(key in create for key in ("command", "program", "text", "data", "resume")))
        self.assertEqual(result["command"], "bash")
        self.assertFalse(result["agent_ready"])

    def test_same_key_returns_exact_cached_result_after_restart_without_side_effects(self):
        result = self.reopen()
        self.probe.stop()
        self.hub.close()
        self.hub = self.make_hub()
        self.probe = patch.object(self.hub, "_probe_reopen_terminal", side_effect=AssertionError("No repeated probe"))
        self.probe.start()
        before = (len(self.broker.requests), len(self.broker.actions))
        self.assertEqual(self.reopen(), result)
        self.assertEqual(before, (len(self.broker.requests), len(self.broker.actions)))
        with self.assertRaises(ValueError):
            self.reopen(expected_terminal_id=OTHER)

    def test_changed_expected_id_and_closed_workers_never_touch_transport(self):
        result = self.reopen(expected_terminal_id=OTHER)
        self.assertEqual(result["recovery"]["code"], "terminal_generation_changed")
        self.hub.store.remember("316", THREAD, "My worker", mode="closed")
        result = self.reopen(request_key="closed-intent")
        self.assertEqual(result["recovery"]["code"], "worker_not_open")
        self.assertEqual(self.broker.actions, [])
        self.assertEqual(self.broker.requests, [])

    def test_manager_identity_is_forbidden(self):
        with patch.object(self.hub.controllers, "is_controller_reference", return_value=True), self.assertRaises(PermissionError):
            self.reopen()
        self.assertEqual(self.broker.requests, [])

    def test_scoped_controller_needs_worker_and_creation_authority(self):
        for forbidden in ("_require_controller_scope", "_require_creation_scope"):
            with self.subTest(forbidden=forbidden), \
                 patch.object(self.hub, "_require_controller_scope") as worker, \
                 patch.object(self.hub, "_require_creation_scope") as creation:
                (worker if forbidden == "_require_controller_scope" else creation).side_effect = PermissionError("Denied")
                with self.assertRaises(PermissionError):
                    self.reopen(origin="plugin", controller_id="controller-fixture")
        self.assertEqual(self.broker.requests, [])

    def test_legacy_broker_feature_is_rejected_before_fences_or_remote_probe(self):
        request = self.hub.store.enqueue("316", THREAD, "old prompt", request_key="old")
        self.broker.features = []
        result = self.reopen()
        self.assertEqual(result["recovery"]["next_action"], "update_display_service")
        self.assertEqual(self.hub.store.delivery(request)["state"], "queued")
        self.assertEqual(self.hub.store.agent("316", THREAD)["mode"], "terminal")
        self.probe_mock.assert_not_called()
        self.assertEqual(self.broker.actions, [])

    def test_route_or_identity_failure_is_not_absence_and_never_closes(self):
        self.probe_mock.return_value = {"ok": False, "reason": "unavailable"}
        result = self.reopen()
        self.assertEqual(result["operation_state"], "blocked")
        self.assertEqual(result["recovery"]["code"], "reopen_preflight_unavailable")
        self.assertEqual(self.broker.actions, [])
        self.assertEqual(self.creates(), [])

    def test_missing_remote_requires_exact_absence_proof_and_verified_receipt(self):
        self.broker.records.pop(OLD)
        self.broker.missing_record = True
        self.probe_mock.return_value = {"ok": True, "terminal_id": OLD, "exists": False}
        result = self.reopen()
        self.assertTrue(result["reopened"])
        self.assertEqual(self.broker.actions[0][2]["expected_remote"], {"exists": False})

    def test_pending_inputs_all_origins_are_fenced_before_close_but_unknown_retained(self):
        pending = []
        for i, state in enumerate(RETRYABLE):
            rid = self.hub.store.enqueue("316", THREAD, "old prompt", origin="plugin" if i % 2 else "ui", request_key="pending-" + str(i), kind="prompt")
            self.hub.store.update_outbox(rid, state)
            pending.append(rid)
        unknown = self.hub.store.enqueue("316", THREAD, "uncertain", request_key="uncertain")
        self.hub.store.update_outbox(unknown, "unknown")
        submitted = self.hub.store.enqueue("316", THREAD, "submitted", request_key="submitted")
        self.hub.store.update_outbox(submitted, "submitted")

        def inspect_fence():
            self.assertEqual(self.hub.store.agent("316", THREAD)["mode"], "closing")
            self.assertTrue(all(self.hub.store.delivery(rid)["state"] == "not_sent" for rid in pending))
            with self.assertRaises(ValueError):
                self.hub.terminal_input("316", THREAD, "late human input", expected_terminal_id=OLD)
        self.broker.before_close = inspect_fence
        self.assertTrue(self.reopen()["reopened"])
        self.assertEqual(self.hub.store.delivery(unknown)["state"], "unknown")
        self.assertEqual(self.hub.store.delivery(submitted)["state"], "submitted")
        audits = [e for e in self.hub.store.events() if e["kind"] == "terminal_reopen_input_fenced"]
        self.assertEqual(len(audits), len(pending))
        self.assertFalse(any("text" in e["data"] for e in audits))
        before = len(self.broker.actions)
        result = self.hub.send("316", THREAD, "old prompt", origin="ui", request_key="pending-0", expected_terminal_id=self.hub.store.agent("316", THREAD)["terminal_id"])
        self.assertEqual(result["state"], "not_sent")
        self.assertEqual(len(self.broker.actions), before)

    def test_submitting_input_blocks_reopen_without_fence_or_close(self):
        rid = self.hub.store.enqueue("316", THREAD, "in flight")
        self.hub.store.update_outbox(rid, "submitting")
        result = self.reopen()
        self.assertEqual(result["recovery"]["code"], "input_submission_in_progress")
        self.assertEqual(self.broker.actions, [])
        self.probe_mock.assert_not_called()

    def test_close_timeout_and_legacy_reply_are_unknown_and_never_repeated(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.broker.legacy_close = legacy
                self.broker.close_error = None if legacy else TimeoutError("private server diagnostic")
                result = self.reopen(request_key="close-" + str(legacy))
                self.assertEqual(result["operation_state"], "unknown")
                self.assertEqual(result["recovery"]["code"], "reopen_close_unknown")
                self.assertNotIn("private server diagnostic", json.dumps(result))
                count = len(self.broker.actions)
                self.assertEqual(self.reopen(request_key="close-" + str(legacy)), result)
                self.assertEqual(len(self.broker.actions), count)
                self.assertEqual(self.creates(), [])
                self.hub.store.remember("316", THREAD, "My worker", mode="terminal")

    def test_create_timeout_retains_new_id_and_does_not_create_twice(self):
        self.broker.create_error = TimeoutError("private create diagnostic")
        result = self.reopen()
        self.assertEqual(result["operation_state"], "unknown")
        self.assertEqual(result["recovery"]["code"], "reopen_create_unknown")
        self.assertNotEqual(result["terminal_id"], OLD)
        self.assertEqual(self.hub.store.agent("316", THREAD)["terminal_id"], result["terminal_id"])
        self.assertEqual(self.reopen(), result)
        self.assertEqual(len(self.creates()), 1)
        self.assertNotIn("private create diagnostic", json.dumps(result))
        self.assertFalse(self.reopen(request_key="unsafe-new-key", expected_terminal_id=result["terminal_id"])["reopened"])
        self.assertEqual(len(self.creates()), 1)

    def test_owner_change_before_close_or_after_close_never_creates(self):
        self.broker.owner_flip_at = 2
        result = self.reopen()
        self.assertEqual(result["recovery"]["code"], "display_owner_changed")
        self.assertEqual(self.broker.actions, [])
        self.broker.owner = dict(OWNER)
        self.broker.health_count = 0
        self.broker.owner_flip_at = 3
        result = self.reopen(request_key="postclose-owner")
        self.assertEqual(result["operation_state"], "unknown")
        self.assertEqual(self.creates(), [])

    def test_started_operation_is_unknown_without_replaying_any_side_effect(self):
        # Simulate a process loss after its durable started fence.
        import hashlib
        operation = "terminal-reopen:reopen-1"
        fingerprint = hashlib.sha256(json.dumps(["316", THREAD, OLD, "ui", None]).encode()).hexdigest()
        self.hub.store.begin_operation(operation, fingerprint)
        result = self.reopen()
        self.assertEqual(result["operation_state"], "unknown")
        self.assertEqual(result["recovery"]["code"], "reopen_unknown")
        self.assertEqual(self.broker.actions, [])
        self.assertEqual(self.broker.requests, [])

    def test_native_binding_is_only_previous_historical_metadata_not_new_live_binding(self):
        binding = {"state": "bound", "session_id": "fixture-session", "process_identity": {"pid": 5}}
        self.hub.store.setting(self.hub._output_key("316", OLD), binding)
        result = self.reopen()
        self.assertEqual(self.hub.store.setting(self.hub._output_key("316", OLD)), binding)
        self.assertIsNone(self.hub.store.setting(self.hub._output_key("316", result["terminal_id"])))
        self.assertEqual(result["output_binding"], {"state": "unbound"})
        self.assertEqual(result["previous_output_binding"]["state"], "historical")
        self.assertFalse(result["previous_output_binding"]["live_session_verified"])

    def test_old_reconnect_recovery_is_archived_not_applied_to_replacement_after_restart(self):
        recovery = {"code": "original_session_missing", "request_key": "old-reconnect", "request_origin": "plugin",
            "next_action": "inspect_connection", "message": "Old original session missing"}
        self.hub.store.setting(self.hub._recovery_key("316", THREAD), recovery)
        result = self.reopen()
        self.assertTrue(result["reopened"])
        self.assertIsNone(self.hub.store.setting(self.hub._recovery_key("316", THREAD)))
        self.assertNotIn("recovery", self.hub.get_agent("316", THREAD))
        generation = self.hub.store.setting(self.hub._generation_key("316", THREAD))
        self.assertEqual(generation["previous_recovery"], recovery)
        self.probe.stop()
        self.hub.close()
        self.hub = self.make_hub()
        self.probe = patch.object(self.hub, "_probe_reopen_terminal", side_effect=AssertionError("No probe"))
        self.probe.start()
        fresh = self.hub.get_agent("316", THREAD)
        self.assertNotIn("recovery", fresh)
        self.assertEqual(fresh["terminal_id"], result["terminal_id"])

    def test_output_receipt_always_names_captured_generation_not_later_current_row(self):
        def output(tid, after):
            self.hub.store.remember("316", THREAD, "My worker", terminal_id=OTHER)
            return {"data": "old frame", "seq": 4, "reset": False, "terminal_id": "untrusted"}
        self.broker.output = output
        result = self.hub.terminal_output("316", THREAD, 3)
        self.assertEqual(result["terminal_id"], OLD)
        self.assertEqual(self.hub.store.agent("316", THREAD)["terminal_id"], OTHER)

    def test_preexisting_raw_input_race_cannot_be_redirected_to_new_generation(self):
        original = self.hub._require_terminal_generation
        current = []
        def interleave(row, expected=None, origin="ui"):
            current.append(self.reopen()["terminal_id"])
            return original(row, expected, origin)
        with patch.object(self.hub, "_require_terminal_generation", side_effect=interleave):
            self.hub.terminal_input("316", THREAD, "old buffered input", expected_terminal_id=OLD)
        self.assertEqual(self.broker.actions[-1][:2], (OLD, "input"))
        self.assertNotEqual(current[0], OLD)

    def test_plugin_reopen_dispatch_namespaces_key_and_reads_own_unknown_receipt_only(self):
        self.broker.create_error = TimeoutError("lost reply")
        args = {"agent_id": "316::" + THREAD, "request_key": "captain-reopen", "expected_terminal_id": OLD, "confirmed": True}
        with patch.object(self.hub.controllers, "view_context", return_value={"controller": {
                "state": "ready", "session_id": "fixture-session", "tools_ready": True}}), \
             patch.object(self.hub, "_require_controller_scope") as worker, \
             patch.object(self.hub, "_require_creation_scope") as creation:
            result = self.hub.plugin_call("reopen_agent", args, controller_id="fixture-controller")
            self.assertEqual(result["operation_state"], "unknown")
            self.assertTrue(result["operation_id"].startswith("controller:fixture-controller:"))
            self.assertEqual(self.hub.plugin_call("reopen_agent", args, controller_id="fixture-controller"), result)
            self.assertEqual(worker.call_count, 1)
            self.assertEqual(creation.call_count, 2)
            worker.side_effect = PermissionError("No new mutation of uncertain worker")
            with self.assertRaises(PermissionError):
                self.hub.plugin_call("reopen_agent", args | {"request_key": "different-intent"}, controller_id="fixture-controller")
        self.assertEqual(len(self.creates()), 1)

    def test_stale_or_legacy_ui_writes_resize_close_and_messages_are_refused_after_reopen(self):
        new = self.reopen()["terminal_id"]
        calls = [lambda expected: self.hub.terminal_input("316", THREAD, "old", expected_terminal_id=expected),
                 lambda expected: self.hub.terminal_resize("316", THREAD, 80, 24, expected_terminal_id=expected),
                 lambda expected: self.hub.terminal_close("316", THREAD, True, expected_terminal_id=expected),
                 lambda expected: self.hub.send("316", THREAD, "old", request_key="late-prompt", expected_terminal_id=expected)]
        before = len(self.broker.actions)
        for call in calls:
            for expected in (None, OLD):
                with self.subTest(expected=expected), self.assertRaises(ValueError):
                    call(expected)
        self.assertEqual(len(self.broker.actions), before)
        self.hub.terminal_input("316", THREAD, "fresh", expected_terminal_id=new)
        self.assertEqual(self.broker.actions[-1][:2], (new, "input"))

    def test_local_replacement_uses_no_remote_probe(self):
        self.add_worker(host="local", thread="local-worker", tid=OTHER)
        result = self.reopen(host="local", thread_id="local-worker", expected_terminal_id=OTHER)
        self.assertTrue(result["reopened"])
        self.probe_mock.assert_not_called()
        self.assertNotIn("expected_remote", self.broker.actions[0][2])

    def test_missing_local_record_is_not_verified_absence(self):
        self.add_worker(host="local", thread="local-worker", tid=OTHER)
        self.broker.records.pop(OTHER)
        self.broker.missing_record = True
        result = self.reopen(host="local", thread_id="local-worker", expected_terminal_id=OTHER)
        self.assertEqual(result["operation_state"], "unknown")
        self.assertEqual(self.creates(), [])

    def test_verified_close_requires_all_matching_receipt_fields(self):
        valid = {"id": OLD, "closed": True, "closed_verified": True, "record_removed": True,
            "remote_closed_verified": True, "local_display_exit_verified": True}
        for key in valid:
            with self.subTest(key=key):
                receipt = dict(valid)
                receipt[key] = OTHER if key == "id" else False
                with patch.object(self.broker, "write", return_value=receipt):
                    result = self.reopen(request_key="bad-receipt-" + key)
                self.assertEqual(result["operation_state"], "unknown")
                self.assertEqual(self.creates(), [])
                self.hub.store.remember("316", THREAD, "My worker", mode="terminal")


class ReopenProbeTests(unittest.TestCase):
    def test_strict_close_timeout_does_not_change_ordinary_close_or_input_budgets(self):
        client = TerminalClient()
        with patch.object(client, "request", return_value={}) as request:
            client.write(OLD, "close", {"require_verified": True})
            self.assertEqual(request.call_args.kwargs["timeout"], 60)
            client.write(OLD, "close", {})
            self.assertEqual(request.call_args.kwargs["timeout"], 20)
            client.write(OLD, "input", {"data": "fixture"})
        from agent_captain.transport.terminal_client import FOREGROUND_RPC_TIMEOUT
        self.assertEqual(request.call_args.kwargs["timeout"], FOREGROUND_RPC_TIMEOUT)

    def test_actual_close_helper_source_is_sent_but_only_probe_operation_invoked(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = ReopenBroker()
            hub = Hub(directory, terminals=broker)
            hub.hosts = {"316": {"transport": "ssh", "target": "fixture-host", "label": "316"}}
            try:
                payload = {"ok": True, "terminal_id": OLD, "exists": True, "identity": IDENTITY}
                result = subprocess.CompletedProcess([], 0, remote_terminal_close.MARKER + json.dumps(payload), "")
                with patch("agent_captain.core.terminal_hub.subprocess.run", return_value=result) as run:
                    self.assertEqual(hub._probe_reopen_terminal("316", OLD), payload)
                command = run.call_args.args[0]
                self.assertEqual(command[-1], "python3 - probe --session " + OLD)
                self.assertEqual(command[-2], "fixture-host")
                self.assertEqual(run.call_args.kwargs["timeout"], 45)
                self.assertNotIn("close --session", command[-1])
                self.assertTrue(run.call_args.kwargs["input"].startswith("import base64,zlib"))
                invalid = dict(payload, identity={key: value for key, value in IDENTITY.items() if key != "boot_id"})
                result = subprocess.CompletedProcess([], 0, remote_terminal_close.MARKER + json.dumps(invalid), "")
                with patch("agent_captain.core.terminal_hub.subprocess.run", return_value=result):
                    self.assertFalse(hub._probe_reopen_terminal("316", OLD)["ok"])
            finally:
                hub.close()


if __name__ == "__main__":
    unittest.main()
