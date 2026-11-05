"""Loopback-only shared terminal UI, MCP gateway and history diagnostic CLI."""
from __future__ import annotations

import argparse
import atexit
import json
import os
import secrets
import sys
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agent_captain import __version__ as VERSION
from agent_captain.core.terminal_hub import Hub, HistoryReadError
from agent_captain.paths import ROOT


def connection_file():
    override = (os.environ.get("AGENTCAPTAIN_CONNECTION_FILE")
                or os.environ.get("PRON_HUB_CONNECTION_FILE")
                or os.environ.get("CEO_HUB_CONNECTION_FILE"))
    if override:
        return Path(override).expanduser()
    # Checkout-owned state is shared by ordinary shells and packaged desktop
    # processes, whose identical APPDATA strings may address different files.
    return ROOT / ".runtime" / "service.json"


def sync_connection_aliases(payload):
    """Refresh explicitly registered old Hub descriptors, never account auth.

    Some already-running legacy bridges cannot load the new reader until their
    client restarts. Private path-only aliases preserve those same recipients
    across a normal service-token rotation. Missing, malformed, different-port
    or nonstandard targets are left untouched; no credentials are logged.
    """
    reference = ROOT / '.runtime' / 'service-compatibility-references.json'
    updated = 0
    try:
        if reference.is_symlink():
            return 0
        raw = reference.read_bytes()
        values = json.loads(raw) if len(raw) <= 8192 else None
        paths = values.get('service_files') if isinstance(values, dict) and set(values) == {'service_files'} else None
        if not isinstance(paths, list) or not 1 <= len(paths) <= 8:
            return 0
    except (OSError, ValueError, UnicodeError):
        return 0
    for value in paths:
        try:
            if (not isinstance(value, str) or len(value) > 4096 or value.startswith(('\\\\', '//'))
                    or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)):
                continue
            target = Path(value)
            if (not target.is_absolute() or target.is_symlink() or not target.is_file()
                    or target.name != 'service.json' or target.parent.name.casefold() not in ('ceohub', 'ceo-hub')
                    or target.resolve() == connection_file().resolve()):
                continue
            old_raw = target.read_bytes()
            old = json.loads(old_raw) if len(old_raw) <= 65536 else None
            if (not isinstance(old, dict) or set(old) != {'url', 'token', 'pid', 'version'}
                    or old.get('url', '').rstrip('/') != payload['url'].rstrip('/')
                    or type(old.get('pid')) is not int or old['pid'] <= 0
                    or not isinstance(old.get('token'), str) or not 16 <= len(old['token']) <= 4096
                    or not isinstance(old.get('version'), str)):
                continue
            temporary = target.with_name(target.name + '.' + secrets.token_hex(8) + '.tmp')
            with temporary.open('x', encoding='utf-8') as stream:
                stream.write(json.dumps(payload))
            temporary.chmod(0o600)
            temporary.replace(target)
            updated += 1
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return updated


def create_server(hub, port=8766, token=None):
    session_token = token or secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def trusted_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{server.server_port}"

        def authenticated(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
                value = cookie.get("ceo_session")
                return bool(value and secrets.compare_digest(value.value, session_token))
            except Exception:
                return False

        def plugin_identity(self):
            # The request body never establishes a controller's identity. Only
            # this bearer capability, authenticated by the owning service, does.
            if self.headers.get("Origin"):
                return False, None
            value = self.headers.get("Authorization", "")
            if not value.startswith("Bearer "):
                return False, None
            credential = value[7:]
            if secrets.compare_digest(credential, session_token):
                return True, None  # Compatibility channel: global UI scope.
            try:
                controller_id = hub.controllers.authenticate(credential)
            except Exception:
                return False, None
            return controller_id is not None, controller_id

        def respond(self, status, payload, content_type="application/json; charset=utf-8", cookie=False):
            data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
            if cookie:
                self.send_header("Set-Cookie", f"ceo_session={session_token}; HttpOnly; SameSite=Strict; Path=/")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self.trusted_host():
                self.respond(403, {"error": "Invalid host"})
                return
            parsed = urlparse(self.path)
            assets = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/ui.js": ("ui.js", "text/javascript; charset=utf-8"),
                      "/controller.js": ("controller.js", "text/javascript; charset=utf-8"),
                      "/terminal_history.js": ("terminal_history.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8"),
                      "/prose.css": ("prose.css", "text/css; charset=utf-8")}
            vendors = {"/vendor/xterm.js": ("@xterm/xterm/lib/xterm.js", "text/javascript; charset=utf-8"),
                       "/vendor/xterm.css": ("@xterm/xterm/css/xterm.css", "text/css; charset=utf-8"),
                       "/vendor/xterm-addon-fit.js": ("@xterm/addon-fit/lib/addon-fit.js", "text/javascript; charset=utf-8")}
            if parsed.path in assets:
                file, mime = assets[parsed.path]
                self.respond(200, (ROOT / "web" / file).read_bytes(), mime, cookie=parsed.path == "/")
            elif parsed.path == "/agent-captain-mark.png":
                # One reviewed, canonical brand asset; never expose the asset
                # directory or resolve a request-supplied filesystem path.
                self.respond(200, (ROOT / "assets" / "brand" / "agentcaptain-mark.png").read_bytes(), "image/png")
            elif parsed.path in vendors:
                file, mime = vendors[parsed.path]
                asset = ROOT / "node_modules" / file
                if asset.is_file():
                    self.respond(200, asset.read_bytes(), mime)
                else:
                    self.respond(503, {"error": "终端依赖未安装，请执行 npm ci。"})
            elif parsed.path == "/health":
                self.respond(200, {"service": "ceo_hub", "version": VERSION, "controller": "codex-plugin", "pid": os.getpid()})
            elif parsed.path == "/api/state" and self.authenticated():
                try:
                    after = int(parse_qs(parsed.query).get("after", ["0"])[0])
                    self.respond(200, hub.state(after))
                except Exception as error:
                    self.respond(400, {"error": str(error)})
            elif parsed.path == "/api/terminal-service/status" and self.authenticated():
                self.respond(200, hub.terminal_service_status())
            elif parsed.path == "/api/terminals/output" and self.authenticated():
                try:
                    args = parse_qs(parsed.query)
                    self.respond(200, hub.terminal_output(args["host"][0], args["thread_id"][0], int(args.get("after", ["0"])[0])))
                except Exception as error:
                    self.respond(400, {"error": str(error)})
            elif parsed.path == "/api/terminals/history" and self.authenticated():
                try:
                    args = parse_qs(parsed.query)
                    self.respond(200, hub.terminal_history(args["host"][0], args["thread_id"][0], int(args.get("lines", ["5000"])[0])))
                except PermissionError as error:
                    self.respond(403, {"error": str(error)})
                except Exception as error:
                    self.respond(400, {"error": str(error)})
            elif parsed.path in ("/api/controllers", "/api/controllers/get", "/api/controllers/output") and self.authenticated():
                try:
                    args = parse_qs(parsed.query)
                    if parsed.path == "/api/controllers":
                        result = {"controllers": hub.controllers.list()}
                    elif parsed.path.endswith("/get"):
                        result = hub.controllers.get(args["id"][0], refresh=True)
                    else:
                        result = hub.controllers.terminal_output(args["id"][0], int(args.get("after", ["0"])[0]))
                    self.respond(200, result)
                except PermissionError as error:
                    self.respond(403, {"error": str(error)})
                except Exception as error:
                    self.respond(400, {"error": str(error)})
            else:
                self.respond(404, {"error": "Not found or unauthorized"})

        def do_POST(self):
            plugin_request = self.path == "/api/plugin"
            authorized, controller_id = self.plugin_identity() if plugin_request else (self.authenticated() and self.headers.get("Origin") == server.origin, None)
            if not self.trusted_host() or not authorized:
                self.respond(403, {"error": "Invalid session or origin"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 100000:
                    raise ValueError("Invalid body size")
                body = json.loads(self.rfile.read(length))
                if plugin_request:
                    self.respond(200, hub.plugin_call(body["tool"], body.get("arguments", {}), controller_id=controller_id))
                elif self.path == "/api/terminal-service/recover":
                    self.respond(200, hub.recover_terminal_service(body.get("confirm") is True, expected_owner=body.get("expected_owner")))
                elif self.path == "/api/controllers/prepare":
                    self.respond(200, hub.controllers.prepare(body))
                elif self.path in ("/api/controllers/start", "/api/controllers/resume"):
                    method = hub.controllers.start if self.path.endswith("/start") else hub.controllers.resume
                    self.respond(200, method(body["id"], body.get("request_key")))
                elif self.path == "/api/controllers/scope":
                    self.respond(200, hub.controllers.set_scope(body["id"], body.get("agent_ids", []),
                        allowed_hosts=body.get("allowed_hosts"), host_roots=body.get("host_roots")))
                elif self.path == "/api/controllers/input":
                    self.respond(200, hub.controllers.input(body["id"], body["data"]))
                elif self.path == "/api/controllers/resize":
                    self.respond(200, hub.controllers.resize(body["id"], body["cols"], body["rows"]))
                elif self.path == "/api/controllers/close":
                    self.respond(200, hub.controllers.close(body["id"], body.get("confirm") is True))
                elif self.path == "/api/controllers/desktop":
                    self.respond(200, hub.controllers.desktop(body["id"], body.get("confirm") is True))
                elif self.path == "/api/hosts/upsert":
                    self.respond(200, hub.upsert_host(body))
                elif self.path == "/api/hosts/connect":
                    self.respond(200, hub.connect_host(body["host"]))
                elif self.path == "/api/directories/list":
                    self.respond(200, hub.list_directories(body["host"], body.get("path", "")))
                elif self.path == "/api/agents/discover":
                    self.respond(200, hub.discover(body["host"], body.get("query", ""), body.get("cwd", ""), body.get("limit", 20)))
                elif self.path == "/api/agents/attach":
                    self.respond(200, hub.attach_agent(body["host"], body["thread_id"], body.get("alias", "")))
                elif self.path == "/api/agents/update":
                    self.respond(200, hub.update_agent(body["host"], body["thread_id"], alias=body.get("alias"),
                        pinned=body.get("pinned"), request_key=body.get("request_key")))
                elif self.path == "/api/agents/reconnect":
                    self.respond(200, hub.reconnect_agent(body["host"], body["thread_id"], request_key=body.get("request_key")))
                elif self.path == "/api/agents/auto-reconnect":
                    self.respond(200, hub.set_auto_reconnect(body["host"], body["thread_id"], body.get("enabled"),
                        body.get("expected_terminal_id"), body.get("request_key")))
                elif self.path == "/api/agents/reconcile-reconnect":
                    self.respond(200, hub.reconcile_reconnect(body["host"], body["thread_id"], body.get("request_key"), confirm=body.get("confirm") is True))
                elif self.path == "/api/agents/reconcile-original-worker":
                    self.respond(200, hub.reconcile_original_worker(body["host"], body["thread_id"], body.get("request_key"), confirm=body.get("confirm") is True))
                elif self.path == "/api/terminals/create":
                    self.respond(200, hub.create_terminal(body["host"], body.get("cwd", ""), body["title"], body.get("thread_id"),
                        body.get("cols", 100), body.get("rows", 30), body.get("active") is True, body.get("request_key")))
                elif self.path == "/api/terminals/connect":
                    self.respond(200, hub.connect_terminal(body["host"], body["thread_id"]))
                elif self.path == "/api/terminals/input":
                    self.respond(200, hub.terminal_input(body["host"], body["thread_id"], body["data"], expected_terminal_id=body.get("expected_terminal_id")))
                elif self.path == "/api/terminals/resize":
                    self.respond(200, hub.terminal_resize(body["host"], body["thread_id"], body["cols"], body["rows"], expected_terminal_id=body.get("expected_terminal_id")))
                elif self.path == "/api/terminals/close":
                    self.respond(200, hub.terminal_close(body["host"], body["thread_id"], body.get("confirm") is True, expected_terminal_id=body.get("expected_terminal_id")))
                elif self.path == "/api/terminals/reopen":
                    self.respond(200, hub.reopen_terminal(body["host"], body["thread_id"], body.get("request_key"),
                        body.get("expected_terminal_id"), confirm=body.get("confirmed") is True))
                elif self.path == "/api/agents/activate":
                    self.respond(200, hub.activate_agent(body["host"], body["thread_id"], body["active"] is True))
                elif self.path == "/api/agents/read":
                    self.respond(200, hub.read_agent(body["host"], body["thread_id"], body.get("limit", 18),
                        cursor=body.get("cursor"), after_cursor=body.get("after_cursor"), include_tools=body.get("include_tools", False)))
                elif self.path == "/api/agents/bind-output":
                    self.respond(200, hub.bind_output(body["host"], body["thread_id"], body["session_id"]))
                elif self.path == "/api/agents/message":
                    self.respond(200, hub.send(body["host"], body["thread_id"], body["text"], request_key=body.get("request_key"), expected_terminal_id=body.get("expected_terminal_id")))
                elif self.path == "/api/agents/create":
                    self.respond(200, hub.create_agent(body["host"], body["cwd"], body["title"], body.get("text", ""), body.get("active", True), body.get("request_key"), command=body.get("command", "")))
                else:
                    self.respond(404, {"error": "Not found"})
            except HistoryReadError as error:
                self.respond(409 if error.reason_code == "ssh_read_in_progress" else 400,
                             {"error": str(error), "code": error.reason_code})
            except PermissionError as error:
                self.respond(403, {"error": str(error)})
            except Exception as error:
                self.respond(400, {"error": str(error)})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.origin = f"http://127.0.0.1:{server.server_port}"
    server.session_token = session_token
    hub.controllers.set_gateway_origin(server.origin)
    return server


def serve(port=8766):
    hub = Hub()
    server = create_server(hub, port)
    descriptor = connection_file()
    descriptor.parent.mkdir(parents=True, exist_ok=True)
    temporary = descriptor.with_suffix(".tmp")
    payload = {"url": server.origin, "token": server.session_token, "version": VERSION, "pid": os.getpid()}
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(descriptor)
    sync_connection_aliases(payload)
    hub.start_auto_reconnect()
    atexit.register(hub.close)
    print(f"AgentCaptain connection service {VERSION}: {server.origin}", flush=True)
    print("Captain 是当前 Codex 管理对话；本服务不另运行管理模型、不自动扫描对话。", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        hub.close()


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="AgentCaptain — 一个主控，统筹本机与远程 Agent。")
    sub = parser.add_subparsers(dest="command", required=True)
    web = sub.add_parser("serve")
    web.add_argument("--port", type=int, default=8766)
    for command in ("find", "read", "probe"):
        child = sub.add_parser(command)
        child.add_argument("--host", default="local")
        if command == "find":
            child.add_argument("--query", default="")
            child.add_argument("--cwd", default="")
            child.add_argument("--limit", type=int, default=15)
        elif command == "read":
            child.add_argument("thread_id")
            child.add_argument("--limit", type=int, default=12)
    args = parser.parse_args(argv)
    if args.command == "serve":
        serve(args.port)
        return
    hub = Hub()
    try:
        request = {"op": args.command, **vars(args)}
        request.pop("command", None)
        request.pop("host", None)
        print(json.dumps(hub.history(args.host, request), ensure_ascii=False, indent=2))
    finally:
        hub.close()


if __name__ == "__main__":
    main()
