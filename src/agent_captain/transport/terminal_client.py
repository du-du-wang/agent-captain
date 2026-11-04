"""Private loopback client and lazy launcher for the independent PTY owner."""
from __future__ import annotations

import json
import errno
import hashlib
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse, quote
import uuid
from agent_captain.infrastructure.process_identity import broker_owner_state, observe_process, valid_owner_identity
from agent_captain.infrastructure.background_process import BackgroundLaunchUnknown, background_environment, launch_background

from agent_captain.paths import ROOT
BROKER_WIRE_VERSION = "0.3.0"
# An SSH foreground check can take 150 seconds on a relayed route. Snapshots,
# output and raw input wait for one check. Each of a prompt's two fresh checks
# can first join a passive check already in progress (up to four checks total).
# Keep the loopback request alive long enough to receive the broker's result
# instead of abandoning a still-running check or an uncertain input action.
SSH_CONTROL_TIMEOUT = 150
SSH_CONTROL_CONNECT_TIMEOUT = 60
FOREGROUND_RPC_TIMEOUT = SSH_CONTROL_TIMEOUT + 15
PROMPT_RPC_TIMEOUT = 4 * SSH_CONTROL_TIMEOUT + 30


def broker_script(identity=None):
    """Allow only this checkout's canonical and migrated legacy entrypoints."""
    canonical = ROOT / "terminal" / "terminal_broker.cjs"
    if identity is not None:
        for script in (canonical, ROOT / "terminal_broker.cjs"):
            if valid_owner_identity(identity, script):
                return script
        raise ValueError("Invalid terminal broker owner identity")
    return canonical


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Terminal broker redirects are not allowed")


class BrokerError(RuntimeError):
    def __init__(self, message, code=None, status=None):
        super().__init__(message)
        self.code, self.status = code, status


def legacy_descriptor_path():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CEOHub/terminal-broker.json"
    return Path.home() / ".local/state/ceo-hub/terminal-broker.json"


def worker_reference_path():
    return ROOT / ".runtime" / "worker-terminal-reference.json"


def descriptor_path():
    override = os.environ.get("CEO_HUB_TERMINAL_FILE")
    if override:
        return Path(override).expanduser()
    reference = worker_reference_path()
    if not os.path.lexists(reference):
        return legacy_descriptor_path()
    # A small, private pointer preserves the existing PTY owner; it contains no
    # bearer token. Invalid pinned references must never silently choose another
    # user's/namespace's descriptor or manufacture a replacement owner.
    try:
        with reference.open("rb") as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise ValueError()
        value = json.loads(raw)
        pointer = value.get("descriptor_file") if isinstance(value, dict) else None
        if (not isinstance(value, dict) or set(value) != {"descriptor_file"}
                or not isinstance(pointer, str) or not pointer or len(pointer) > 4096
                or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in pointer)
                or pointer.startswith(("\\\\", "//"))):
            raise ValueError()
        target = Path(pointer)
        if not target.is_absolute() or not target.is_file() or target == reference:
            raise ValueError()
        return target
    except (OSError, ValueError, TypeError, UnicodeError):
        raise ValueError("Invalid pinned worker terminal reference; repair the private reference without restarting work terminals") from None


class TerminalClient:
    def __init__(self, descriptor_file=None, runtime=None, launch_env=None):
        self.lock = threading.RLock()
        self.descriptor_file = Path(descriptor_file) if descriptor_file is not None else None
        self.runtime = Path(runtime) if runtime is not None else ROOT / ".runtime"
        # Optional manager-only launch context. Laziness keeps metadata reads
        # free of registry/network activity; worker clients retain the original
        # plain environment-copy behavior.
        self.launch_env = launch_env
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    @staticmethod
    def _read_descriptor_file(filename):
        with Path(filename).open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("Invalid terminal descriptor size")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Invalid local terminal broker descriptor")
        url = data.get("url", "")
        if not isinstance(url, str) or len(url) > 4096 or any(char.isspace() for char in url):
            raise ValueError("Invalid local terminal broker descriptor")
        parsed = urlparse(url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("Invalid local terminal broker descriptor")
        if not isinstance(data.get("token"), str) or not 16 <= len(data["token"]) <= 4096 or any(ord(char) < 33 or ord(char) > 126 for char in data["token"]):
            raise ValueError("Invalid terminal capability")
        if type(data.get("pid")) is not int or not 0 < data["pid"] <= 2147483647:
            raise ValueError("Invalid local terminal broker identity")
        generation = data.get("recovery_id")
        if generation is not None and (not isinstance(generation, str) or len(generation) != 32
                                       or any(char not in "0123456789abcdef" for char in generation)):
            raise ValueError("Invalid terminal broker recovery generation")
        if "owner_identity" in data:
            if not valid_owner_identity(data["owner_identity"], broker_script(data["owner_identity"])):
                raise ValueError("Invalid terminal broker process birth identity")
        return data

    def descriptor(self):
        return self._read_descriptor_file(self.descriptor_file or descriptor_path())

    def _anchor_default_worker(self, chosen):
        if self.descriptor_file is not None or os.environ.get("CEO_HUB_TERMINAL_FILE"):
            return
        try:
            physical = Path(chosen).resolve(strict=True)
            self._read_descriptor_file(physical)
            reference = worker_reference_path()
            if os.path.lexists(reference):
                if descriptor_path().resolve(strict=True) != physical:
                    raise ValueError()
                return
            reference.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                with reference.open("x", encoding="utf-8", newline="\n") as stream:
                    json.dump({"descriptor_file": str(physical)}, stream)
                    stream.write("\n")
                reference.chmod(0o600)
            except FileExistsError:
                # Another verified workflow may have established the same owner.
                # Never replace an unknown or competing reference.
                if descriptor_path().resolve(strict=True) != physical:
                    raise ValueError()
        except (OSError, ValueError):
            raise BrokerError("Could not safely anchor the verified work-terminal owner; inspect and repair the private reference. Existing references and work terminals were not replaced.",
                              "bound_transport_unavailable") from None

    def request(self, path, body=None, timeout=20):
        data = self.descriptor()
        headers = {"Authorization": "Bearer " + data["token"]}
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            with self.opener.open(urllib.request.Request(data["url"].rstrip("/") + path, data=payload, headers=headers), timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            try:
                reply = json.loads(error.read())
            except Exception:
                reply = {}
            raise BrokerError(reply.get("message", "Terminal broker request failed"), reply.get("error"), error.code) from None

    def running(self):
        try:
            data = self.descriptor()
            health = self.request("/health", timeout=2)
            return health.get("pid") == data.get("pid") and health.get("version") == "0.3.0"
        except Exception:
            return False

    @staticmethod
    def _pid_state(pid):
        """Only affirmative OS evidence establishes death; access errors do not."""
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.GetExitCodeProcess.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return "dead" if ctypes.get_last_error() == 87 else "unknown"
            try:
                exit_code = wintypes.DWORD()
                if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return "unknown"
                return "alive" if exit_code.value == 259 else "dead"
            finally:
                kernel.CloseHandle(handle)
        try:
            os.kill(pid, 0)
            return "alive"
        except ProcessLookupError:
            return "dead"
        except PermissionError:
            return "alive"
        except OSError:
            return "unknown"

    @staticmethod
    def _port_state(url):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                # Windows loopback refusal can arrive after about two seconds.
                # Wait for that affirmative result; timeouts remain unknown.
                probe.settimeout(4)
                result = probe.connect_ex(("127.0.0.1", urlparse(url).port))
            if result == 0:
                return "open"
            return "refused" if result in (errno.ECONNREFUSED, 10061) else "unknown"
        except (OSError, ValueError):
            return "unknown"

    def _owner_state(self, data):
        state = self._pid_state(data["pid"])
        if state != "alive":
            return state
        return broker_owner_state(data["pid"], data.get("owner_identity"), broker_script(data.get("owner_identity")),
                                  observation=observe_process(data["pid"]))

    @staticmethod
    def _lock_owner(raw):
        try:
            legacy = int(raw.decode("ascii"))
            if raw == str(legacy).encode("ascii") and 0 < legacy <= 2147483647:
                return {"pid": legacy}
        except (ValueError, UnicodeError):
            pass
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {"pid", "owner_identity"}
                or type(value["pid"]) is not int or not 0 < value["pid"] <= 2147483647
                or not valid_owner_identity(value["owner_identity"], broker_script(value["owner_identity"]))):
            raise ValueError("Invalid broker lease owner")
        return value

    @staticmethod
    def _snapshot_file(filename, limit):
        filename = Path(filename)
        physical = filename.resolve(strict=True)
        # Maintenance must not replace a descriptor through a changed link.
        if physical != filename.absolute() or filename.is_symlink():
            raise ValueError("Linked recovery files require manual inspection")
        before = filename.stat()
        with filename.open("rb") as stream:
            raw = stream.read(limit + 1)
            opened = os.fstat(stream.fileno())
        after = filename.stat()
        # CPython on Windows reports ctime differently for stat(path) and
        # fstat(handle). Compare identity/content timestamps across both, and
        # compare ctime only between the two path stats; never weaken inode/hash
        # or subsequent snapshot checks to paper over this platform difference.
        core = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
        identity = lambda value: core(value) + (value.st_ctime_ns,)
        if (len(raw) > limit or core(before) != core(opened) or core(opened) != core(after)
                or before.st_ctime_ns != after.st_ctime_ns):
            raise ValueError("Recovery files changed during validation")
        return {"path": physical, "raw": raw, "identity": identity(after),
                "sha256": hashlib.sha256(raw).hexdigest()}

    @classmethod
    def _same_snapshot(cls, previous):
        try:
            current = cls._snapshot_file(previous["path"], len(previous["raw"]))
            return current == previous
        except (OSError, ValueError):
            return False

    @staticmethod
    def _lifecycle(state, code, data=None, **changes):
        # No paths, bearer tokens, hashes, raw exceptions, or child environments
        # cross the web/MCP boundary. This is service lifecycle, not task status.
        value = {"state": state, "code": code, "running": state == "ready", "can_recover": state == "offline",
                 "owner_recovered": False, "previous_pid": None, "previous_recovery_id": None, "pid": (data or {}).get("pid"),
                 "version": (data or {}).get("version"), "sessions_restored": False,
                 "started_empty": False,
                 "message": {"bound_owner_ready": "原本地终端显示服务已连接。",
                             "bound_owner_offline": "原本地终端显示服务已退出，可确认恢复；不会启动或重发工作任务。",
                             "bound_owner_recovered": "已恢复原本地显示服务；工作终端需要单独重新附着。",
                             "confirmation_required": "请明确确认恢复本地显示服务。"}.get(code, "原本地终端服务状态尚不能安全恢复；请检查原服务，不要替换或重发任务。"),
                 "recoverable": state == "offline", "recovered": False, "recovery_id": (data or {}).get("recovery_id")}
        value.update(changes)
        value["recovered"] = value["owner_recovered"]
        return value

    def _bound_context(self):
        if self.descriptor_file is not None or os.environ.get("CEO_HUB_TERMINAL_FILE") or self.launch_env is not None:
            raise ValueError("Recovery is restricted to the pinned default worker owner")
        reference = self._snapshot_file(worker_reference_path(), 8192)
        chosen = descriptor_path()
        descriptor = self._snapshot_file(chosen, 65536)
        data = self._read_descriptor_file(chosen)
        if data.get("version") != BROKER_WIRE_VERSION:
            raise ValueError("Unsupported terminal broker wire version")
        if not self._same_snapshot(reference) or not self._same_snapshot(descriptor):
            raise ValueError("Bound terminal ownership changed")
        return {"reference": reference, "descriptor": descriptor, "data": data,
                "maintenance": Path(str(descriptor["path"]) + ".recovery.lock")}

    def _context_unchanged(self, context, include_descriptor=True):
        return self._same_snapshot(context["reference"]) and (not include_descriptor or self._same_snapshot(context["descriptor"]))

    @staticmethod
    def _guard_identity(snapshot):
        # Device and ctime have different Windows representations in CPython
        # and Node. Inode/size/mtime are exact decimal strings in both runtimes.
        identity = snapshot["identity"]
        return {"ino": str(identity[1]), "size": str(identity[2]), "mtime_ns": str(identity[3])}

    def _bound_check(self, context, own_maintenance=False):
        data = context["data"]
        if not self._context_unchanged(context):
            return self._lifecycle("blocked", "bound_owner_changed", data)
        # request() authenticates with the existing private capability. A live
        # endpoint that refuses authentication is NOT a stale, replaceable owner.
        refused = False
        try:
            health = self.request("/health", timeout=4)
            if (health.get("ok") is True and health.get("pid") == data["pid"]
                    and health.get("recovery_id") == data.get("recovery_id")
                    and health.get("version") == BROKER_WIRE_VERSION and self._context_unchanged(context)):
                return self._lifecycle("ready", "bound_owner_ready", data)
        except urllib.error.URLError as error:
            reason = error.reason
            refused = isinstance(reason, OSError) and getattr(reason, "errno", None) in (errno.ECONNREFUSED, 10061)
        except ConnectionRefusedError:
            refused = True
        except Exception:
            pass
        pid_state = self._owner_state(data)
        if pid_state != "dead":
            return self._lifecycle("blocked" if pid_state == "alive" else "unknown",
                                   "bound_owner_alive" if pid_state == "alive" else "bound_owner_pid_unknown", data)
        if not refused or self._port_state(data["url"]) != "refused":
            return self._lifecycle("unknown", "bound_owner_health_unknown", data)
        lock_file = Path(str(context["descriptor"]["path"]) + ".lock")
        lock_snapshot = None
        if os.path.lexists(lock_file):
            try:
                lock_snapshot = self._snapshot_file(lock_file, 16384)
                lock_owner = self._lock_owner(lock_snapshot["raw"])
                if (lock_owner["pid"] != data["pid"] or lock_owner.get("owner_identity") != data.get("owner_identity")
                        or self._owner_state(lock_owner) != "dead"):
                    raise ValueError()
            except (OSError, ValueError, UnicodeError):
                return self._lifecycle("blocked", "bound_owner_lock_unknown", data)
        if os.path.lexists(context["maintenance"]) and not own_maintenance:
            return self._lifecycle("blocked", "bound_owner_recovery_busy", data)
        if not self._context_unchanged(context):
            return self._lifecycle("blocked", "bound_owner_changed", data)
        if "lock" in context and lock_snapshot != context["lock"]:
            return self._lifecycle("blocked", "bound_owner_changed", data)
        context["lock"] = lock_snapshot
        return self._lifecycle("offline", "bound_owner_offline", data)

    def bound_owner_status(self):
        """Read-only status. Never starts services, SSH, shells, or models."""
        with self.lock:
            try:
                return self._bound_check(self._bound_context())
            except (OSError, ValueError, TypeError):
                return self._lifecycle("blocked", "bound_owner_reference_invalid")

    def recover_bound_owner(self, confirm=False, expected_owner=None):
        """Explicitly restore the dead pinned PTY display owner, not sessions.

        A current-user child uses the *same physical descriptor namespace*.
        No old process is killed, no pinned reference is removed, and no remote
        terminal/model/task is started. The caller separately reattaches tmux.
        """
        if confirm is not True:
            return self._lifecycle("blocked", "confirmation_required")
        with self.lock:
            try:
                context = self._bound_context()
            except (OSError, ValueError, TypeError):
                return self._lifecycle("blocked", "bound_owner_reference_invalid")
            data = context["data"]
            if expected_owner is not None and (not isinstance(expected_owner, dict)
                    or set(expected_owner) - {"pid", "version"}
                    or type(expected_owner.get("pid")) is not int
                    or expected_owner.get("pid") != data["pid"]
                    or expected_owner.get("version", BROKER_WIRE_VERSION) != BROKER_WIRE_VERSION):
                return self._lifecycle("blocked", "bound_owner_changed", data)
            status = self._bound_check(context)
            if status["state"] != "offline":
                return status
            maintenance = context["maintenance"]
            recovery_id = uuid.uuid4().hex
            receipt = json.dumps({"pid": os.getpid(), "nonce": recovery_id}, separators=(",", ":")).encode("ascii")
            try:
                with maintenance.open("xb") as stream:
                    stream.write(receipt)
                maintenance.chmod(0o600)
            except FileExistsError:
                return self._lifecycle("blocked", "bound_owner_recovery_busy", data)
            except OSError:
                return self._lifecycle("blocked", "bound_owner_maintenance_failed", data)
            maintenance_snapshot = None
            launch_attempted = False
            preserve_maintenance = False
            try:
                maintenance_snapshot = self._snapshot_file(maintenance, 256)
                status = self._bound_check(context, own_maintenance=True)
                if status["state"] != "offline":
                    return status
                node = shutil.which("node")
                if not node or not (ROOT / "node_modules/node-pty").exists():
                    return self._lifecycle("blocked", "bound_owner_dependencies_missing", data)
                if not self._context_unchanged(context) or not self._same_snapshot(maintenance_snapshot):
                    return self._lifecycle("blocked", "bound_owner_changed", data)
                guard = {"descriptor_file": str(context["descriptor"]["path"]),
                         "descriptor_sha256": context["descriptor"]["sha256"], "pid": data["pid"],
                         "lock_sha256": context["lock"]["sha256"] if context["lock"] else None,
                         "maintenance_sha256": maintenance_snapshot["sha256"], "recovery_id": recovery_id,
                         "descriptor_identity": self._guard_identity(context["descriptor"]),
                         "lock_identity": self._guard_identity(context["lock"]) if context["lock"] else None,
                         "maintenance_identity": self._guard_identity(maintenance_snapshot),
                         "reference_file": str(context["reference"]["path"]),
                         "reference_sha256": context["reference"]["sha256"],
                         "reference_identity": self._guard_identity(context["reference"])}
                environment = os.environ.copy()
                environment["CEO_HUB_TERMINAL_FILE"] = guard["descriptor_file"]
                environment["CEO_HUB_RECOVERY_GUARD"] = json.dumps(guard, separators=(",", ":"))
                environment["CEO_HUB_IDENTITY_PYTHON"] = sys.executable
                self.runtime.mkdir(parents=True, exist_ok=True)
                # A launch timeout may happen after WMI accepted its one-shot
                # plan. Fence the attempt *before* crossing that boundary. An
                # exception is not proof that no broker child was started.
                launch_attempted = True
                preserve_maintenance = True
                process = launch_background([node, str(broker_script())], cwd=ROOT,
                    stdout_path=self.runtime / "terminal.out.log", stderr_path=self.runtime / "terminal.err.log",
                    env_overrides=background_environment(environment))
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if not self._context_unchanged(context, include_descriptor=False) or not self._same_snapshot(maintenance_snapshot):
                        return self._lifecycle("unknown", "bound_owner_changed_after_launch", data)
                    try:
                        current = self._read_descriptor_file(context["descriptor"]["path"])
                        if (current["pid"] == process.pid and current.get("version") == BROKER_WIRE_VERSION
                                and current.get("recovery_id") == recovery_id):
                            health = self.request("/health", timeout=1)
                            if (health.get("ok") is True and health.get("pid") == process.pid
                                    and health.get("recovery_id") == recovery_id
                                    and health.get("version") == BROKER_WIRE_VERSION
                                    and self._context_unchanged(context, include_descriptor=False)
                                    and self._read_descriptor_file(context["descriptor"]["path"]) == current):
                                sessions = self.request("/sessions", timeout=1)
                                if sessions.get("sessions") != []:
                                    return self._lifecycle("unknown", "bound_owner_not_empty_after_launch", current)
                                if (not self._context_unchanged(context, include_descriptor=False)
                                        or self._read_descriptor_file(context["descriptor"]["path"]) != current):
                                    return self._lifecycle("unknown", "bound_owner_changed_after_launch", current)
                                preserve_maintenance = False
                                return self._lifecycle("ready", "bound_owner_recovered", current,
                                                       owner_recovered=True, previous_pid=data["pid"],
                                                       previous_recovery_id=data.get("recovery_id"), recovery_id=recovery_id, started_empty=True)
                        elif not self._same_snapshot(context["descriptor"]):
                            return self._lifecycle("unknown", "bound_owner_competing_after_launch", data)
                    except Exception:
                        pass
                    if process.poll() is not None:
                        # No optimistic success or retry: a child that failed
                        # publication may have raced another maintenance action.
                        preserve_maintenance = False
                        return self._lifecycle("unknown", "bound_owner_launch_failed", data)
                    time.sleep(.15)
                return self._lifecycle("unknown", "bound_owner_readiness_unknown", data)
            except BackgroundLaunchUnknown:
                # The launcher's private receipt/launch ID remain available to
                # operators for reconciliation. Never expose those paths or
                # remove this original fence on an unconfirmed dispatch.
                return self._lifecycle("unknown", "bound_owner_readiness_unknown", data)
            except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError):
                return self._lifecycle("unknown" if launch_attempted else "blocked",
                                       "bound_owner_readiness_unknown" if launch_attempted else "bound_owner_maintenance_failed", data)
            finally:
                if not preserve_maintenance and maintenance_snapshot is not None and self._same_snapshot(maintenance_snapshot):
                    try:
                        maintenance.unlink()
                    except OSError:
                        pass

    def ensure_started(self):
        with self.lock:
            pinned = self.descriptor_file is None and not os.environ.get("CEO_HUB_TERMINAL_FILE") and os.path.lexists(worker_reference_path())
            try:
                chosen = (self.descriptor_file or descriptor_path()).resolve()
            except (OSError, ValueError):
                if pinned:
                    raise BrokerError("The bound work-terminal reference is invalid or missing its target; repair the private reference to the original broker. No replacement broker was started.",
                                      "bound_transport_unavailable") from None
                raise
            if self.running():
                self._anchor_default_worker(chosen)
                return
            if pinned:
                raise BrokerError("The bound work-terminal owner is unavailable; verify the original broker and repair the private reference. No replacement broker was started and existing work terminals were not restarted.",
                                  "bound_transport_unavailable")
            node = shutil.which("node")
            if not node:
                raise RuntimeError("需要 Node.js；请安装 Node.js 后执行 npm ci。")
            if not (ROOT / "node_modules/node-pty").exists():
                raise RuntimeError("终端依赖未安装；请在 AgentCaptain 目录执行 npm ci。")
            logs = self.runtime
            logs.mkdir(parents=True, exist_ok=True)
            inherited = self.launch_env() if callable(self.launch_env) else self.launch_env
            environment = os.environ.copy() if inherited is None else dict(inherited)
            environment["CEO_HUB_TERMINAL_FILE"] = str(chosen)
            environment["CEO_HUB_IDENTITY_PYTHON"] = sys.executable
            process = launch_background([node, str(broker_script())], cwd=ROOT,
                stdout_path=logs / "terminal.out.log", stderr_path=logs / "terminal.err.log",
                env_overrides=background_environment(environment))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("终端服务启动失败；请查看 .runtime/terminal.err.log。")
                if self.running():
                    self._anchor_default_worker(chosen)
                    return
                time.sleep(.15)
            raise RuntimeError("终端服务尚未就绪；请查看 .runtime/terminal.err.log。")

    def sessions(self):
        # Reading registry/state never launches the broker or SSH.
        try:
            return self.request("/sessions", timeout=3).get("sessions", [])
        except Exception:
            return []

    def ended_display_snapshot(self, terminal_id):
        """Private, authenticated evidence for one terminated SSH display.

        Never turn a failed registry query into an empty registry, launch an
        owner, attach a PTY, or expose the descriptor capability. File-content
        identities also bind older owners that have no recovery generation.
        Callers must compare two receipts around their remote read-only proof.
        """
        try:
            context = self._bound_context()
            if self._bound_check(context).get("state") != "ready":
                raise ValueError()
            reply = self.request("/sessions", timeout=3)
            records = reply.get("sessions") if isinstance(reply, dict) else None
            if not isinstance(records, list) or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in records):
                raise ValueError()
            selected = [item for item in records if item["id"] == terminal_id]
            if len(selected) != 1:
                raise ValueError()
            record = selected[0]
            pid = record.get("pid")
            if type(pid) is not int or not 0 < pid <= 2147483647:
                raise ValueError()
            pid_state = self._pid_state(pid)
            if self._bound_check(context).get("state") != "ready":
                raise ValueError()
            data = context["data"]
            return {"owner": {key: data.get(key) for key in ("pid", "version", "recovery_id")},
                    "reference_sha256": context["reference"]["sha256"],
                    "descriptor_sha256": context["descriptor"]["sha256"],
                    "reference_identity": context["reference"]["identity"],
                    "descriptor_identity": context["descriptor"]["identity"],
                    # An old owner without a birth record was already proven
                    # alive by two authenticated health checks above. Preserve
                    # that compatible receipt; offline recovery still requires
                    # the stricter _owner_state evidence and never trusts PID
                    # existence alone.
                    "owner_pid_state": self._owner_state(data) if "owner_identity" in data else self._pid_state(data["pid"]),
                    "session": record, "display_pid_state": pid_state}
        except Exception:
            raise BrokerError("原显示结束证据尚不能确认；保留未知结果，不重复附着。", "ended_display_unconfirmed") from None

    def live_display_snapshot(self, terminal_id):
        """Authenticate one existing live SSH display without probing its TUI.

        Two receipts can bracket remote read-only checks. Only immutable local
        attachment fields are included: normal output, geometry and human input
        changes must not impersonate a replacement display or block a read.
        """
        try:
            with self.lock:
                context = self._bound_context()
                if self._bound_check(context).get("state") != "ready":
                    raise ValueError()
                reply = self.request("/sessions", timeout=3)
                records = reply.get("sessions") if isinstance(reply, dict) else None
                if (not isinstance(records, list)
                        or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in records)):
                    raise ValueError()
                selected = [item for item in records if item["id"] == terminal_id]
                if len(selected) != 1:
                    raise ValueError()
                record = selected[0]
                pid = record.get("pid")
                if (record.get("transport") != "ssh" or record.get("connected") is not True
                        or type(pid) is not int or not 0 < pid <= 2147483647):
                    raise ValueError()
                process = observe_process(pid)
                birth, executable = process.get("birth"), process.get("executable")
                if (process.get("state") != "alive" or not isinstance(birth, str)
                        or not re.fullmatch(r"(?:win-filetime:[0-9]{1,24}|linux-start:[a-f0-9-]{36}:[0-9]{1,24})", birth)
                        or not isinstance(executable, str) or not Path(executable).is_absolute()
                        or Path(executable).name.lower() not in ("ssh", "ssh.exe")
                        or record.get("display_identity") != {"birth": birth, "executable": executable}):
                    raise ValueError()
                if self._bound_check(context).get("state") != "ready":
                    raise ValueError()
                data = context["data"]
                if self._owner_state(data) != "alive":
                    raise ValueError()
                return {"owner": {key: data.get(key) for key in ("pid", "version", "recovery_id")},
                        "reference_sha256": context["reference"]["sha256"],
                        "descriptor_sha256": context["descriptor"]["sha256"],
                        "reference_identity": context["reference"]["identity"],
                        "descriptor_identity": context["descriptor"]["identity"], "owner_pid_state": "alive",
                        "session": {key: record.get(key) for key in
                                    ("id", "host", "transport", "target", "cwd", "pid", "connected", "created_at", "display_identity")},
                        "display_pid_state": "alive", "display_identity": {"birth": birth, "executable": executable}}
        except Exception:
            raise BrokerError("原显示连接证据尚不能确认；保留未知结果，不重复附着。", "live_display_unconfirmed") from None

    def create(self, body):
        self.ensure_started()
        return self.request("/sessions/create", body)

    def snapshot(self, terminal_id, lines=60):
        return self.request(f"/sessions/{quote(terminal_id, safe='')}/snapshot?lines={min(5000, max(1, int(lines)))}",
                            timeout=FOREGROUND_RPC_TIMEOUT)

    def output(self, terminal_id, after=0):
        if int(after) == 0:
            snapshot = self.snapshot(terminal_id, 5000)
            return {"data": "", "reset": True, "ansi": snapshot["ansi"], "seq": snapshot["current_seq"],
                    "connected": snapshot["connected"], "state": snapshot["state"], "status": snapshot["state"],
                    "command": snapshot.get("command", ""), "agent_ready": snapshot.get("agent_ready", False),
                    "blocked_reason": snapshot.get("blocked_reason"), "human_busy": snapshot.get("human_busy", False),
                    "human_input": snapshot.get("human_input"),
                    "cols": snapshot["cols"], "rows": snapshot["rows"]}
        return self.request(f"/sessions/{quote(terminal_id, safe='')}/output?after={max(0, int(after))}",
                            timeout=FOREGROUND_RPC_TIMEOUT)

    def write(self, terminal_id, action, body):
        if action not in ("input", "prompt", "resize", "close"):
            raise ValueError("Unknown terminal action")
        timeout = (PROMPT_RPC_TIMEOUT if action == "prompt" else FOREGROUND_RPC_TIMEOUT if action in ("input", "resize")
                   else 60 if action == "close" and body.get("require_verified") is True else 20)
        return self.request(f"/sessions/{quote(terminal_id, safe='')}/{action}", body, timeout=timeout)
