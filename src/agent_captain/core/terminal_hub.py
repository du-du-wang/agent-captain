"""Terminal-first management service; the Captain lives in the user's frontend."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import shlex
import subprocess
import threading
import time
import uuid
from pathlib import Path

from agent_captain import __version__ as VERSION
from agent_captain.sessions import catalog, response_wire
from agent_captain.transport.directory_browser import DirectoryBrowser
from agent_captain.transport.ssh_source import pack_python_source
from agent_captain.infrastructure.store import Store
from agent_captain.transport.terminal_client import BrokerError, TerminalClient, SSH_CONTROL_CONNECT_TIMEOUT, SSH_CONTROL_TIMEOUT
from agent_captain.core.controllers import ControllerService, default_workspace_root
from agent_captain.core.management_policy import public_management_policy
from agent_captain.core.auto_reconnect import AutoReconnect
from agent_captain.transport import remote_terminal_history, remote_terminal_close
from agent_captain.paths import ROOT, source_path
RETRYABLE = ("queued", "waiting_for_agent", "human_busy", "awaiting_connection", "blocked")
MANAGEMENT_EVENTS = ("host_configured", "host_connected", "host_unavailable", "agent_attached",
                     "agent_created", "agent_activated", "agent_deactivated", "message_submitted",
                     "terminal_input_submitted", "terminal_state", "terminal_output", "terminal_closed", "agent_updated", "agent_reconnected", "terminal_reopened")


HISTORY_ERROR_MESSAGES = {
    "ssh_handshake_failed": "独立 SSH 读取通道握手失败；已有显示连接可能仍在线，请检查 SSH 转发路由。",
    "ssh_authentication_failed": "独立 SSH 读取通道认证失败；请检查该机器的 SSH 身份配置。",
    "ssh_host_key_failed": "独立 SSH 读取通道的主机密钥核验失败；请人工核对主机身份，不要跳过核验。",
    "ssh_timeout": "独立 SSH 读取请求超时；请检查 SSH 路由后重试。",
    "ssh_transport_unavailable": "独立 SSH 读取通道当前不可用；已有显示连接的状态不因此改变。",
    "remote_read_failed": "远端只读会话查询失败；请检查所选会话记录和远端 python3。",
    "protocol_invalid": "远端未返回有效的只读会话查询结果；请检查 SSH 启动环境和远端 python3。",
    "output_read_failed": "真实会话输出读取或身份核验失败；请核对所选 Session 后重试。",
    "ssh_read_in_progress": "同一 SSH 目标已有完整会话读取正在进行，本次未发起新 SSH；请等前次读取结束，再沿用原游标重试。 [ssh_read_in_progress]",
}


class HistoryReadError(RuntimeError):
    """Only a fixed public classification crosses the SSH error boundary."""

    def __init__(self, reason_code, read_transport=None):
        self.reason_code = reason_code if reason_code in HISTORY_ERROR_MESSAGES else "remote_read_failed"
        self.read_transport = read_transport
        super().__init__(HISTORY_ERROR_MESSAGES[self.reason_code])


def _ssh_history_failure(stderr, returncode):
    # SSH/provider diagnostics can contain tokens, startup text or arbitrary
    # remote output. Inspect fixed signatures only; never return that text.
    diagnostic = stderr.lower() if isinstance(stderr, str) else ""
    if any(token in diagnostic for token in ("host key verification failed", "remote host identification has changed", "offending ", "no matching host key type found")):
        return "ssh_host_key_failed"
    if any(token in diagnostic for token in ("permission denied (", "authentication failed", "too many authentication failures", "no supported authentication methods available")):
        return "ssh_authentication_failed"
    if any(token in diagnostic for token in ("banner exchange", "kex_exchange_identification", "ssh_exchange_identification")):
        return "ssh_handshake_failed"
    if any(token in diagnostic for token in ("connection timed out", "operation timed out", "connect timeout")):
        return "ssh_timeout"
    return "ssh_transport_unavailable" if returncode == 255 else "remote_read_failed"


def _safe_output_failure(error):
    if isinstance(error, HistoryReadError):
        return error.reason_code, str(error)
    # Keep the existing, fixed fail-closed identity messages, not arbitrary
    # exceptions raised by a provider, file system, or transport library.
    known = {
        "读取期间 Codex 进程已变化，未返回旧会话输出；请重新读取。": "session_identity_changed",
        "读取期间 Codex 会话已切换，未返回旧会话输出；请重新读取。": "session_identity_changed",
        "读取期间会话关联证据冲突或失效，未返回记录；请重新核验。": "output_binding_invalid",
        "管理会话输出不能作为工作 agent 读取。": "scope_denied",
    }
    message = str(error)
    if message in known:
        return known[message], message
    return "output_read_failed", HISTORY_ERROR_MESSAGES["output_read_failed"]


class Hub:
    def __init__(self, runtime=None, terminals=None, directories=None):
        self.runtime = Path(runtime or ROOT / ".runtime")
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.runtime / "hub.sqlite")
        filename = ROOT / "config.local.json"
        self.config = json.loads((filename if filename.exists() else ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.hosts = self.store.setting("hosts") or self.config["hosts"]
        self.connections = {"local": {"connected": True, "history_available": True}}
        self.terminals = terminals if terminals is not None else TerminalClient()
        self.directories = directories if directories is not None else DirectoryBrowser()
        self.registry_lock = threading.RLock()
        self._auto_config_lock = threading.RLock()
        # Admission is fail-fast, not a network-spanning registry lock or a
        # hidden retry queue. Aliases registered with the same target share it.
        self._read_admission_lock = threading.Lock()
        self._reading_routes = set()
        # Passive response observations belong to a configured route, never to
        # a periodic ping or an extra SSH probe. One entry per host bounds it.
        self._host_latency_lock = threading.Lock()
        self._host_latencies = {}
        self._reconnecting = set()
        self.event_condition = threading.Condition()
        self.stopping = False
        self._stop = threading.Event()
        # Real management workspaces live outside this checkout: Codex folder
        # trust must not implicitly grant access to the entire Hub repository.
        # Explicit runtimes (including tests) keep all state in that namespace.
        self.controllers = ControllerService(self, workspace_root=default_workspace_root(runtime))
        # The management policy changes visibility, not execution consent. Old
        # inactive-worker deliveries must never become newly eligible merely
        # because the activation checkbox no longer exists.
        self.store.fence_pending_policy_upgrade(
            [(row["host"], row["thread_id"]) for row in self.managed_worker_rows() if not row.get("active")],
            RETRYABLE, "automatic-worker-management-v1",
            "连接即管理升级已保留原记录；旧未激活终端的待发送输入未提交，不会自动执行。")
        self._watcher = threading.Thread(target=self._watch_terminals, name="ceo-terminal-events", daemon=True)
        # Metadata reads never start the broker, SSH, a model, or a conversation.
        self._watcher.start()
        # Construction is also used by read-only CLIs and maintenance helpers.
        # Only the owning web service explicitly starts the reconnect worker.
        self.auto_reconnect = AutoReconnect(self)
        self._auto_reconnect_thread = None

    def start_auto_reconnect(self):
        with self._auto_config_lock:
            if self.stopping or self._auto_reconnect_thread is not None:
                return
            self._auto_reconnect_thread = threading.Thread(
                target=self.auto_reconnect.run, name="agent-captain-auto-reconnect", daemon=True)
            self._auto_reconnect_thread.start()

    @staticmethod
    def agent_id(host, thread_id):
        return f"{host}::{thread_id}"

    def host(self, name):
        if name not in self.hosts:
            raise ValueError("未知机器；请先在 UI 配置机器。")
        return self.hosts[name]

    def record_event(self, kind, **data):
        self.store.event(kind, **data)
        with self.event_condition:
            self.event_condition.notify_all()

    def upsert_host(self, data):
        name = data.get("id", "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", name):
            raise ValueError("机器 ID 只能包含字母、数字、短横线和下划线。")
        transport, target = data.get("transport", "ssh"), data.get("target", "").strip()
        if transport not in ("local", "ssh") or name == "local" and transport != "local":
            raise ValueError("transport 必须为 local 或 ssh，本机入口不能改为 SSH。")
        if transport == "ssh" and (not target or target.startswith("-") or any(c.isspace() or ord(c) < 32 for c in target)):
            raise ValueError("请输入有效的 SSH alias 或 user@host。")
        config = {"label": data.get("label", name).strip() or name, "transport": transport, "target": target}
        with self.registry_lock:
            old = self.hosts.get(name)
            if old and (old.get("transport"), old.get("target", "")) != (transport, target) and any(a["host"] == name for a in self.store.agents()):
                raise ValueError("此机器已有工作终端；请以新 ID 添加新地址。")
            with self._host_latency_lock:
                self.hosts[name] = config
                if old is None or (old.get("transport"), old.get("target", "")) != (transport, target):
                    # Removing this entry also invalidates in-flight receipts,
                    # including a later switch back to the same target string.
                    self._host_latencies.pop(name, None)
            self.store.setting("hosts", self.hosts)
            self.connections.pop(name, None)
        self.record_event("host_configured", host=name)
        return {"id": name, **config}

    @staticmethod
    def _unknown_host_latency(config):
        local = config.get("transport") == "local"
        return {"state": "local" if local else "unknown", "metric": "local" if local else "ssh_response",
                "last_ms": None, "last_success_at": None, "last_attempt_at": None, "reason_code": None}

    def _begin_host_latency(self, host, config):
        route = (config.get("transport"), config.get("target", ""))
        with self._host_latency_lock:
            for removed in self._host_latencies.keys() - self.hosts.keys():
                self._host_latencies.pop(removed, None)
            current = self.hosts.get(host)
            if current is None or (current.get("transport"), current.get("target", "")) != route:
                return None
            entry = self._host_latencies.get(host)
            if entry is None or entry["route"] != route:
                entry = {"route": route, "latest_attempt": 0, "value": self._unknown_host_latency(config)}
                self._host_latencies[host] = entry
            entry["latest_attempt"] += 1
            return entry, entry["latest_attempt"], time.time()

    def _finish_host_latency(self, host, observation, started, reason_code=None):
        if observation is None:
            return
        elapsed_ms, completed_at = max(0, round((time.monotonic() - started) * 1000)), time.time()
        entry, attempt, attempted_at = observation
        with self._host_latency_lock:
            config = self.hosts.get(host)
            if (self._host_latencies.get(host) is not entry or entry["latest_attempt"] != attempt
                    or config is None or (config.get("transport"), config.get("target", "")) != entry["route"]):
                return  # A new route or newer read must not inherit old timing.
            value = {**entry["value"], "last_attempt_at": attempted_at,
                     "state": "failed" if reason_code else "measured", "reason_code": reason_code}
            if reason_code is None:
                value.update(last_ms=elapsed_ms, last_success_at=completed_at)
            entry["value"] = value

    def _host_latency_view(self, host, config):
        if config.get("transport") == "local":
            return self._unknown_host_latency(config)
        with self._host_latency_lock:
            entry = self._host_latencies.get(host)
            if entry is not None and entry["route"] == (config.get("transport"), config.get("target", "")):
                return dict(entry["value"])
        return self._unknown_host_latency(config)

    def history(self, host, request):
        config = dict(self.host(host))
        if config["transport"] == "local":
            return catalog.main(request)
        target = config["target"]
        if target.startswith("-") or any(c.isspace() or ord(c) < 32 for c in target):
            raise ValueError("Invalid SSH target")
        observation, started = self._begin_host_latency(host, config), time.monotonic()
        try:
            value = self._ssh_history(config, request)
        except HistoryReadError as error:
            self._finish_host_latency(host, observation, started, error.reason_code)
            raise
        except Exception:
            self._finish_host_latency(host, observation, started, "remote_read_failed")
            raise
        self._finish_host_latency(host, observation, started)
        return value

    def _ssh_history(self, config, request):
        target = config["target"]
        payload = base64.urlsafe_b64encode(json.dumps(request, ensure_ascii=False).encode()).decode()
        # Send compact portable read-only modules through stdin. No remote installation,
        # provider startup or authentication/configuration file reads are needed.
        modules = (("session_binding", "conversation_output", "verified_output", "response_wire") if request["op"] == "read_bound_output"
                   else ("session_binding", "conversation_output") if request["op"] in ("bind_output", "resolve_output", "read_output") else ())
        source = "import sys,types\n"
        for name in modules:
            code = source_path("sessions", name + ".py").read_text(encoding="utf-8")
            source += f"_module=types.ModuleType({name!r});sys.modules[{name!r}]=_module;exec(compile({code!r},{name!r},'exec'),_module.__dict__)\n"
        source += source_path("sessions", "catalog.py").read_text(encoding="utf-8").replace("from __future__ import annotations\n", "", 1)
        packed = pack_python_source(source)
        # Only this fixed read-only transaction may retry once. Every attempt
        # redoes BOTH live identity checks with the original cursor; never use
        # this policy for input, attachment, authentication or protocol errors.
        batched = request["op"] == "read_bound_output"
        deadlines = (75, 45) if batched else (45,)
        started = time.monotonic()
        trace = []
        for attempt, deadline in enumerate(deadlines):
            attempt_started = time.monotonic()
            try:
                reply = subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ControlMaster=no", "-o", "ControlPersist=no",
                    "-o", "ConnectTimeout=30", target, "python3 - " + payload],
                    input=packed, text=True, encoding="utf-8", errors="replace",
                    capture_output=True, timeout=deadline, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                failure = _ssh_history_failure(reply.stderr, reply.returncode) if reply.returncode else None
            except subprocess.TimeoutExpired:
                failure = "ssh_timeout"
            except (OSError, UnicodeError):
                failure = "ssh_transport_unavailable"
            if failure:
                trace.append({"attempt": attempt + 1, "reason_code": failure,
                              "elapsed_ms": round((time.monotonic() - attempt_started) * 1000)})
                if attempt + 1 < len(deadlines) and failure in ("ssh_timeout", "ssh_handshake_failed", "ssh_transport_unavailable"):
                    continue
                diagnostic = {"kind": "ssh", "attempts": attempt + 1,
                    "elapsed_ms": round((time.monotonic() - started) * 1000), "attempt_trace": trace,
                    "batched_identity_checks": True} if batched else None
                raise HistoryReadError(failure, diagnostic) from None
            try:
                result = response_wire.decode_reply(reply.stdout) if batched else json.loads(reply.stdout)
            except (ValueError, TypeError, UnicodeError):
                raise HistoryReadError("protocol_invalid") from None
            if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                raise HistoryReadError("protocol_invalid")
            if not result["ok"]:
                raise HistoryReadError("remote_read_failed")
            if not isinstance(result.get("result"), dict):
                raise HistoryReadError("protocol_invalid")
            value = result["result"]
            if batched:
                value = {**value, "read_transport": {"kind": "ssh", "attempts": attempt + 1,
                         "elapsed_ms": round((time.monotonic() - started) * 1000),
                         "response_bytes": len(reply.stdout.encode("utf-8")),
                         "attempt_trace": trace + [{"attempt": attempt + 1, "reason_code": "ok",
                             "elapsed_ms": round((time.monotonic() - attempt_started) * 1000)}],
                         "batched_identity_checks": True}}
            return value

    def connect_host(self, host):
        self.host(host)
        try:
            result = self.history(host, {"op": "probe"})
            state = {"connected": True, "history_available": result.get("history_exists", False), **result}
        except Exception as error:
            state = {"connected": False, "error": str(error)}
        self.connections[host] = state
        self.record_event("host_connected" if state["connected"] else "host_unavailable", host=host, **state)
        return {"host": host, **state}

    def discover(self, host, query="", cwd="", limit=20):
        if self.controllers.is_controller_reference(host, query, cwd=cwd or None):
            raise PermissionError("管理会话不能作为工作 agent 登记。")
        result = self.history(host, {"op": "find", "query": query, "cwd": cwd, "limit": limit})
        result["threads"] = [row for row in result.get("threads", [])
                             if not self.controllers.is_controller_reference(host, row.get("id", ""), cwd=row.get("cwd"))]
        return {"host": host, **result}

    def list_directories(self, host, path=""):
        return {"host": host, **self.directories.list(self.host(host), path)}

    @staticmethod
    def _closed_status(row, runtime):
        mode = row.get("mode")
        if mode == "closed":
            return {"connected": False, "status": "closed", "state": "closed", "agent_ready": False,
                    "blocked_reason": None, "closed": True, "close_state": "closed"}
        if mode in ("closing", "close_unknown"):
            return {"connected": bool(runtime.get("connected")), "status": mode, "state": mode,
                    "agent_ready": False, "closed": False, "close_state": "unknown" if mode == "close_unknown" else "closing"}
        return {}

    @staticmethod
    def _require_open(row):
        if row.get("mode") in ("closed", "closing", "close_unknown"):
            raise ValueError("此终端已关闭或关闭结果尚未确认，不能再输入或重新连接。请新建终端；需要原对话时可在新终端运行 codex resume。")

    @staticmethod
    def _generation_key(host, thread_id):
        return "terminal-generation:" + host + ":" + thread_id

    def _require_terminal_generation(self, row, expected_terminal_id=None, origin="ui"):
        # Old clients may address the first generation by the worker card. Once
        # that card has been explicitly replaced, only a generation-aware UI
        # may write/resize/close it. Native plugin actions resolve the current
        # worker under registry_lock; their old request keys stay fenced.
        if expected_terminal_id is not None:
            if (not isinstance(expected_terminal_id, str)
                    or expected_terminal_id != row.get("terminal_id")):
                raise ValueError("终端已切换到新一代；旧画面操作未发送，请重新打开终端。")
        elif origin == "ui" and self.store.setting(self._generation_key(row["host"], row["thread_id"])):
            raise ValueError("此终端已重新打开；请刷新并使用明确的新终端身份，旧界面操作未发送。")

    def is_managed_worker(self, row):
        """Registered open worker identity; cheap metadata, never live probing.

        A disconnected attachment stays in the management list for exact
        reconnect and metadata. History-only rows, closing fences and manager
        identities/workspaces are never promoted to workers by this policy.
        The old stored ``active`` field is deliberately not an authority.
        """
        if (not isinstance(row, dict) or row.get("mode") != "terminal"
                or not isinstance(row.get("terminal_id"), str)
                or not re.fullmatch(r"ceohub-[a-f0-9]{16,48}", row["terminal_id"])
                or not row.get("host") or not row.get("thread_id")):
            return False
        return not self.controllers.is_controller_reference(row["host"], row["thread_id"], cwd=row.get("cwd"))

    def managed_worker_rows(self):
        """Only enrolled worker rows, without broker startup or machine scans."""
        return [row for row in self.store.agents() if self.is_managed_worker(row)]

    def get_agent(self, host, thread_id, plugin=False, sessions=None):
        if self.controllers.is_controller_reference(host, thread_id):
            raise PermissionError("管理会话不是工作 agent；请使用主控专用入口。")
        row = self.store.agent(host, thread_id)
        if not row:
            raise ValueError("未登记的工作终端；请先在 UI 打开或连接。")
        if self.controllers.is_controller_reference(host, thread_id, cwd=row.get("cwd")):
            raise PermissionError("管理工作区不能作为工作 agent 读取。")
        if plugin and not self.is_managed_worker(row):
            raise PermissionError("只有已连接登记且未关闭的共享工作终端可被管理；历史记录和管理会话不是工作终端。")
        safe = {key: value for key, value in row.items() if key not in ("access", "owned")}
        runtime = next((s for s in (self.terminals.sessions() if sessions is None else sessions) if s["id"] == row.get("terminal_id")), {})
        binding = self.store.setting(self._output_key(host, row.get("terminal_id"))) if row.get("terminal_id") else None
        result = {**safe, "active": self.is_managed_worker(row), "management_policy": "connected_workers",
                "pinned": bool(row.get("pinned")), "host_label": self.host(host).get("label", host),
                "agent_id": self.agent_id(host, thread_id), "source": "terminal" if row.get("terminal_id") else "history",
                "output_binding": {"state": "recorded", "session_id": binding["session_id"], "method": binding.get("method"),
                                   "note": "上次关联；读取时重新核验当前进程。"} if binding else {"state": "unbound"},
                "source_thread_id": None if thread_id.startswith("ceohub-") else thread_id,
                "connected": bool(runtime.get("connected")), "status": runtime.get("state", "not_connected"),
                "state": runtime.get("state", "not_connected"), "command": runtime.get("command", ""),
                "agent_ready": bool(runtime.get("agent_ready")), "human_busy": bool(runtime.get("human_busy")), "blocked_reason": runtime.get("blocked_reason"),
                "human_input": runtime.get("human_input"),
                **self._closed_status(row, runtime)}
        generation = self.store.setting(self._generation_key(host, thread_id))
        if generation:
            result["terminal_generation"] = {"terminal_id": row.get("terminal_id"), "requires_expected_terminal_id": True}
            if generation.get("previous_session_id"):
                result["previous_output_binding"] = {"state": "historical", "session_id": generation["previous_session_id"],
                    "terminal_id": generation["previous_terminal_id"], "live_session_verified": False,
                    "note": "上一终端的历史关联；新终端尚未启动或关联该会话。"}
        recovery = self.store.setting(self._recovery_key(host, thread_id))
        if recovery:
            recovery = dict(recovery)
            if recovery.get("code") == "reconnect_unknown":
                proof = self.store.setting("terminal-owner-recovery-proof") or {}
                proof_origin = recovery.get("request_origin") if recovery.get("request_origin") in ("ui", "plugin") else "ui"
                fingerprint = self._reconnect_fingerprint(host, thread_id, row.get("terminal_id"), proof_origin, None)
                keys = [key.removeprefix("agent-reconnect:") for key, value in proof.get("operations", {}).items()
                        if key.startswith("agent-reconnect:") and value == fingerprint]
                if keys:
                    recovery.update(reconcile_available=True,
                                    next_action="reconcile_reconnect" if proof_origin == "ui" else "reconcile_original_worker",
                                    reconcile_request_keys=keys)
                elif (recovery.get("request_origin") == "ui" and isinstance(recovery.get("request_key"), str)
                      and runtime.get("id") == row.get("terminal_id") and runtime.get("host") == host
                      and runtime.get("transport") == "ssh" and runtime.get("target") == self.host(host).get("target")
                      and runtime.get("connected") is False and runtime.get("state") == "exited"
                      and runtime.get("human_busy") is False and type(runtime.get("exit_code")) is int):
                    # A cached candidate only offers the explicit check button.
                    # No PID, SSH, owner lifecycle or operation write happens on
                    # a passive metadata read; the confirmed action does all
                    # authenticated/live checks before changing an unknown.
                    try:
                        self._existing_display_identity(runtime, row.get("terminal_id"))
                        self._request_key(recovery["request_key"])
                    except (ValueError, TypeError):
                        pass
                    else:
                        recovery.update(reconcile_available=True, next_action="reconcile_reconnect",
                                        reconcile_request_keys=[recovery["request_key"]])
                elif (recovery.get("request_origin") in ("ui", "plugin") and isinstance(recovery.get("request_key"), str)
                      and runtime.get("id") == row.get("terminal_id") and runtime.get("host") == host
                      and runtime.get("transport") == "ssh" and runtime.get("target") == self.host(host).get("target")
                      and runtime.get("connected") is True and runtime.get("state") != "exited"
                      and isinstance(runtime.get("display_identity"), dict)):
                    # Offer an explicit read-only check for this exact pending
                    # UI result, never silently turn a live screen into proof.
                    try:
                        self._existing_display_identity(runtime, row.get("terminal_id"))
                        self._request_key(recovery["request_key"])
                        with self.store.lock:
                            operation = self.store.db.execute("select fingerprint,state,result from operations where request_key=?",
                                ("agent-reconnect:" + recovery["request_key"],)).fetchone()
                        previous = json.loads(operation["result"]) if operation and operation["result"] else {}
                        stamp = previous.get("owner_stamp", {})
                        candidate_fingerprint = self._reconnect_fingerprint(host, thread_id, row.get("terminal_id"), recovery["request_origin"], None)
                        if (not operation or operation["fingerprint"] != candidate_fingerprint or operation["state"] != "done"
                                or previous.get("state") != "unknown" or previous.get("recovery") != recovery
                                or type(stamp.get("pid")) is not int or stamp["pid"] <= 0
                                or not isinstance(stamp.get("recovery_id"), str)
                                or not re.fullmatch(r"[a-f0-9]{32}", stamp["recovery_id"])):
                            raise ValueError()
                    except (ValueError, TypeError, AttributeError):
                        pass
                    else:
                        recovery.update(reconcile_available=True,
                                        next_action="reconcile_reconnect" if recovery["request_origin"] == "ui" else "reconcile_original_worker",
                                        reconcile_kind="live_display",
                                        reconcile_request_keys=[recovery["request_key"]])
            result["recovery"] = recovery
        scheduler = getattr(self, "auto_reconnect", None)
        result["auto_reconnect"] = scheduler.view(result) if scheduler else AutoReconnect.disabled_view()
        result["management"] = self._management(result)
        return result

    @staticmethod
    def _management(agent):
        """One cached presentation shared by UI, get/list and scoped tools.

        Ready means input availability, not task completion. This method never
        performs SSH, reads transcripts or guesses which business task is active.
        """
        state, mode = agent.get("state"), agent.get("mode")
        if mode == "closed" or state == "closed":
            status, label, tone, action, attention = "closed", "已结束", "neutral", "none", False
        elif mode in ("closing", "close_unknown"):
            status, label, tone, action, attention = mode, "关闭结果待核对" if mode == "close_unknown" else "正在关闭", "warning", "none", True
        elif not agent.get("terminal_id"):
            status, label, tone, action, attention = "history", "历史记录", "neutral", "view_history", False
        elif agent.get("recovery", {}).get("code") in ("original_session_missing", "original_session_changed", "reconnect_unknown"):
            unknown = agent["recovery"]["code"] == "reconnect_unknown"
            status, label, tone, action, attention = ("recovery_unknown" if unknown else "missing"), ("重连结果待核对" if unknown else "原终端待核对"), "warning", "inspect_connection", True
        elif not agent.get("connected"):
            recovery = agent.get("recovery", {})
            missing = recovery.get("code") in ("original_session_missing", "original_session_changed")
            status, label, tone, action, attention = ("missing" if missing else "disconnected"), ("原终端待核对" if missing else "连接已断开"), "warning", ("inspect_connection" if missing else "reconnect"), True
        elif agent.get("human_busy"):
            activity = agent.get("human_input") or {}
            label = ("输入状态待确认" if activity.get("reason") == "uncertain_input" else "有未提交输入") if activity.get("state") == "draft" else "你正在输入"
            status, tone, action, attention = "human_busy", "neutral", "open_terminal", False
        elif agent.get("blocked_reason"):
            status, label, tone, action, attention = "needs_input", "需人工处理", "warning", "open_terminal", True
        elif state == "shell":
            status, label, tone, action, attention = "shell", "终端就绪 · 未启动 agent", "neutral", "open_terminal", True
        elif agent.get("agent_ready"):
            status, label, tone, action, attention = "ready", "Agent 可交互", "positive", "open_terminal", False
        else:
            status, label, tone, action, attention = "checking", "已连接 · 待核验", "neutral", "open_terminal", False
        return {"status": status, "label": label, "tone": tone, "primary_action": action, "needs_attention": attention}

    @staticmethod
    def _recovery_key(host, thread_id):
        return "agent-recovery:" + hashlib.sha256(json.dumps([host, thread_id]).encode()).hexdigest()

    @staticmethod
    def _reconnect_fingerprint(host, thread_id, terminal_id, origin, controller_id):
        return hashlib.sha256(json.dumps([host, thread_id, terminal_id, origin, controller_id]).encode()).hexdigest()

    def terminal_service_status(self):
        """Read only the bound display owner; never launch or reconnect workers.

        This browser-facing presentation deliberately excludes descriptor paths,
        bearer capabilities and process arguments from the client's diagnostics.
        """
        try:
            value = self.terminals.bound_owner_status()
        except Exception:
            value = {"state": "unknown", "code": "owner_status_unavailable", "running": False, "can_recover": False}
        return self._terminal_service_view(value)

    @staticmethod
    def _terminal_service_view(value):
        if not isinstance(value, dict):
            value = {}
        state = value.get("state") if value.get("state") in ("ready", "offline", "blocked", "unknown") else "unknown"
        running = value.get("running") is True and state == "ready"
        available = value.get("can_recover") is True and state == "offline" and not running
        messages = {
            "ready": "本机工作终端显示服务正常；恢复服务不会自动重连 agent 或发送任务。",
            "offline": "本机工作终端显示服务已离线；可恢复绑定的显示服务，远端工作会话不会被重启。",
            "blocked": "当前不能安全恢复显示服务；请核对原服务身份，不会启动替代 owner。",
            "unknown": "显示服务状态尚不能确认；请先核对原 owner，不会强制启动或重放输入。",
        }
        # Error codes are fixed identifiers, not raw exception messages.
        code = value.get("code", "owner_status_unavailable")
        if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_]{1,80}", code):
            code = "owner_status_unavailable"
        result = {"state": state, "code": code, "running": running, "recovery_available": available,
                  "can_recover": available, "message": messages[state],
                  "next_action": "reconnect_selected_agent" if running else "recover_display_service" if available else "inspect_display_service",
                  "owner_recovered": value.get("owner_recovered") is True, "sessions_restored": False}
        for key in ("pid", "previous_pid"):
            if type(value.get(key)) is int and value[key] > 0:
                result[key] = value[key]
        if value.get("version") == "0.3.0":
            result["version"] = "0.3.0"
        recovery_id = value.get("recovery_id")
        if isinstance(recovery_id, str) and re.fullmatch(r"[a-f0-9]{32}", recovery_id):
            result["recovery_id"] = recovery_id
        previous_recovery_id = value.get("previous_recovery_id")
        if value.get("owner_recovered") is True and isinstance(previous_recovery_id, str) and re.fullmatch(r"[a-f0-9]{32}", previous_recovery_id):
            result["previous_recovery_id"] = previous_recovery_id
        result["started_empty"] = value.get("started_empty") is True
        return result

    def recover_terminal_service(self, confirm=False, expected_owner=None):
        """Explicit UI action: recover only the dead local display owner.

        Preserve legacy unknown-operation rows. A proven new owner can later
        reconcile only completed unknown reconnects stamped with the exact old
        owner's generation, after explicit per-agent confirmation. Unstamped
        legacy results remain unknown even when the new registry is empty.
        """
        if confirm is not True:
            raise ValueError("请明确确认仅恢复本机终端显示服务；此操作不恢复 agent 或重发任务。")
        with self.registry_lock:
            with self.store.lock:
                rows = self.store.db.execute("select request_key,fingerprint,result from operations where state='done' and request_key like 'agent-reconnect:%'").fetchall()
            unknowns = {}
            for row in rows:
                try:
                    previous = json.loads(row["result"])
                    if previous.get("state") == "unknown" and previous.get("recovery", {}).get("code") == "reconnect_unknown":
                        unknowns[row["request_key"]] = {"fingerprint": row["fingerprint"], "owner_stamp": previous.get("owner_stamp"),
                                                        "recovery": previous.get("recovery")}
                except (ValueError, TypeError, AttributeError):
                    continue
            try:
                value = self.terminals.recover_bound_owner(confirm=True, expected_owner=expected_owner)
            except Exception:
                value = {"state": "unknown", "code": "owner_recovery_unconfirmed", "running": False, "can_recover": False}
            result = self._terminal_service_view(value)
            candidates = {}
            old_stamp = {"pid": result.get("previous_pid"), "recovery_id": result.get("previous_recovery_id")}
            if result.get("previous_recovery_id"):
                # A legacy PID is not a generation proof. Only the current
                # agent's recorded attempt (or its single unambiguous unknown)
                # can be reconciled; never unlock an arbitrary historical key.
                for agent in self.store.agents():
                    recovery = self.store.setting(self._recovery_key(agent["host"], agent["thread_id"])) or {}
                    if recovery.get("code") != "reconnect_unknown":
                        continue
                    origin = recovery.get("request_origin") if recovery.get("request_origin") in ("ui", "plugin") else "ui"
                    fingerprint = self._reconnect_fingerprint(agent["host"], agent["thread_id"], agent.get("terminal_id"), origin, None)
                    matches = [key for key, item in unknowns.items() if item["fingerprint"] == fingerprint]
                    selected = "agent-reconnect:" + recovery["request_key"] if isinstance(recovery.get("request_key"), str) else matches[0] if len(matches) == 1 else None
                    if (selected in matches and unknowns[selected]["owner_stamp"] == old_stamp
                            and (origin != "plugin" or unknowns[selected]["recovery"] == recovery)):
                        candidates[selected] = fingerprint
            if result["running"] and result["owner_recovered"] and result["started_empty"] and result.get("previous_pid") and result.get("pid") and result.get("recovery_id") and result["previous_pid"] != result["pid"]:
                self.store.setting("terminal-owner-recovery-proof", {"pid": result["pid"], "previous_pid": result["previous_pid"],
                                   "previous_recovery_id": result.get("previous_recovery_id"),
                                   "recovery_id": result["recovery_id"], "operations": candidates})
            return result

    def reconcile_reconnect(self, host, thread_id, request_key, confirm=False):
        """Recheck a legacy unknown result without attaching or sending input.

        An empty registry alone is never proof. Either a confirmed dead-owner
        recovery receipt proves absence, or the authenticated original display
        record proves that an attachment may have run but has now ended. The
        latter additionally requires the original immutable remote identity.
        Started operations and different targets stay fenced in both cases.
        """
        if confirm is not True:
            raise ValueError("请明确确认核对原重连结果；此操作不会附着终端或发送任务。")
        self._request_key(request_key)
        if not request_key:
            raise ValueError("请提供原重连的稳定 request_key，不要创建新键。")
        with self.registry_lock:
            agent = self.get_agent(host, thread_id)
            self._require_open(agent)
            operation = "agent-reconnect:" + request_key
            fingerprint = self._reconnect_fingerprint(host, thread_id, agent.get("terminal_id"), "ui", None)
            with self.store.lock:
                row = self.store.db.execute("select * from operations where request_key=?", (operation,)).fetchone()
            if not row or row["fingerprint"] != fingerprint:
                raise ValueError("原重连请求与该工作终端不匹配；不能使用其他会话或主控的请求键。")
            if row["state"] != "done":
                raise ValueError("原重连可能仍在执行；不能核销或重放。")
            previous = json.loads(row["result"])
            if (previous.get("state") == "done" and previous.get("recovery", {}).get("reconciled") is True
                    and previous["recovery"].get("reconciliation_kind") == "live_display"
                    and previous["recovery"].get("request_key") == request_key
                    and previous["recovery"].get("request_origin") == "ui"
                    and self.store.setting(self._recovery_key(host, thread_id)) == previous["recovery"]):
                result = agent | {"operation": "reconcile_reconnect", "operation_id": request_key,
                                  "operation_state": "done", "recovery": previous["recovery"]}
                result["management"] = self._management(result)
                return result
            if (previous.get("state") == "blocked" and previous.get("recovery", {}).get("code") == "local_display_unavailable"
                    and previous["recovery"].get("reconciled") is True):
                result = agent | {"operation": "reconcile_reconnect", "operation_id": request_key,
                                  "operation_state": "blocked", "recovery": previous["recovery"]}
                result["management"] = self._management(result)
                return result
            proof = self.store.setting("terminal-owner-recovery-proof") or {}
            owner = self.terminal_service_status()
            if previous.get("state") != "unknown" or previous.get("recovery", {}).get("code") != "reconnect_unknown":
                raise ValueError("原重连不是可核对的未知结果；不解锁或重放。")
            if agent.get("connected"):
                return self._reconcile_live_display(agent, request_key, operation, fingerprint, row, previous)
            if (proof.get("operations", {}).get(operation) != fingerprint
                    or not owner["running"] or owner.get("pid") != proof.get("pid")
                    or not proof.get("recovery_id") or owner.get("recovery_id") != proof.get("recovery_id")):
                return self._reconcile_ended_display(agent, request_key, operation, fingerprint, row, previous)
            # Unlike sessions(), an unavailable broker must not masquerade as an
            # authoritative empty registry. Verify the exact owner again after.
            try:
                current = self.terminals.request("/sessions", timeout=3)
            except Exception:
                raise ValueError("显示服务登记状态不能确认；保留原未知结果，不重复附着。") from None
            sessions = current.get("sessions") if isinstance(current, dict) else None
            if not isinstance(sessions, list) or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in sessions):
                raise ValueError("显示服务登记状态不能确认；保留原未知结果。")
            if any(item["id"] == agent.get("terminal_id") for item in sessions):
                raise ValueError("原显示附着已存在；请核对该终端，不重复附着。")
            after = self.terminal_service_status()
            if not after["running"] or after.get("pid") != proof.get("pid") or after.get("recovery_id") != proof.get("recovery_id"):
                raise ValueError("核对期间显示服务身份已改变；不解锁原请求。")
            recovery = {"code": "local_display_unavailable", "message": "已确认旧显示 owner 已结束，原附着在恢复后的 owner 中不存在；可使用原请求键重试，仅接回原终端。",
                        "next_action": "reconnect", "retryable": True, "reconciled": True,
                        "request_key": request_key, "request_origin": "ui"}
            with self.store.lock, self.store.db:
                changed = self.store.db.execute("update operations set result=? where request_key=? and fingerprint=? and state='done' and result=?",
                    (json.dumps({"state": "blocked", "recovery": recovery}, ensure_ascii=False), operation, fingerprint, row["result"])).rowcount
            if changed != 1:
                raise ValueError("原重连记录已改变；请重新核对，不重放。")
            self.store.setting(self._recovery_key(host, thread_id), recovery)
            result = agent | {"operation": "reconcile_reconnect", "operation_id": request_key, "operation_state": "blocked", "recovery": recovery}
            result["management"] = self._management(result)
            return result

    @staticmethod
    def _valid_existing_display_identity(identity, terminal_id):
        return (isinstance(identity, dict)
                and set(identity) == {"name", "session", "created", "pane", "pane_pid", "pane_start_ticks"}
                and identity["name"] == terminal_id
                and isinstance(identity["session"], str) and re.fullmatch(r"\$[0-9]+", identity["session"]) is not None
                and isinstance(identity["pane"], str) and re.fullmatch(r"%[0-9]+", identity["pane"]) is not None
                and all(type(identity[key]) is int and identity[key] > 0 for key in ("created", "pane_pid", "pane_start_ticks")))

    @staticmethod
    def _existing_display_identity(record, terminal_id):
        """Strictly decode only our canonical existing-attachment marker."""
        marker = record.get("cwd")
        prefix = "/.__pronhub_existing__/" + terminal_id + "/"
        if not isinstance(marker, str) or len(marker) > 4096 or not marker.startswith(prefix):
            raise ValueError("原显示缺少既存终端身份凭据；保留未知结果。")
        encoded = marker[len(prefix):]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,2048}", encoded):
            raise ValueError("原显示身份凭据无效；保留未知结果。")
        try:
            identity = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            canonical = base64.urlsafe_b64encode(json.dumps(identity, sort_keys=True).encode()).decode().rstrip("=")
            if canonical != encoded or not Hub._valid_existing_display_identity(identity, terminal_id):
                raise ValueError()
            return identity
        except (ValueError, TypeError, UnicodeError, KeyError):
            raise ValueError("原显示身份凭据无效；保留未知结果。") from None

    def _reconcile_live_display(self, agent, request_key, operation, fingerprint, row, previous, origin="ui"):
        """Resolve an uncertain attachment by proving its existing live display.

        This only records the outcome of the original UI/legacy plugin request. It never
        enables a retry, attaches a display, or writes terminal input.
        """
        host, thread_id, terminal_id = agent["host"], agent["thread_id"], agent.get("terminal_id")
        config = dict(self.host(host))
        recovery_key = self._recovery_key(host, thread_id)
        with self.store.lock:
            enrolled = self.store.agent(host, thread_id)
            saved = self.store.db.execute("select value from settings where key=?", (recovery_key,)).fetchone()
        selected = json.loads(saved["value"]) if saved else None
        stamp = previous.get("owner_stamp")
        if (origin not in ("ui", "plugin") or not enrolled or enrolled.get("mode") != "terminal"
                or not isinstance(terminal_id, str) or not re.fullmatch(r"ceohub-[a-f0-9]{16,48}", terminal_id)
                or enrolled.get("terminal_id") != terminal_id
                or config.get("transport") != "ssh" or not isinstance(selected, dict)
                or selected != previous.get("recovery") or selected.get("code") != "reconnect_unknown"
                or selected.get("request_key") != request_key or selected.get("request_origin") != origin
                or not isinstance(stamp, dict) or set(stamp) != {"pid", "recovery_id"}
                or type(stamp.get("pid")) is not int or stamp["pid"] <= 0
                or not isinstance(stamp.get("recovery_id"), str) or not re.fullmatch(r"[a-f0-9]{32}", stamp["recovery_id"])):
            raise ValueError("只能核对当前原请求及其原显示服务身份；保留未知结果。")

        def snapshot():
            try:
                value = self.terminals.live_display_snapshot(terminal_id)
            except Exception:
                raise ValueError("原显示连接的认证证据尚不能确认；保留未知结果。") from None
            record = value.get("session") if isinstance(value, dict) else None
            owner = value.get("owner") if isinstance(value, dict) else None
            process = value.get("display_identity") if isinstance(value, dict) else None
            if (not isinstance(record, dict) or not isinstance(owner, dict) or not isinstance(process, dict)
                    or type(owner.get("pid")) is not int or not 0 < owner["pid"] <= 2147483647
                    or owner.get("version") != "0.3.0"
                    or {key: owner.get(key) for key in ("pid", "recovery_id")} != stamp
                    or any(not isinstance(value.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", value[key])
                           for key in ("reference_sha256", "descriptor_sha256"))
                    or any(not isinstance(value.get(key), (list, tuple)) or len(value[key]) != 5
                           or any(type(part) is not int for part in value[key]) for key in ("reference_identity", "descriptor_identity"))
                    or value.get("owner_pid_state") != "alive" or value.get("display_pid_state") != "alive"
                    or set(process) != {"birth", "executable"} or not isinstance(process.get("birth"), str)
                    or not re.fullmatch(r"(?:win-filetime:[0-9]{1,24}|linux-start:[a-f0-9-]{36}:[0-9]{1,24})", process["birth"])
                    or not isinstance(process.get("executable"), str) or not Path(process["executable"]).is_absolute()
                    or Path(process["executable"]).name.lower() not in ("ssh", "ssh.exe")
                    or record.get("id") != terminal_id or record.get("host") != host or record.get("transport") != "ssh"
                    or record.get("target") != config.get("target") or record.get("connected") is not True
                    or record.get("display_identity") != process
                    or type(record.get("pid")) is not int or not 0 < record["pid"] <= 2147483647):
                raise ValueError("原显示、SSH 进程或服务身份不一致；保留未知结果。")
            self._existing_display_identity(record, terminal_id)
            return value

        before = snapshot()
        identity = self._existing_display_identity(before["session"], terminal_id)
        for _ in range(2):
            try:
                remote = self._probe_remote_terminal(host, terminal_id)
            except Exception:
                raise ValueError("远端原 tmux/pane 核验暂不可用；保留未知结果。") from None
            if (not isinstance(remote, dict) or remote.get("ok") is not True
                    or remote.get("exists") is not True or remote.get("identity") != identity):
                raise ValueError("远端原 tmux/pane 身份不能确认一致；保留未知结果。")
        after = snapshot()
        if before != after or dict(self.host(host)) != config:
            raise ValueError("核对期间 owner、SSH 显示或机器配置已变化；保留未知结果。")
        recovery = {"code": "reconnected", "next_action": "open_terminal", "retryable": False,
                    "reconciled": True, "reconciliation_kind": "live_display", "request_key": request_key, "request_origin": origin,
                    "message": "已核验原重连的现有显示连接和同一远端 tmux/pane；仅记录核验结果，未重新附着或发送输入。"}
        if origin == "plugin":
            recovery.update(maintenance_actor="ui", original_origin="plugin")
        audit_key = "agent-reconnect-reconciliation:" + hashlib.sha256(operation.encode()).hexdigest()
        with self.store.lock, self.store.db:
            if self.store.agent(host, thread_id) != enrolled:
                raise ValueError("原工作终端登记在核对期间已变化；保留未知结果。")
            changed = self.store.db.execute("update operations set result=? where request_key=? and fingerprint=? and state='done' and result=?",
                (json.dumps({"state": "done", "recovery": recovery, "owner_stamp": stamp}, ensure_ascii=False),
                 operation, fingerprint, row["result"])).rowcount
            selected_changed = self.store.db.execute("update settings set value=? where key=? and value=?",
                (json.dumps(recovery), recovery_key, saved["value"])).rowcount
            if changed != 1 or selected_changed != 1:
                raise ValueError("原请求或当前核对记录已变化；保留未知结果，不重放。")
            old_audit = self.store.db.execute("select value from settings where key=?", (audit_key,)).fetchone()
            audits = json.loads(old_audit["value"]) if old_audit else []
            if not isinstance(audits, list):
                raise ValueError("原核对审计不能确认；保留未知结果。")
            audits.append({"kind": "live_display", "at": time.time(), "request_key": request_key,
                           "maintenance_actor": "ui", "original_origin": origin,
                           "fingerprint": fingerprint, "previous_result": previous, "owner_stamp": stamp,
                           "display_pid": before["session"]["pid"], "display_identity": before["display_identity"],
                           "descriptor_sha256": before["descriptor_sha256"], "remote_identity": identity})
            self.store.db.execute("insert or replace into settings values(?,?)", (audit_key, json.dumps(audits)))
        result = self.get_agent(host, thread_id) | {"operation": "reconcile_reconnect" if origin == "ui" else "reconcile_original_worker", "operation_id": request_key,
                                                 "operation_state": "done", "recovery": recovery}
        result["management"] = self._management(result)
        return result

    def _reconcile_ended_display(self, agent, request_key, operation, fingerprint, row, previous):
        """Explicit UI reconciliation, not a claim that the old request never ran.

        Keep the old unknown result in a durable private audit, then permit only
        a retry of that same request key. Neither this check nor the audit sends
        input, creates a shell, or replays a business task.
        """
        host, thread_id, terminal_id = agent["host"], agent["thread_id"], agent.get("terminal_id")
        recovery_key = self._recovery_key(host, thread_id)
        with self.store.lock:
            saved = self.store.db.execute("select value from settings where key=?", (recovery_key,)).fetchone()
        selected = json.loads(saved["value"]) if saved else None
        if (not isinstance(selected, dict) or selected.get("code") != "reconnect_unknown"
                or selected.get("request_key") != request_key or selected.get("request_origin") != "ui"
                or previous.get("recovery", {}).get("request_key") != request_key
                or previous.get("recovery", {}).get("request_origin") != "ui"
                or self.host(host)["transport"] != "ssh"):
            raise ValueError("仅能核对该终端当前的原 UI 请求；不解锁其他请求。")
        try:
            before = self.terminals.ended_display_snapshot(terminal_id)
        except Exception:
            raise ValueError("原显示结束证据尚不能确认；保留未知结果，不重复附着。") from None
        record = before.get("session") if isinstance(before, dict) else None
        owner = before.get("owner") if isinstance(before, dict) else None
        if (not isinstance(record, dict) or not isinstance(owner, dict)
                or type(owner.get("pid")) is not int or owner["pid"] <= 0 or owner.get("version") != "0.3.0"
                or any(not isinstance(before.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", before[key]) for key in ("reference_sha256", "descriptor_sha256"))
                or any(not isinstance(before.get(key), (list, tuple)) or len(before[key]) != 5
                       or any(type(item) is not int for item in before[key]) for key in ("reference_identity", "descriptor_identity"))
                or before.get("owner_pid_state") != "alive"
                or record.get("id") != terminal_id or record.get("host") != host or record.get("transport") != "ssh"
                or record.get("target") != self.host(host).get("target")
                or record.get("connected") is not False or record.get("state") != "exited"
                or record.get("human_busy") is not False
                or type(record.get("pid")) is not int or record["pid"] <= 0
                or type(record.get("exit_code")) is not int or not 0 <= record["exit_code"] <= 4294967295
                or before.get("display_pid_state") != "dead"):
            raise ValueError("原 SSH 显示尚未明确结束，或输入/身份不能确认；保留未知结果。")
        stamp = previous.get("owner_stamp")
        if stamp is not None and stamp != {"pid": owner["pid"], "recovery_id": owner.get("recovery_id")}:
            raise ValueError("原请求的显示服务身份已变化；保留未知结果。")
        identity = self._existing_display_identity(record, terminal_id)
        remote = self._probe_remote_terminal(host, terminal_id)
        if remote.get("ok") is not True or remote.get("exists") is not True or remote.get("identity") != identity:
            raise ValueError("远端原 tmux/pane 身份尚不能确认一致；保留未知结果，不新建终端。")
        try:
            after = self.terminals.ended_display_snapshot(terminal_id)
        except Exception:
            raise ValueError("复核期间显示服务状态不能确认；保留未知结果。") from None
        if before != after:
            raise ValueError("复核期间 owner、原显示或进程状态已变化；不解锁原请求。")
        recovery = {"code": "local_display_unavailable",
                    "message": "原重连可能已执行，但已核验该 SSH 显示现在结束、远端原 tmux/pane 未变；可用原请求键接回显示，不重启模型或重发任务。",
                    "next_action": "reconnect", "retryable": True, "reconciled": True,
                    "reconciliation_kind": "ended_display", "request_key": request_key, "request_origin": "ui"}
        audit_key = "agent-reconnect-reconciliation:" + hashlib.sha256(operation.encode()).hexdigest()
        with self.store.lock, self.store.db:
            changed = self.store.db.execute("update operations set result=? where request_key=? and fingerprint=? and state='done' and result=?",
                (json.dumps({"state": "blocked", "recovery": recovery, "reconciliation_identity": identity}, ensure_ascii=False), operation, fingerprint, row["result"])).rowcount
            selected_changed = self.store.db.execute("update settings set value=? where key=? and value=?",
                (json.dumps(recovery), recovery_key, saved["value"])).rowcount
            if changed != 1 or selected_changed != 1:
                raise ValueError("原重连或当前请求记录已改变；不解锁或重放。")
            old_audit = self.store.db.execute("select value from settings where key=?", (audit_key,)).fetchone()
            audits = json.loads(old_audit["value"]) if old_audit else []
            if not isinstance(audits, list):
                raise ValueError("原核对审计不能确认；保留未知结果。")
            audits.append({"kind": "ended_display", "at": time.time(), "request_key": request_key,
                           "fingerprint": fingerprint, "previous_result": previous,
                           "owner_pid": owner["pid"], "display_pid": record["pid"],
                           "descriptor_sha256": before["descriptor_sha256"],
                           "remote_identity": identity,
                           "remote_identity_sha256": hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()})
            self.store.db.execute("insert or replace into settings values(?,?)", (audit_key, json.dumps(audits)))
        result = agent | {"operation": "reconcile_reconnect", "operation_id": request_key,
                          "operation_state": "blocked", "recovery": recovery}
        result["management"] = self._management(result)
        return result

    @staticmethod
    def _valid_original_worker_binding(binding, terminal_id):
        """Validate a recorded original Linux worker, never guess by cwd/date."""
        if (not isinstance(binding, dict) or binding.get("state") != "bound"
                or binding.get("binding_evidence") != "held_rollout" or binding.get("live_session_verified") is not True
                or binding.get("historical", False) is not False or binding.get("terminal_id") != terminal_id
                or binding.get("source") != "cli" or not isinstance(binding.get("session_id"), str)
                or not re.fullmatch(r"[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}", binding["session_id"])):
            return False
        identity = binding.get("process_identity")
        expected = {"platform", "terminal_id", "pane_id", "pane_pid", "pane_start_ticks", "pid", "start_ticks", "pgid", "tty_nr", "tty", "uid"}
        if (not isinstance(identity, dict) or set(identity) != expected or identity.get("platform") != "linux"
                or identity.get("terminal_id") != terminal_id
                or not isinstance(identity.get("pane_id"), str) or not re.fullmatch(r"%[0-9]+", identity["pane_id"])
                or not isinstance(identity.get("tty"), str) or not re.fullmatch(r"/dev/(?:pts/[0-9]+|tty[A-Za-z0-9]*)", identity["tty"])
                or any(type(identity.get(key)) is not int or identity[key] <= 0 for key in ("pane_pid", "pane_start_ticks", "pid", "start_ticks", "pgid", "tty_nr"))
                or type(identity.get("uid")) is not int or identity["uid"] < 0):
            return False
        rollout = binding.get("rollout_identity")
        if (not isinstance(rollout, dict) or set(rollout) != {"device", "inode"}
                or type(rollout["device"]) is not int or rollout["device"] < 0
                or type(rollout["inode"]) is not int or rollout["inode"] <= 0):
            return False
        for key in ("codex_home", "rollout_path", "cwd", "process_cwd"):
            value = binding.get(key)
            if (not isinstance(value, str) or not value.startswith("/") or len(value) > 4096
                    or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)):
                return False
        return True

    @staticmethod
    def _matches_original_worker_binding(value, binding, terminal_id):
        if not isinstance(value, dict) or not Hub._valid_original_worker_binding(
                value | {"terminal_id": terminal_id, "live_session_verified": True}, terminal_id):
            return False
        if value.get("terminal_id", terminal_id) != terminal_id or value.get("live_session_verified", True) is not True:
            return False
        keys = ("session_id", "process_identity", "rollout_identity", "rollout_path", "codex_home", "cwd", "process_cwd", "source")
        return all(value.get(key) == binding.get(key) for key in keys)

    def reconcile_original_worker(self, host, thread_id, request_key, confirm=False):
        """Human UI maintenance for an exact legacy plugin reconnect result.

        Frozen live-display evidence can finish that original request. A proven
        replacement of its dead owner can instead permit a same-key retry while
        the authenticated new registry is empty. Both preserve plugin origin.
        Match the current native worker and exact Session to the complete
        binding saved at maintenance, then pin the CURRENT tmux identity for
        same-key reattachment. A legacy binding was not frozen when the old
        request started; this check alone cannot establish its provenance at
        that earlier time or invent the old tmux object's creation identity.
        No input, attachment or model startup occurs here.
        """
        if confirm is not True:
            raise ValueError("请明确确认核对原工作进程；此操作不附着终端或发送输入。")
        self._request_key(request_key)
        if not request_key:
            raise ValueError("请提供当前原重连请求键；不能新建或替换请求键。")
        with self.registry_lock:
            agent = self.get_agent(host, thread_id)
            self._require_open(agent)
            terminal_id = agent.get("terminal_id")
            config = dict(self.host(host))
            if (agent.get("mode") != "terminal" or not isinstance(terminal_id, str)
                    or not re.fullmatch(r"ceohub-[a-f0-9]{16,48}", terminal_id) or config.get("transport") != "ssh"):
                raise ValueError("仅能核对已登记且未关闭的原 SSH 工作终端。")
            operation = "agent-reconnect:" + request_key
            # The UI is an explicit maintenance actor, NOT the original plugin
            # or a controller. Only a legacy controller=None fingerprint fits.
            fingerprint = self._reconnect_fingerprint(host, thread_id, terminal_id, "plugin", None)
            recovery_key = self._recovery_key(host, thread_id)
            binding_key = self._output_key(host, terminal_id)
            with self.store.lock:
                row = self.store.db.execute("select * from operations where request_key=?", (operation,)).fetchone()
                selected_row = self.store.db.execute("select value from settings where key=?", (recovery_key,)).fetchone()
                binding_row = self.store.db.execute("select value from settings where key=?", (binding_key,)).fetchone()
            if not row or row["fingerprint"] != fingerprint or row["state"] != "done":
                raise ValueError("原请求不是该终端已结束的 legacy plugin 请求；不能核对其他主控或进行中请求。")
            previous = json.loads(row["result"])
            selected = json.loads(selected_row["value"]) if selected_row else None
            if (not isinstance(selected, dict) or selected.get("request_key") != request_key
                    or selected.get("request_origin") != "plugin"
                    or previous.get("recovery", {}).get("request_key") != request_key
                    or previous.get("recovery", {}).get("request_origin") != "plugin"):
                raise ValueError("只能核对当前精确的原 plugin 请求；来源和请求键不会被改写。")
            if (previous.get("state") == "done" and selected == previous.get("recovery")
                    and selected.get("reconciled") is True and selected.get("reconciliation_kind") == "live_display"
                    and selected.get("maintenance_actor") == "ui" and selected.get("original_origin") == "plugin"):
                result = agent | {"operation": "reconcile_original_worker", "operation_id": request_key,
                                  "operation_state": "done", "recovery": selected}
                result["management"] = self._management(result)
                return result
            if (previous.get("state") == "blocked" and selected == previous.get("recovery")
                    and selected.get("reconciliation_kind") == "owner_recovery" and selected.get("reconciled") is True
                    and selected.get("maintenance_actor") == "ui" and selected.get("original_origin") == "plugin"):
                result = agent | {"operation": "reconcile_original_worker", "operation_id": request_key,
                                  "operation_state": "blocked", "recovery": selected}
                result["management"] = self._management(result)
                return result
            if (previous.get("state") == "blocked" and selected == previous.get("recovery")
                    and selected.get("reconciliation_kind") == "worker_continuity" and selected.get("reconciled") is True
                    and selected.get("maintenance_actor") == "ui" and selected.get("original_origin") == "plugin"
                    and self._valid_existing_display_identity(previous.get("reconciliation_identity"), terminal_id)
                    and self._valid_original_worker_binding(previous.get("reconciliation_worker_binding"), terminal_id)):
                result = agent | {"operation": "reconcile_original_worker", "operation_id": request_key,
                                  "operation_state": "blocked", "recovery": selected}
                result["management"] = self._management(result)
                return result
            if (previous.get("state") != "unknown" or previous.get("recovery", {}).get("code") != "reconnect_unknown"
                    or selected.get("code") != "reconnect_unknown"):
                raise ValueError("原请求不是待核对的未知结果；不解锁或重放。")
            if agent.get("connected"):
                return self._reconcile_live_display(agent, request_key, operation, fingerprint, row, previous, origin="plugin")
            proof = self.store.setting("terminal-owner-recovery-proof") or {}
            if proof.get("operations", {}).get(operation) == fingerprint:
                return self._reconcile_recovered_plugin_display(agent, request_key, operation, fingerprint, row, previous, selected_row, proof)
            binding = json.loads(binding_row["value"]) if binding_row else None
            if not self._valid_original_worker_binding(binding, terminal_id):
                raise ValueError("缺少原工作进程的完整已核验会话绑定；保留未知结果，不猜测其他会话。")

            def ended_snapshot():
                try:
                    value = self.terminals.ended_display_snapshot(terminal_id)
                except Exception:
                    raise ValueError("原显示结束证据尚不能确认；保留未知结果。") from None
                record = value.get("session") if isinstance(value, dict) else None
                owner = value.get("owner") if isinstance(value, dict) else None
                if (not isinstance(record, dict) or not isinstance(owner, dict)
                        or type(owner.get("pid")) is not int or owner["pid"] <= 0 or owner.get("version") != "0.3.0"
                        or any(not isinstance(value.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", value[key]) for key in ("reference_sha256", "descriptor_sha256"))
                        or any(not isinstance(value.get(key), (list, tuple)) or len(value[key]) != 5
                               or any(type(item) is not int for item in value[key]) for key in ("reference_identity", "descriptor_identity"))
                        or value.get("owner_pid_state") != "alive" or value.get("display_pid_state") != "dead"
                        or record.get("id") != terminal_id or record.get("host") != host or record.get("transport") != "ssh"
                        or record.get("target") != config.get("target") or record.get("connected") is not False
                        or record.get("state") != "exited" or record.get("human_busy") is not False
                        or type(record.get("pid")) is not int or record["pid"] <= 0
                        or type(record.get("exit_code")) is not int or not 0 <= record["exit_code"] <= 4294967295):
                    raise ValueError("原显示尚未明确退出，或服务/输入身份无法核验；保留未知结果。")
                return value

            def fresh_binding():
                try:
                    value = self.history(host, {"op": "bind_output", "terminal_id": terminal_id})
                except Exception:
                    raise ValueError("原工作进程的只读会话核验暂不可用；保留未知结果。") from None
                if not self._matches_original_worker_binding(value, binding, terminal_id):
                    raise ValueError("当前前台进程或真实会话与原完整绑定不一致；不附着替代工作进程。")
                return value

            before = ended_snapshot()
            stamp = previous.get("owner_stamp")
            if stamp is not None and stamp != {"pid": before["owner"]["pid"], "recovery_id": before["owner"].get("recovery_id")}:
                raise ValueError("原请求所属的显示 owner 已变化；保留未知结果。")
            fresh_binding()
            remote = self._probe_remote_terminal(host, terminal_id)
            identity = remote.get("identity") if isinstance(remote, dict) else None
            process = binding["process_identity"]
            if (not isinstance(remote, dict) or remote.get("ok") is not True or remote.get("exists") is not True
                    or not self._valid_existing_display_identity(identity, terminal_id)
                    or identity["pane"] != process["pane_id"] or identity["pane_pid"] != process["pane_pid"]
                    or identity["pane_start_ticks"] != process["pane_start_ticks"]):
                raise ValueError("当前 tmux/pane 不能锚定到原工作进程；保留未知结果。")
            fresh_binding()
            after = ended_snapshot()
            if before != after or dict(self.host(host)) != config:
                raise ValueError("核对期间 owner、显示行或机器配置已变化；不解锁原请求。")
            recovery = {"code": "local_display_unavailable", "next_action": "reconnect", "retryable": True,
                        "reconciled": True, "reconciliation_kind": "worker_continuity",
                        "binding_basis": "saved_binding_at_maintenance",
                        "maintenance_actor": "ui", "original_origin": "plugin",
                        "request_key": request_key, "request_origin": "plugin",
                        "message": "原重连可能已执行，但显示已结束；当前 native 工作进程及精确会话与已保存完整绑定一致，已锚定当前 tmux 身份。该绑定并非原请求时冻结的证据；仅允许原请求键接回显示，不启动模型或重发任务。"}
            audit_key = "agent-reconnect-reconciliation:" + hashlib.sha256(operation.encode()).hexdigest()
            with self.store.lock, self.store.db:
                unchanged_binding = self.store.db.execute("select value from settings where key=?", (binding_key,)).fetchone()
                if not unchanged_binding or unchanged_binding["value"] != binding_row["value"]:
                    raise ValueError("原会话绑定在核对期间已变化；不解锁原请求。")
                changed = self.store.db.execute("update operations set result=? where request_key=? and fingerprint=? and state='done' and result=? "
                    "and exists(select 1 from settings where key=? and value=?) "
                    "and exists(select 1 from agents where host=? and thread_id=? and terminal_id=? and mode='terminal')",
                    (json.dumps({"state": "blocked", "recovery": recovery, "reconciliation_identity": identity,
                                 "reconciliation_worker_binding": binding}, ensure_ascii=False),
                     operation, fingerprint, row["result"], binding_key, binding_row["value"], host, thread_id, terminal_id)).rowcount
                selected_changed = self.store.db.execute("update settings set value=? where key=? and value=?",
                    (json.dumps(recovery), recovery_key, selected_row["value"])).rowcount
                if changed != 1 or selected_changed != 1:
                    raise ValueError("原请求或当前核对记录已变化；不解锁或重放。")
                old_audit = self.store.db.execute("select value from settings where key=?", (audit_key,)).fetchone()
                audits = json.loads(old_audit["value"]) if old_audit else []
                if not isinstance(audits, list):
                    raise ValueError("原核对审计不能确认；保留未知结果。")
                audits.append({"kind": "worker_continuity", "at": time.time(), "request_key": request_key,
                               "binding_basis": "saved_binding_at_maintenance", "original_request_binding_frozen": False,
                               "maintenance_actor": "ui", "original_origin": "plugin", "fingerprint": fingerprint,
                               "previous_result": previous, "original_binding": binding, "anchored_current_identity": identity,
                               "owner_pid": before["owner"]["pid"], "display_pid": before["session"]["pid"],
                               "descriptor_sha256": before["descriptor_sha256"]})
                self.store.db.execute("insert or replace into settings values(?,?)", (audit_key, json.dumps(audits)))
            result = agent | {"operation": "reconcile_original_worker", "operation_id": request_key,
                              "operation_state": "blocked", "recovery": recovery}
            result["management"] = self._management(result)
            return result

    def _reconcile_recovered_plugin_display(self, agent, request_key, operation, fingerprint, row, previous, selected_row, proof):
        """Confirm absence only under a proven replacement of the old owner.

        Preserve the original legacy plugin request and allow its same-key
        existing-session retry. Empty registries alone never supply this proof.
        """
        host, thread_id, terminal_id = agent["host"], agent["thread_id"], agent["terminal_id"]
        recovery_key = self._recovery_key(host, thread_id)
        enrolled = self.store.agent(host, thread_id)
        selected = json.loads(selected_row["value"]) if selected_row else None
        stamp = previous.get("owner_stamp")
        expected_old = {"pid": proof.get("previous_pid"), "recovery_id": proof.get("previous_recovery_id")}
        if (selected != previous.get("recovery") or not enrolled or enrolled.get("mode") != "terminal"
                or enrolled.get("terminal_id") != terminal_id or not isinstance(stamp, dict) or stamp != expected_old
                or set(stamp) != {"pid", "recovery_id"} or type(stamp.get("pid")) is not int
                or type(proof.get("pid")) is not int or type(proof.get("previous_pid")) is not int
                or proof["pid"] <= 0 or proof["previous_pid"] <= 0 or proof["pid"] == proof["previous_pid"]
                or not isinstance(proof.get("recovery_id"), str) or not re.fullmatch(r"[a-f0-9]{32}", proof["recovery_id"])
                or not isinstance(proof.get("previous_recovery_id"), str)
                or not re.fullmatch(r"[a-f0-9]{32}", proof["previous_recovery_id"])
                or proof.get("operations", {}).get(operation) != fingerprint):
            raise ValueError("原请求与已恢复 owner 的精确凭据不一致；保留未知结果。")

        def owner_matches():
            owner = self.terminal_service_status()
            return (owner.get("running") is True and owner.get("pid") == proof["pid"]
                    and owner.get("recovery_id") == proof["recovery_id"])

        if not owner_matches():
            raise ValueError("恢复后的显示服务身份尚不能确认；保留未知结果。")
        try:
            registry = self.terminals.request("/sessions", timeout=3)
        except Exception:
            raise ValueError("恢复后的显示登记暂不能确认；保留未知结果。") from None
        if not isinstance(registry, dict) or registry.get("sessions") != [] or not owner_matches():
            raise ValueError("恢复后的 owner 未确认空登记或身份已变化；保留未知结果。")
        recovery = {"code": "local_display_unavailable", "next_action": "reconnect", "retryable": True,
                    "reconciled": True, "reconciliation_kind": "owner_recovery", "maintenance_actor": "ui",
                    "original_origin": "plugin", "request_origin": "plugin", "request_key": request_key,
                    "message": "已核验原显示 owner 已结束、恢复后的同一 owner 登记为空；仅允许原 plugin 请求键接回原终端，不启动模型或重发任务。"}
        audit_key = "agent-reconnect-reconciliation:" + hashlib.sha256(operation.encode()).hexdigest()
        with self.store.lock, self.store.db:
            proof_row = self.store.db.execute("select value from settings where key='terminal-owner-recovery-proof'").fetchone()
            if (self.store.agent(host, thread_id) != enrolled or not proof_row or json.loads(proof_row["value"]) != proof):
                raise ValueError("原登记或服务恢复凭据已变化；保留未知结果。")
            changed = self.store.db.execute("update operations set result=? where request_key=? and fingerprint=? and state='done' and result=?",
                (json.dumps({"state": "blocked", "recovery": recovery}, ensure_ascii=False), operation, fingerprint, row["result"])).rowcount
            selected_changed = self.store.db.execute("update settings set value=? where key=? and value=?",
                (json.dumps(recovery), recovery_key, selected_row["value"])).rowcount
            if changed != 1 or selected_changed != 1:
                raise ValueError("原请求或当前核对记录已变化；保留未知结果。")
            old_audit = self.store.db.execute("select value from settings where key=?", (audit_key,)).fetchone()
            audits = json.loads(old_audit["value"]) if old_audit else []
            if not isinstance(audits, list):
                raise ValueError("原核对审计不能确认；保留未知结果。")
            audits.append({"kind": "owner_recovery", "at": time.time(), "request_key": request_key,
                           "maintenance_actor": "ui", "original_origin": "plugin", "fingerprint": fingerprint,
                           "previous_result": previous, "old_owner_stamp": stamp,
                           "owner_stamp": {"pid": proof["pid"], "recovery_id": proof["recovery_id"]}})
            self.store.db.execute("insert or replace into settings values(?,?)", (audit_key, json.dumps(audits)))
        result = self.get_agent(host, thread_id) | {"operation": "reconcile_original_worker", "operation_id": request_key,
                                                 "operation_state": "blocked", "recovery": recovery}
        result["management"] = self._management(result)
        return result

    def update_agent(self, host, thread_id, alias=None, pinned=None, origin="ui", request_key=None, controller_id=None):
        if origin not in ("ui", "plugin"):
            raise ValueError("更新来源必须为 ui 或 plugin。")
        if alias is not None and (not isinstance(alias, str) or not alias.strip() or len(alias.strip()) > 200 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in alias)):
            raise ValueError("工作名称不能为空，最长 200 字符，不接受控制字符。")
        if pinned is not None and type(pinned) is not bool:
            raise ValueError("pinned 必须为明确布尔值。")
        if alias is None and pinned is None:
            raise ValueError("至少提供工作名称或置顶状态。")
        self._request_key(request_key)
        if origin == "plugin" and not request_key:
            raise ValueError("管理操作必须提供稳定 request_key。")
        with self.registry_lock:
            self.get_agent(host, thread_id, plugin=origin == "plugin")
            if controller_id is not None:
                self._require_controller_scope(controller_id, host, thread_id)
            key = "agent-update:" + request_key if request_key else None
            if key:
                fingerprint = hashlib.sha256(json.dumps([host, thread_id, alias, pinned, origin, controller_id], ensure_ascii=False).encode()).hexdigest()
                previous = self.store.begin_operation(key, fingerprint)
                if previous is not None:
                    return self.get_agent(host, thread_id)
            self.store.update_agent_metadata(host, thread_id, alias.strip() if alias is not None else None, pinned)
            result = self.get_agent(host, thread_id)
            if key:
                self.store.finish_operation(key, {"agent_id": result["agent_id"], "updated": True})
            self.record_event("agent_updated", host=host, thread_id=thread_id, alias=result["alias"], pinned=result["pinned"], origin=origin)
            return result

    def set_auto_reconnect(self, host, thread_id, enabled, expected_terminal_id,
                           request_key, origin="ui", controller_id=None):
        """Explicit per-generation opt-in; it never attaches or sends input."""
        if type(enabled) is not bool or origin not in ("ui", "plugin"):
            raise ValueError("自动重连需要明确的布尔设置和有效来源。")
        if not isinstance(expected_terminal_id, str) or not re.fullmatch(r"ceohub-[a-f0-9]{16,48}", expected_terminal_id):
            raise ValueError("请提供当前终端的精确身份。")
        self._request_key(request_key)
        if not request_key:
            raise ValueError("自动重连设置必须提供稳定 request_key。")
        # Do not acquire registry_lock: an in-flight network reconnect can hold
        # it for minutes, while disabling future attempts must remain immediate.
        with self._auto_config_lock:
            agent = self.get_agent(host, thread_id, plugin=origin == "plugin")
            if agent.get("terminal_id") != expected_terminal_id:
                raise ValueError("终端已切换，请刷新后再设置自动重连。")
            if controller_id is not None:
                self._require_controller_scope(controller_id, host, thread_id)
            if enabled and (not self.is_managed_worker(agent) or self.host(host)["transport"] != "ssh"):
                raise ValueError("自动重连只支持未关闭的 SSH 工作终端。")
            operation = "auto-reconnect-config:" + request_key
            fingerprint = hashlib.sha256(json.dumps(
                [host, thread_id, expected_terminal_id, enabled, origin, controller_id]).encode()).hexdigest()
            previous = self.store.begin_operation(operation, fingerprint)
            if previous is not None:
                if previous.get("updated") is not True:
                    raise ValueError("原设置请求未应用，请刷新后重新设置。")
                return self.get_agent(host, thread_id)
            try:
                self.auto_reconnect.configure(host, thread_id, enabled, expected_terminal_id, request_key)
            except Exception:
                self.store.finish_operation(operation, {"updated": False})
                raise
            self.store.finish_operation(operation, {"updated": True, "terminal_id": expected_terminal_id})
            self.record_event("agent_updated", host=host, thread_id=thread_id,
                              auto_reconnect=enabled, origin=origin)
            return self.get_agent(host, thread_id)

    def _probe_remote_terminal(self, host, terminal_id):
        if not isinstance(terminal_id, str) or not re.fullmatch(r"ceohub-[a-f0-9]{16,48}", terminal_id):
            return {"ok": False, "exists": False, "reason": "invalid_terminal_id"}
        config = self.host(host)
        if config["transport"] != "ssh":
            return {"ok": False, "exists": False, "reason": "unsupported_transport"}
        target = config.get("target", "")
        if not isinstance(target, str) or not target or target.startswith("-") or any(char.isspace() or ord(char) < 32 for char in target):
            return {"ok": False, "exists": False, "reason": "invalid_ssh_target"}
        # Send the same source the old 0.3 broker loads per attachment. Probe mode
        # only reads the chosen tmux/pane identity, never input or model config.
        source = source_path("transport", "remote_pty_bridge.py").read_text(encoding="utf-8")
        try:
            reply = subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ControlMaster=no", "-o", "ControlPersist=no", "-o", f"ConnectTimeout={SSH_CONTROL_CONNECT_TIMEOUT}",
                target, "python3 - probe --session " + shlex.quote(terminal_id)], input=pack_python_source(source),
                text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=SSH_CONTROL_TIMEOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if reply.returncode or len(reply.stdout) > 65536:
                detail = (reply.stderr or "")[:4096].casefold()
                reason = "ssh_host_key_required" if "host key verification failed" in detail else "ssh_auth_required" if "permission denied" in detail or "authentication failed" in detail else "route_unavailable"
                return {"ok": False, "exists": False, "reason": reason}
            lines = [line.removeprefix("CEO_HUB_PTY_RESULT=") for line in reply.stdout.splitlines() if line.startswith("CEO_HUB_PTY_RESULT=")]
            result = json.loads(lines[0]) if len(lines) == 1 else None
            if not isinstance(result, dict) or type(result.get("exists")) is not bool or type(result.get("ok")) is not bool:
                raise ValueError()
            if result.get("exists"):
                identity = result.get("identity")
                if not isinstance(identity, dict) or set(identity) != {"name", "session", "created", "pane", "pane_pid", "pane_start_ticks"} or identity["name"] != terminal_id or not isinstance(identity["session"], str) or not re.fullmatch(r"\$[0-9]+", identity["session"]) or not isinstance(identity["pane"], str) or not re.fullmatch(r"%[0-9]+", identity["pane"]) or any(type(identity[key]) is not int or identity[key] <= 0 for key in ("created", "pane_pid", "pane_start_ticks")):
                    raise ValueError()
            return result
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return {"ok": False, "exists": False, "reason": "route_unavailable"}

    def reconnect_agent(self, host, thread_id, origin="ui", request_key=None, controller_id=None):
        if origin not in ("ui", "plugin"):
            raise ValueError("重连来源必须为 ui 或 plugin。")
        self._request_key(request_key)
        if origin == "plugin" and not request_key:
            raise ValueError("管理重连必须提供稳定 request_key。")
        with self.registry_lock:
            agent = self.get_agent(host, thread_id, plugin=origin == "plugin")
            if controller_id is not None:
                self._require_controller_scope(controller_id, host, thread_id)
            owner_stamp = None
            reconciled_identity = None
            reconciled_worker_binding = None
            attachment_requested = False
            def finish(code, message, next_action, state="blocked", view=None, retryable=False, persist=True):
                recovery = {"code": code, "message": message, "next_action": next_action,
                            "request_key": request_key, "request_origin": origin}
                if retryable:
                    recovery["retryable"] = True
                self.store.setting(self._recovery_key(host, thread_id), recovery)
                result = (view or self.get_agent(host, thread_id)) | {"recovery": recovery, "operation": "reconnect_agent", "operation_state": state,
                    "operation_id": request_key}
                result["management"] = self._management(result)
                if operation and persist:
                    saved = {"state": state, "recovery": recovery}
                    # A known pre-attachment failure must not discard the
                    # immutable identity needed by the same-key retry. Once an
                    # attachment was requested, a fresh unknown cannot reuse it.
                    if state == "blocked" and retryable and not attachment_requested and reconciled_identity is not None:
                        saved["reconciliation_identity"] = reconciled_identity
                        if reconciled_worker_binding is not None:
                            saved["reconciliation_worker_binding"] = reconciled_worker_binding
                    if state == "unknown" and owner_stamp is not None:
                        saved["owner_stamp"] = owner_stamp
                    self.store.finish_operation(operation, saved)
                return result
            request_key = request_key or "ui-reconnect-" + str(uuid.uuid4())
            operation = "agent-reconnect:" + request_key
            fingerprint = self._reconnect_fingerprint(host, thread_id, agent.get("terminal_id"), origin, controller_id)
            pre_attach_route_message = "SSH 路由暂不可达；请检查机器或恢复 SSH 上游/隧道路由，再由用户重试。不会自动重启服务。"
            try:
                previous = self.store.begin_operation(operation, fingerprint)
            except RuntimeError:
                # Do not mark an in-flight operation as completed merely because
                # another request with its key arrived while it was uncertain.
                return finish("reconnect_unknown", "原重连结果尚未核验；请先检查连接，不使用新请求键强制重试。", "inspect_connection", "unknown", persist=False)
            if previous is not None:
                recovery = previous.get("recovery", {})
                local_retryable = previous.get("state") == "blocked" and recovery.get("code") == "local_display_unavailable" and recovery.get("retryable") is True
                # A user retry with the same key may repeat only the read-only
                # pre-attachment route check. Match the earlier fixed receipt
                # too, so known old failures can recover without a new key.
                # Post-attachment route failures, auth/key prompts and unknown
                # outcomes remain fenced; a code alone is not launch evidence.
                route_retryable = (previous.get("state") == "blocked" and recovery.get("code") == "route_unavailable"
                    and recovery.get("message") == pre_attach_route_message and recovery.get("next_action") == "check_connection"
                    and recovery.get("request_key") == request_key and recovery.get("request_origin") == origin
                    and (recovery.get("retryable") is None or recovery.get("retryable") is True) and "owner_stamp" not in previous)
                if route_retryable or (local_retryable and self.terminal_service_status()["running"]):
                    if recovery.get("reconciliation_kind") in ("ended_display", "worker_continuity") or "reconciliation_identity" in previous:
                        reconciled_identity = previous.get("reconciliation_identity")
                        if not self._valid_existing_display_identity(reconciled_identity, agent.get("terminal_id")):
                            return finish("reconnect_unknown", "原核对身份凭据不能确认；不重复附着。", "inspect_connection", "unknown", persist=False)
                    if recovery.get("reconciliation_kind") == "worker_continuity" or "reconciliation_worker_binding" in previous:
                        reconciled_worker_binding = previous.get("reconciliation_worker_binding")
                        if not self._valid_original_worker_binding(reconciled_worker_binding, agent.get("terminal_id")):
                            return finish("reconnect_unknown", "原工作进程核对凭据不能确认；不重复附着。", "inspect_connection", "unknown", persist=False)
                    with self.store.lock, self.store.db:
                        row = self.store.db.execute("select result from operations where request_key=? and fingerprint=? and state='done'", (operation, fingerprint)).fetchone()
                        changed = bool(row and json.loads(row["result"]) == previous and self.store.db.execute("update operations set state='started',result=null where request_key=? and fingerprint=? and state='done' and result=?",
                            (operation, fingerprint, row["result"])).rowcount == 1)
                    if not changed:
                        return finish("reconnect_unknown", "原重连记录已改变；请先核对连接，不重复附着。", "inspect_connection", "unknown", persist=False)
                    # The receipt proves only the earlier attempt. Consume its
                    # candidate before a new attempt so a fresh post-attach
                    # unknown cannot be incorrectly reconciled with old proof.
                    proof = self.store.setting("terminal-owner-recovery-proof") or {}
                    if operation in proof.get("operations", {}):
                        proof["operations"].pop(operation)
                        self.store.setting("terminal-owner-recovery-proof", proof)
                else:
                    result = self.get_agent(host, thread_id) | {"operation": "reconnect_agent", "operation_state": previous["state"], "operation_id": request_key,
                        "recovery": previous["recovery"]}
                    result["management"] = self._management(result)
                    return result
            if agent.get("mode") in ("closed", "closing", "close_unknown"):
                return finish("closed", "此终端已结束或关闭结果待核对；不会重新启动。", "none")
            if agent.get("connected"):
                if reconciled_identity is not None:
                    return finish("reconnect_unknown", "核对后显示已被其他操作连接；请检查原显示身份，不重复附着。", "inspect_connection", "unknown")
                return finish("already_connected", "当前显示连接仍在；未重复附着，也未发送任何输入。", "open_terminal", "done", agent)
            if not agent.get("terminal_id"):
                return finish("missing_terminal", "没有可恢复的原终端；请由用户选择或新建终端。", "inspect_connection")
            if self.host(host)["transport"] != "ssh":
                return finish("local_reconnect_unsupported", "本机终端进程不能由重连按钮重新启动；请核对原显示服务或手动打开终端。", "inspect_connection")
            before = self._probe_remote_terminal(host, agent["terminal_id"])
            if not before.get("ok"):
                if before.get("reason") == "ssh_auth_required":
                    return finish("ssh_auth_required", "SSH 认证需要由用户处理；不会读取或改写凭据，也不会自动尝试登录。", "check_connection")
                if before.get("reason") == "ssh_host_key_required":
                    return finish("ssh_host_key_required", "SSH 主机密钥需要由用户核验；不会跳过验证或改写 known_hosts。", "check_connection")
                return finish("route_unavailable", pre_attach_route_message, "check_connection", retryable=True)
            if not before.get("exists"):
                return finish("original_session_missing", "原 tmux 会话不存在；未创建替代 shell、未启动或恢复 Codex。", "inspect_connection")
            if reconciled_identity is not None and before.get("identity") != reconciled_identity:
                return finish("original_session_changed", "核对后的远端原 tmux/pane 身份已改变；未附着替代终端或启动模型。", "inspect_connection")
            if reconciled_worker_binding is not None:
                try:
                    worker_before = self.history(host, {"op": "bind_output", "terminal_id": agent["terminal_id"]})
                except Exception:
                    return finish("route_unavailable", pre_attach_route_message, "check_connection", retryable=True)
                if not self._matches_original_worker_binding(worker_before, reconciled_worker_binding, agent["terminal_id"]):
                    return finish("original_session_changed", "接回显示前原 native 工作进程或精确 Session 已变化；未附着替代 agent。", "inspect_connection")
            if controller_id is not None:
                self._require_controller_scope(controller_id, host, thread_id)
            proof = before["identity"]
            marker = "/.__pronhub_existing__/" + agent["terminal_id"] + "/" + base64.urlsafe_b64encode(json.dumps(proof, sort_keys=True).encode()).decode().rstrip("=")
            self._reconnecting.add(self.agent_id(host, thread_id))
            attachment_returned = False
            try:
                owner = self.terminal_service_status()
                if owner["running"] and owner.get("pid") and owner.get("recovery_id"):
                    owner_stamp = {"pid": owner["pid"], "recovery_id": owner["recovery_id"]}
                attachment_requested = True
                runtime = self.terminals.create({"session_id": agent["terminal_id"], "host": host, "transport": "ssh", "target": self.host(host)["target"],
                    "cwd": marker, "title": agent["alias"], "cols": 100, "rows": 30})
                attachment_returned = True
                after = self._probe_remote_terminal(host, agent["terminal_id"])
                if not after.get("ok"):
                    return finish("reconnect_unknown", "已请求原显示附着，但后续身份核验暂不可用；不重复重连或发送任务。", "inspect_connection", "unknown")
                if not after.get("exists") or after.get("identity") != proof:
                    return finish("original_session_changed", "原 tmux/pane 身份已经改变；未确认恢复，不发送输入，也不关闭其他工作进程。", "inspect_connection", "blocked")
                if reconciled_worker_binding is not None:
                    try:
                        worker_after = self.history(host, {"op": "bind_output", "terminal_id": agent["terminal_id"]})
                    except Exception:
                        return finish("reconnect_unknown", "显示已请求附着，但原 native 工作进程的后置核验暂不可用；不重复附着或发送任务。", "inspect_connection", "unknown")
                    if not self._matches_original_worker_binding(worker_after, reconciled_worker_binding, agent["terminal_id"]):
                        return finish("original_session_changed", "显示附着后原 native 工作进程或精确 Session 已变化；未确认恢复，不重启或发送输入。", "inspect_connection")
                current = self.terminals.snapshot(agent["terminal_id"], 60)
                view = self.get_agent(host, thread_id, sessions=[current])
                if not view["connected"]:
                    return finish("route_unavailable", "原显示附着仍未连接；请检查 SSH 上游或隧道路由，再由用户重试。", "check_connection", "blocked", view)
                code = "reconnected" if view["agent_ready"] else "awaiting_foreground"
                message = "已重新附着原终端并核验相同 tmux/pane；未重启 agent、未输入任务。" if view["agent_ready"] else "原终端显示已附着且身份一致；前台尚未就绪，请进入终端核对。"
                self.record_event("agent_reconnected", host=host, thread_id=thread_id, terminal_id=agent["terminal_id"], origin=origin)
                return finish(code, message, "open_terminal", "done", view)
            except BrokerError as error:
                if error.code == "bound_transport_unavailable" and not attachment_returned:
                    attachment_requested = False
                    return finish("local_display_unavailable", "本机原工作终端显示服务离线；附着请求尚未发送。请先恢复绑定的显示服务，再用原请求键重连此终端。不会重启远端 agent 或重发任务。",
                                  "recover_display_service", "blocked", retryable=True)
                return finish("reconnect_unknown", "原显示连接结果未知；请核对原终端和路由，不重放任务或自动重启。", "inspect_connection", "unknown")
            except Exception:
                return finish("reconnect_unknown", "原显示连接结果未知；请核对原终端和路由，不重放任务或自动重启。", "inspect_connection", "unknown")
            finally:
                self._reconnecting.discard(self.agent_id(host, thread_id))

    def split_agent_id(self, agent_id):
        parts = agent_id.split("::", 1)
        if len(parts) != 2 or not all(parts):
            raise ValueError("请使用 list_active_agents 返回的精确 agent_id。")
        self.host(parts[0])
        return parts

    def attached_agents(self, active_only=False):
        sessions = self.terminals.sessions()
        return [self.get_agent(a["host"], a["thread_id"], sessions=sessions) for a in self.store.agents()
                if not self.controllers.is_controller_reference(a["host"], a["thread_id"], cwd=a.get("cwd"))
                and (not active_only or self.is_managed_worker(a))]

    def connected_hosts(self, agents=None):
        agents = self.attached_agents() if agents is None else agents
        connected = {host for host, state in self.connections.items() if state.get("connected")}
        connected.update(a["host"] for a in agents if a["connected"] and a["state"] in ("shell", "agent"))
        return connected

    def _directory(self, config, cwd):
        if not isinstance(cwd, str) or not cwd or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in cwd):
            raise ValueError("请输入绝对工作目录，不接受终端控制字符。")
        absolute = bool(re.match(r"^[A-Za-z]:[\\/]", cwd)) or Path(cwd).is_absolute()
        if not (absolute if config["transport"] == "local" else cwd.startswith("/")):
            raise ValueError("工作目录必须是该机器上的绝对路径。")

    @staticmethod
    def _request_key(value):
        if value is not None and (not isinstance(value, str) or not value or len(value) > 200):
            raise ValueError("request_key 必须为 1 至 200 字符的稳定请求键。")

    @staticmethod
    def _child_key(request_key, suffix):
        combined = request_key + suffix
        return combined if len(combined) <= 200 else hashlib.sha256(request_key.encode()).hexdigest() + suffix

    def _probe_reopen_terminal(self, host, terminal_id):
        """Fresh, boot-qualified identity for one explicit destructive intent."""
        config = self.host(host)
        target = config.get("target", "")
        if (config.get("transport") != "ssh" or not remote_terminal_close.SESSION.fullmatch(terminal_id)
                or not isinstance(target, str) or not target or target.startswith("-")
                or any(char.isspace() or ord(char) < 32 for char in target)):
            return {"ok": False, "reason": "invalid_target"}
        try:
            source = source_path("transport", "remote_terminal_close.py").read_text(encoding="utf-8")
            reply = subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ControlMaster=no", "-o", "ControlPersist=no",
                "-o", "ConnectTimeout=30", target, "python3 - probe --session " + shlex.quote(terminal_id)],
                input=pack_python_source(source), text=True, encoding="utf-8", errors="strict", capture_output=True,
                timeout=45, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if reply.returncode or len(reply.stdout.encode("utf-8")) > 65536:
                return {"ok": False, "reason": "route_or_identity_unavailable"}
            lines = [line[len(remote_terminal_close.MARKER):] for line in reply.stdout.splitlines()
                     if line.startswith(remote_terminal_close.MARKER)]
            value = json.loads(lines[0]) if len(lines) == 1 else None
            if (not isinstance(value, dict) or value.get("ok") is not True or value.get("terminal_id") != terminal_id
                    or type(value.get("exists")) is not bool
                    or set(value) != ({"ok", "terminal_id", "exists", "identity"} if value["exists"] else {"ok", "terminal_id", "exists"})
                    or value["exists"] and not remote_terminal_close.valid_identity(value.get("identity"), terminal_id)):
                raise ValueError()
            return value
        except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired):
            return {"ok": False, "reason": "route_or_identity_unavailable"}

    @staticmethod
    def _reopen_owner(health):
        if (not isinstance(health, dict) or health.get("ok") is not True
                or type(health.get("pid")) is not int or not 0 < health["pid"] <= 2147483647
                or not isinstance(health.get("recovery_id"), str)
                or not re.fullmatch(r"[a-f0-9]{32}", health["recovery_id"])
                or not isinstance(health.get("features"), list) or "verified_close" not in health["features"]):
            raise ValueError("verified_close_unavailable")
        return {"pid": health["pid"], "recovery_id": health["recovery_id"]}

    def reopen_terminal(self, host, thread_id, request_key, expected_terminal_id, confirm=False, origin="ui", controller_id=None):
        """Explicitly replace one worker terminal, never resume its model.

        Every side effect is fenced by a durable intent. A lost response cannot
        justify a second close or create, even after restarting the web service.
        """
        if confirm is not True:
            raise ValueError("重新打开会终止旧终端及其中的进程，并创建空白终端；请明确确认。")
        if origin not in ("ui", "plugin"):
            raise ValueError("重新打开来源必须为 ui 或 plugin。")
        self._request_key(request_key)
        if not request_key:
            raise ValueError("重新打开必须提供稳定 request_key。")
        if not isinstance(expected_terminal_id, str) or not remote_terminal_close.SESSION.fullmatch(expected_terminal_id):
            raise ValueError("必须提供确认时的精确旧终端身份。")
        with self.registry_lock:
            agent = self.get_agent(host, thread_id, plugin=False)
            operation = "terminal-reopen:" + request_key
            fingerprint = hashlib.sha256(json.dumps([host, thread_id, expected_terminal_id, origin, controller_id]).encode()).hexdigest()
            if controller_id is not None:
                self._require_creation_scope(controller_id, host, agent.get("cwd", ""))
                with self.store.lock:
                    receipt = self.store.db.execute("select fingerprint from operations where request_key=?", (operation,)).fetchone()
                # A same-controller receipt can still be read after this very
                # request fenced its worker as uncertain. It is never authority
                # for a second mutation or for a fresh closed-worker request.
                if not receipt or receipt["fingerprint"] != fingerprint or self.is_managed_worker(self.store.agent(host, thread_id)):
                    self._require_controller_scope(controller_id, host, thread_id)
            try:
                previous = self.store.begin_operation(operation, fingerprint)
            except RuntimeError:
                return {**agent, "operation": "reopen_terminal", "operation_id": request_key, "operation_state": "unknown",
                    "reopened": False, "previous_terminal_id": expected_terminal_id,
                    "recovery": {"code": "reopen_unknown", "next_action": "inspect_connection",
                        "message": "原重新打开请求尚未完成核验；不会重复关闭或创建，请保留原请求键。"}}
            if previous is not None:
                return previous

            def finish(code, message, state="blocked", reopened=False):
                result = {**self.get_agent(host, thread_id), "operation": "reopen_terminal", "operation_id": request_key,
                    "operation_state": state, "reopened": reopened, "previous_terminal_id": expected_terminal_id,
                    "recovery": {"code": code, "message": message,
                        "next_action": "open_terminal" if reopened else "update_display_service" if code == "verified_close_unavailable" else "inspect_connection"}}
                self.store.finish_operation(operation, result)
                return result

            if not self.is_managed_worker(self.store.agent(host, thread_id)):
                return finish("worker_not_open", "只有未关闭的已登记工作终端可以重新打开；不会恢复已关闭或管理会话。")
            if agent.get("terminal_id") != expected_terminal_id:
                return finish("terminal_generation_changed", "当前终端与确认时的旧终端不同；未关闭或创建，请重新核对。")
            config = dict(self.host(host))
            self._directory(config, agent.get("cwd", ""))
            # No default owner launcher, broad registry scan, or remote action
            # is allowed until the bound broker advertises exact-close support.
            try:
                owner = self._reopen_owner(self.terminals.request("/health", timeout=3))
            except Exception:
                return finish("verified_close_unavailable", "原显示服务尚不支持严格核验关闭；请先更新显示服务，未关闭或创建终端。")
            with self.store.lock:
                submitting = self.store.db.execute("select 1 from outbox where host=? and thread_id=? and state='submitting' limit 1",
                    (host, thread_id)).fetchone()
            if submitting:
                return finish("input_submission_in_progress", "该终端仍有提交中的输入；请先核对提交结果，未关闭或创建终端。")
            remote = None
            if config["transport"] == "ssh":
                probe = self._probe_reopen_terminal(host, expected_terminal_id)
                if not probe.get("ok"):
                    return finish("reopen_preflight_unavailable", "无法确认旧远端终端的精确身份或不存在；未关闭或创建，请检查 SSH 路由。")
                remote = {"exists": probe["exists"]}
                if probe["exists"]:
                    remote["identity"] = probe["identity"]
            try:
                if self._reopen_owner(self.terminals.request("/health", timeout=3)) != owner:
                    raise ValueError()
            except Exception:
                return finish("display_owner_changed", "核验期间显示服务身份变化；未关闭或创建，请核对原 owner。")

            # Fence before close so neither a stale browser nor an old queued
            # Captain prompt can be redirected to the replacement generation.
            self.auto_reconnect.cancel(host, thread_id, agent["terminal_id"])
            self.store.remember(host, thread_id, agent["alias"], active=False, mode="closing")
            self.store.fence_terminal_reopen(host, thread_id, expected_terminal_id, request_key, RETRYABLE)
            self.record_event("terminal_reopen_started", host=host, thread_id=thread_id,
                terminal_id=expected_terminal_id, operation_id=request_key, origin=origin)
            body = {"require_verified": True, "expected_owner": owner, "expected_host": host,
                "expected_transport": config["transport"], "expected_target": config.get("target", "")}
            if remote is not None:
                body["expected_remote"] = remote
            try:
                closed = self.terminals.write(expected_terminal_id, "close", body)
                if (not isinstance(closed, dict) or closed.get("id") != expected_terminal_id
                        or closed.get("closed") is not True or closed.get("closed_verified") is not True
                        or closed.get("record_removed") is not True
                        or not (closed.get("local_display_exit_verified") is True
                            or config["transport"] == "ssh" and closed.get("missing_record") is True)
                        or config["transport"] == "ssh" and closed.get("remote_closed_verified") is not True):
                    raise ValueError()
                if self._reopen_owner(self.terminals.request("/health", timeout=3)) != owner:
                    raise ValueError()
            except Exception:
                self.store.remember(host, thread_id, agent["alias"], active=False, mode="close_unknown")
                return finish("reopen_close_unknown", "旧终端关闭或显示服务身份未获严格确认；未创建新终端，不会重复关闭。", "unknown")

            terminal_id = "ceohub-" + uuid.uuid5(uuid.NAMESPACE_URL,
                json.dumps(["agentcaptain-terminal-reopen-v1", host, thread_id, expected_terminal_id, request_key], ensure_ascii=False)).hex
            binding = self.store.setting(self._output_key(host, expected_terminal_id)) or {}
            generation = {"previous_terminal_id": expected_terminal_id, "terminal_id": terminal_id,
                "operation_id": request_key, "previous_session_id": binding.get("session_id"), "at": time.time()}
            # Register the new ID before issuing its only create attempt. If a
            # response is lost, the same worker card retains that exact ID.
            generation = self.store.replace_terminal_generation(host, thread_id, expected_terminal_id, terminal_id,
                generation, self._generation_key(host, thread_id), self._recovery_key(host, thread_id))
            self.record_event("terminal_generation_replaced", host=host, thread_id=thread_id,
                previous_terminal_id=expected_terminal_id, terminal_id=terminal_id, operation_id=request_key,
                previous_session_id=generation["previous_session_id"], historical=True)
            try:
                runtime = self.terminals.request("/sessions/create", {"session_id": terminal_id, "host": host,
                    "transport": config["transport"], "target": config.get("target", ""), "cwd": agent["cwd"],
                    "title": agent["alias"], "cols": 100, "rows": 30, "expected_owner": owner, "require_verified": True})
                if (not isinstance(runtime, dict) or runtime.get("id") != terminal_id or runtime.get("host") != host
                        or runtime.get("transport") != config["transport"] or runtime.get("target", "") != config.get("target", "")
                        or runtime.get("cwd") != agent["cwd"] or runtime.get("connected") is not True
                        or self._reopen_owner(self.terminals.request("/health", timeout=3)) != owner):
                    raise ValueError()
            except Exception:
                self.store.remember(host, thread_id, agent["alias"], active=False, mode="close_unknown")
                return finish("reopen_create_unknown", "旧终端已严格关闭，新终端创建结果待核对；已保留新身份，不会重复创建或启动模型。", "unknown")
            self.store.remember(host, thread_id, agent["alias"], active=True, mode="terminal")
            self.record_event("terminal_reopened", host=host, thread_id=thread_id, previous_terminal_id=expected_terminal_id,
                terminal_id=terminal_id, operation_id=request_key, origin=origin, model_started=False)
            return finish("reopened", "已关闭并清理精确旧终端，在原目录打开新的空白终端；未启动 Codex、恢复会话或发送任务。", "done", True)

    def create_terminal(self, host, cwd, title, thread_id=None, cols=100, rows=30, active=False, request_key=None, controller_id=None):
        config = self.host(host)
        if self.controllers.is_controller_reference(host, thread_id, cwd=cwd or None):
            raise PermissionError("管理会话不能作为工作终端创建或连接。")
        self._request_key(request_key)
        if not isinstance(cwd, str):
            raise ValueError("工作目录必须为路径字符串；留空可使用机器主目录。")
        # Resolve only the chosen machine's home, outside the registry lock.
        # Existing connections keep their recorded cwd without another SSH call.
        previous_hint = self.store.agent(host, thread_id) if thread_id else None
        if previous_hint:
            self._require_open(previous_hint)
        if thread_id and not thread_id.startswith("ceohub-") and (previous_hint is None or not cwd):
            if not previous_hint or not previous_hint.get("cwd"):
                metadata = self.history(host, {"op": "read", "thread_id": thread_id, "limit": 1})
                cwd, title = cwd or metadata["cwd"], title or metadata["title"]
        if not cwd:
            cwd = (previous_hint or {}).get("cwd") or self.directories.home(config)
        with self.registry_lock:
            previous = self.store.agent(host, thread_id) if thread_id else None
            if previous:
                self._require_open(previous)
            if previous and previous.get("terminal_id") and previous.get("cwd"):
                if cwd and cwd != previous["cwd"]:
                    raise ValueError("已有终端仍在原工作目录；请新建终端使用其他目录。")
                cwd = previous["cwd"]
            self._directory(config, cwd)
            if self.controllers.is_controller_reference(host, thread_id, cwd=cwd):
                raise PermissionError("管理工作区不能登记为工作 agent。")
            if controller_id is not None:
                self._require_creation_scope(controller_id, host, cwd)
            if not isinstance(title, str) or not title.strip() or len(title.strip()) > 200 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in title):
                raise ValueError("工作名称不能为空，最长 200 字符。")
            title = title.strip()
            if type(cols) is not int or not 20 <= cols <= 500 or type(rows) is not int or not 5 <= rows <= 200:
                raise ValueError("终端尺寸必须为 20 至 500 列、5 至 200 行。")
            operation_key = "terminal:" + request_key if request_key else None
            if operation_key:
                fingerprint = hashlib.sha256(json.dumps([host, cwd, title, thread_id, cols, rows, bool(active)], ensure_ascii=False).encode()).hexdigest()
                cached = self.store.begin_operation(operation_key, fingerprint)
                if cached is not None:
                    return cached
            terminal_id = previous.get("terminal_id") if previous else None
            terminal_id = terminal_id or "ceohub-" + (uuid.uuid5(uuid.NAMESPACE_URL, "ceo-hub:" + request_key).hex if request_key else uuid.uuid4().hex)
            thread_id = thread_id or terminal_id
            # Register first, so an interrupted create remains visible and can
            # reconnect the same owned terminal rather than silently duplicating.
            self.store.remember(host, thread_id, title or thread_id, active=True if previous is None else None,
                                mode="terminal", terminal_id=terminal_id, cwd=cwd)
            try:
                if controller_id is not None:
                    self._require_creation_scope(controller_id, host, cwd)
                runtime = self.terminals.create({"session_id": terminal_id, "host": host, "transport": config["transport"],
                    "target": config.get("target", ""), "cwd": cwd, "title": title, "cols": cols, "rows": rows})
                result = {"created": True, **self.get_agent(host, thread_id, sessions=[runtime]), **runtime,
                          "agent_id": self.agent_id(host, thread_id), "terminal_id": terminal_id, "thread_id": thread_id}
            except BrokerError as error:
                # A pinned owner's pre-launch failure is known not to have
                # created a second terminal; do not describe it as unknown.
                delivery = {"state": "blocked" if error.code == "bound_transport_unavailable" else "unknown",
                            "error": str(error), "code": error.code}
                result = {"created": False, **self.get_agent(host, thread_id, sessions=[]), "delivery": delivery}
            except Exception as error:
                result = {"created": False, **self.get_agent(host, thread_id, sessions=[]), "delivery": {"state": "unknown", "error": str(error)}}
            if not thread_id.startswith("ceohub-"):
                quoted = "'" + thread_id.replace("'", "''") + "'" if config["transport"] == "local" else shlex.quote(thread_id)
                result["resume_command"] = "codex resume " + quoted
            if operation_key:
                self.store.finish_operation(operation_key, result)
            if result["created"]:
                self.record_event("agent_attached", host=host, thread_id=thread_id, terminal_id=terminal_id)
            return result

    def connect_terminal(self, host, thread_id, cwd="", title="", cols=100, rows=30):
        previous = self.store.agent(host, thread_id)
        cwd, title = cwd or (previous or {}).get("cwd", ""), title or (previous or {}).get("alias", "")
        return self.create_terminal(host, cwd, title, thread_id, cols, rows)

    def attach_agent(self, host, thread_id, alias=""):
        return self.connect_terminal(host, thread_id, title=alias)

    def activate_agent(self, host, thread_id, active):
        """Compatibility no-op: enrolled open terminals are always managed."""
        if type(active) is not bool:
            raise ValueError("兼容 active 参数必须为布尔值。")
        with self.registry_lock:
            row = self.get_agent(host, thread_id)
            self._require_open(row)
        return {**row, "activation_compatibility": {"ignored": True, "requested_active": active,
            "policy": "connected_workers", "message": "已连接的工作终端自动纳入管理；此兼容调用不改变连接、管理范围或待发送输入。"}}

    @staticmethod
    def _output_key(host, terminal_id):
        return "output_binding:" + host + ":" + terminal_id

    def _probe_output(self, host, agent, view):
        request = {"op": "bind_output"}
        if self.host(host)["transport"] == "ssh":
            request["terminal_id"] = agent["terminal_id"]
        else:
            if not view.get("connected"):
                return {"state": "unbound", "reason": "terminal_disconnected"}
            request["root_pid"] = view.get("pid")
        return self.history(host, request)

    def bind_output(self, host, thread_id, session_id):
        """Human-selected exact session, bound to this terminal's live process.

        This is a fallback for a Codex resume picker that exposes no session ID
        in argv or open handles. It never sends /status or other worker input.
        """
        if not isinstance(session_id, str) or not re.fullmatch(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", session_id):
            raise ValueError("请输入当前 Codex /status 显示的完整 Session ID。")
        session_id = session_id.lower()
        if self.controllers.is_controller_reference(host, session_id):
            raise PermissionError("不能将管理会话输出关联为工作 agent。")
        agent = self.get_agent(host, thread_id)
        self._require_open(agent)
        if not agent.get("terminal_id"):
            raise ValueError("请先选择工作终端。")
        view = self.terminals.snapshot(agent["terminal_id"], 1)
        probe = self._probe_output(host, agent, view)
        if not probe.get("process_identity") or not probe.get("codex_home"):
            raise ValueError("无法核验这个终端中的 Codex 进程，未建立关联。")
        if probe.get("state") == "unbound" and probe.get("reason") != "no_exact_session_evidence":
            raise ValueError("会话关联证据冲突或失效，未建立关联：" + probe.get("reason", "unknown"))
        if probe.get("state") == "bound" and probe.get("binding_evidence") == "held_rollout" and probe["session_id"] != session_id:
            raise ValueError("输入 ID 与当前 Codex 进程的会话证据不同，未建立关联。")
        resolved = self.history(host, {"op": "resolve_output", "session_id": session_id, "codex_home": probe["codex_home"]})
        if resolved.get("state") not in ("bound", "resolved"):
            raise ValueError(resolved.get("reason", "未找到这个精确会话的输出记录。"))
        verify = self._probe_output(host, agent, view)
        if verify.get("process_identity") != probe["process_identity"]:
            raise ValueError("关联期间 Codex 进程已变化，请重新确认当前 Session ID。")
        if verify.get("state") == "bound" and verify.get("binding_evidence") == "held_rollout" and verify.get("session_id") != session_id:
            raise ValueError("关联期间 Codex 会话已变化，请重新确认 Session ID。")
        binding = {**resolved, "process_identity": probe["process_identity"], "method": "human_selected_session",
                   "terminal_id": agent["terminal_id"]}
        self.store.setting(self._output_key(host, agent["terminal_id"]), binding)
        return {"agent_id": agent["agent_id"], "output_binding": {"state": "bound", "session_id": session_id,
                 "method": binding["method"]}, "note": "已关联所选会话记录；没有重启或输入到工作 agent。"}

    @staticmethod
    def _bound_conversation_page(output, binding, read_transport=None):
        pinned_note = " 当前进程未暴露确切会话 ID，这是用户已选或上次核验的会话记录；如果在 Codex 内使用 /new 或 /resume 切换过会话，请重新关联，不能把旧记录当新会话结果。" if not binding.get("live_session_verified") else ""
        return {**output, "note": output.get("note", "") + pinned_note,
                "output_binding": {"state": "bound", "session_id": binding["session_id"],
                "method": binding.get("method", "process_evidence" if binding.get("live_session_verified") else "last_verified_session"),
                "historical": bool(binding.get("historical")), "live_session_verified": bool(binding.get("live_session_verified"))},
                **({"read_transport": read_transport} if read_transport is not None else {})}

    def _remote_conversation_output(self, host, agent, view, saved, limit, cursor, after_cursor, include_tools):
        """One SSH read transaction, with native identity checks on BOTH sides.

        The screen snapshot remains independent foreground evidence. It cannot
        authorize this read or replace the remote process/rollout checks.
        """
        page = self.history(host, {"op": "read_bound_output", "terminal_id": agent["terminal_id"],
            "saved_binding": saved, "historical_allowed": agent.get("mode") in ("closed", "closing", "close_unknown") or not view.get("connected"),
            "limit": limit, "include_tools": include_tools,
            **({"cursor": cursor} if cursor else {}), **({"after_cursor": after_cursor} if after_cursor else {})})
        if page.get("state") == "unavailable":
            messages = {
                "session_identity_changed": "读取期间 Codex 进程已变化，未返回旧会话输出；请重新读取。",
                "output_binding_invalid": "读取期间会话关联证据冲突或失效，未返回记录；请重新核验。",
            }
            if page.get("reason_code") in messages:
                raise ValueError(messages[page["reason_code"]])
            raise HistoryReadError("remote_read_failed")
        if page.get("state") == "unbound":
            reason = page.get("reason", "session_identity_unavailable")
            if not isinstance(reason, str) or not re.fullmatch(r"[a-z_]{1,80}", reason):
                raise HistoryReadError("protocol_invalid")
            return {"output_binding": {"state": "unbound", "reason": reason}, "messages": [], "content_complete": False,
                    "note": "尚未准确关联 Codex 会话输出。请用当前终端 /status 的 Session ID 在 UI「关联会话」。屏幕不是完整回复。"}
        binding, output = page.get("binding"), page.get("output")
        if (page.get("state") != "bound" or not isinstance(binding, dict) or not isinstance(output, dict)
                or binding.get("state") != "bound" or binding.get("terminal_id") != agent["terminal_id"]
                or not isinstance(binding.get("session_id"), str) or not binding["session_id"]
                or type(binding.get("live_session_verified")) is not bool or type(binding.get("historical")) is not bool
                or binding["historical"] and binding["live_session_verified"]
                or output.get("source") != "codex_rollout" or output.get("id") != binding["session_id"]
                or not isinstance(output.get("messages"), list)):
            raise HistoryReadError("protocol_invalid")
        if self.controllers.is_controller_reference(host, binding["session_id"]):
            raise PermissionError("管理会话输出不能作为工作 agent 读取。")
        self.store.setting(self._output_key(host, agent["terminal_id"]), binding)
        return self._bound_conversation_page(output, binding, page.get("read_transport"))

    def _conversation_output(self, host, agent, view, limit, cursor=None, after_cursor=None, include_tools=False):
        key = self._output_key(host, agent["terminal_id"])
        saved = self.store.setting(key)
        try:
            if self.host(host)["transport"] == "ssh":
                return self._remote_conversation_output(host, agent, view, saved, limit, cursor, after_cursor, include_tools)
            probe = self._probe_output(host, agent, view)
            if probe.get("state") == "bound" and probe.get("binding_evidence") == "held_rollout":
                # argv's initial resume selector survives in-TUI /new /resume.
                # Only a held current rollout is direct live-session evidence.
                binding = {**probe, "terminal_id": agent["terminal_id"],
                           "live_session_verified": probe.get("binding_evidence") == "held_rollout"}
            elif saved and (probe.get("reason") == "no_exact_session_evidence" or probe.get("binding_evidence") == "explicit_resume") and probe.get("process_identity") and probe["process_identity"] == saved.get("process_identity"):
                binding = {**saved, "live_session_verified": False}
            elif probe.get("state") == "bound":
                binding = {**probe, "terminal_id": agent["terminal_id"], "live_session_verified": False}
            elif saved and (agent.get("mode") in ("closed", "closing", "close_unknown") or not view.get("connected")
                            or probe.get("reason") == "foreground_shell"):
                # Detached/closed terminals may still read their last VERIFIED
                # association, but this is explicitly historical, not live state.
                binding = {**saved, "historical": True, "live_session_verified": False}
            else:
                return {"output_binding": {"state": "unbound", "reason": probe.get("reason", "session_identity_unavailable")},
                        "messages": [], "content_complete": False,
                        "note": "尚未准确关联 Codex 会话输出。请用当前终端 /status 的 Session ID 在 UI「关联会话」。屏幕不是完整回复。"}
            if self.controllers.is_controller_reference(host, binding["session_id"]):
                raise PermissionError("管理会话输出不能作为工作 agent 读取。")
            self.store.setting(key, binding)
            output = self.history(host, {"op": "read_output", "session_id": binding["session_id"],
                "rollout_path": binding["rollout_path"], "codex_home": binding["codex_home"], "limit": limit,
                "include_tools": include_tools,
                **({"cursor": cursor} if cursor else {}), **({"after_cursor": after_cursor} if after_cursor else {})})
            # A new process or a deactivated scope during I/O must not return a
            # different session under an earlier authorization/cursor.
            if not binding.get("historical"):
                verify = self._probe_output(host, agent, view)
                if verify.get("process_identity") != binding.get("process_identity"):
                    raise ValueError("读取期间 Codex 进程已变化，未返回旧会话输出；请重新读取。")
                if verify.get("state") == "bound" and verify.get("binding_evidence") == "held_rollout":
                    if verify.get("session_id") != binding["session_id"]:
                        raise ValueError("读取期间 Codex 会话已切换，未返回旧会话输出；请重新读取。")
                    binding["live_session_verified"] = True
                elif verify.get("state") == "unbound" and verify.get("reason") != "no_exact_session_evidence":
                    raise ValueError("读取期间会话关联证据冲突或失效，未返回记录；请重新核验。")
                else:
                    binding["live_session_verified"] = False
            return self._bound_conversation_page(output, binding)
        except Exception as error:
            reason_code, reason = _safe_output_failure(error)
            if cursor or after_cursor:
                if isinstance(error, HistoryReadError) and reason_code in (
                        "ssh_handshake_failed", "ssh_timeout", "ssh_transport_unavailable",
                        "ssh_authentication_failed", "ssh_host_key_failed"):
                    raise ValueError("本次会话输出分页未读到记录，原游标未被判为失效；处理连接问题后可沿用同一游标重试："
                                     + reason + " [" + reason_code + "]") from None
                raise ValueError("会话输出分页无法继续，请重新读取最新记录：" + reason + " [" + reason_code + "]") from None
            return {"output_binding": {"state": "unavailable", "reason": reason, "reason_code": reason_code}, "messages": [],
                    "content_complete": False, "note": "真实会话输出当前不可用；以下屏幕仅作状态参考，不是完整回复。",
                    **({"read_transport": error.read_transport} if isinstance(error, HistoryReadError) and error.read_transport else {})}

    def read_agent(self, host, thread_id, limit=18, plugin=False, cursor=None, after_cursor=None, include_tools=False):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit 必须为 1 至 100 的消息数。")
        if cursor and after_cursor:
            raise ValueError("cursor 和 after_cursor 不能同时使用。")
        if type(include_tools) is not bool:
            raise ValueError("include_tools 必须为布尔值。")
        agent = self.get_agent(host, thread_id, plugin=plugin)
        route = self.host(host)
        target = route["target"] if route["transport"] == "ssh" else None
        if target is not None:
            with self._read_admission_lock:
                if target in self._reading_routes:
                    raise HistoryReadError("ssh_read_in_progress")
                self._reading_routes.add(target)
        try:
            return self._read_agent_output(host, thread_id, agent, limit, plugin, cursor, after_cursor, include_tools)
        finally:
            if target is not None:
                with self._read_admission_lock:
                    self._reading_routes.discard(target)

    def _read_agent_output(self, host, thread_id, agent, limit, plugin, cursor, after_cursor, include_tools):
        if not agent.get("terminal_id"):
            return {**agent, **self.history(host, {"op": "read", "thread_id": thread_id, "limit": limit}), "source": "persisted_history"}
        try:
            view = self.terminals.snapshot(agent["terminal_id"], min(500, max(1, int(limit)) * 8))
        except Exception:
            if agent.get("mode") not in ("closed", "closing", "close_unknown"):
                raise
            view = {"screen": "", "ansi": "", "connected": False, "output_unavailable": True}
        output = self._conversation_output(host, agent, view, limit, cursor, after_cursor, include_tools)
        # Keep the screen for foreground/input diagnosis. Semantic messages come
        # from the selected Codex rollout, never reconstructed from ANSI frames.
        result = {**agent, **view, "agent_id": agent["agent_id"], "source": "terminal",
                  "history_complete": False, **output, "conversation_status": output.get("status"),
                  "status": view.get("state", agent.get("state", "unknown")), **self._closed_status(agent, view)}
        self.get_agent(host, thread_id, plugin=plugin)  # recheck scope after I/O
        return result

    @staticmethod
    def _delivery(row):
        return {**row, "request_id": row["id"], "delivery": row["state"]}

    def _submit(self, host, thread_id, data, kind, origin, request_key, controller_id=None, expected_terminal_id=None):
        self._request_key(request_key)
        if origin not in ("ui", "plugin"):
            raise ValueError("输入来源必须为 ui 或 plugin。")
        with self.registry_lock:
            agent = self.get_agent(host, thread_id, plugin=origin == "plugin")
            if controller_id is not None:
                self._require_controller_scope(controller_id, host, thread_id)
            self._require_open(agent)
            self._require_terminal_generation(agent, expected_terminal_id, origin)
            request_id = self.store.enqueue(host, thread_id, data, origin=origin, request_key=request_key, kind=kind)
            row = self.store.delivery(request_id)
            if row["state"] not in RETRYABLE:
                return self._delivery(row)
            if not agent.get("terminal_id"):
                self.store.update_outbox(request_id, "awaiting_connection", error="请先在 UI 连接共享终端。")
                return self._delivery(self.store.delivery(request_id))
            try:
                # Last check under the same lock used by scope revocation; an
                # earlier queued/human_busy delivery is never a future grant.
                self.get_agent(host, thread_id, plugin=origin == "plugin")
                if controller_id is not None:
                    self._require_controller_scope(controller_id, host, thread_id)
                self.store.update_outbox(request_id, "submitting")
                body = {"text" if kind == "prompt" else "data": data, "origin": origin, "request_key": request_key or request_id}
                reply = self.terminals.write(agent["terminal_id"], "prompt" if kind == "prompt" else "input", body)
                state = reply.get("state", "submitted" if reply.get("accepted") else "unknown")
                self.store.update_outbox(request_id, state)
                if state == "submitted":
                    self.record_event("message_submitted" if kind == "prompt" else "terminal_input_submitted",
                                      host=host, thread_id=thread_id, request_id=request_id, origin=origin)
            except PermissionError:
                self.store.update_outbox(request_id, "scope_revoked", error="输入提交前授权已失效；未提交输入不会执行。")
            except BrokerError as error:
                state = {"human_busy": "human_busy", "waiting_for_agent": "waiting_for_agent", "agent_not_started": "waiting_for_agent",
                         "agent_not_confirmed": "waiting_for_agent", "terminal_exited": "awaiting_connection", "terminal_not_found": "awaiting_connection",
                         # This broker fence runs before PTY.write: delivery is
                         # definitely blocked, not uncertain, and the SAME key
                         # may retry after a verified geometry repair.
                         "geometry_unknown": "blocked",
                         "invalid_prompt": "not_sent", "invalid_input": "not_sent", "request_conflict": "not_sent"}.get(error.code, "unknown")
                self.store.update_outbox(request_id, state, error=str(error))
            except Exception as error:
                self.store.update_outbox(request_id, "unknown", error=str(error))
            return self._delivery(self.store.delivery(request_id))

    def send(self, host, thread_id, text, origin="ui", request_key=None, controller_id=None, expected_terminal_id=None):
        if not isinstance(text, str) or not text.strip() or len(text) > 30000 or any((ord(c) < 32 and c not in "\r\n\t") or 127 <= ord(c) <= 159 for c in text):
            raise ValueError("消息不能为空，最长 30000 字符，不接受终端控制字符。")
        return self._submit(host, thread_id, text, "prompt", origin, request_key, controller_id=controller_id, expected_terminal_id=expected_terminal_id)

    def terminal_input(self, host, thread_id, data, origin="ui", request_key=None, controller_id=None, expected_terminal_id=None):
        if not isinstance(data, str) or not data or len(data) > 30000:
            raise ValueError("终端输入不能为空，最长 30000 字符。")
        if origin == "plugin":
            if not request_key:
                raise ValueError("插件输入必须提供稳定 request_key。")
            return self._submit(host, thread_id, data, "input", origin, request_key, controller_id=controller_id, expected_terminal_id=expected_terminal_id)
        # User keystrokes can contain login secrets: forward without persisting.
        if origin != "ui":
            raise ValueError("输入来源必须为 ui 或 plugin。")
        if self.agent_id(host, thread_id) in self._reconnecting:
            raise ValueError("此终端正在核验原显示附着，请稍后输入；没有发送这些按键。")
        # No registry lock or slow process probe here: user keystrokes must reach
        # the broker during a Captain readiness probe and acquire its typing lease.
        agent = self.store.agent(host, thread_id)
        if not agent or not agent.get("terminal_id"):
            raise ValueError("请先连接共享终端。")
        self._require_open(agent)
        self._require_terminal_generation(agent, expected_terminal_id)
        return self.terminals.write(agent["terminal_id"], "input", {"data": data, "origin": "ui"})

    def terminal_output(self, host, thread_id, after=0):
        agent = self.get_agent(host, thread_id)
        try:
            output = self.terminals.output(agent["terminal_id"], after)
        except Exception:
            if agent.get("mode") not in ("closed", "closing", "close_unknown"):
                raise
            return {"data": "", "seq": max(0, int(after)), "reset": False, "output_unavailable": True, "terminal_id": agent["terminal_id"],
                    **self._closed_status(agent, {}), "note": "终端缓存暂时不可读取；登记记录仍保留，不会重新启动或连接。"}
        return {**output, "terminal_id": agent["terminal_id"], **self._closed_status(agent, output)}

    def terminal_resize(self, host, thread_id, cols=100, rows=30, expected_terminal_id=None):
        agent = self.get_agent(host, thread_id)
        self._require_open(agent)
        self._require_terminal_generation(agent, expected_terminal_id)
        return self.terminals.write(agent["terminal_id"], "resize", {"cols": cols, "rows": rows})

    def terminal_history(self, host, thread_id, lines=5000):
        """One explicit selected SSH pane read, never terminal input/attachment."""
        if type(lines) is not int or not 1 <= lines <= remote_terminal_history.MAX_LINES:
            raise ValueError("终端历史行数必须为 1 至 5000。")
        with self.registry_lock:
            agent = self.get_agent(host, thread_id)
            self._require_open(agent)
            terminal_id = agent.get("terminal_id")
            if not isinstance(terminal_id, str) or not remote_terminal_history.SESSION.fullmatch(terminal_id):
                raise ValueError("此登记没有可读取历史的共享终端。")
            config = dict(self.host(host))
            if config["transport"] != "ssh":
                raise ValueError("本机终端请使用本地滚动区查看已保留的历史；完整 Codex 输出可使用「会话输出」。")
            target = config.get("target", "")
            if (not isinstance(target, str) or not target or target.startswith("-")
                    or any(char.isspace() or ord(char) < 32 for char in target)):
                raise ValueError("SSH 目标无效，未读取终端历史。")
        source = source_path("transport", "remote_terminal_history.py").read_text(encoding="utf-8")
        try:
            reply = subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ControlMaster=no", "-o", "ControlPersist=no", "-o", "ConnectTimeout=30",
                target, "python3 - --session " + shlex.quote(terminal_id) + " --lines " + str(lines)],
                input=pack_python_source(source), text=True, encoding="utf-8", errors="replace", capture_output=True,
                timeout=45, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if not isinstance(reply.stdout, str) or len(reply.stdout.encode("utf-8")) > remote_terminal_history.MAX_RESPONSE_BYTES:
                raise ValueError()
            payloads = [line[len(remote_terminal_history.RESULT_MARKER):] for line in reply.stdout.splitlines()
                        if line.startswith(remote_terminal_history.RESULT_MARKER)]
            payload = json.loads(payloads[0]) if len(payloads) == 1 else None
            if reply.returncode or not isinstance(payload, dict) or payload.get("ok") is not True:
                reason = payload.get("reason") if isinstance(payload, dict) else None
                if reason == "identity_changed":
                    raise remote_terminal_history.HistoryError("identity_changed")
                raise ValueError()
            result = payload.get("result")
            expected_keys = {"source", "text", "lines", "line_count", "history_size", "alternate_on", "cols", "rows",
                "truncated", "history_complete", "terminal_id", "identity", "captured_at", "note"}
            if (not isinstance(result, dict) or set(result) != expected_keys
                    or result.get("source") != "tmux_pane_history" or result.get("terminal_id") != terminal_id
                    or not remote_terminal_history.valid_identity(result.get("identity"), terminal_id)
                    or type(result.get("lines")) is not int or result["lines"] != lines
                    or type(result.get("line_count")) is not int or not 0 <= result["line_count"] <= lines
                    or type(result.get("history_size")) is not int or not 0 <= result["history_size"] <= 2 ** 31 - 1
                    or type(result.get("alternate_on")) is not bool or type(result.get("truncated")) is not bool
                    or result.get("history_complete") is not False
                    or type(result.get("cols")) is not int or not 1 <= result["cols"] <= 500
                    or type(result.get("rows")) is not int or not 1 <= result["rows"] <= 200
                    or not isinstance(result.get("captured_at"), str) or len(result["captured_at"]) > 128
                    or not isinstance(result.get("text"), str)
                    or len(result["text"].encode("utf-8")) > remote_terminal_history.MAX_TEXT_BYTES
                    or any(ord(char) < 32 and char not in "\n\t" or 127 <= ord(char) <= 159 for char in result["text"])
                    or result["line_count"] != (len(result["text"].split("\n")) if result["text"] else 0)):
                raise ValueError()
        except remote_terminal_history.HistoryError:
            raise ValueError("读取期间原 tmux/pane 身份或显示状态已变化，未返回历史；请重新核对所选终端。") from None
        except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired):
            # Do not echo SSH banners, provider errors, terminal text or paths in
            # failures. No connection/auth settings or worker state are changed.
            raise ValueError("只读终端历史暂不可用；请检查 SSH 路由或原终端，或使用「会话输出」。未重连、输入或启动任何会话。") from None
        with self.registry_lock:
            current = self.get_agent(host, thread_id)
            self._require_open(current)
            if current.get("terminal_id") != terminal_id or dict(self.host(host)) != config:
                raise ValueError("读取期间所选终端登记已变化，未返回历史；请重新核对。")
        return {key: value for key, value in result.items() if key not in ("identity", "note")} | {"note": remote_terminal_history.NOTE}

    def terminal_close(self, host, thread_id, confirm=False, expected_terminal_id=None):
        if confirm is not True:
            raise ValueError("关闭会终止这个终端及其中的进程，请明确确认关闭。")
        with self.registry_lock:
            agent = self.get_agent(host, thread_id)
            self._require_terminal_generation(agent, expected_terminal_id)
            terminal_id = agent.get("terminal_id")
            if not terminal_id or not re.fullmatch(r"ceohub-[a-f0-9]{16,48}", terminal_id):
                raise ValueError("此记录没有可关闭的共享终端。")
            operation = "terminal-close:" + terminal_id
            fingerprint = hashlib.sha256(json.dumps([host, thread_id, terminal_id]).encode()).hexdigest()
            try:
                cached = self.store.begin_operation(operation, fingerprint)
            except RuntimeError:
                cached = {"closed": False, "close_state": "unknown", "state": "close_unknown", "status": "close_unknown",
                          "host": host, "thread_id": thread_id, "terminal_id": terminal_id,
                          "error": "原关闭请求结果不确定；为避免误关后续进程，不会自动重试。"}
                self.store.remember(host, thread_id, agent["alias"], active=False, mode="close_unknown")
                self.store.finish_operation(operation, cached)
            if cached is not None:
                return cached
            # Persist the closing fence before talking to the broker. Stale UI
            # keyboard/resize requests cannot operate or reconnect this ID.
            self.auto_reconnect.cancel(host, thread_id, terminal_id)
            self.store.remember(host, thread_id, agent["alias"], active=False, mode="closing")
            self.store.revoke_pending(host, thread_id, RETRYABLE)
            try:
                reply = self.terminals.write(terminal_id, "close", {})
                if reply.get("closed") is not True or reply.get("id") != terminal_id:
                    raise RuntimeError("终端服务未确认关闭精确目标。")
                self.store.remember(host, thread_id, agent["alias"], active=False, mode="closed")
                result = {"closed": True, "close_state": "closed", "state": "closed", "status": "closed", "connected": False,
                          "active": False, "host": host, "thread_id": thread_id, "terminal_id": terminal_id, "id": terminal_id}
                self.record_event("terminal_closed", host=host, thread_id=thread_id, terminal_id=terminal_id, state="closed")
            except Exception as error:
                self.store.remember(host, thread_id, agent["alias"], active=False, mode="close_unknown")
                result = {"closed": False, "close_state": "unknown", "state": "close_unknown", "status": "close_unknown",
                          "active": False, "host": host, "thread_id": thread_id, "terminal_id": terminal_id, "error": str(error)}
                self.record_event("terminal_state", host=host, thread_id=thread_id, terminal_id=terminal_id, state="close_unknown", error=str(error))
            self.store.finish_operation(operation, result)
        return result

    def create_agent(self, host, cwd, title, text="", active=True, request_key=None, origin="ui", command="", controller_id=None):
        config = self.host(host)
        if not isinstance(text, str) or len(text) > 30000 or any((ord(c) < 32 and c not in "\r\n\t") or 127 <= ord(c) <= 159 for c in text):
            raise ValueError("任务文本最长 30000 字符，不接受终端控制字符。")
        if origin not in ("ui", "plugin"):
            raise ValueError("输入来源必须为 ui 或 plugin。")
        if origin == "plugin" and host not in self.connected_hosts():
            raise PermissionError("该机器尚未在 UI 连接。")
        self._directory(config, cwd)
        if not isinstance(command, str) or len(command) > 30000 or "\0" in command:
            raise ValueError("启动指令最长 30000 字符，不能含空字符。")
        if controller_id is not None:
            if command:
                raise PermissionError("管理会话创建只打开 shell；启动命令需用户在终端自行执行。")
            self._require_creation_scope(controller_id, host, cwd)
            # Canonicalize on the selected host before creating, then check the
            # resolved path as well. A symlink/.. cannot escape granted roots.
            try:
                cwd = self.directories.list(config, cwd)["path"]
            except Exception:
                raise PermissionError("无法核验所选授权目录，未创建终端。") from None
            self._require_creation_scope(controller_id, host, cwd)
        request_key = request_key or "ui-create-" + str(uuid.uuid4())
        self._request_key(request_key)
        fingerprint = hashlib.sha256(json.dumps([host, cwd, title, text, bool(active), command, origin], ensure_ascii=False).encode()).hexdigest()
        with self.registry_lock:
            if controller_id is not None:
                self._require_creation_scope(controller_id, host, cwd)
            previous = self.store.begin_operation(request_key, fingerprint)
            if previous is None:
                try:
                    result = {"created": True, **self.create_terminal(host, cwd, title, active=active, request_key=request_key, controller_id=controller_id)}
                    if result["created"]:
                        if controller_id is not None:
                            self.controllers.add_created_agent(controller_id, host, result["thread_id"])
                        self.record_event("agent_created", host=host, thread_id=result["thread_id"], origin=origin)
                    else:
                        self.store.finish_operation(request_key, result)
                        return result
                except Exception as error:
                    result = {"created": False, "delivery": {"state": "unknown", "error": str(error)}, "request_key": request_key}
                    self.store.finish_operation(request_key, result)
                    return result
            else:
                result = previous
                if not result.get("created"):
                    return result
                self.get_agent(host, result["thread_id"], plugin=origin == "plugin")
                if controller_id is not None:
                    self._require_controller_scope(controller_id, host, result["thread_id"])
            try:
                if command:
                    result["startup_delivery"] = self._submit(host, result["thread_id"], command.rstrip("\r\n") + "\r", "input", origin, self._child_key(request_key, ":startup"), controller_id=controller_id)
                if text.strip() and (not command or result["startup_delivery"]["state"] == "submitted"):
                    result["delivery"] = self.send(host, result["thread_id"], text, origin=origin, request_key=self._child_key(request_key, ":message"), controller_id=controller_id)
                elif command:
                    result["delivery"] = result["startup_delivery"]
            except Exception as error:
                result["delivery"] = {"state": "unknown", "error": str(error)}
            self.store.finish_operation(request_key, result)
            return result

    def _require_controller_scope(self, controller_id, host, thread_id):
        row = self.store.agent(host, thread_id)
        if not self.is_managed_worker(row) or not self.controllers.authorize(controller_id, host, thread_id):
            raise PermissionError("此工作终端不在该管理会话的当前授权范围内。")
        if self.controllers.is_controller_reference(host, thread_id, cwd=row.get("cwd")):
            raise PermissionError("管理会话不能管理自身或其他管理会话。")

    def _require_creation_scope(self, controller_id, host, cwd):
        if not self.controllers.authorize_creation(controller_id, host, cwd):
            raise PermissionError("该管理会话未获得此机器和项目目录的新建授权。")
        if self.controllers.is_controller_reference(host, None, cwd=cwd):
            raise PermissionError("管理工作区不能作为工作 agent 新建。")

    @staticmethod
    def _controller_prefix(controller_id):
        return "controller:" + controller_id + ":"

    def _controller_key(self, controller_id, request_key):
        self._request_key(request_key)
        if not request_key:
            raise ValueError("管理操作必须提供稳定 request_key。")
        return self._controller_prefix(controller_id) + hashlib.sha256(request_key.encode("utf-8")).hexdigest()

    def scope_revoking(self, controller_id, removed_agent_ids):
        """Called under registry_lock; revoke only this controller's pending work."""
        prefix = self._controller_prefix(controller_id)
        with self.registry_lock, self.store.lock, self.store.db:
            for agent_id in removed_agent_ids:
                host, thread_id = self.split_agent_id(agent_id)
                placeholders = ",".join("?" for _ in RETRYABLE)
                self.store.db.execute("update outbox set state='scope_revoked',error=? where host=? and thread_id=? "
                    "and origin='plugin' and substr(request_key,1,?)=? and state in (" + placeholders + ")",
                    ("已撤销此管理会话的授权；未提交输入不会执行。", host, thread_id, len(prefix), prefix, *RETRYABLE))

    def plugin_events(self, after=0, timeout_seconds=0, controller_id=None):
        deadline, cursor = time.monotonic() + min(30, max(0, float(timeout_seconds))), max(0, int(after))
        while True:
            selected = []
            for event in self.store.events(cursor, kinds=MANAGEMENT_EVENTS):
                cursor = event["id"]
                data = event["data"]
                agent = self.store.agent(data.get("host"), data.get("thread_id"))
                if self.is_managed_worker(agent) and (controller_id is None or self.controllers.authorize(controller_id, agent["host"], agent["thread_id"])):
                    selected.append(event)
            if selected or time.monotonic() >= deadline:
                return {"events": selected, "cursor": cursor}
            with self.event_condition:
                self.event_condition.wait(min(1, max(0, deadline - time.monotonic())))

    def plugin_call(self, tool, arguments, controller_id=None):
        if not isinstance(arguments, dict):
            raise ValueError("工具参数必须为对象。")
        if controller_id is not None:
            # Authoritative identity comes from HTTP authentication, never from
            # arguments or a descriptor ID repeated by the model.
            context = self.controllers.view_context(controller_id)
            if tool != "get_controller_context":
                controller = context.get("controller", {})
                if (controller.get("state") in ("prepared", "bootstrapping", "initializing", "attaching", "unknown", "closed")
                        or not controller.get("session_id") or not controller.get("tools_ready")):
                    raise PermissionError("管理初始化仅允许核验自身工具；会话身份与工具就绪后才能访问工作 agent。")
        if tool == "get_controller_context":
            if controller_id is None:
                return {"mode": "legacy", "controller_id": None, "legacy_scope_shared": True,
                        "management_policy": "connected_workers", "worker_access": "automatic",
                        "effective_worker_count": len(self.managed_worker_rows()),
                        "scope_note": "旧插件管理全部已登记的共享工作终端，不是独立管理会话身份。"}
            self.controllers.mark_tool_seen(controller_id)
            return self.controllers.view_context(controller_id)
        if tool == "list_active_agents":
            if controller_id is None:
                agents = self.attached_agents(active_only=True)
            else:
                with self.registry_lock:
                    sessions = self.terminals.sessions()
                    agents = []
                    for agent_id in self.controllers.allowed_agents(controller_id):
                        host, thread_id = self.split_agent_id(agent_id)
                        row = self.store.agent(host, thread_id)
                        if self.is_managed_worker(row) and self.controllers.authorize(controller_id, host, thread_id):
                            agents.append(self.get_agent(host, thread_id, plugin=True, sessions=sessions))
            connected = self.connected_hosts(agents)
            if controller_id is not None:
                connected &= set(self.controllers.allowed_hosts(controller_id)) | {a["host"] for a in agents}
            return {"agents": agents, "controller": "current_codex_conversation", "management_policy": "connected_workers",
                    "manager_role_policy": public_management_policy(include_instructions=False),
                    "worker_access": "automatic", "effective_worker_count": len(agents),
                    "connected_hosts": sorted(connected), "legacy_scope_shared": controller_id is None,
                    "scope_note": "已连接登记的工作终端自动纳入管理；包含暂时断线的原终端，不扫描机器或全部历史。"}
        if tool == "poll_events":
            return self.plugin_events(arguments.get("after", 0), arguments.get("timeout_seconds", 0), controller_id=controller_id)
        if tool == "create_agent":
            if not arguments.get("request_key"):
                raise ValueError("创建必须提供稳定 request_key。")
            return self.create_agent(arguments["host"], arguments["cwd"], arguments["title"], arguments.get("text", ""),
                active=True, request_key=self._controller_key(controller_id, arguments["request_key"]) if controller_id is not None else arguments["request_key"],
                origin="plugin", command=arguments.get("command", ""), controller_id=controller_id)
        if tool == "get_delivery":
            row = self.store.delivery(arguments["request_id"])
            if not row:
                raise ValueError("没有此送达记录。")
            self.get_agent(row["host"], row["thread_id"], plugin=True)
            if controller_id is not None:
                self._require_controller_scope(controller_id, row["host"], row["thread_id"])
                if row["origin"] != "plugin" or not str(row.get("request_key") or "").startswith(self._controller_prefix(controller_id)):
                    raise PermissionError("此送达记录不属于当前管理会话。")
            return self._delivery(row)
        host, thread_id = self.split_agent_id(arguments["agent_id"])
        if tool == "reopen_agent":
            if not arguments.get("request_key"):
                raise ValueError("重新打开必须提供稳定 request_key。")
            return self.reopen_terminal(host, thread_id,
                self._controller_key(controller_id, arguments["request_key"]) if controller_id is not None else arguments["request_key"],
                arguments.get("expected_terminal_id"), confirm=arguments.get("confirmed", False), origin="plugin", controller_id=controller_id)
        if controller_id is not None:
            self._require_controller_scope(controller_id, host, thread_id)
        if tool == "get_agent":
            result = self.get_agent(host, thread_id, plugin=True)
            if controller_id is not None:
                self._require_controller_scope(controller_id, host, thread_id)
            return result
        if tool == "read_agent":
            result = self.read_agent(host, thread_id, arguments.get("limit", 18), plugin=True,
                                   cursor=arguments.get("cursor"), after_cursor=arguments.get("after_cursor"),
                                   include_tools=arguments.get("include_tools", False))
            if controller_id is not None:
                self._require_controller_scope(controller_id, host, thread_id)
            return result
        if tool in ("update_agent", "reconnect_agent", "set_auto_reconnect"):
            if not arguments.get("request_key"):
                raise ValueError("管理操作必须提供稳定 request_key。")
            key = self._controller_key(controller_id, arguments["request_key"]) if controller_id is not None else arguments["request_key"]
            if tool == "set_auto_reconnect":
                return self.set_auto_reconnect(host, thread_id, arguments.get("enabled"),
                    arguments.get("expected_terminal_id"), key, origin="plugin", controller_id=controller_id)
            if tool == "update_agent":
                return self.update_agent(host, thread_id, alias=arguments.get("alias"), pinned=arguments.get("pinned"),
                    origin="plugin", request_key=key, controller_id=controller_id)
            return self.reconnect_agent(host, thread_id, origin="plugin", request_key=key, controller_id=controller_id)
        if tool in ("send_agent_message", "send_terminal_input"):
            if not arguments.get("request_key"):
                raise ValueError("发送必须提供稳定 request_key。")
            if tool == "send_agent_message":
                return self.send(host, thread_id, arguments["text"], origin="plugin", controller_id=controller_id,
                    request_key=self._controller_key(controller_id, arguments["request_key"]) if controller_id is not None else arguments["request_key"])
            return self.terminal_input(host, thread_id, arguments["data"], origin="plugin", controller_id=controller_id,
                request_key=self._controller_key(controller_id, arguments["request_key"]) if controller_id is not None else arguments["request_key"])
        raise ValueError("Unsupported plugin tool")

    def _watch_terminals(self):
        seen = {}
        while not self._stop.wait(1.5):
            try:
                with self.registry_lock:
                    if self.stopping:
                        return
                    sessions = {s["id"]: s for s in self.terminals.sessions()}
                    for row in self.store.agents():
                        view = sessions.get(row.get("terminal_id"))
                        key = (row["host"], row["thread_id"])
                        if not view and key not in seen:
                            continue
                        status = self._closed_status(row, view or {})
                        now = (status.get("connected", view.get("connected")), status.get("state", view.get("state")), view.get("command"), view.get("current_seq")) if view else (False, status.get("state", "not_connected"), "", seen[key][3])
                        old = seen.get(key)
                        if now != old:
                            self.record_event("terminal_output" if old and now[:3] == old[:3] else "terminal_state", host=key[0], thread_id=key[1],
                                terminal_id=row["terminal_id"], connected=now[0], state=now[1], command=now[2], current_seq=now[3])
                        seen[key] = now
            except Exception:
                continue  # Broker absence is normal; never launch or reconnect it.

    def state(self, after=0):
        agents = self.attached_agents()
        connected = self.connected_hosts(agents)
        events = self.store.recent_events(kinds=MANAGEMENT_EVENTS) if int(after) == 0 else self.store.events(after, kinds=MANAGEMENT_EVENTS)
        # A background SSH attach holds registry_lock across network I/O. The
        # workbench only needs a metadata snapshot and must stay responsive.
        # Host entries are replaced under this short lock, never edited in place.
        with self._host_latency_lock:
            hosts = [(host, dict(config), dict(self.connections.get(host, {}))) for host, config in self.hosts.items()]
        return {"version": VERSION, "controller": "codex-plugin", "legacy_scope_shared": True,
                "management_policy": "connected_workers", "worker_access": "automatic",
                "effective_worker_count": sum(bool(agent.get("active")) for agent in agents),
                "hosts": [{"id": host, **config, **connection, "connected": host in connected,
                           "latency": self._host_latency_view(host, config)} for host, config, connection in hosts],
                "agents": agents, "events": events, "cursor": self.store.event_cursor() if int(after) == 0 else (events[-1]["id"] if events else int(after)),
                "outbox": self.store.outbox(), "approvals": []}

    def close(self):
        if self.stopping:
            return
        self.stopping = True
        self._stop.set()
        self.auto_reconnect.stop()
        if self._auto_reconnect_thread is not None:
            self._auto_reconnect_thread.join(timeout=5)
        self._watcher.join(timeout=5)
        self.controllers.shutdown()
        with self.registry_lock:
            self.store.close()
        # Deliberately leave independent broker/PTY/tmux processes running.
