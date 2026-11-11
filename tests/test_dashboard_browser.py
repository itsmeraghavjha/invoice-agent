"""Opens the generated team page in a real browser and checks what a person would see.

Expected numbers are worked out here, in Python, from the data embedded in the page (in the
team's timezone), so the page is not just being checked against its own logic.

Needs:  pip install pytest playwright   and   playwright install chromium
Skipped automatically when either is missing.
"""
import csv
import io
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.dashboard import build_dashboard
from modules.database import HistoryDB
from modules.demo_data import make_demo_db

TZ = ZoneInfo("Asia/Kolkata")


# ------------------------------------------------------------------ fixtures
@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # no browser installed on this machine
            pytest.skip(f"Chromium is not available: {e}")
        yield b
        b.close()


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    d = tmp_path_factory.mktemp("demo")
    db = make_demo_db(str(d / "demo.db"))
    out = build_dashboard(db, str(d / "demo.html"), stale_hours=24, demo=True)
    return out, embedded(out)


def embedded(path):
    html = open(path, encoding="utf-8").read()
    return json.loads(re.search(r'<script id="app-data" type="application/json">(.*?)</script>', html, re.S).group(1))


class Opened:
    def __init__(self, browser, path, **ctx):
        self.context = browser.new_context(timezone_id="Asia/Kolkata", locale="en-IN",
                                           accept_downloads=True, **{"viewport": {"width": 1440, "height": 900}, **ctx})
        self.page = self.context.new_page()
        self.problems, self.dialogs = [], []
        self.page.on("console", lambda m: self.problems.append(m.text) if m.type in ("error", "warning") else None)
        self.page.on("pageerror", lambda e: self.problems.append(str(e)))
        self.page.on("dialog", lambda d: (self.dialogs.append(d.message), d.dismiss()))
        self.page.goto("file://" + path)

    def close(self):
        self.context.close()


@pytest.fixture
def open_page(browser):
    opened = []

    def _open(path, **ctx):
        o = Opened(browser, path, **ctx)
        opened.append(o)
        return o
    yield _open
    for o in opened:
        o.close()


def total(page):
    m = re.search(r"of (\d+) file", page.inner_text("#count"))
    return int(m.group(1)) if m else 0


def local(iso):
    return datetime.fromisoformat(iso).astimezone(TZ)


def in_view(files, view, anchor):
    out = []
    for f in files:
        if not f["received"]:
            continue
        d = local(f["received"]).date()
        if view == "day" and d == anchor:
            out.append(f)
        elif view == "week":
            mon = anchor - timedelta(days=anchor.weekday())
            if mon <= d < mon + timedelta(days=7):
                out.append(f)
        elif view == "month" and (d.year, d.month) == (anchor.year, anchor.month):
            out.append(f)
    return out if view != "all" else list(files)


def today_ist():
    return datetime.now(TZ).date()


def click_view(page, name):
    page.click(f'#seg [data-view="{name}"]')


# ------------------------------------------------------------------ the tests
def test_opens_cleanly_on_today(open_page, demo):
    path, data = demo
    o = open_page(path)
    assert o.problems == [] and o.dialogs == []
    assert "Today" in o.page.inner_text("#plabel")
    assert total(o.page) == len(in_view(data["files"], "day", today_ist()))
    assert o.page.is_visible("#demo")                       # invented data is clearly labelled


@pytest.mark.parametrize("view", ["day", "week", "month", "all"])
def test_each_period_shows_exactly_the_files_in_it(open_page, demo, view):
    path, data = demo
    o = open_page(path)
    click_view(o.page, view)
    assert total(o.page) == len(in_view(data["files"], view, today_ist()))


def test_previous_period_and_next_is_blocked_in_the_future(open_page, demo):
    path, data = demo
    o = open_page(path)
    click_view(o.page, "month")
    assert o.page.is_disabled("#next")
    o.page.click("#prev")
    last_month = (today_ist().replace(day=1) - timedelta(days=1))
    assert total(o.page) == len(in_view(data["files"], "month", last_month))
    assert not o.page.is_disabled("#next")
    o.page.click("#today")
    assert total(o.page) == len(in_view(data["files"], "month", today_ist()))


def test_clicking_a_day_in_the_strip_opens_that_day(open_page, demo):
    path, data = demo
    o = open_page(path)
    click_view(o.page, "month")
    busiest = max((d for d in range(1, today_ist().day + 1)),
                  key=lambda d: len(in_view(data["files"], "day", today_ist().replace(day=d))))
    o.page.locator('#strip button', has=o.page.locator('b', has_text=re.compile(rf'^{busiest}$'))).first.click()
    assert total(o.page) == len(in_view(data["files"], "day", today_ist().replace(day=busiest)))


def test_search_needs_every_word_and_ignores_case(open_page, demo):
    path, data = demo
    o = open_page(path)
    click_view(o.page, "all")
    f = next(x for x in data["files"] if x["vendor"] and x["folder"])
    words = [f["vendor"], f["folder"].split("/")[-1].upper()]
    o.page.fill("#q", " ".join(words))
    o.page.wait_for_timeout(300)
    hay = lambda x: " ".join(str(x.get(k) or "") for k in
                             ("name", "invoice_no", "vendor", "plant", "po", "grn", "folder", "notes", "invoice_date")).lower()
    expected = [x for x in data["files"] if all(w.lower() in hay(x) for w in words)]
    assert 0 < len(expected) and total(o.page) == len(expected)
    o.page.fill("#q", "zzz-no-such-invoice")
    o.page.wait_for_timeout(300)
    assert "No files match" in o.page.inner_text("#empty") and o.page.is_visible("#clear")
    o.page.click("#clear")
    assert total(o.page) == len(data["files"]) and o.page.input_value("#q") == ""


def test_status_type_and_location_filters(open_page, demo):
    path, data = demo
    o = open_page(path)
    click_view(o.page, "all")
    files = data["files"]
    for status, ok in [("failed", lambda f: f["status"] == "failed"), ("review", lambda f: f["status"] == "review"),
                       ("skipped", lambda f: f["status"] == "skipped"), ("ok", lambda f: f["status"] == "ok"),
                       ("attention", lambda f: f["status"] != "ok")]:
        o.page.select_option("#status", status)
        assert total(o.page) == len([f for f in files if ok(f)]), status
    o.page.select_option("#status", "all")
    o.page.select_option("#type", "PO")
    assert total(o.page) == len([f for f in files if f["kind"] == "PO"])
    o.page.select_option("#type", "old")
    assert total(o.page) == len([f for f in files if f["legacy"]])
    o.page.select_option("#type", "")
    loc = o.page.locator("#loc option").nth(1).get_attribute("value")
    o.page.select_option("#loc", loc)
    assert 0 < total(o.page) < len(files)
    assert all(loc.lower() in (r.lower()) for r in o.page.locator("#tbody tr.row td:nth-child(5)").all_inner_texts()[:20])


def test_sorting_by_amount_both_ways(open_page, demo):
    path, data = demo
    o = open_page(path)
    click_view(o.page, "all")
    amounts = [f["gross"] for f in data["files"] if f["gross"] is not None]

    def shown():
        cells = o.page.locator("#tbody tr.row td:nth-child(8)").all_inner_texts()
        return [float(c.replace(",", "")) for c in cells if c.strip()]

    o.page.click('#thead button:has-text("Gross")')           # first click: biggest first
    desc = shown()
    assert desc[0] == pytest.approx(max(amounts)) and desc == sorted(desc, reverse=True)
    assert o.page.get_attribute('#thead th:has(button:has-text("Gross"))', "aria-sort") == "descending"
    o.page.click('#thead button:has-text("Gross")')           # second click: smallest first
    asc = shown()
    assert asc == sorted(asc) and asc[0] == pytest.approx(min(amounts))


def test_default_order_is_newest_first_and_pages_hold_100(open_page, demo):
    path, data = demo
    o = open_page(path)
    click_view(o.page, "all")
    assert o.page.locator("#tbody tr.row").count() == 100
    assert f"Page 1 of {-(-len(data['files']) // 100)}" in o.page.inner_text("#pager")
    o.page.click("#pager >> text=Next")
    assert "Page 2" in o.page.inner_text("#pager") and o.page.locator("#tbody tr.row").count() == 100


def test_row_details_open_and_close(open_page, demo):
    path, _ = demo
    o = open_page(path)
    click_view(o.page, "week")
    btn = o.page.locator("#tbody .xbtn").first
    before = o.page.locator("#tbody tr").count()
    btn.click()
    assert o.page.locator("#tbody tr").count() == before + 1
    o.page.locator("#tbody .xbtn").first.click()
    assert o.page.locator("#tbody tr").count() == before


def test_banner_buttons_jump_to_the_problem_files(open_page, demo):
    path, data = demo
    o = open_page(path)
    o.page.click('#alerts button:has-text("Show failed")')
    assert o.page.input_value("#status") == "failed"
    assert total(o.page) == len([f for f in data["files"] if f["status"] == "failed"])
    assert o.page.get_attribute('#seg [data-view="all"]', "aria-pressed") == "true"


def test_csv_download_is_exactly_the_filtered_list(open_page, demo):
    path, data = demo
    o = open_page(path)
    click_view(o.page, "all")
    o.page.select_option("#status", "attention")
    with o.page.expect_download() as dl:
        o.page.click("#csv")
    raw = open(dl.value.path(), encoding="utf-8").read()
    assert raw.startswith("﻿")                                   # opens correctly in Excel
    rows = list(csv.reader(io.StringIO(raw.lstrip("﻿"))))
    assert rows[0][:3] == ["Status", "Received", "File"]
    assert len(rows) - 1 == len([f for f in data["files"] if f["status"] != "ok"])
    assert {r[0] for r in rows[1:]} <= {"Failed", "Needs review", "Not a PDF"}


def test_search_and_view_survive_a_reload(open_page, demo):
    path, _ = demo
    o = open_page(path)
    click_view(o.page, "week")
    o.page.select_option("#status", "review")
    o.page.fill("#q", "courier")
    o.page.wait_for_timeout(300)
    n = total(o.page)
    o.page.reload()
    assert o.page.get_attribute('#seg [data-view="week"]', "aria-pressed") == "true"
    assert o.page.input_value("#q") == "courier" and o.page.input_value("#status") == "review"
    assert total(o.page) == n


def test_a_bot_that_stopped_running_is_called_out(open_page, tmp_path):
    db = make_demo_db(str(tmp_path / "s.db"), last_run_hours_ago=72)
    out = build_dashboard(db, str(tmp_path / "s.html"), stale_hours=24, demo=True)
    o = open_page(out)
    assert "late" in o.page.get_attribute("#health", "class")
    assert "The bot has not run for" in o.page.inner_text("#alerts")


def test_a_run_that_never_finished_is_called_out(open_page, tmp_path):
    path = str(tmp_path / "u.db")
    db = HistoryDB(path)
    db.start_run("both")                                              # started, never finished
    db.conn.execute("UPDATE runs SET started_at=?", ((datetime.now(TZ) - timedelta(hours=3)).isoformat(),))
    db.conn.commit(); db.close()
    o = open_page(build_dashboard(path, str(tmp_path / "u.html"), stale_hours=24))
    assert "did not finish" in o.page.inner_text("#alerts")


def test_empty_database_explains_what_to_do(open_page, tmp_path):
    path = str(tmp_path / "e.db")
    HistoryDB(path).close()
    o = open_page(build_dashboard(path, str(tmp_path / "e.html"), stale_hours=24))
    assert o.problems == []
    assert "Nothing recorded yet" in o.page.inner_text("#empty")
    assert "No bot run recorded yet" in o.page.inner_text("#health")


def test_hostile_file_names_stay_inert_text_and_csv_cannot_run_formulas(open_page, tmp_path):
    path = str(tmp_path / "h.db")
    db = HistoryDB(path)
    run = db.start_run("both")
    now = datetime.now(TZ).isoformat(timespec="seconds")
    evil = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script>.pdf'
    for i, (name, inv) in enumerate([(evil, '"><svg onload=window.__pwned=3>'), ("=HYPERLINK(\"http://x\",\"y\").pdf", "+1+1")]):
        db.log_invoice(run, {"file_id": f"h{i}", "kind": "NonPO", "file_name": name, "folder": "Regions/A/Nov-26",
                             "url": "javascript:alert(1)", "received_at": now, "invoice_no": inv, "gross_amount": 5.0,
                             "line_items": 1, "warnings": ""})
    db.finish_run(run); db.close()
    o = open_page(build_dashboard(path, str(tmp_path / "h.html"), stale_hours=24))
    click_view(o.page, "all")
    assert o.dialogs == [] and o.page.evaluate("window.__pwned") is None
    assert o.page.locator("#tbody img, #tbody svg, #tbody script").count() == 0
    assert evil in o.page.inner_text("#tbody")                         # shown as plain text
    assert o.page.locator("#tbody a[href^='javascript']").count() == 0   # unsafe link is not clickable
    with o.page.expect_download() as dl:
        o.page.click("#csv")
    raw = open(dl.value.path(), encoding="utf-8").read()
    assert "'=HYPERLINK" in raw and "'+1+1" in raw                       # defused for Excel


def test_phone_width_does_not_scroll_sideways(open_page, demo):
    path, _ = demo
    o = open_page(path, viewport={"width": 390, "height": 844})
    for view in ("day", "month", "all"):
        click_view(o.page, view)
        assert o.page.evaluate("document.documentElement.scrollWidth") <= 391, view


def test_dark_mode_is_actually_dark(open_page, demo):
    path, _ = demo
    o = open_page(path, color_scheme="dark")
    r, g, b = map(int, re.findall(r"\d+", o.page.evaluate("getComputedStyle(document.body).backgroundColor"))[:3])
    assert (0.299 * r + 0.587 * g + 0.114 * b) < 60


def test_slash_focuses_the_search_box(open_page, demo):
    path, _ = demo
    o = open_page(path)
    o.page.keyboard.press("/")
    assert o.page.evaluate("document.activeElement.id") == "q"


def test_a_file_at_midnight_belongs_to_the_new_day(open_page, tmp_path):
    path = str(tmp_path / "m.db")
    db = HistoryDB(path)
    run = db.start_run("both")
    for fid, when in [("late", "2026-11-09T23:59:59+05:30"), ("midnight", "2026-11-10T00:00:00+05:30")]:
        db.log_invoice(run, {"file_id": fid, "kind": "NonPO", "file_name": fid + ".pdf", "folder": "Regions/A/Nov-26",
                             "received_at": when, "gross_amount": 1.0, "line_items": 1, "warnings": ""})
    db.finish_run(run); db.close()
    out = build_dashboard(path, str(tmp_path / "m.html"), stale_hours=24)
    o = open_page(out)
    for day, expected in [("2026-11-09", ["late.pdf"]), ("2026-11-10", ["midnight.pdf"])]:
        o.page.goto(f"file://{out}#v=day&d={day}")
        o.page.reload()
        names = [t.split("\n")[0] for t in o.page.locator("#tbody tr.row td:nth-child(4)").all_inner_texts()]
        assert names == expected, day
