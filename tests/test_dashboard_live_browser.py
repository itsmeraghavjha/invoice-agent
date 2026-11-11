"""The LIVE dashboard in a real browser: a real server, a real page, and a pretend bot that writes
to the database while the page is open. Checks the page updates by itself, without a reload.

Needs:  pip install pytest playwright   and   playwright install chromium   (skipped otherwise)
"""
import os
import sys
import threading
import time
from datetime import datetime

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.dashboard import build_dashboard
from modules.dashboard_server import DashboardApp, make_server
from modules.database import HistoryDB
from modules.demo_data import make_demo_db

FAST = 1   # seconds between checks, so tests do not wait 30 s


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Live:
    def __init__(self, db, password=None, port=0):
        self.db = db
        self.app = DashboardApp(db, stale_hours=24, refresh_seconds=30, password=password)
        self.app.refresh_seconds = FAST                      # tests only: the real minimum is 5 s
        self.server = make_server(self.app, "127.0.0.1", port)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def bot_run(db, files=(), issues=()):
    """Do what the real bot does at the end of a run."""
    h = HistoryDB(db)
    run = h.start_run("both")
    for name in files:
        h.log_invoice(run, {"file_id": name, "kind": "NonPO", "file_name": name, "folder": "Regions/North/Nov-26",
                            "received_at": now_iso(), "gross_amount": 100.0, "line_items": 1, "warnings": ""})
    for name in issues:
        h.log_issue(run, "failed_file", name, file_id=name, file_name=name, folder="Regions/North/Nov-26",
                    detail="429 RESOURCE_EXHAUSTED")
    h.finish_run(run, pdfs_seen=len(files) + len(issues), new_files=len(files), failed_files=len(issues))
    h.close()


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "live.db")
    HistoryDB(path).close()
    bot_run(path, files=["first.pdf"])
    return path


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"Chromium is not available: {e}")
        yield b
        b.close()


@pytest.fixture
def session(browser):
    made = {"ctx": [], "srv": []}

    def start(db, password=None, credentials=None, port=0, **ctx):
        srv = Live(db, password, port)
        made["srv"].append(srv)
        opts = {"timezone_id": "Asia/Kolkata", "locale": "en-IN", "viewport": {"width": 1440, "height": 900}, **ctx}
        if credentials:
            opts["http_credentials"] = credentials
        context = browser.new_context(accept_downloads=True, **opts)
        made["ctx"].append(context)
        page = context.new_page()
        page.problems = []
        page.on("console", lambda m: page.problems.append(m.text) if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: page.problems.append(str(e)))
        return srv, page

    yield start
    for c in made["ctx"]:
        c.close()
    for s in made["srv"]:
        try:
            s.stop()
        except Exception:
            pass


def wait_until(page, js, timeout=10.0):
    """Poll a JavaScript expression until it is true. (page.wait_for_function evaluates text with
    eval(), which the page's security policy rightly blocks; page.evaluate is not affected.)"""
    deadline = time.monotonic() + timeout
    while not page.evaluate(js):
        if time.monotonic() > deadline:
            raise AssertionError(f"Timed out after {timeout:.0f}s waiting for: {js}")
        page.wait_for_timeout(100)


def rows(page):
    return page.locator("#tbody tr.row").count()


def show_all(page):
    page.click('#seg [data-view="all"]')


# ------------------------------------------------------------------ the tests
def test_a_new_bot_run_appears_on_its_own_and_your_view_is_left_alone(session, db):
    srv, page = session(db)
    page.goto(srv.url)
    show_all(page)
    page.fill("#q", "pdf")
    page.locator("#tbody .xbtn").first.click()                 # a row the person has opened
    page.evaluate("window.__same_page = 42")                   # would vanish if the page reloaded
    assert rows(page) == 1 and page.locator("#tbody tr.detail").count() == 1
    assert "Live" in page.inner_text("#live")

    bot_run(db, files=["second.pdf", "third.pdf"])             # "the bot runs"

    wait_until(page, "document.querySelectorAll('#tbody tr.row').length === 3")
    assert page.evaluate("window.__same_page") == 42           # the page was not reloaded
    assert page.input_value("#q") == "pdf"                     # the search is kept
    assert page.locator("#tbody tr.detail").count() == 1       # and so is the row that was open
    assert page.problems == []


def test_failures_and_the_alert_banner_update_live(session, db):
    srv, page = session(db)
    page.goto(srv.url)
    assert "needs a look" not in page.inner_text("#alerts")

    bot_run(db, issues=["stuck.pdf"])

    wait_until(page, "document.getElementById('alerts').innerText.includes('needs a look')")
    page.click('#alerts button:has-text("Show failed")')
    assert "stuck.pdf" in page.inner_text("#tbody")


def test_the_bot_runs_table_and_last_run_line_follow_new_runs(session, db):
    srv, page = session(db)
    page.goto(srv.url)
    assert page.locator("#runs tr").count() == 1
    bot_run(db, files=["x.pdf"])
    wait_until(page, "document.querySelectorAll('#runs tr').length === 2")
    assert "Last bot run" in page.inner_text("#health")


def test_losing_the_server_is_called_out_and_coming_back_clears_it(session, db):
    srv, page = session(db)
    page.goto(srv.url)
    show_all(page)
    assert rows(page) == 1
    port = srv.port

    srv.stop()                                                  # the dashboard computer goes away

    wait_until(page, "document.getElementById('live').classList.contains('lost')", 10.0)
    text = page.inner_text("#alerts")
    assert "Live updates have stopped" in text and "out of date" in text
    assert "Not connected" in page.inner_text("#live")
    assert rows(page) == 1                                      # what was on screen is still there, not blanked

    bot_run(db, files=["while-away.pdf"])
    back = Live(db, port=port)                                  # ...and comes back
    try:
        wait_until(page, "!document.getElementById('live').classList.contains('lost')", 15.0)
        assert "Live updates have stopped" not in page.inner_text("#alerts")
        wait_until(page, "document.querySelectorAll('#tbody tr.row').length === 2", 10.0)
    finally:
        back.stop()


def test_try_now_button_reconnects_immediately(session, db):
    srv, page = session(db)
    page.goto(srv.url)
    port = srv.port
    srv.stop()
    wait_until(page, "document.getElementById('live').classList.contains('lost')", 10.0)
    back = Live(db, port=port)
    try:
        page.click('#alerts button:has-text("Try now")')
        wait_until(page, "!document.getElementById('live').classList.contains('lost')", 10.0)
    finally:
        back.stop()


def test_a_bot_that_silently_stops_is_flagged_even_if_nobody_refreshes(session, db):
    srv, page = session(db)
    page.clock.install()
    page.goto(srv.url)
    assert "has not run for" not in page.inner_text("#alerts")
    assert "late" not in page.get_attribute("#health", "class")

    page.clock.fast_forward("30:00:00")                         # 30 hours pass with the page left open
    wait_until(page, "document.getElementById('alerts').innerText.includes('has not run for')", 10.0)
    assert "late" in page.get_attribute("#health", "class")


def test_password_protected_page_keeps_updating_in_the_browser(session, db):
    srv, page = session(db, password="team-pass", credentials={"username": "team", "password": "team-pass"})
    page.goto(srv.url)
    show_all(page)
    assert rows(page) == 1
    bot_run(db, files=["after-login.pdf"])
    wait_until(page, "document.querySelectorAll('#tbody tr.row').length === 2", 10.0)   # polling is authenticated too


def test_without_the_password_nothing_is_shown(session, db):
    srv, page = session(db, password="team-pass")
    response = page.goto(srv.url)
    assert response.status == 401
    assert "first.pdf" not in page.content()


def test_the_wrong_password_shows_nothing(session, db):
    srv, page = session(db, password="team-pass", credentials={"username": "x", "password": "nope"})
    assert page.goto(srv.url).status == 401


def test_page_runs_cleanly_under_the_security_policy_including_csv_download(session, db):
    srv, page = session(db)
    page.goto(srv.url)
    for view in ("week", "month", "all", "day"):
        page.click(f'#seg [data-view="{view}"]')
    show_all(page)
    page.locator("#tbody .xbtn").first.click()
    page.select_option("#status", "ok")
    with page.expect_download() as dl:
        page.click("#csv")
    assert open(dl.value.path(), encoding="utf-8").read().lstrip("﻿").startswith("Status,")
    page.wait_for_timeout(2500)                                 # a couple of live refreshes happen meanwhile
    assert page.problems == []                                  # no blocked scripts, styles or requests


def test_a_saved_html_file_is_not_live_and_makes_no_requests(browser, tmp_path):
    path = make_demo_db(str(tmp_path / "d.db"))
    out = build_dashboard(path, str(tmp_path / "d.html"), stale_hours=24, demo=True)
    ctx = browser.new_context()
    page = ctx.new_page()
    seen = []
    page.on("request", lambda r: seen.append(r.url))
    page.goto("file://" + out)
    page.wait_for_timeout(1500)
    assert page.is_hidden("#live")
    assert all(u.startswith("file://") or u.startswith("data:") for u in seen)
    ctx.close()


def test_live_refresh_does_not_pile_up_requests_when_the_server_is_slow(session, db):
    srv, page = session(db)
    hits = []
    page.on("request", lambda r: hits.append(r.url) if r.url.endswith("data.json") else None)
    page.goto(srv.url)
    page.wait_for_timeout(4500)
    assert 2 <= len(hits) <= 6                                  # about one a second, never a flood
