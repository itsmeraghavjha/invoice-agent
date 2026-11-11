"""Tests for the live dashboard server at the HTTP level (no browser needed)."""
import base64
import gzip
import http.client
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import dashboard_server
from modules.dashboard_server import DashboardApp, check_bind, make_server
from modules.database import HistoryDB
from modules.demo_data import make_demo_db


class Running:
    """A real server on a free port, for the length of a test."""

    def __init__(self, db, password=None, port=0):
        self.app = DashboardApp(db, stale_hours=24, refresh_seconds=30, password=password)
        self.server = make_server(self.app, "127.0.0.1", port)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def get(self, path="/", method="GET", headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        out = (r.status, {k.lower(): v for k, v in r.getheaders()}, data)
        c.close()
        return out

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "live.db")
    h = HistoryDB(path)
    run = h.start_run("both")
    h.log_invoice(run, {"file_id": "a1", "kind": "NonPO", "file_name": "a1.pdf", "folder": "Regions/N/Nov-26",
                        "received_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "gross_amount": 10.0, "line_items": 1, "warnings": ""})
    h.finish_run(run, pdfs_seen=1, new_files=1)
    h.close()
    return path


@pytest.fixture
def srv(db):
    s = Running(db)
    yield s
    s.stop()


def basic(password, user="anyone"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


# ------------------------------------------------------------------ routes
def test_page_and_data_are_served_with_live_settings_and_safe_headers(srv):
    status, h, body = srv.get("/")
    assert status == 200 and h["content-type"].startswith("text/html")
    page = body.decode()
    data = json.loads(re.search(r'<script id="app-data" type="application/json">(.*?)</script>', page, re.S).group(1))
    assert data["live"] == {"url": "data.json", "every": 30} and [f["id"] for f in data["files"]] == ["a1"]

    status, h, body = srv.get("/data.json")
    assert status == 200 and h["content-type"].startswith("application/json")
    assert json.loads(body)["live"]["url"] == "data.json"
    for hh in (h, srv.get("/")[1]):
        assert hh["x-content-type-options"] == "nosniff" and hh["x-frame-options"] == "DENY"
        assert "default-src 'none'" in hh["content-security-policy"] and "connect-src 'self'" in hh["content-security-policy"]
        assert "python" not in hh.get("server", "").lower()          # no version information handed out


def test_healthz_is_open_and_shows_no_data(db):
    s = Running(db, password="secret")
    try:
        status, _, body = s.get("/healthz")
        assert status == 200 and json.loads(body) == {"ok": True}
    finally:
        s.stop()


@pytest.mark.parametrize("path", ["/nope", "/modules/dashboard.py", "/invoice_history.db", "/../../etc/passwd",
                                  "/%2e%2e/%2e%2e/etc/passwd", "/.env", "/data.json/../../config.py", "//etc/passwd"])
def test_nothing_else_is_served_from_disk(srv, path):
    status, _, body = srv.get(path)
    assert status == 404 and b"root:" not in body and b"GEMINI" not in body


def test_query_strings_are_ignored_not_executed(srv):
    assert srv.get("/data.json?x=1&y=<script>")[0] == 200


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_it_is_read_only(srv, method):
    status, h, _ = srv.get("/data.json", method=method, body=b"x=1" if method in ("POST", "PUT", "PATCH") else None)
    assert status == 405 and h["allow"] == "GET, HEAD"


def test_head_returns_headers_without_a_body(srv):
    status, h, body = srv.get("/data.json", method="HEAD")
    assert status == 200 and body == b"" and int(h["content-length"]) > 0


# ---------------------------------------------------------------- password
def test_password_is_required_and_the_username_does_not_matter(db):
    s = Running(db, password="s3cret pässword")
    try:
        status, h, body = s.get("/")
        assert status == 401 and "Basic" in h["www-authenticate"] and b"a1" not in body
        assert s.get("/data.json")[0] == 401
        assert s.get("/", headers=basic("wrong"))[0] == 401
        assert s.get("/", headers=basic("s3cret pässword", user="anyone"))[0] == 200
        assert s.get("/data.json", headers=basic("s3cret pässword", user=""))[0] == 200
        for junk in ("Basic !!!not-base64", "Basic", "Bearer abc", "Basic " + base64.b64encode(b"nocolon").decode(), ""):
            assert s.get("/", headers={"Authorization": junk})[0] == 401, junk     # never a 500
    finally:
        s.stop()


def test_an_empty_password_means_no_password(db):
    s = Running(db, password="")
    try:
        assert s.get("/")[0] == 200
    finally:
        s.stop()


# ----------------------------------------------------------------- caching
def test_etag_gives_304_until_the_database_changes(srv, db):
    status, h, _ = srv.get("/data.json")
    tag = h["etag"]
    status, h2, body = srv.get("/data.json", headers={"If-None-Match": tag})
    assert status == 304 and body == b"" and h2["etag"] == tag

    time.sleep(0.05)
    bot = HistoryDB(db)                                   # "the bot runs"
    run = bot.start_run("both")
    bot.log_invoice(run, {"file_id": "b2", "kind": "PO", "file_name": "b2.pdf", "folder": "R/Nov-26/1. Capex",
                          "received_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                          "gross_amount": 5.0, "line_items": 1, "warnings": ""})
    bot.finish_run(run, new_files=1)
    bot.close()

    status, h3, body = srv.get("/data.json", headers={"If-None-Match": tag})
    assert status == 200 and h3["etag"] != tag
    assert {f["id"] for f in json.loads(body)["files"]} == {"a1", "b2"}


def test_page_and_data_have_different_tags_so_neither_is_mistaken_for_the_other(srv):
    assert srv.get("/")[1]["etag"] != srv.get("/data.json")[1]["etag"]


def test_gzip_when_asked_and_identical_content(srv):
    _, _, plain = srv.get("/data.json")
    status, h, packed = srv.get("/data.json", headers={"Accept-Encoding": "gzip, deflate"})
    assert status == 200 and h["content-encoding"] == "gzip" and len(packed) < len(plain)
    assert json.loads(gzip.decompress(packed)) == json.loads(plain)


def test_the_database_is_only_read_when_it_changes(db, monkeypatch):
    calls = []
    real = dashboard_server.collect_data
    monkeypatch.setattr(dashboard_server, "collect_data", lambda *a, **k: calls.append(1) or real(*a, **k))
    s = Running(db)
    try:
        for _ in range(10):
            s.get("/data.json")
            s.get("/")
        assert len(calls) == 1                           # twenty requests, one database read
    finally:
        s.stop()


def test_many_people_at_once(srv):
    with ThreadPoolExecutor(20) as pool:
        results = list(pool.map(lambda i: srv.get("/data.json" if i % 2 else "/")[0], range(60)))
    assert set(results) == {200}


# -------------------------------------------------------------- never writes
def test_serving_never_changes_the_database_file(db):
    before, mtime = open(db, "rb").read(), os.stat(db).st_mtime_ns
    s = Running(db)
    try:
        for p in ("/", "/data.json", "/nope"):
            s.get(p)
        s.get("/data.json", method="POST", body=b"x")
    finally:
        s.stop()
    assert open(db, "rb").read() == before and os.stat(db).st_mtime_ns == mtime
    assert sorted(os.listdir(os.path.dirname(db))) == ["live.db"]      # no journal or temp files left


def test_an_old_database_without_the_new_tables_is_served_without_being_upgraded(tmp_path):
    path = str(tmp_path / "old.db")
    h = HistoryDB(path); h.log_success("legacy", "legacy.pdf"); h.close()
    s = Running(path)
    try:
        d = json.loads(s.get("/data.json")[2])
        assert [f["id"] for f in d["files"]] == ["legacy"] and d["runs"] == []
    finally:
        s.stop()
    import sqlite3
    assert {r[0] for r in sqlite3.connect(path).execute("SELECT name FROM sqlite_master WHERE type='table'")} == {"processed_files"}


def test_no_database_yet_is_an_empty_page_not_an_error_and_creates_nothing(tmp_path):
    missing = str(tmp_path / "not-yet.db")
    s = Running(missing)
    try:
        status, _, body = s.get("/data.json")
        assert status == 200 and json.loads(body)["files"] == [] and not os.path.exists(missing)
        assert s.get("/")[0] == 200
    finally:
        s.stop()


def test_an_unreadable_database_is_a_503_the_page_can_survive(tmp_path):
    s = Running(str(tmp_path))                            # a folder where a database file should be
    try:
        status, h, body = s.get("/data.json")
        assert status == 503 and h["retry-after"] == "5" and "error" in json.loads(body)
        assert s.get("/healthz")[0] == 200                # the server itself is still fine
    finally:
        s.stop()


def test_a_server_can_be_stopped_and_started_again_on_the_same_port(db):
    s = Running(db)
    port = s.port
    s.stop()
    again = Running(db, port=port)
    try:
        assert again.get("/healthz")[0] == 200
    finally:
        again.stop()


# --------------------------------------------------------------- bind safety
def test_network_access_without_a_password_is_refused():
    with pytest.raises(SystemExit) as e:
        check_bind("0.0.0.0", None)
    assert "DASHBOARD_PASSWORD" in str(e.value) and "--no-password" in str(e.value)
    with pytest.raises(SystemExit):
        check_bind("192.168.1.20", "")


@pytest.mark.parametrize("host,password,allow", [("127.0.0.1", None, False), ("localhost", None, False),
                                                 ("0.0.0.0", "pw", False), ("0.0.0.0", None, True)])
def test_safe_ways_to_start(host, password, allow):
    check_bind(host, password, allow)


def test_refresh_interval_cannot_be_set_below_five_seconds(db):
    assert DashboardApp(db, refresh_seconds=1).refresh_seconds == 5
    assert DashboardApp(db, refresh_seconds=45).refresh_seconds == 45


def test_a_port_that_is_taken_fails_cleanly(srv):
    with pytest.raises(OSError):
        make_server(srv.app, "127.0.0.1", srv.port)
