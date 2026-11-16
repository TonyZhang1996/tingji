"""Loopback-only desktop UI server with session-token APIs and bounded media ranges."""
from __future__ import annotations

import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
import secrets
import threading
from urllib.parse import unquote, urlsplit

from .desktop_controller import MeetingController
from .version import __version__


class DesktopServer:
    def __init__(self, controller: MeetingController, dist_dir: Path, port: int = 0):
        self.controller = controller
        self.dist_dir = Path(dist_dir).resolve()
        self.token = secrets.token_urlsafe(32)
        self.nonce = secrets.token_urlsafe(18)
        owner = self
        class Handler(BaseHTTPRequestHandler):
            server_version = f"MeetingDesktop/{__version__}"
            def log_message(self, *_args):
                pass
            def do_GET(self):
                owner._handle(self, False)
            def do_HEAD(self):
                owner._handle(self, True)
            def do_POST(self):
                owner._post(self)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_port
        self.url = f"http://127.0.0.1:{self.port}/"
        self.origin = self.url.rstrip("/")
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="meeting-ui-http")
        self._thread.start()
        return self

    def shutdown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread:
            self._thread.join(timeout=3)

    def _headers(self, handler, status, mime, size=None, *, cookie=False, extra=None):
        handler.send_response(status)
        handler.send_header("Content-Type", mime)
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Security-Policy", f"default-src 'self'; script-src 'self' 'nonce-{self.nonce}'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        if cookie:
            handler.send_header("Set-Cookie", f"meeting_session={self.token}; HttpOnly; SameSite=Strict; Path=/")
        if size is not None:
            handler.send_header("Content-Length", str(size))
        for key, value in (extra or {}).items():
            handler.send_header(key, value)
        handler.end_headers()

    def _json(self, handler, status, value, head=False):
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        try:
            self._headers(handler, status, "application/json; charset=utf-8", len(raw))
            if not head:
                handler.wfile.write(raw)
        except ConnectionError:
            # Closing/changing audio can abort an otherwise valid local request.
            pass

    def _host_ok(self, handler):
        return handler.headers.get("Host", "") == f"127.0.0.1:{self.port}"

    def _token_ok(self, handler):
        return hmac.compare_digest(handler.headers.get("X-Meeting-Token", ""), self.token)

    def _cookie_ok(self, handler):
        try:
            cookies = SimpleCookie()
            cookies.load(handler.headers.get("Cookie", ""))
            value = cookies["meeting_session"].value if "meeting_session" in cookies else ""
            return hmac.compare_digest(value, self.token)
        except Exception:
            return False

    def _handle(self, handler, head=False):
        try:
            if not self._host_ok(handler):
                return self._json(handler, 403, {"message": "不允许的访问来源。"}, head)
            path = unquote(urlsplit(handler.path).path)
            if path.startswith("/api/media/"):
                if not self._cookie_ok(handler):
                    return self._json(handler, 403, {"message": "桌面会话已失效，请重新打开工具。"}, head)
                return self._media(handler, path.removeprefix("/api/media/"), head)
            if path.startswith("/api/"):
                if not self._token_ok(handler):
                    return self._json(handler, 403, {"message": "桌面会话已失效，请重新打开工具。"}, head)
                if path == "/api/state":
                    return self._json(handler, 200, self.controller.state(), head)
                return self._json(handler, 404, {"message": "接口不存在。"}, head)
            if path in ("/", "/index.html"):
                entry = self.dist_dir / "index.html"
                if not entry.is_file():
                    return self._json(handler, 503, {"message": "界面文件尚未构建，请先完成前端构建。"}, head)
                html = entry.read_text(encoding="utf-8")
                bootstrap = f'<script nonce="{self.nonce}">window.__MEETING_TOKEN__={json.dumps(self.token)};</script>'
                html = html.replace("<head>", "<head>" + bootstrap, 1) if "<head>" in html else bootstrap + html
                data = html.encode("utf-8")
                self._headers(handler, 200, "text/html; charset=utf-8", len(data), cookie=True)
                if not head:
                    handler.wfile.write(data)
                return
            relative = path.lstrip("/")
            asset = (self.dist_dir / relative).resolve()
            if not asset.is_relative_to(self.dist_dir) or not asset.is_file():
                return self._json(handler, 404, {"message": "文件不存在。"}, head)
            mime = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
            if asset.suffix in (".js", ".mjs"):
                mime = "text/javascript"
            self._file(handler, asset, mime, head)
        except ConnectionError:
            pass
        except FileNotFoundError:
            self._json(handler, 404, {"message": "文件不存在或已移动。"}, head)
        except Exception:
            self._json(handler, 500, {"message": "读取本地界面数据失败，请查看任务是否仍在写入。"}, head)

    def _post(self, handler):
        try:
            if (not self._host_ok(handler) or not self._token_ok(handler)
                    or handler.headers.get("Origin", self.origin) != self.origin):
                return self._json(handler, 403, {"message": "不允许的访问来源或会话已失效。"})
            path = urlsplit(handler.path).path
            if not path.startswith("/api/"):
                return self._json(handler, 404, {"message": "接口不存在。"})
            if handler.headers.get_content_type() != "application/json":
                return self._json(handler, 415, {"message": "请使用 JSON 请求。"})
            try:
                length = int(handler.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length < 0 or length > 1024 * 1024:
                return self._json(handler, 413, {"message": "请求内容过大。"})
            raw = handler.rfile.read(length)
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(payload, dict):
                raise ValueError("请求内容必须是对象。")
            state = self.controller.dispatch(path.removeprefix("/api/"), payload)
            self._json(handler, 200, state)
        except (ValueError, json.JSONDecodeError, UnicodeError) as exc:
            # App validation messages are controlled and do not include request bodies.
            self._json(handler, 400, {"message": str(exc) if not isinstance(exc, (json.JSONDecodeError, UnicodeError)) else "请求内容格式错误。"})
        except ConnectionError:
            pass
        except Exception:
            self._json(handler, 500, {"message": "操作未完成，进度已经保留；请检查本地文件和环境。"})

    def _media(self, handler, identifier, head):
        if not re.fullmatch(r"[a-f0-9]{24}", identifier):
            return self._json(handler, 404, {"message": "音频不存在。"}, head)
        file = self.controller.media_path(identifier)
        self._file(handler, file, mimetypes.guess_type(file.name)[0] or "application/octet-stream", head, ranges=True)

    def _file(self, handler, file, mime, head=False, *, ranges=False):
        size = file.stat().st_size
        start, end, status = 0, size - 1, 200
        extra = {"Accept-Ranges": "bytes"} if ranges else {}
        requested = handler.headers.get("Range") if ranges else None
        if requested:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested.strip())
            if not match or not any(match.groups()):
                return self._range_error(handler, size, head)
            left, right = match.groups()
            if not left:
                suffix = int(right)
                if suffix <= 0:
                    return self._range_error(handler, size, head)
                start, end = max(0, size - suffix), size - 1
            else:
                start = int(left)
                end = min(int(right), size - 1) if right else size - 1
            if start >= size or start < 0 or end < start:
                return self._range_error(handler, size, head)
            status = 206
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"
        length = max(0, end - start + 1)
        self._headers(handler, status, mime, length, extra=extra)
        if not head:
            with file.open("rb") as stream:
                stream.seek(start)
                remaining = length
                while remaining:
                    data = stream.read(min(256 * 1024, remaining))
                    if not data:
                        break
                    handler.wfile.write(data)
                    remaining -= len(data)

    def _range_error(self, handler, size, head):
        self._headers(handler, 416, "text/plain; charset=utf-8", 0, extra={"Content-Range": f"bytes */{size}"})

