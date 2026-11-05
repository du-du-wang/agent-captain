"""Opt-in recovery of exact enrolled SSH displays, without model/task input.

The ten-minute schedule lives in the Hub, not a browser timer. A pending or
unknown attachment is a fence, never evidence that another attach is safe.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import uuid


INTERVAL_SECONDS = 600
_STATES = frozenset({"off", "connected", "waiting", "reconnecting", "needs_attention"})
_SUCCESS = frozenset({"reconnected", "already_connected", "awaiting_foreground"})
_HARD_RECOVERY = frozenset({
    "reconnect_unknown", "original_session_missing", "original_session_changed", "ssh_auth_required",
    "ssh_host_key_required", "missing_terminal", "closed", "local_display_unavailable",
})
_MESSAGES = {
    "reconnected": "已自动接回原终端。",
    "already_connected": "原终端显示连接仍在线。",
    "awaiting_foreground": "原终端已接回，前台状态待核验。",
    "route_unavailable": "SSH 路由暂不可达，十分钟后按原请求重试。",
    "reconnect_unknown": "上次重连结果待核对，自动重连已暂停。",
    "original_session_missing": "原终端已不存在，请手动选择恢复方式。",
    "original_session_changed": "原终端身份已变化，自动重连已暂停。",
    "ssh_auth_required": "SSH 认证需要人工处理，自动重连已暂停。",
    "ssh_host_key_required": "SSH 主机密钥需要人工核验，自动重连已暂停。",
    "display_service_unavailable": "本机终端显示服务不可用，请恢复服务并手动接回原终端，再继续自动重连。",
    "local_display_unavailable": "本机终端显示服务不可用，自动重连已暂停。",
    "human_input_pending": "终端有未提交输入，本轮不接回；十分钟后再检查。",
    "missing_terminal": "没有可接回的原终端，请手动处理。",
    "closed": "终端已关闭。",
}


def _timestamp(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _public_result(code):
    code = code if isinstance(code, str) and code in _MESSAGES else "reconnect_unknown"
    return {"code": code, "message": _MESSAGES[code]}


class AutoReconnect:
    def __init__(self, hub, *, clock=time.time):
        self.hub = hub
        self.clock = clock
        self._lock = threading.RLock()
        self._tick_lock = threading.Lock()
        self._stop = threading.Event()

    @staticmethod
    def disabled_view():
        return {"enabled": False, "interval_seconds": INTERVAL_SECONDS, "status": "off",
                "next_attempt_at": None, "last_attempt_at": None, "last_result": None}

    @staticmethod
    def _key(host, thread_id):
        return "auto-reconnect:" + hashlib.sha256(json.dumps([host, thread_id]).encode()).hexdigest()

    def _stopping(self):
        return self._stop.is_set() or self.hub.stopping

    def _load(self, host, thread_id):
        value = self.hub.store.setting(self._key(host, thread_id))
        return value if isinstance(value, dict) else {}

    def _save(self, host, thread_id, value):
        self.hub.store.setting(self._key(host, thread_id), value)

    def _cas(self, host, thread_id, before, after):
        """Cross-process claim/result fence; no database lock spans SSH."""
        with self.hub.store.lock, self.hub.store.db:
            return self.hub.store.db.execute("update settings set value=? where key=? and value=?",
                (json.dumps(after), self._key(host, thread_id), json.dumps(before))).rowcount == 1

    def _eligible(self, agent):
        try:
            return (self.hub.is_managed_worker(agent)
                    and self.hub.host(agent["host"]).get("transport") == "ssh")
        except (KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def _matches(value, agent):
        return (value.get("enabled") is True
                and isinstance(agent.get("terminal_id"), str)
                and value.get("terminal_id") == agent["terminal_id"])

    def view(self, agent, runtime_connected=None):
        """Cheap read-only presentation; no SSH, owner launch or schedule write."""
        value = self._load(agent["host"], agent["thread_id"])
        if not self._matches(value, agent) or not self._eligible(agent):
            return self.disabled_view()
        state = value.get("status")
        state = state if state in _STATES and state != "off" else "needs_attention"
        connected = agent.get("connected") if runtime_connected is None else runtime_connected
        if connected is True and state not in ("reconnecting", "needs_attention"):
            state = "connected"
        elif connected is False and state == "connected":
            state = "waiting"
        result = value.get("last_result")
        return {"enabled": True, "interval_seconds": INTERVAL_SECONDS, "status": state,
                "next_attempt_at": None if state in ("connected", "needs_attention") else _timestamp(value.get("next_attempt_at")),
                "last_attempt_at": _timestamp(value.get("last_attempt_at")),
                "last_result": _public_result(result.get("code")) if isinstance(result, dict) else None}

    def configure(self, host, thread_id, enabled, expected_terminal_id, request_key):
        """The Hub wrapper owns permissions and durable metadata idempotence.

        Do not acquire registry_lock here: turning the switch off must remain
        possible while an already accepted canonical reconnect holds that lock.
        """
        if type(enabled) is not bool:
            raise ValueError("自动重连开关必须为布尔值。")
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 200:
            raise ValueError("自动重连设置需要稳定 request_key。")
        with self._lock:
            if self._stopping():
                raise ValueError("后台服务正在关闭，未更改自动重连设置。")
            agent = self.hub.get_agent(host, thread_id)
            if agent.get("terminal_id") != expected_terminal_id:
                raise ValueError("终端已切换，请刷新后重新设置自动重连。")
            if enabled and not self._eligible(agent):
                raise ValueError("自动重连只支持未关闭的 SSH 工作终端。")
            old = self._load(host, thread_id)
            same_generation = old.get("terminal_id") == expected_terminal_id
            pending_key = (old.get("uncertain_request_key") or
                           (old.get("request_key") if old.get("status") == "reconnecting" else None)) if same_generation else None
            connected = agent.get("connected") is True
            value = {"enabled": enabled, "terminal_id": expected_terminal_id,
                     "revision": uuid.uuid4().hex, "config_request_key": request_key,
                     "status": "connected" if enabled and connected else "waiting" if enabled else "off",
                     "next_attempt_at": self.clock() + INTERVAL_SECONDS if enabled and not connected else None,
                     "last_attempt_at": old.get("last_attempt_at"), "last_result": old.get("last_result"),
                     "request_key": old.get("request_key") if same_generation else None,
                     "uncertain_request_key": pending_key, "paused": False}
            if pending_key:
                receipt = self._known_receipt(host, thread_id, value, pending_key)
                if receipt is not None and self._safe_receipt(receipt, pending_key):
                    value["uncertain_request_key"] = None
                    value["request_key"] = pending_key if self._retryable_route(receipt, pending_key) else None
                elif enabled:
                    value.update(status="needs_attention", next_attempt_at=None, paused=True,
                                 last_result=_public_result("reconnect_unknown"))
            # Enabling a schedule never overrides an existing uncertain attach.
            if enabled and agent.get("recovery", {}).get("code") == "reconnect_unknown":
                value.update(status="needs_attention", next_attempt_at=None, paused=True,
                             last_result=_public_result("reconnect_unknown"))
            self._save(host, thread_id, value)
            return self.view(agent)

    def cancel(self, host, thread_id, terminal_id):
        """Retire only the opted-in generation being closed or replaced."""
        with self._lock:
            if self._stopping():
                return
            for _ in range(2):
                value = self._load(host, thread_id)
                if value.get("terminal_id") != terminal_id:
                    return
                if self._cas(host, thread_id, value, {**value, "enabled": False, "status": "off", "next_attempt_at": None,
                            "request_key": None, "uncertain_request_key": None, "revision": uuid.uuid4().hex}):
                    return

    def _pause(self, host, thread_id, value, code):
        before = dict(value)
        value.update(status="needs_attention", next_attempt_at=None, paused=True, last_result=_public_result(code))
        self._cas(host, thread_id, before, value)

    def _sessions(self):
        reply = self.hub.terminals.request("/sessions", timeout=3)
        records = reply.get("sessions") if isinstance(reply, dict) else None
        if (not isinstance(records, list)
                or any(not isinstance(row, dict) or not isinstance(row.get("id"), str)
                       or type(row.get("connected")) is not bool for row in records)
                or len({row["id"] for row in records}) != len(records)):
            raise ValueError("Invalid display registry")
        return records

    @staticmethod
    def _retryable_route(result, request_key):
        if not isinstance(result, dict) or not isinstance(result.get("recovery"), dict):
            return False
        recovery = result.get("recovery", {})
        # This exact pre-attach receipt is emitted before create(). A generic
        # route error after attachment is not permission for another attach.
        return (result.get("operation_state", result.get("state")) == "blocked"
                and recovery.get("code") == "route_unavailable" and recovery.get("retryable") is True
                and recovery.get("request_key") == request_key and recovery.get("request_origin") == "ui"
                and "owner_stamp" not in result
                and recovery.get("next_action") == "check_connection"
                and recovery.get("message") == "SSH 路由暂不可达；请检查机器或恢复 SSH 上游/隧道路由，再由用户重试。不会自动重启服务。")

    def _apply_outcome(self, host, thread_id, value, result):
        before = dict(value)
        result = result if isinstance(result, dict) else {}
        key = value.get("request_key")
        recovery = result.get("recovery")
        recovery = recovery if isinstance(recovery, dict) else {}
        code = recovery.get("code", "reconnect_unknown")
        code = code if isinstance(code, str) else "reconnect_unknown"
        state = result.get("operation_state", result.get("state")) if isinstance(result, dict) else "unknown"
        value["last_result"] = _public_result(code)
        if state == "done" and code in _SUCCESS:
            value.update(status="connected", next_attempt_at=None, request_key=None, uncertain_request_key=None, paused=False)
        elif self._retryable_route(result, key):
            value.update(status="waiting", paused=False, uncertain_request_key=None)
        else:
            value.update(status="needs_attention", next_attempt_at=None, paused=True)
            if self._safe_receipt(result, key):
                value["uncertain_request_key"] = None
        self._cas(host, thread_id, before, value)

    @staticmethod
    def _safe_receipt(result, key):
        state = result.get("operation_state", result.get("state"))
        recovery = result.get("recovery")
        if not isinstance(recovery, dict) or not isinstance(recovery.get("code"), str):
            return False
        code = recovery["code"]
        return ((state == "done" and code in _SUCCESS)
                or AutoReconnect._retryable_route(result, key)
                or state == "blocked" and "owner_stamp" not in result and code in {
                    "ssh_auth_required", "ssh_host_key_required", "original_session_missing", "missing_terminal",
                    "closed", "local_display_unavailable", "local_reconnect_unsupported"})

    def _known_receipt(self, host, thread_id, value, key):
        if not isinstance(key, str) or not re.fullmatch(r"auto-reconnect-[a-f0-9]{32}", key):
            return None
        expected = self.hub._reconnect_fingerprint(host, thread_id, value["terminal_id"], "ui", None)
        with self.hub.store.lock:
            row = self.hub.store.db.execute("select fingerprint,state,result from operations where request_key=?",
                                            ("agent-reconnect:" + key,)).fetchone()
        try:
            result = json.loads(row["result"]) if row and row["state"] == "done" and row["fingerprint"] == expected else None
        except (ValueError, TypeError):
            result = None
        return result if (isinstance(result, dict) and isinstance(result.get("recovery"), dict)
                          and isinstance(result["recovery"].get("code"), str)) else None

    def _recover_claim(self, host, thread_id, value):
        """Read a crash receipt, without submitting or replaying any action."""
        key = value.get("uncertain_request_key") or value.get("request_key")
        if not value.get("uncertain_request_key"):
            before = dict(value)
            value["uncertain_request_key"] = key or "unresolved"
            if not self._cas(host, thread_id, before, value):
                return
        result = self._known_receipt(host, thread_id, value, key)
        if isinstance(result, dict):
            self._apply_outcome(host, thread_id, value, result)
        else:
            self._pause(host, thread_id, value, "reconnect_unknown")

    def _observe(self, row, records, now):
        host, thread_id = row["host"], row["thread_id"]
        with self._lock:
            if self._stopping():
                return False
            current = self.hub.store.agent(host, thread_id)
            value = self._load(host, thread_id)
            if not current or not self._matches(value, current) or not self._eligible(current):
                if value.get("enabled"):
                    self.cancel(host, thread_id, value.get("terminal_id"))
                return False
            agent = self.hub.get_agent(host, thread_id, sessions=records)
            if value.get("status") == "reconnecting" or value.get("uncertain_request_key"):
                self._recover_claim(host, thread_id, value)
                value = self._load(host, thread_id)
            if not self._matches(value, current):
                return False
            if value.get("uncertain_request_key"):
                return False
            if agent.get("recovery", {}).get("code") == "reconnect_unknown":
                self._pause(host, thread_id, value, "reconnect_unknown")
                return False
            connected = agent.get("connected")
            if type(connected) is not bool:
                self._pause(host, thread_id, value, "display_service_unavailable")
                return False
            if connected:
                if (value.get("paused") and agent.get("recovery", {}).get("code") not in _SUCCESS):
                    return False
                before = dict(value)
                value.update(status="connected", next_attempt_at=None, paused=False, request_key=None)
                self._cas(host, thread_id, before, value)
                return False
            if value.get("paused"):
                return False
            if agent.get("recovery", {}).get("code") in _HARD_RECOVERY:
                self._pause(host, thread_id, value, agent["recovery"]["code"])
                return False
            due = _timestamp(value.get("next_attempt_at"))
            if due is None:
                before = dict(value)
                value.update(status="waiting", next_attempt_at=now + INTERVAL_SECONDS)
                self._cas(host, thread_id, before, value)
                return False
            return now >= due

    def _attempt(self, row, now):
        host, thread_id = row["host"], row["thread_id"]
        # This is the same lock the canonical reconnect/close/reopen uses.
        # After waiting for it, re-read the switch and generation before claim.
        with self.hub.registry_lock:
            with self._lock:
                if self._stopping():
                    return
                now = self.clock()
                current = self.hub.get_agent(host, thread_id)
                value = self._load(host, thread_id)
                due = _timestamp(value.get("next_attempt_at"))
                if (not self._eligible(current) or not self._matches(value, current) or value.get("paused") or value.get("uncertain_request_key")
                        or due is None or now < due or current.get("connected")):
                    return
                if current.get("recovery", {}).get("code") in _HARD_RECOVERY:
                    self._pause(host, thread_id, value, current["recovery"]["code"])
                    return
                if current.get("human_busy"):
                    before = dict(value)
                    value.update(status="waiting", next_attempt_at=now + INTERVAL_SECONDS,
                                 last_result=_public_result("human_input_pending"))
                    self._cas(host, thread_id, before, value)
                    return
                key = value.get("request_key")
                if key is not None and (not isinstance(key, str) or not re.fullmatch(r"auto-reconnect-[a-f0-9]{32}", key)):
                    self._pause(host, thread_id, value, "reconnect_unknown")
                    return
                before = dict(value)
                key = key or "auto-reconnect-" + uuid.uuid4().hex
                value.update(request_key=key, uncertain_request_key=key, status="reconnecting",
                             last_attempt_at=now, next_attempt_at=now + INTERVAL_SECONDS)
                if not self._cas(host, thread_id, before, value):
                    return
                claim = dict(value)
            # Configuration and cancellation do not hold registry_lock, so a
            # disable remains immediate while this accepted action finishes.
            try:
                result = self.hub.reconnect_agent(host, thread_id, origin="ui", request_key=claim["request_key"])
            except Exception:
                result = {"operation_state": "unknown", "recovery": {"code": "reconnect_unknown"}}
            with self._lock:
                if self._stopping():
                    return
                current = self.hub.store.agent(host, thread_id)
                latest = self._load(host, thread_id)
                if (not current or not self._matches(latest, current)
                        or latest.get("revision") != claim.get("revision")
                        or latest.get("request_key") != claim["request_key"]):
                    return
                self._apply_outcome(host, thread_id, latest, result)

    def tick(self):
        """One bounded pass; one authenticated cached registry read, no scans."""
        if self._stopping() or not self._tick_lock.acquire(blocking=False):
            return
        try:
            rows = []
            for row in self.hub.store.agents():
                value = self._load(row["host"], row["thread_id"])
                if value.get("enabled") is not True:
                    continue
                if not self._matches(value, row) or not self._eligible(row):
                    self.cancel(row["host"], row["thread_id"], value.get("terminal_id"))
                else:
                    rows.append(row)
            if not rows:
                return
            try:
                records = self._sessions()
            except Exception:
                with self._lock:
                    if self._stopping():
                        return
                    for row in rows:
                        value = self._load(row["host"], row["thread_id"])
                        if self._matches(value, row):
                            self._pause(row["host"], row["thread_id"], value, "display_service_unavailable")
                return
            for row in rows:
                if self._stopping():
                    break
                now = self.clock()
                if self._observe(row, records, now):
                    self._attempt(row, now)
        finally:
            self._tick_lock.release()

    def run(self):
        while not self._stop.wait(5):
            if self.hub.stopping:
                break
            try:
                self.tick()
            except Exception:
                # A local observation failure never starts another service or
                # emits arbitrary transport diagnostics into public metadata.
                continue

    def stop(self):
        self._stop.set()
