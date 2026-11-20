"""Pure fake/local-owned controller tests: never run Codex, SSH or user state."""
from __future__ import annotations

import io
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_captain.core.controllers import ControllerService, INITIALIZATION, LEGACY_ROLE, ROLE, resolve_codex_command, default_workspace_root, legacy_workspace_root, manager_launch_environment, ROOT
from agent_captain.core.management_policy import MANAGEMENT_POLICY, MANAGER_ROLE, POLICY_VERSION
from agent_captain.transport.terminal_client import BrokerError
from agent_captain.infrastructure.store import Store

SID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


class FakeDirectories:
    def __init__(self):
        self.calls = []

    def list(self, config, cwd):
        self.calls.append((config, cwd))
        return {"path": str(Path(cwd).resolve()) if config["transport"] == "local" else cwd}


class FakeRegistry:
    HKEY_CURRENT_USER = "fake-hkcu"

    def __init__(self, values=None, failure=False):
        self.values = values or {}
        self.failure = failure
        self.opened = 0

    def OpenKey(self, root, path):
        self.opened += 1
        if self.failure:
            raise OSError("registry access unavailable")
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise OSError("no such setting")
        return self.values[name], 0


class FakeHub:
    def __init__(self, root):
        self.runtime = Path(root)
        self.store = Store(self.runtime / "fake.sqlite")
        self.registry_lock = threading.RLock()
        self.hosts = {"local": {"transport": "local"}, "remote": {"transport": "ssh", "target": "fake"}}
        self.directories = FakeDirectories()
        self.revoked = []

    @staticmethod
    def agent_id(host, thread_id):
        return host + "::" + thread_id

    def split_agent_id(self, aid):
        if "::" not in aid:
            raise ValueError("Invalid worker")
        host, tid = aid.split("::", 1)
        self.host(host)
        return host, tid

    def host(self, host):
        if host not in self.hosts:
            raise ValueError("Unknown host")
        return self.hosts[host]

    @staticmethod
    def _directory(config, path):
        if not isinstance(path, str) or not path or any(ord(c) < 32 for c in path):
            raise ValueError("Invalid path")
        if not (path.startswith("/") if config["transport"] == "ssh" else Path(path).is_absolute()):
            raise ValueError("Absolute path required")

    def scope_revoking(self, cid, removed):
        self.revoked.append((cid, removed))

    def is_managed_worker(self, row):
        return bool(row and row.get("mode") == "terminal"
                    and re.fullmatch(r"ceohub-[a-f0-9]{16,48}", row.get("terminal_id") or "")
                    and not self.controllers.is_controller_reference(row["host"], row["thread_id"], row.get("cwd")))

    def managed_worker_rows(self):
        return [row for row in self.store.agents() if self.is_managed_worker(row)]


class FakeTerminals:
    def __init__(self):
        self.records = {}
        self.creates, self.writes = [], []
        self.create_error = None
        self.write_error = None
        self.on_create = None

    def create(self, body):
        self.creates.append(body)
        if not re.fullmatch(r"ceohub-[a-f0-9]{16,48}", body["session_id"]):
            raise ValueError("Broker terminal ID grammar")
        if body["session_id"] in self.records and not self.records[body["session_id"]]["connected"]:
            raise ValueError("Exited local PTY cannot be resurrected")
        if self.create_error:
            raise self.create_error
        row = {"id": body["session_id"], "connected": True, "agent_ready": True, "human_busy": False,
               "state": "agent", "command": "codex", "cols": 110, "rows": 32, "current_seq": 1,
               "screen": "safe fake terminal", "ansi": "safe fake terminal", "blocked_reason": None}
        self.records[row["id"]] = row
        if self.on_create:
            self.on_create(body)
        return row

    def request(self, path, timeout=2):
        return {"features": ["direct_program", "input_busy", "idle_close"]}

    def sessions(self):
        return list(self.records.values())

    def snapshot(self, tid, lines=50):
        return dict(self.records[tid])

    def output(self, tid, after=0):
        return self.snapshot(tid) | {"reset": after == 0, "data": "", "seq": 1}

    def write(self, tid, action, body):
        self.writes.append((tid, action, body))
        if self.write_error:
            raise self.write_error
        if action == "close":
            self.records[tid]["connected"] = False
            return {"id": tid, "closed": True}
        return {"submitted": True}


class FakeProcess:
    def __init__(self, events, callback=None, code=0, timeout=False, pause=None):
        self.stdout = io.StringIO("\n".join(json.dumps(e) if not isinstance(e, str) else e for e in events))
        self.callback, self.code, self.timeout, self.pause = callback, code, timeout, pause
        self.terminated = self.killed = False

    def wait(self, timeout=None):
        if self.pause:
            self.pause.wait(3)
        if self.callback:
            self.callback()
            self.callback = None
        if self.timeout and not self.terminated:
            raise subprocess.TimeoutExpired("owned fake", timeout)
        return self.code

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hub, self.terminals = FakeHub(self.temp.name), FakeTerminals()
        self.launches = []
        self.events = [{"type": "thread.started", "thread_id": SID}, {"type": "turn.completed"}]
        self.code, self.handshake, self.timeout, self.pause = 0, True, False, None
        self.processes = []
        self.service = ControllerService(self.hub, terminals=self.terminals, launcher=self.launch)
        self.hub.controllers = self.service
        self.terminals.on_create = lambda body: self.service.mark_tool_seen(body["controller_id"])
        self.service.command_resolver = lambda: [str(Path(self.temp.name) / "fake-codex.exe")]
        self.service.set_origin("http://127.0.0.1:12345")

    def tearDown(self):
        if self.pause:
            self.pause.set()
        for thread in list(self.service._threads.values()):
            thread.join(5)
        self.service.shutdown()
        self.hub.store.close()
        self.temp.cleanup()

    def launch(self, argv, **kwargs):
        self.launches.append((argv, kwargs))
        cid = next(c["id"] for c in self.service.list() if c["workspace"] == kwargs["cwd"])
        callback = (lambda: self.service.mark_tool_seen(cid)) if self.handshake else None
        process = FakeProcess(self.events, callback, self.code, self.timeout, self.pause)
        self.processes.append(process)
        return process

    def worker(self, tid="worker", active=True, host="local", cwd=None):
        terminal_id = "ceohub-" + hashlib.sha256((host + tid).encode()).hexdigest()[:24]
        self.hub.store.remember(host, tid, "Worker", active=active, mode="terminal", terminal_id=terminal_id,
                                cwd=cwd or str(Path(self.temp.name) / "business"))
        return host + "::" + tid

    def prepare(self, **body):
        return self.service.prepare(body)

    def started(self):
        c = self.prepare()
        self.service.start(c["id"], "start-1")
        self.settled(c["id"])
        return self.service.get(c["id"], refresh=True)

    def settled(self, cid):
        deadline = time.monotonic() + 5
        while cid in self.service._threads and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertNotIn(cid, self.service._threads)

    def test_owned_workspace_and_private_capability_no_response_leak(self):
        c = self.prepare()
        row = self.service._row(c["id"])
        descriptor = json.loads(Path(row["descriptor_file"]).read_text())
        self.assertEqual(descriptor["version"], "0.7.0")
        self.assertEqual(c["state"], "prepared")
        self.assertEqual(self.service.authenticate(descriptor["token"]), c["id"])
        self.assertIsNone(self.service.authenticate("wrong" * 10))
        serialized = json.dumps(self.service.list())
        self.assertNotIn(descriptor["token"], serialized)
        self.assertNotIn("cap_hash", serialized)
        self.assertNotIn("descriptor_file", serialized)
        self.assertEqual(self.hub.store.agents(), [])
        workspace = Path(c["workspace"])
        self.assertIn("human", (workspace / "AGENTS.md").read_text())
        settings = (workspace / ".codex/config.toml").read_text()
        self.assertIn("[mcp_servers.agent_captain]", settings)
        self.assertIn("AGENTCAPTAIN_CONNECTION_FILE", settings)
        self.assertIn("AGENTCAPTAIN_CONTROLLER_ID", settings)
        self.assertNotIn("[mcp_servers.pron_hub]", settings)
        self.assertNotIn("PRON_HUB_CONNECTION_FILE", settings)
        if os.name != "nt":
            self.assertEqual(Path(row["descriptor_file"]).stat().st_mode & 0o777, 0o600)

    def test_default_primary_and_explicit_new(self):
        c = self.prepare()
        self.assertEqual(c["id"], self.prepare()["id"])
        self.assertNotEqual(c["id"], self.prepare(new=True)["id"])
        with self.assertRaises(ValueError):
            self.prepare(title="a different manager")

    def test_created_worker_joins_automatic_management_with_legacy_inactive_workers(self):
        old = self.worker('old-worker')
        c = self.prepare(agent_ids=[old])
        with self.hub.store.lock, self.hub.store.db:
            self.hub.store.db.execute('update agents set active=0 where thread_id=?', ('old-worker',))
        new = self.worker('new-worker')
        self.service.add_created_agent(c['id'], 'local', 'new-worker')
        self.assertEqual(set(self.service.allowed_agents(c['id'])), {old, new})
        self.service._update(c["id"], state="ready", session_id=SID, tools_ready=1)
        self.assertTrue(self.service.authorize(c['id'], 'local', 'old-worker'))

    def test_uninitialized_manager_cannot_authorize_automatically_enrolled_workers(self):
        self.worker('old-worker', active=False)
        c = self.prepare()
        self.assertFalse(self.service.authorize(c['id'], 'local', 'old-worker'))

    def test_manager_ancestor_directory_does_not_block_home_or_other_work(self):
        c = self.prepare()
        workspace = Path(c['workspace'])
        self.assertFalse(self.service.is_controller_reference('local', '', str(workspace.parent)))
        self.assertFalse(self.service.is_controller_reference('local', '', str(workspace.parent / 'other-project')))
        self.assertTrue(self.service.is_controller_reference('local', '', str(workspace)))
        self.assertTrue(self.service.is_controller_reference('local', '', str(workspace / 'nested')))

    def test_explicit_workspace_root_outside_checkout_and_only_owned_cid(self):
        base = Path(self.temp.name) / "separate-management-workspaces"
        service = ControllerService(self.hub, terminals=self.terminals, launcher=self.launch, workspace_root=base)
        service.set_origin("http://127.0.0.1:12345")
        c = service.prepare({})
        workspace = Path(c["workspace"])
        self.assertEqual(workspace, base / c["id"])
        self.assertFalse(workspace.resolve().is_relative_to(ROOT.resolve()))
        self.assertEqual([p.name for p in base.iterdir()], [c["id"]])
        self.assertTrue((workspace / "AGENTS.md").is_file())
        self.assertTrue((workspace / ".codex/config.toml").is_file())
        self.assertNotEqual(Path(service._row(c["id"])["descriptor_file"]).parent, workspace)

    def test_preexisting_workspace_file_is_never_overwritten(self):
        cid = SID
        workspace = self.service.workspace_root / cid
        workspace.mkdir()
        sentinel = workspace / "AGENTS.md"
        sentinel.write_text("user-owned guidance", encoding="utf-8")
        with patch("agent_captain.core.controllers.uuid.uuid4", return_value=cid):
            with self.assertRaises(FileExistsError):
                self.prepare()
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "user-owned guidance")
        self.assertEqual(self.service.list(), [])
        self.assertFalse((self.service.runtime / cid / "connection.json").exists())

    def test_default_workspace_root_explicit_runtime_is_owned_and_non_mutating(self):
        explicit = Path(self.temp.name) / "not-created"
        self.assertEqual(default_workspace_root(explicit), explicit / "workspaces")
        self.assertFalse(explicit.exists())
        if os.name == "nt":
            with patch.dict(os.environ, {"LOCALAPPDATA": str(explicit)}):
                selected = default_workspace_root()
                self.assertEqual(selected, explicit / "AgentCaptain" / "management-workspaces")
                self.assertEqual(legacy_workspace_root(), explicit / "PRONHub" / "management-workspaces")
        else:
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(explicit)}):
                selected = default_workspace_root()
                self.assertEqual(selected, explicit / "agent-captain" / "management-workspaces")
                self.assertEqual(legacy_workspace_root(), explicit / "pron-hub" / "management-workspaces")
        self.assertFalse(explicit.exists())

    def test_prepare_request_key_conflict_and_replay(self):
        c = self.prepare(title="One", request_key="prepare-1")
        self.assertEqual(c["id"], self.prepare(title="One", request_key="prepare-1")["id"])
        with self.assertRaises(ValueError):
            self.prepare(title="Two", request_key="prepare-1")

    def test_manager_mcp_overrides_reuse_only_one_existing_server_namespace(self):
        c = self.prepare()
        row = self.service._row(c["id"])
        configuration = Path(c["workspace"]) / ".codex" / "config.toml"
        for sections, expected in (("[mcp_servers.agent_captain]\n", "agent_captain"),
                                   ("[mcp_servers.pron_hub]\n", "pron_hub"),
                                   ("[mcp_servers.pron_hub]\n[mcp_servers.agent_captain]\n", "agent_captain")):
            with self.subTest(sections=sections):
                configuration.write_text(sections, encoding="utf-8")
                before = configuration.read_bytes()
                arguments = self.service._overrides(row)
                values = dict(part.split("=", 1) for part in arguments[1::2])
                prefix = "mcp_servers." + expected
                self.assertEqual(arguments[::2], ["-c"] * 5)
                self.assertEqual(set(values), {prefix + suffix for suffix in (
                    ".command", ".args", ".required", ".env.AGENTCAPTAIN_CONNECTION_FILE",
                    ".env.AGENTCAPTAIN_CONTROLLER_ID")})
                self.assertEqual(json.loads(values[prefix + ".env.AGENTCAPTAIN_CONTROLLER_ID"]), c["id"])
                self.assertEqual(json.loads(values[prefix + ".env.AGENTCAPTAIN_CONNECTION_FILE"]), row["descriptor_file"])
                self.assertEqual(configuration.read_bytes(), before)
        self.assertEqual(self.launches, [])
        self.assertEqual(self.terminals.creates, [])
        self.assertEqual(self.terminals.writes, [])

    def test_new_default_accepts_exact_owned_legacy_workspace_without_moving_it(self):
        c = self.prepare()
        old_workspace = Path(c["workspace"])
        canonical = Path(self.temp.name) / "AgentCaptain" / "management-workspaces"
        legacy = Path(self.temp.name) / "PRONHub" / "management-workspaces"
        legacy.mkdir(parents=True)
        workspace = legacy / c["id"]
        old_workspace.rename(workspace)
        (workspace / "AGENTS.md").write_text(LEGACY_ROLE, encoding="utf-8")
        with self.hub.store.lock, self.hub.store.db:
            self.hub.store.db.execute("update controllers set workspace=? where id=?", (str(workspace), c["id"]))
        with patch("agent_captain.core.controllers.default_workspace_root", return_value=canonical), \
             patch("agent_captain.core.controllers.legacy_workspace_root", return_value=legacy):
            service = ControllerService(self.hub, terminals=self.terminals, launcher=self.launch, workspace_root=canonical)
            result = service.prepare({})
        self.assertEqual(result["id"], c["id"])
        self.assertEqual(result["workspace"], str(workspace))
        self.assertEqual(result["policy_sync"]["status"], "current")
        self.assertEqual((workspace / "AGENTS.md").read_text(encoding="utf-8"), ROLE)
        self.assertFalse((canonical / c["id"]).exists())
        self.assertEqual(self.launches, [])
        self.assertEqual(self.terminals.creates, [])
        self.assertEqual(self.terminals.writes, [])

    def test_explicit_custom_workspace_root_cannot_gain_legacy_default_filesystem_authority(self):
        c = self.prepare()
        canonical = Path(self.temp.name) / "AgentCaptain" / "management-workspaces"
        legacy = Path(self.temp.name) / "PRONHub" / "management-workspaces"
        legacy.mkdir(parents=True)
        workspace = legacy / c["id"]
        Path(c["workspace"]).rename(workspace)
        path = workspace / "AGENTS.md"
        path.write_text(LEGACY_ROLE, encoding="utf-8")
        before = path.read_bytes()
        self.hub.store.setting("controller-policy-file:" + c["id"], {
            "version": "owned-old-policy", "sha256": hashlib.sha256(before).hexdigest()})
        with self.hub.store.lock, self.hub.store.db:
            self.hub.store.db.execute("update controllers set workspace=? where id=?", (str(workspace), c["id"]))
        with patch("agent_captain.core.controllers.default_workspace_root", return_value=canonical), \
             patch("agent_captain.core.controllers.legacy_workspace_root", return_value=legacy):
            custom = Path(self.temp.name) / "explicit-custom-manager-base"
            service = ControllerService(self.hub, terminals=self.terminals, launcher=self.launch, workspace_root=custom)
            result = service.prepare({})
        self.assertEqual(result["policy_sync"]["status"], "unsafe_path")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.launches, [])
        self.assertEqual(self.terminals.creates, [])
        self.assertEqual(self.terminals.writes, [])

    def test_automatic_access_ignores_old_narrow_grants_and_legacy_activation(self):
        aid = self.worker()
        other = self.worker("other", active=False)
        c = self.prepare(agent_ids=[aid])
        self.assertEqual(set(c["scope"]), {aid, other})
        self.assertFalse(self.service.authorize(c["id"], "local", "worker"))
        self.service.start(c["id"], "start-scope")
        self.settled(c["id"])
        self.assertTrue(self.service.authorize(c["id"], "local", "worker"))
        c2 = self.prepare(new=True, agent_ids=[aid])
        self.service._update(c2["id"], state="ready", session_id=OTHER, tools_ready=1)
        self.assertTrue(self.service.authorize(c2["id"], "local", "worker"))
        self.assertTrue(self.service.authorize(c["id"], "local", "other"))
        self.hub.store.set_active("local", "worker", False)
        self.assertTrue(self.service.authorize(c["id"], "local", "worker"))
        later = self.worker("connected-later", active=False, host="remote")
        self.assertIn(later, self.service.allowed_agents(c["id"]))
        self.assertTrue(self.service.authorize(c["id"], "remote", "connected-later"))
        self.assertEqual(self.service.allowed_hosts(c["id"]), {})

    def test_legacy_scope_is_compatible_but_missing_closed_history_are_not_workers(self):
        aid = self.worker(active=False)
        c = self.prepare(agent_ids=[aid, "local::missing"])
        self.service._update(c["id"], state="ready", session_id=SID, tools_ready=1)
        self.assertTrue(self.service.authorize(c["id"], "local", "worker"))
        self.assertFalse(self.service.authorize(c["id"], "local", "missing"))
        for mode in ("closed", "closing", "close_unknown", "history"):
            with self.hub.store.lock, self.hub.store.db:
                self.hub.store.db.execute("update agents set mode=? where thread_id='worker'", (mode,))
            self.assertFalse(self.service.authorize(c["id"], "local", "worker"), mode)
            self.assertEqual(self.service.allowed_agents(c["id"]), [])
        self.assertEqual(self.prepare(agent_ids=[])["id"], c["id"])

    def test_legacy_scope_changes_neither_shrink_automatic_access_nor_revoke_pending(self):
        aid = self.worker()
        c = self.prepare(agent_ids=[aid], host_roots={"remote": ["/work"]})
        self.service._update(c["id"], state="ready", session_id=SID, tools_ready=1)
        pending = self.hub.store.enqueue("local", "worker", "pending", origin="plugin", request_key="pending")
        self.service.set_scope(c["id"], [])
        self.assertEqual(self.hub.revoked, [])
        self.assertTrue(self.service.authorize(c["id"], "local", "worker"))
        self.assertEqual(self.hub.store.delivery(pending)["state"], "queued")
        self.assertEqual(self.service.allowed_agents(c["id"]), [aid])
        self.assertEqual(self.service.allowed_hosts(c["id"]), {"remote": ["/work"]})
        legacy = self.hub.store.db.execute("select host,thread_id from controller_worker_scope where controller_id=?", (c["id"],)).fetchall()
        self.assertEqual([tuple(row) for row in legacy], [("local", "worker")])
        self.service.set_scope(c["id"], [], host_roots={})
        self.assertEqual(self.service.allowed_hosts(c["id"]), {})

    def test_manager_cannot_be_worker_by_id_or_workspace(self):
        c = self.started()
        self.assertTrue(self.service.is_controller_reference("local", SID))
        self.assertTrue(self.service.is_controller_reference("local", c["terminal_id"]))
        aid = self.worker(tid="bad-alias", cwd=c["workspace"])
        with self.assertRaises(PermissionError):
            self.service.set_scope(c["id"], [aid])
        self.assertNotIn(aid, self.service.allowed_agents(c["id"]))
        self.assertFalse(self.service.authorize(c["id"], "local", "bad-alias"))
        self.assertFalse(self.service.authorize(c["id"], "local", SID))

    def test_host_permission_requires_explicit_canonical_root(self):
        c = self.prepare(allowed_hosts=["remote"])
        self.assertFalse(self.service.authorize_creation(c["id"], "remote", "/work/project"))
        self.service.set_scope(c["id"], [], host_roots={"remote": ["/work"]})
        self.service.start(c["id"], "start-roots")
        self.settled(c["id"])
        self.assertTrue(self.service.authorize_creation(c["id"], "remote", "/work/project"))
        self.assertFalse(self.service.authorize_creation(c["id"], "remote", "/workspace"))
        self.assertFalse(self.service.authorize_creation(c["id"], "remote", "/work/../other"))
        self.assertEqual(self.hub.directories.calls[-1][1], "/work")

    def test_bootstrap_exact_native_identity_read_only_and_tools_handshake(self):
        c = self.started()
        self.assertEqual(c["session_id"], SID)
        self.assertEqual(c["state"], "ready")
        self.assertTrue(c["tools_ready"])
        argv, kwargs = self.launches[0]
        self.assertIn("--json", argv)
        self.assertIn("read-only", argv)
        self.assertNotIn("--yolo", argv)
        self.assertNotIn("--ignore-user-config", argv)
        self.assertEqual(kwargs["env"], os.environ.copy())
        self.assertEqual(argv[-1], INITIALIZATION)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        body = self.terminals.creates[0]
        self.assertEqual(body["cwd"], c["workspace"])
        self.assertIn(SID, body["args"])
        self.assertNotIn(INITIALIZATION, body["args"])

    def test_duplicate_start_and_new_request_during_bootstrap_never_spawn_twice(self):
        self.pause = threading.Event()
        c = self.prepare()
        self.service.start(c["id"], "start-1")
        self.service.start(c["id"], "start-1")
        # The native SID can arrive before the bootstrap completes. A second
        # start may safely report pending rather than raise, but never attaches
        # or creates a second process while the first turn is running.
        self.service.start(c["id"], "start-2")
        self.pause.set()
        self.settled(c["id"])
        self.assertEqual(len(self.launches), 1)

    def test_missing_identity_unknown_and_different_key_no_retry(self):
        self.events = [{"type": "item.completed", "item": {"type": "agent_message", "text": "Session: " + SID}}]
        c = self.started()
        self.assertEqual(c["state"], "unknown")
        self.assertIsNone(c["session_id"])
        with self.assertRaises(RuntimeError):
            self.service.start(c["id"], "new-start")
        self.assertEqual(len(self.launches), 1)

    def test_inflight_start_before_native_identity_reports_same_operation(self):
        c = self.prepare()
        self.service._update(c['id'], state='bootstrapping', phase='native_session')
        with patch.dict(self.service._threads, {c['id']: object()}):
            result = self.service.start(c['id'], 'early-duplicate-start')
        self.assertEqual(result['id'], c['id'])
        self.assertEqual(result['state'], 'bootstrapping')
        self.assertIsNone(result['session_id'])
        self.assertEqual(self.launches, [])
        self.assertEqual(self.terminals.creates, [])

    def test_unclassified_failed_turn_is_not_success_even_with_zero_exit(self):
        self.events = [{'type': 'thread.started', 'thread_id': SID},
                       {'type': 'turn.failed', 'error': {'message': 'Synthetic unknown failure'}}]
        c = self.started()
        self.assertEqual(c['state'], 'failed')
        self.assertEqual(c['error']['code'], 'bootstrap_failed')
        self.assertEqual(c['session_id'], SID)
        self.assertEqual(self.terminals.creates, [])

    def test_reasoning_and_invalid_json_not_saved_or_identity(self):
        self.events = ["not-json", {"type": "item.completed", "item": {"type": "reasoning", "text": "PRIVATE THOUGHTS " + OTHER}},
                       {"type": "thread.started", "thread_id": SID}]
        c = self.started()
        self.assertEqual(c["session_id"], SID)
        self.assertNotIn("PRIVATE THOUGHTS", json.dumps(self.service.list()))
        self.assertNotIn(OTHER, json.dumps(self.service.list()))

    def test_conflicting_or_invalid_native_identity_is_not_ready(self):
        self.events += [{"type": "thread.started", "thread_id": OTHER}]
        c = self.started()
        self.assertEqual(c["state"], "unknown")
        self.assertEqual(c["error"]["code"], "invalid_session_event")
        self.assertEqual(self.terminals.creates, [])

    def test_tools_not_seen_never_claim_ready(self):
        self.handshake = False
        c = self.started()
        self.assertEqual(c["session_id"], SID)
        self.assertEqual(c["error"]["code"], "tools_unverified")
        self.assertFalse(c["tools_ready"])
        self.assertEqual(self.terminals.creates, [])

    def test_failed_bootstrap_keeps_session_and_resume_does_not_init_again(self):
        self.code = 1
        c = self.started()
        self.assertEqual(c["state"], "failed")
        self.assertEqual(c["session_id"], SID)
        resumed = self.service.resume(c["id"], "resume-1")
        self.assertEqual(resumed["session_id"], SID)
        self.assertEqual(len(self.launches), 1)
        self.assertNotIn(INITIALIZATION, self.terminals.creates[0]["args"])

    def test_timeout_terminates_only_owned_bootstrap(self):
        self.timeout = True
        c = self.started()
        self.assertEqual(c["error"]["code"], "bootstrap_timeout")
        self.assertTrue(self.processes[0].terminated)
        self.assertEqual(self.terminals.writes, [])

    def test_structured_error_classification_never_exposes_secret_error_body(self):
        self.events += [{"type": "turn.failed", "error": {"message": "Background server has incompatible feature settings SECRET_TOKEN"}}]
        self.code = 1
        c = self.started()
        self.assertEqual(c["error"]["code"], "daemon_settings_conflict")
        self.assertNotIn("SECRET_TOKEN", json.dumps(c))
        self.assertEqual(c["session_id"], SID)

    def test_bootstrap_handshake_does_not_prove_interactive_tool_readiness(self):
        self.terminals.on_create = None
        c = self.started()
        self.assertEqual(c["session_id"], SID)
        self.assertFalse(c["tools_ready"])
        self.assertEqual(c["state"], "needs_attention")
        self.assertEqual(c["error"]["code"], "tools_unverified")
        self.service.mark_tool_seen(c["id"])
        self.assertEqual(self.service.list()[0]["state"], "ready")

    def test_desktop_atomic_busy_rejection_preserves_hub_entry(self):
        c = self.started()
        self.terminals.write_error = BrokerError("raw secret", "human_busy", 409)
        with self.assertRaisesRegex(RuntimeError, "原输入入口保留"):
            self.service.desktop(c["id"], True)
        self.assertEqual(self.service.get(c["id"])["input_owner"], "hub")
        self.assertTrue(self.terminals.records[c["terminal_id"]]["connected"])

    def test_desktop_older_broker_rejected_before_close(self):
        c = self.started()
        self.terminals.request = lambda *args, **kwargs: {"features": ["direct_program"]}
        with self.assertRaisesRegex(RuntimeError, "尚不支持安全移交"):
            self.service.desktop(c["id"], True)
        self.assertEqual(self.terminals.writes, [])
        self.assertEqual(self.service.get(c["id"])["input_owner"], "hub")

    def test_terminal_unknown_uses_deterministic_id_and_no_blind_retry(self):
        self.terminals.create_error = RuntimeError("SECRET token in backend error")
        c = self.started()
        self.assertEqual(c["state"], "unknown")
        self.assertRegex(c["terminal_id"], r"^ceohub-[a-f0-9]{32}$")
        self.assertNotIn("SECRET", json.dumps(c))
        with self.assertRaises(RuntimeError):
            self.service.resume(c["id"], "resume-1")
        self.assertEqual(len(self.terminals.creates), 1)

    def test_same_session_resume_idempotent_and_no_worker_close(self):
        c = self.started()
        self.service.resume(c["id"], "resume-1")
        self.service.resume(c["id"], "resume-1")
        self.assertEqual(len(self.terminals.creates), 1)
        self.service.close(c["id"], True)
        self.assertEqual([w[0] for w in self.terminals.writes], [c["terminal_id"]])
        closed = self.service.get(c["id"])
        self.assertEqual(closed["state"], "closed")
        descriptor = json.loads(Path(self.service._row(c["id"])["descriptor_file"]).read_text())
        self.assertIsNone(self.service.authenticate(descriptor["token"]))
        self.assertEqual(self.service.resume(c["id"], "return-1")["session_id"], SID)
        self.assertEqual(len(self.launches), 1)

    def test_resume_keeps_initialization_launcher_despite_changed_path(self):
        original = str(Path(self.temp.name) / "initial-app-codex.exe")
        resolved = []
        def resolver():
            resolved.append(True)
            return [original]
        self.service.command_resolver = resolver
        c = self.started()
        self.assertEqual(len(resolved), 1)
        self.assertEqual(self.hub.store.setting("controller-launch:" + c["id"]), [original])
        self.service.close(c["id"], True)
        self.service.command_resolver = lambda: self.fail("resume must not consult changed PATH")
        result = self.service.resume(c["id"], "resume-pinned")
        self.assertEqual(result["session_id"], SID)
        self.assertEqual(self.terminals.creates[-1]["program"], original)
        self.assertEqual(len(self.launches), 1)

    def test_missing_pinned_launcher_never_falls_back(self):
        c = self.started()
        self.service.close(c["id"], True)
        with self.hub.store.lock, self.hub.store.db:
            self.hub.store.db.execute("delete from settings where key=?", ("controller-launch:" + c["id"],))
        self.service.command_resolver = lambda: self.fail("missing pin must not fallback")
        result = self.service.resume(c["id"], "resume-no-pin")
        self.assertEqual(result["state"], "needs_attention")
        self.assertEqual(result["error"]["code"], "codex_unavailable")
        self.assertEqual(result["session_id"], SID)
        self.assertEqual(len(self.terminals.creates), 1)

    def test_deleted_pinned_binary_refuses_clearly_without_new_terminal(self):
        c = self.started()
        self.service.close(c["id"], True)
        # Turn on the real existence guard after fake bootstrap. Its fake pin
        # never exists: no real CLI or file needs to be created/deleted.
        self.service._launcher_injected = False
        self.service.command_resolver = lambda: self.fail("deleted pin must not fallback")
        result = self.service.resume(c["id"], "resume-deleted-pin")
        self.assertEqual(result["error"]["code"], "codex_unavailable")
        self.assertEqual(len(self.terminals.creates), 1)

    def test_pinned_launcher_stores_no_flags_env_or_arbitrary_script(self):
        c = self.started()
        command = self.hub.store.setting("controller-launch:" + c["id"])
        self.assertEqual(len(command), 1)
        self.assertNotIn("PRON_HUB_CONNECTION_FILE", json.dumps(command))
        for unsafe in ([command[0], "--api-key=secret"], [str(Path(self.temp.name) / "run.cmd")],
                       [str(Path(self.temp.name) / "node.exe"), str(Path(self.temp.name) / "unknown.js")]):
            self.hub.store.setting("controller-launch:" + c["id"], unsafe)
            with self.assertRaises(RuntimeError):
                self.service._trusted_launch(c["id"])

    def test_desktop_requires_confirm_ready_and_no_draft_truthful_state(self):
        c = self.started()
        with self.assertRaises(ValueError):
            self.service.desktop(c["id"])
        self.terminals.records[c["terminal_id"]]["human_busy"] = True
        with self.assertRaises(RuntimeError):
            self.service.desktop(c["id"], True)
        self.terminals.records[c["terminal_id"]]["human_busy"] = False
        result = self.service.desktop(c["id"], True)
        self.assertEqual(result["url"], "codex://threads/" + SID)
        self.assertEqual(result["state"], "desktop_unverified")
        self.assertFalse(result["desktop_verified"])
        self.assertEqual(result["input_owner"], "desktop")
        with self.assertRaises(PermissionError):
            self.service.input(c["id"], "should not enter second writer")
        self.assertEqual(self.service.resume(c["id"], "return-1")["session_id"], SID)

    def test_desktop_close_unknown_never_opens_or_restarts_other_entry(self):
        c = self.started()
        self.terminals.write_error = RuntimeError("secret credential")
        with self.assertRaisesRegex(RuntimeError, "释放结果未知"):
            self.service.desktop(c["id"], True)
        self.assertEqual(self.service.get(c["id"])["state"], "unknown")
        self.assertEqual(len(self.terminals.creates), 1)

    def test_setup_menu_is_not_ready_even_when_codex_process_exists(self):
        c = self.started()
        self.terminals.records[c["terminal_id"]].update(agent_ready=False, blocked_reason="daemon_settings_conflict")
        after = self.service.get(c["id"], refresh=True)
        self.assertEqual(after["state"], "needs_attention")
        self.assertFalse(after["agent_ready"])

    def test_context_scoped_self_safe_not_worker(self):
        c = self.prepare()
        context = self.service.view_context(c["id"])
        self.assertEqual(context["mode"], "scoped")
        self.assertEqual(context["controller_id"], c["id"])
        self.assertEqual(context["worker_access"], "automatic")
        self.assertEqual(context["controller"]["worker_access"], "automatic")
        self.assertTrue(context["policy"]["scope_enforced_by_hub"])
        self.assertFalse(context["policy"]["operating_system_isolation"])
        self.assertEqual(self.hub.store.agents(), [])

    def test_generated_bootstrap_explains_automatic_workers_and_explicit_creation_roots(self):
        c = self.prepare()
        role = (Path(c["workspace"]) / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("automatically manageable", role)
        self.assertIn("No worker activation", role)
        self.assertIn("explicit allowed project roots", role)
        self.assertIn("Initialization only calls get_controller_context", role)
        self.assertIn("All registered open shared worker terminals", INITIALIZATION)
        self.assertIn("Call only", INITIALIZATION)

    def legacy_policy(self, controller):
        path = Path(controller["workspace"]) / "AGENTS.md"
        path.write_text(LEGACY_ROLE, encoding="utf-8")
        with self.hub.store.lock, self.hub.store.db:
            self.hub.store.db.execute("delete from settings where key=?",
                ("controller-policy-file:" + controller["id"],))
        return path

    def test_new_manager_policy_is_expert_delegate_first_without_broader_capability(self):
        c = self.prepare()
        path = Path(c["workspace"]) / "AGENTS.md"
        self.assertEqual(path.read_text(encoding="utf-8"), MANAGER_ROLE)
        self.assertEqual(ROLE, MANAGER_ROLE)
        self.assertIn("Do project work yourself only when the user asks you to", ROLE)
        self.assertIn("Write in English", ROLE)
        self.assertIn("Initialization only calls get_controller_context", ROLE)
        record = self.hub.store.setting("controller-policy-file:" + c["id"])
        self.assertEqual(record, {"version": POLICY_VERSION,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        context = self.service.view_context(c["id"])
        self.assertEqual(context["management_policy"]["execution_policy"], "delegate_first")
        self.assertEqual(context["management_policy"]["decision_owner"], "human_user")
        self.assertEqual(context["management_instructions"], MANAGER_ROLE)
        self.assertEqual(context["policy_sync"]["status"], "current")
        self.assertFalse(context["policy_sync"]["reload_required"])
        self.assertFalse(self.service.authorize_creation(c["id"], "remote", "/arbitrary"))
        self.assertEqual(self.launches, [])
        self.assertEqual(self.terminals.creates, [])
        self.assertEqual(self.terminals.writes, [])

    def test_current_context_policy_is_independent_and_does_not_mutate_shared_contract(self):
        c = self.prepare()
        policy = self.service.view_context(c["id"])["management_policy"]
        policy["direct_execution_exceptions"].append("unsafe_test_override")
        self.assertNotIn("unsafe_test_override", MANAGEMENT_POLICY["direct_execution_exceptions"])
        self.assertNotIn("unsafe_test_override",
            self.service.view_context(c["id"])["management_policy"]["direct_execution_exceptions"])

    def test_read_only_context_detects_legacy_and_supplies_current_policy_without_rewriting(self):
        c = self.prepare()
        path = self.legacy_policy(c)
        before, metadata = path.read_bytes(), path.stat()
        context = self.service.view_context(c["id"])
        self.service.get(c["id"])
        self.service.list()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mtime_ns, metadata.st_mtime_ns)
        self.assertIsNone(self.hub.store.setting("controller-policy-file:" + c["id"]))
        self.assertEqual(context["policy_sync"]["status"], "outdated")
        self.assertFalse(context["policy_sync"]["workspace_updated"])
        self.assertEqual(context["management_policy"]["version"], POLICY_VERSION)
        self.assertEqual(context["management_instructions"], MANAGER_ROLE)
        self.assertEqual(self.terminals.writes, [])
        self.assertEqual(self.terminals.creates, [])
        self.assertEqual(self.launches, [])

    def test_explicit_prepare_reuse_migrates_exact_legacy_role_without_new_session(self):
        worker = self.worker()
        c = self.prepare(agent_ids=[worker])
        path = self.legacy_policy(c)
        refreshed = self.prepare()
        self.assertEqual(refreshed["id"], c["id"])
        self.assertEqual(refreshed["workspace"], c["workspace"])
        self.assertEqual(path.read_text(encoding="utf-8"), ROLE)
        self.assertEqual(refreshed["policy_sync"]["status"], "current")
        self.assertTrue(refreshed["policy_sync"]["workspace_updated"])
        self.assertEqual(refreshed["scope"], [worker])
        self.assertEqual(refreshed["allowed_hosts"], {})
        self.assertEqual(self.terminals.writes, [])
        self.assertEqual(self.terminals.creates, [])
        self.assertEqual(self.launches, [])

    def test_prepare_request_replay_can_sync_policy_without_replaying_initialization(self):
        c = self.prepare(request_key="prepare-policy")
        path = self.legacy_policy(c)
        self.assertEqual(self.prepare(request_key="prepare-policy")["id"], c["id"])
        self.assertEqual(path.read_text(encoding="utf-8"), ROLE)
        self.assertEqual(self.launches, [])
        self.assertEqual(self.terminals.creates, [])
        self.assertEqual(self.terminals.writes, [])

    def test_start_and_connected_resume_sync_only_owned_role_and_keep_native_identity(self):
        c = self.prepare()
        path = self.legacy_policy(c)
        self.service.start(c["id"], "start-policy")
        self.settled(c["id"])
        current = self.service.get(c["id"], refresh=True)
        self.assertEqual(path.read_text(encoding="utf-8"), ROLE)
        self.assertEqual(current["session_id"], SID)
        self.assertEqual(self.launches[0][0][-1], INITIALIZATION)
        tid, launches, creates = current["terminal_id"], len(self.launches), len(self.terminals.creates)
        self.legacy_policy(current)
        result = self.service.resume(c["id"], "resume-policy")
        self.assertEqual(path.read_text(encoding="utf-8"), ROLE)
        self.assertEqual(result["session_id"], SID)
        self.assertEqual(result["terminal_id"], tid)
        self.assertEqual(len(self.launches), launches)
        self.assertEqual(len(self.terminals.creates), creates)
        self.assertEqual(self.terminals.writes, [])

    def test_generated_hash_can_migrate_a_previously_owned_role(self):
        c = self.prepare()
        path = Path(c["workspace"]) / "AGENTS.md"
        prior = "# Test-only prior Hub generated instructions\nKeep the human in charge.\n"
        path.write_text(prior, encoding="utf-8")
        self.hub.store.setting("controller-policy-file:" + c["id"], {
            "version": "prior-owned-policy", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        self.assertEqual(self.service.view_context(c["id"])["policy_sync"]["status"], "outdated")
        self.prepare()
        self.assertEqual(path.read_text(encoding="utf-8"), ROLE)

    def test_custom_policy_edits_preserved_and_never_exposed_in_context(self):
        c = self.prepare()
        path = Path(c["workspace"]) / "AGENTS.md"
        for custom in (ROLE + "\nUser custom rule: private test marker.\n",
                       LEGACY_ROLE + "\nUser custom rule: private test marker.\n"):
            path.write_text(custom, encoding="utf-8")
            before, metadata = path.read_bytes(), path.stat()
            result = self.prepare()
            context = self.service.view_context(c["id"])
            self.assertEqual(result["policy_sync"]["status"], "custom_preserved")
            self.assertEqual(context["policy_sync"]["status"], "custom_preserved")
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(path.stat().st_mtime_ns, metadata.st_mtime_ns)
            self.assertNotIn("private test marker", json.dumps(context))
            self.assertEqual(context["management_instructions"], ROLE)
        self.assertEqual(self.terminals.writes, [])
        self.assertEqual(self.terminals.creates, [])
        self.assertEqual(self.launches, [])

    def test_missing_or_invalid_encoding_policy_is_not_recreated_or_replaced(self):
        c = self.prepare()
        path = Path(c["workspace"]) / "AGENTS.md"
        path.unlink()
        self.assertEqual(self.prepare()["policy_sync"]["status"], "missing")
        self.assertFalse(path.exists())
        path.write_bytes(b"\xff\xfe\x00\x00")
        before = path.read_bytes()
        self.assertEqual(self.prepare()["policy_sync"]["status"], "custom_preserved")
        self.assertEqual(path.read_bytes(), before)

    def test_recorded_hash_does_not_allow_sync_outside_exact_owned_workspace(self):
        c = self.prepare()
        outside = Path(self.temp.name) / "user-business"
        outside.mkdir()
        path = outside / "AGENTS.md"
        path.write_text(LEGACY_ROLE, encoding="utf-8")
        self.hub.store.setting("controller-policy-file:" + c["id"], {
            "version": "prior-owned-policy", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        with self.hub.store.lock, self.hub.store.db:
            self.hub.store.db.execute("update controllers set workspace=? where id=?", (str(outside), c["id"]))
        before = path.read_bytes()
        self.assertEqual(self.prepare()["policy_sync"]["status"], "unsafe_path")
        self.assertEqual(path.read_bytes(), before)

    def test_linked_workspace_guard_refuses_matching_policy_without_touching_file(self):
        c = self.prepare()
        path = self.legacy_policy(c)
        workspace = path.parent
        original_is_symlink = Path.is_symlink
        def linked_owned_directory(candidate):
            return candidate == workspace or original_is_symlink(candidate)
        before = path.read_bytes()
        with patch("agent_captain.core.controllers.Path.is_symlink", linked_owned_directory):
            self.assertEqual(self.prepare()["policy_sync"]["status"], "unsafe_path")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.launches, [])
        self.assertEqual(self.terminals.creates, [])
        self.assertEqual(self.terminals.writes, [])

    def test_invalid_start_or_resume_request_cannot_trigger_policy_write(self):
        c = self.prepare()
        path = self.legacy_policy(c)
        before = path.read_bytes()
        for action in (self.service.start, self.service.resume):
            with self.assertRaises(ValueError):
                action(c["id"], "bad key with spaces")
            self.assertEqual(path.read_bytes(), before)
        self.assertIsNone(self.hub.store.setting("controller-policy-file:" + c["id"]))

    def test_owned_historical_runtime_workspace_remains_migratable(self):
        c = self.prepare()
        path = self.legacy_policy(c)
        service = ControllerService(self.hub, terminals=self.terminals, launcher=self.launch,
                                    workspace_root=Path(self.temp.name) / "new-manager-base")
        self.assertEqual(service.prepare({})["id"], c["id"])
        self.assertEqual(path.read_text(encoding="utf-8"), ROLE)
        self.assertEqual(self.launches, [])
        self.assertEqual(self.terminals.creates, [])

    def test_linked_policy_file_is_never_replaced_even_with_matching_hash(self):
        c = self.prepare()
        path = self.legacy_policy(c)
        alias = Path(self.temp.name) / "human-linked-policy.md"
        try:
            os.link(path, alias)
        except OSError:
            self.skipTest("This filesystem does not permit hard links")
        before = alias.read_bytes()
        self.hub.store.setting("controller-policy-file:" + c["id"], {
            "version": "prior-owned-policy", "sha256": hashlib.sha256(before).hexdigest()})
        self.assertEqual(self.prepare()["policy_sync"]["status"], "unsafe_path")
        self.assertEqual(alias.read_bytes(), before)
        self.assertEqual(path.read_bytes(), before)

    def test_policy_sync_rechecks_human_edits_before_atomic_replace(self):
        c = self.prepare()
        path = self.legacy_policy(c)
        from agent_captain.core.controllers import _private_write
        custom = LEGACY_ROLE + "\nHuman edited during policy sync.\n"
        def changed_while_writing(temporary, text):
            _private_write(temporary, text)
            path.write_text(custom, encoding="utf-8")
        with patch("agent_captain.core.controllers._private_write", side_effect=changed_while_writing):
            result = self.prepare()
        self.assertEqual(result["policy_sync"]["status"], "custom_preserved")
        self.assertEqual(result["policy_sync"]["reason"], "changed_during_sync")
        self.assertEqual(path.read_text(encoding="utf-8"), custom)
        self.assertEqual(list(path.parent.glob(".pron-policy-*.tmp")), [])

    def test_authentication_initialization_and_identity_fences_survive_automatic_access(self):
        self.worker(active=False)
        c = self.prepare()
        for state in ("prepared", "bootstrapping", "initializing", "attaching", "unknown", "closed"):
            self.service._update(c["id"], state=state, session_id=SID, tools_ready=1)
            self.assertFalse(self.service.authorize(c["id"], "local", "worker"), state)
        self.service._update(c["id"], state="ready", cap_active=0)
        self.assertFalse(self.service.authorize(c["id"], "local", "worker"))
        self.service._update(c["id"], cap_active=1, tools_ready=0)
        self.assertFalse(self.service.authorize(c["id"], "local", "worker"))
        self.service._update(c["id"], tools_ready=1, session_id=None)
        self.assertFalse(self.service.authorize(c["id"], "local", "worker"))
        self.service._update(c["id"], session_id=SID)
        self.assertTrue(self.service.authorize(c["id"], "local", "worker"))
        self.assertFalse(self.service.authorize("invalid", "local", "worker"))

    def test_upgrade_fences_old_out_of_scope_pending_without_replaying_final_states(self):
        granted = self.worker("granted")
        self.worker("not-granted")
        self.worker("inactive", active=False)
        c = self.prepare(agent_ids=[granted])
        with self.hub.store.lock, self.hub.store.db:
            self.hub.store.db.execute("delete from settings where key='controller-automatic-worker-access-v1'")
        deliveries = {}
        for index, (tid, state) in enumerate((("granted", "human_busy"), ("not-granted", "queued"),
                ("inactive", "waiting_for_agent"), ("not-granted", "submitted"),
                ("not-granted", "unknown"), ("not-granted", "scope_revoked"))):
            key = "controller:" + c["id"] + ":" + str(index) * 64
            rid = self.hub.store.enqueue("local", tid, "old text", origin="plugin", request_key=key)
            self.hub.store.update_outbox(rid, state)
            deliveries[(tid, state)] = rid
        service = ControllerService(self.hub, terminals=self.terminals, launcher=self.launch)
        for (tid, old), rid in deliveries.items():
            expected = "not_sent" if old in ("queued", "waiting_for_agent") else old
            self.assertEqual(self.hub.store.delivery(rid)["state"], expected)
        self.assertEqual(self.terminals.writes, [])
        self.assertEqual(self.terminals.creates, [])
        self.assertEqual(self.launches, [])
        self.assertEqual(self.hub.store.setting("controller-automatic-worker-access-v1"), {"fenced": 2})
        fresh = self.hub.store.enqueue("local", "not-granted", "fresh authorized text", origin="plugin",
            request_key="controller:" + c["id"] + ":" + "f" * 64)
        ControllerService(self.hub, terminals=self.terminals, launcher=self.launch)
        self.assertEqual(self.hub.store.delivery(fresh)["state"], "queued")

    def test_context_ready_uses_current_owned_terminal_observation(self):
        c = self.started()
        context = self.service.view_context(c["id"])["controller"]
        self.assertEqual(context["state"], "ready")
        self.assertTrue(context["tools_ready"])
        self.assertTrue(context["connected"])
        self.assertTrue(context["agent_ready"])

    def test_context_prepared_without_terminal_never_claims_ready(self):
        c = self.prepare()
        context = self.service.view_context(c["id"])["controller"]
        self.assertEqual(context["state"], "prepared")
        self.assertFalse(context["connected"])
        self.assertFalse(context["agent_ready"])

    def test_manager_proxy_inherits_only_enabled_literal_loopback_and_no_global_write(self):
        base = {"KEEP": "yes", "NO_PROXY": "custom"}
        registry = FakeRegistry({"ProxyEnable": 1, "ProxyServer": "127.0.0.1:7890"})
        with patch.dict(os.environ, {"UNCHANGED": "yes"}, clear=True):
            result = manager_launch_environment(base, "nt", registry)
            self.assertEqual(dict(os.environ), {"UNCHANGED": "yes"})
        self.assertEqual(base, {"KEEP": "yes", "NO_PROXY": "custom"})
        self.assertEqual(result["HTTP_PROXY"], "http://127.0.0.1:7890")
        self.assertEqual(result["HTTPS_PROXY"], "http://127.0.0.1:7890")
        self.assertEqual(result["NO_PROXY"], "custom")

    def test_manager_proxy_protocol_specific_ipv6_and_unsupported_entries(self):
        registry = FakeRegistry({"ProxyEnable": 1, "ProxyServer": "http=127.0.0.1:1234;https=https://[::1]:4321;socks=127.0.0.1:5555"})
        result = manager_launch_environment({}, "nt", registry)
        self.assertEqual(result, {"HTTP_PROXY": "http://127.0.0.1:1234", "HTTPS_PROXY": "https://[::1]:4321"})

    def test_manager_proxy_respects_any_existing_proxy_env_including_empty(self):
        for key, value in (("HTTP_PROXY", "http://elsewhere:1234"), ("https_proxy", ""), ("All_Proxy", "socks5://existing")):
            registry = FakeRegistry({"ProxyEnable": 1, "ProxyServer": "127.0.0.1:7890"})
            base = {key: value}
            self.assertEqual(manager_launch_environment(base, "nt", registry), base)
            self.assertEqual(registry.opened, 0)

    def test_manager_proxy_disabled_pac_auto_detect_other_platform_or_registry_failure(self):
        unsupported = ({"ProxyEnable": 0}, {"ProxyEnable": "1"}, {"ProxyEnable": 1, "AutoConfigURL": "http://example.test/proxy.pac"},
                       {"ProxyEnable": 1, "AutoDetect": 1})
        for values in unsupported:
            registry = FakeRegistry({"ProxyServer": "127.0.0.1:7890"} | values)
            self.assertEqual(manager_launch_environment({"KEEP": "yes"}, "nt", registry), {"KEEP": "yes"})
        registry = FakeRegistry({"ProxyEnable": 1, "ProxyServer": "127.0.0.1:7890"})
        self.assertEqual(manager_launch_environment({}, "posix", registry), {})
        self.assertEqual(registry.opened, 0)
        self.assertEqual(manager_launch_environment({}, "nt", FakeRegistry(failure=True)), {})

    def test_manager_proxy_refuses_credentials_nonliteral_and_unsafe_urls(self):
        synthetic_credential_url = "http://" + "user:secret" + "@127.0.0.1:7890"
        for value in ("localhost:7890", "192.0.2.1:7890", synthetic_credential_url, "socks5://127.0.0.1:7890",
                      "http://127.0.0.1:7890/path", "http://127.0.0.1:7890/?secret=x", "127.0.0.1:99999", "127.0.0.1:7890\n"):
            registry = FakeRegistry({"ProxyEnable": 1, "ProxyServer": value})
            self.assertEqual(manager_launch_environment({}, "nt", registry), {})

    def test_shutdown_does_not_kill_manager_or_workers(self):
        c = self.started()
        self.service.shutdown()
        self.assertEqual(self.terminals.writes, [])
        self.assertFalse(self.processes[0].terminated)
        self.assertTrue(self.terminals.records[c["terminal_id"]]["connected"])

    def test_restart_does_not_repeat_inflight_launch_and_updates_only_owned_descriptor(self):
        c = self.prepare()
        self.service._update(c["id"], state="bootstrapping")
        new = ControllerService(self.hub, terminals=self.terminals, launcher=self.launch)
        new.set_origin("http://127.0.0.1:23456")
        self.assertEqual(new.get(c["id"])["state"], "unknown")
        descriptor = json.loads(Path(new._row(c["id"])["descriptor_file"]).read_text())
        self.assertEqual(descriptor["url"], "http://127.0.0.1:23456")
        self.assertEqual(self.launches, [])

    def test_origin_refuses_remote_and_credential_urls(self):
        for url in ("https://127.0.0.1:1", "http://example.test:1", "http://secret@127.0.0.1:1", "http://127.0.0.1:1/?token=x"):
            with self.assertRaises(ValueError):
                self.service.set_origin(url)

    def test_resolver_missing_never_uses_shell_or_unknown_cmd(self):
        with patch("agent_captain.core.controllers.shutil.which", return_value=None):
            with self.assertRaises(RuntimeError):
                resolve_codex_command()


if __name__ == "__main__":
    unittest.main()
