#!/usr/bin/env python3
"""Preview the shipped web UI with isolated, synthetic data only.

Run ``python scripts/preview_demo.py`` after ``npm ci`` and visit the printed
loopback URL. This module deliberately imports no application, terminal,
configuration, SSH or model code. It starts no broker or worker and persists
nothing. All task and lifecycle mutations are rejected. The only accepted POSTs
read fixed fixture history or adjust this process's synthetic display geometry.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
STAMP = "2026-01-01T10:00:00Z"
STAMP_EPOCH = datetime.fromisoformat(STAMP).timestamp()
SELECTED_AGENT = "demo-build:demo-release-review"
DEMO_NOTICE = "SYNTHETIC DEMO · 演示数据 · 无真实机器或模型连接 · 写操作已禁用"
MAX_BODY_BYTES = 16384
HOSTS = [
    {"id": "demo-local", "label": "Local workstation", "transport": "local", "connected": True,
     "latency": {"state": "local", "metric": "local", "last_ms": None}},
    {"id": "demo-build", "label": "Build server", "transport": "ssh", "target": "build.example", "connected": True,
     "latency": {"state": "measured", "metric": "ssh_response", "last_ms": 842, "last_success_at": STAMP_EPOCH}},
    {"id": "demo-research", "label": "Research node", "transport": "ssh", "target": "research.example", "connected": True,
     "latency": {"state": "measured", "metric": "ssh_response", "last_ms": 1360, "last_success_at": STAMP_EPOCH}},
]


def _worker(host: str, ident: str, title: str, cwd: str, label: str, pinned: bool = False) -> dict:
    return {
        "host": host, "thread_id": ident, "terminal_id": "terminal-" + ident,
        "mode": "terminal", "alias": title, "title": title, "cwd": cwd,
        "connected": True, "active": True, "command": "codex", "state": "agent",
        "agent_ready": True, "pinned": pinned, "created_at": STAMP,
        "management": {"status": "ready", "label": label, "primary_action": "open_terminal", "needs_attention": False},
    }


WORKERS = [
    _worker("demo-build", "demo-release-review", "Release readiness", "/workspace/sample-app", "Captain 已跟进 · 等待交付审查", True),
    _worker("demo-local", "demo-docs", "Documentation refresh", "C:/workspace/docs", "Captain 已派发 · 正在整理文档"),
    _worker("demo-research", "demo-benchmark", "Benchmark verification", "/workspace/benchmarks", "Captain 已询问 · 校验结果待汇总"),
]
MANAGER = {
    "id": "demo-captain", "title": "Captain · 跨机器项目协作", "state": "ready",
    "phase": "演示：派发 → 跟进 → 交付审查", "session_id": "demo-manager-session",
    "workspace": "C:/example/management", "terminal_id": "demo-manager-terminal",
    "input_owner": "hub", "tools_ready": True, "worker_access": "automatic",
    "effective_worker_count": 3, "scope_count": 3, "allowed_hosts": {},
    "desktop_verified": False, "connected": True, "agent_ready": True, "error": None,
}
MESSAGES = [
    {"id": "demo-message-1", "role": "user", "time": STAMP, "text": "请核验发布准备情况。先检查测试与剩余风险；暂不发布或推送。"},
    {"id": "demo-message-2", "role": "assistant", "time": STAMP, "text": "## 发布准备汇报\n\n- 测试：样例测试集已完成，检查记录已整理。\n- 文档：已委派本机工作 agent 更新快速开始。\n- 交叉核验：研究节点正在复查基准测试方法。\n\n**待决事项：** 当前只提交检查结果，是否发布仍由你决定。\n\n> 以上均为演示数据，不是真实任务或测试结果。"},
]
WORKER_SCREEN = (
    "\x1b[1;38;2;185;233;199mAgentCaptain · Release readiness\x1b[0m\r\n"
    "\x1b[90mSynthetic demo / no live worker\x1b[0m\r\n\r\n"
    "\x1b[1m> Captain: 核验发布准备情况，汇总证据与剩余风险。\x1b[0m\r\n"
    "  暂不发布或推送。\r\n\r\n"
    "  已完成检查\r\n"
    "  \x1b[32m✓\x1b[0m 样例测试结果与变更清单已整理\r\n"
    "  \x1b[32m✓\x1b[0m 文档工作已交由本机 agent 跟进\r\n"
    "  \x1b[32m✓\x1b[0m 研究节点已收到基准复查请求\r\n\r\n"
    "  交付边界\r\n"
    "  · 只做核验，不执行发布\r\n"
    "  · 等待 Captain 汇总并交由用户决策\r\n\r\n"
    "\x1b[90m演示数据 · 在真实产品中可直接与工作 agent 对话。\x1b[0m\r\n"
)
MANAGER_SCREEN = (
    "\x1b[1;38;2;185;233;199mCaptain · Management conversation\x1b[0m\r\n"
    "\x1b[90mSynthetic demo / no local Codex process\x1b[0m\r\n\r\n"
    "> 请汇总这几个项目的进度，只把需要我决定的事情告诉我。\r\n\r\n"
    "  我会向对应工作 agent 询问，而不是重复它们的实现工作。\r\n\r\n"
    "  Build server       发布准备：已收到核验报告，待审查\r\n"
    "  Local workstation  文档更新：已派发，进行中\r\n"
    "  Research node      基准复查：等待证据汇总\r\n\r\n"
    "  需要你决定：是否在核验完成后批准发布。\r\n\r\n"
    "\x1b[90m演示数据 · 最终决策由用户保留。\x1b[0m\r\n"
)

DEMO_CSS = """
.demo-banner{padding:10px 24px;background:#213b31;color:#d3f0df;border-bottom:1px solid #456b55;font:600 11px/1.6 system-ui,sans-serif;letter-spacing:.07em;text-align:center}
.demo-banner a{color:inherit;margin-left:14px;text-decoration:underline}
[data-demo-disabled]{cursor:not-allowed!important;opacity:.45!important}
"""
DEMO_JS = """
'use strict';
// This presentation layer disables unsafe controls; server-side rejection is
// the actual boundary. Never proxy requests to the user's running Hub.
const demoDisabled = '#machine-settings,#add-machine,#new-agent,#discover-button,#controller-scope,#controller-desktop,#terminal-close,#terminal-paste,#terminal-bind-output,#controller-paste,#controller-close,#terminal-connect,#terminal-reconcile,.agent-pin-button,.agent-rename-button,.agent-close-link,.agent-reconnect-button,.host-controls button';
function guardDemo() { document.querySelectorAll(demoDisabled).forEach(element => { element.disabled = true; element.dataset.demoDisabled = 'true'; element.title = '演示数据：此操作已禁用，不连接真实机器或模型。'; }); }
new MutationObserver(guardDemo).observe(document.body, {childList: true, subtree: true});
guardDemo();
for (const event of ['keydown','beforeinput','paste']) document.addEventListener(event, e => { if (e.target.closest?.('.xterm')) { e.preventDefault(); e.stopImmediatePropagation(); } }, true);
"""

STATIC = {
    "/ui.js": ("web/ui.js", "text/javascript; charset=utf-8"),
    "/controller.js": ("web/controller.js", "text/javascript; charset=utf-8"),
    "/terminal_history.js": ("web/terminal_history.js", "text/javascript; charset=utf-8"),
    "/style.css": ("web/style.css", "text/css; charset=utf-8"),
    "/prose.css": ("web/prose.css", "text/css; charset=utf-8"),
    "/agent-captain-mark.png": ("assets/brand/agentcaptain-mark.png", "image/png"),
    "/vendor/xterm.js": ("node_modules/@xterm/xterm/lib/xterm.js", "text/javascript; charset=utf-8"),
    "/vendor/xterm.css": ("node_modules/@xterm/xterm/css/xterm.css", "text/css; charset=utf-8"),
    "/vendor/xterm-addon-fit.js": ("node_modules/@xterm/addon-fit/lib/addon-fit.js", "text/javascript; charset=utf-8"),
}


def fixture_state() -> dict:
    """Return fresh synthetic records, never references to mutable global data."""
    return copy.deepcopy({
        "version": "0.7.1 · DEMO", "demo": True, "hosts": HOSTS, "agents": WORKERS,
        "outbox": [{"id": "demo-delivery", "host": "demo-build", "thread_id": "demo-release-review", "time": STAMP_EPOCH - 12, "submitted_at": STAMP_EPOCH, "state": "submitted", "text": "演示：请核验发布准备，汇总证据与风险；暂不发布或推送。"}],
        "events": [{"id": 1, "kind": "terminal_input_submitted", "time": STAMP, "data": {"host": "demo-build", "thread_id": "demo-release-review", "text": "演示：Captain 已向指定工作会话派发核验请求。"}}],
    })


class DemoServer(ThreadingHTTPServer):
    """Always loopback; constructor has no production configuration parameter."""

    daemon_threads = True

    def __init__(self, port: int = 8877):
        self.geometry: dict[str, tuple[int, int]] = {}
        super().__init__(("127.0.0.1", port), DemoHandler)


class DemoHandler(BaseHTTPRequestHandler):
    server: DemoServer
    server_version = "AgentCaptainSyntheticDemo"

    def setup(self) -> None:
        super().setup()
        # A malformed local client must not retain a request thread forever.
        self.connection.settimeout(5)

    def log_message(self, _format: str, *args) -> None:
        pass

    def _valid_host(self) -> bool:
        return self.headers.get("Host", "") in {
            f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}",
        }

    def _send(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: int, data: dict) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: int = 403, message: str = "Synthetic demo: writes and real connections are disabled.") -> None:
        self._json(status, {"error": message, "code": "demo_read_only", "demo": True})

    def do_GET(self) -> None:
        if not self._valid_host():
            self._error(403, "Only the local preview origin is allowed.")
            return
        request = urlsplit(self.path)
        if request.scheme or request.netloc:
            self._error(400, "Only relative preview routes are accepted; proxying is not supported.")
            return
        path, query = request.path, parse_qs(request.query)
        if path == "/":
            html = (ROOT / "web/index.html").read_text(encoding="utf-8")
            html = html.replace("<title>AgentCaptain</title>", "<title>AgentCaptain · Synthetic demo</title>")
            html = html.replace("</head>", '<link rel="stylesheet" href="/demo.css"></head>')
            html = html.replace("<body>", f'<body><aside class="demo-banner" role="note">{DEMO_NOTICE}</aside>')
            html = html.replace("</body>", '<script src="/demo-guard.js"></script></body>')
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        elif path in STATIC:
            relative, mime = STATIC[path]
            asset = ROOT / relative
            if asset.is_file():
                self._send(200, asset.read_bytes(), mime)
            else:
                self._error(404, "Preview asset missing. Restore the checkout or run npm ci for terminal dependencies.")
        elif path in {"/demo.css", "/demo-guard.js"}:
            self._send(200, (DEMO_CSS if path == "/demo.css" else DEMO_JS).encode("utf-8"), "text/css; charset=utf-8" if path == "/demo.css" else "text/javascript; charset=utf-8")
        elif path == "/api/state":
            self._json(200, fixture_state())
        elif path == "/api/terminal-service/status":
            self._json(200, {"available": True, "running": True, "connected": True, "can_recover": False, "demo": True})
        elif path == "/api/controllers":
            self._json(200, {"controllers": [MANAGER], "demo": True})
        elif path == "/api/controllers/get" and query.get("id") == [MANAGER["id"]]:
            self._json(200, MANAGER)
        elif path in {"/api/terminals/output", "/api/controllers/output"}:
            ident = query.get("id" if "/controllers/" in path else "thread_id", [""])[0]
            is_manager = "/controllers/" in path
            if is_manager and ident != MANAGER["id"] or not is_manager and not self._worker(query):
                self._error(404, "No such synthetic session.")
                return
            cols, rows = self.server.geometry.get(ident, (100, 28))
            reset = query.get("after", ["0"])[0] == "0"
            screen = MANAGER_SCREEN if is_manager else WORKER_SCREEN
            self._json(200, {"data": screen if reset else "", "seq": 1, "reset": reset, "cols": cols, "rows": rows, "connected": True, "command": "codex · DEMO", "demo": True})
        elif path == "/api/terminals/history" and self._worker(query):
            self._json(200, {"text": "SYNTHETIC DEMO\n" + WORKER_SCREEN, "complete": True, "demo": True})
        else:
            self._error(404, "This route is not part of the isolated demo.")

    @staticmethod
    def _worker(values: dict) -> dict | None:
        def scalar(key: str) -> str:
            value = values.get(key, "")
            if isinstance(value, list):
                if len(value) != 1:
                    return ""
                value = value[0]
            return value if isinstance(value, str) and len(value) <= 200 else ""
        return next((worker for worker in WORKERS if worker["host"] == scalar("host") and worker["thread_id"] == scalar("thread_id")), None)

    def do_POST(self) -> None:
        if not self._valid_host() or self.headers.get("Origin") not in {None, "http://" + self.headers.get("Host", "")}:
            self._error()
            return
        request = urlsplit(self.path)
        if request.scheme or request.netloc:
            self._error(400, "Only relative preview routes are accepted; proxying is not supported.")
            return
        try:
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) > 1:
                raise ValueError("Unsupported request framing")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= MAX_BODY_BYTES:
                raise ValueError("Invalid request size")
            data = json.loads(self.rfile.read(length)) if length else {}
            if not isinstance(data, dict):
                raise ValueError("Expected an object")
        except (ValueError, OSError):
            self._error(400, "Invalid demo request.")
            return
        path = request.path
        if path == "/api/agents/read" and (worker := self._worker(data)):
            self._json(200, {"demo": True, "source": "codex_rollout", "session_id": "demo-session-" + worker["thread_id"], "output_binding": {"session_id": "demo-session-" + worker["thread_id"], "live_session_verified": True}, "messages": MESSAGES, "recent_signals": [], "content_complete": True, "has_more": False, "note": "Synthetic demo fixtures only. These are not real conversation records or test results."})
        elif path in {"/api/terminals/resize", "/api/controllers/resize"}:
            # In-memory fixture rendering only: there is no terminal or process.
            manager = path == "/api/controllers/resize"
            ident = data.get("id") if manager else data.get("thread_id")
            cols, rows = data.get("cols"), data.get("rows")
            if (manager and ident != MANAGER["id"] or not manager and not self._worker(data)
                    or type(cols) is not int or not 2 <= cols <= 500
                    or type(rows) is not int or not 2 <= rows <= 200):
                self._error(400, "Invalid synthetic display geometry.")
                return
            self.server.geometry[ident] = (cols, rows)
            self._json(200, {"ok": True, "demo": True, "fixture_only": True})
        else:
            self._error()

    def _reject_write(self) -> None:
        self._error()

    do_PUT = do_PATCH = do_DELETE = _reject_write


def main() -> None:
    parser = argparse.ArgumentParser(description="AgentCaptain offline, synthetic UI preview; never connects to real machines.")
    parser.add_argument("--port", type=int, default=8877, help="Loopback preview port (default: 8877).")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    with DemoServer(args.port) as server:
        print(f"{DEMO_NOTICE}\nhttp://127.0.0.1:{server.server_port}/#agent={SELECTED_AGENT}\nCtrl+C stops only this demo.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
