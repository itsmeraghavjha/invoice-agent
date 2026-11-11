"""Live team dashboard: a small, READ-ONLY web server (Python standard library only).

The team opens http://<this-computer>:<port> and the page keeps itself up to date: it asks
for fresh data every few seconds and redraws without a reload. The server only ever reads the
history database; it cannot change anything, and it does not run the bot.

Safety defaults
- Listens on this computer only (127.0.0.1) unless told otherwise.
- Refuses to listen on the network without a password, unless explicitly overridden.
- Only three GET routes exist; no files are served from disk.
"""
import base64
import gzip
import hashlib
import hmac
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from modules.dashboard import collect_data, render_html

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
MAX_CACHE_AGE = 30   # seconds: rebuild at least this often even if the database file looks unchanged

# The page is one inline script + inline styles and only talks to its own server.
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class DashboardApp:
    """Builds (and caches) what the server sends. Cached until the database file changes."""

    def __init__(self, db_file, stale_hours=24, refresh_seconds=30, password=None, demo=False):
        self.db_file = db_file
        self.stale_hours = stale_hours
        self.refresh_seconds = max(5, int(refresh_seconds))
        self.password = password or None
        self.demo = demo
        self._lock = threading.Lock()
        self._cache = None   # (signature, built_at, snapshot)

    def _signature(self):
        sig = []
        for suffix in ("", "-journal", "-wal"):
            try:
                st = os.stat(self.db_file + suffix)
                sig.append((suffix, st.st_mtime_ns, st.st_size))
            except OSError:
                sig.append((suffix, None, None))
        return tuple(sig)

    def snapshot(self):
        """(data_json_bytes, gzip_bytes, html_bytes, etag). Raises if the database cannot be read."""
        with self._lock:
            sig, now = self._signature(), time.monotonic()
            if self._cache and self._cache[0] == sig and now - self._cache[1] < MAX_CACHE_AGE:
                return self._cache[2]
            data = collect_data(self.db_file, self.stale_hours, self.demo, read_only=True)
            data["live"] = {"url": "data.json", "every": self.refresh_seconds}
            body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            # The tag ignores the timestamp, so "nothing changed" really answers 304.
            stable = dict(data, generated_at=None)
            etag = '"' + hashlib.sha1(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:24] + '"'
            snap = (body, gzip.compress(body, 5), render_html(data).encode("utf-8"), etag)
            self._cache = (sig, now, snap)
            return snap

    def password_ok(self, header):
        if not self.password:
            return True
        try:
            scheme, _, token = (header or "").partition(" ")
            if scheme.lower() != "basic":
                return False
            supplied = base64.b64decode(token.strip()).decode("utf-8").partition(":")[2]
        except Exception:
            return False
        return hmac.compare_digest(supplied.encode("utf-8"), self.password.encode("utf-8"))


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "InvoiceBotDashboard"
        sys_version = ""
        timeout = 30   # a stalled client cannot hold a thread forever

        # ---- helpers
        def _send(self, status, body=b"", ctype="text/plain; charset=utf-8", extra=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD" and status != 304:
                self.wfile.write(body)

        def _authorised(self):
            if app.password_ok(self.headers.get("Authorization")):
                return True
            time.sleep(0.4)   # takes the speed out of password guessing
            self._send(401, b"Password needed.\n", extra={
                "WWW-Authenticate": 'Basic realm="Invoice Bot team view", charset="UTF-8"', "Cache-Control": "no-store"})
            return False

        def _serve(self):
            path = urlsplit(self.path).path
            if path == "/healthz":                      # no data, no password: for uptime checks
                return self._send(200, b'{"ok":true}', "application/json", {"Cache-Control": "no-store"})
            if path not in ("/", "/index.html", "/data.json"):
                return self._send(404, b"Not found.\n")
            if not self._authorised():
                return
            try:
                body, gz, html, etag = app.snapshot()
            except Exception as e:                      # e.g. database locked for too long
                msg = json.dumps({"error": "Could not read the history database", "detail": str(e)}).encode()
                return self._send(503, msg, "application/json", {"Cache-Control": "no-store", "Retry-After": "5"})

            is_json = path == "/data.json"
            tag = etag if is_json else etag[:-1] + '-page"'
            headers = {"ETag": tag, "Cache-Control": "no-cache", "Vary": "Accept-Encoding, Authorization"}
            if self.headers.get("If-None-Match") == tag:
                return self._send(304, b"", extra=headers)
            if is_json:
                if "gzip" in (self.headers.get("Accept-Encoding") or ""):
                    headers["Content-Encoding"] = "gzip"
                    return self._send(200, gz, "application/json; charset=utf-8", headers)
                return self._send(200, body, "application/json; charset=utf-8", headers)
            self._send(200, html, "text/html; charset=utf-8", headers)

        def _refuse(self):
            self._send(405, b"This page is read-only.\n", extra={"Allow": "GET, HEAD"})

        # ---- only reading is allowed
        do_GET = do_HEAD = _serve
        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse

        def log_message(self, fmt, *args):
            pass   # polling would drown the console; startup and errors are printed by the launcher

    return Handler


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(app, host="127.0.0.1", port=8080):
    return DashboardServer((host, port), make_handler(app))


def check_bind(host, password, allow_open=False):
    """Refuse to put invoice data on the network without a password (unless overridden)."""
    if host in LOOPBACK or password or allow_open:
        return
    raise SystemExit(
        "Not starting: --host %s would let anyone on the network read the invoices, and no password is set.\n"
        "  Set DASHBOARD_PASSWORD in .env (recommended), or pass --no-password if you really want it open." % host)


def lan_address():
    """This computer's address on the local network, for telling the team where to go."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))   # no traffic is sent; it just picks the right network card
            return s.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "this-computer"
