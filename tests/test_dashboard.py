"""Tests for the run log and the team dashboard generator (no browser needed)."""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from config import Config
from modules.dashboard import build_dashboard, collect_data, render_html
from modules.database import HistoryDB
from modules.demo_data import make_demo_db


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    for lst in (main.SCAN_ERRORS, main.SCAN_WARNINGS, main.FAILED_FILES, main.SKIPPED_FILES):
        lst.clear()
    main.RUN_STATS.update(folders_scanned=0, pdfs_seen=0)
    monkeypatch.setattr(Config, "DB_FILE", str(tmp_path / "t.db"))
    monkeypatch.setattr(Config, "DRY_RUN", False)
    monkeypatch.setattr(Config, "SINCE", None)
    monkeypatch.setattr(Config, "DASHBOARD_PATH", str(tmp_path / "dash" / "index.html"))


def embedded_data(html):
    m = re.search(r'<script id="app-data" type="application/json">(.*?)</script>', html, re.S)
    return json.loads(m.group(1))


def rec(fid, **kw):
    base = {"file_id": fid, "kind": "NonPO", "file_name": fid + ".pdf", "folder": "Head Office/HO- F&A/Nov-26",
            "url": "https://drive.google.com/x", "received_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "invoice_no": "INV1", "gross_amount": 100.0, "line_items": 1, "warnings": ""}
    base.update(kw)
    return base


# ------------------------------------------------------------------ database
def test_new_tables_are_additive_and_do_not_touch_processed_files(tmp_path):
    path = str(tmp_path / "old.db")
    old = HistoryDB(path)                      # a database as the old version created it
    old.log_success("legacy1", "legacy1.pdf")
    old.close()

    db = HistoryDB(path)
    run_id = db.start_run("both", None, 2)
    db.log_invoice(run_id, rec("new1"))
    db.finish_run(run_id, folders_scanned=3, pdfs_seen=9, new_files=1)
    db.close()

    conn = sqlite3.connect(path)
    assert conn.execute("SELECT file_id, file_name, status FROM processed_files").fetchall() == [
        ("legacy1", "legacy1.pdf", "SUCCESS")]                       # untouched
    assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} >= {
        "processed_files", "runs", "invoice_log", "issue_log"}
    started, finished, seen = conn.execute("SELECT started_at, finished_at, pdfs_seen FROM runs").fetchone()
    assert started and finished and seen == 9


def test_normal_processing_path_does_not_create_dashboard_tables(tmp_path):
    path = str(tmp_path / "x.db")
    db = HistoryDB(path)
    db.log_success("a", "a.pdf")
    assert db.is_processed("a")
    db.close()
    names = {r[0] for r in sqlite3.connect(path).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert names == {"processed_files"}      # workers' hot path is exactly as before


def test_same_issue_seen_again_is_one_row_with_a_counter(tmp_path):
    db = HistoryDB(str(tmp_path / "i.db"))
    r1 = db.start_run("both")
    db.log_issue(r1, "failed_file", "f1", file_id="f1", file_name="f1.pdf", detail="boom")
    r2 = db.start_run("both")
    db.log_issue(r2, "failed_file", "f1", file_id="f1", file_name="f1.pdf", detail="boom again")
    rows = db.conn.execute("SELECT detail, times_seen, run_id FROM issue_log").fetchall()
    assert rows == [("boom again", 2, r2)]


# ------------------------------------------------------------ data quality notes
def test_po_warnings():
    ok = {"invoice_number": "I", "invoice_date": "01.01.2026", "gross_amount": 5}
    assert main.po_warnings(ok, ["5000000001"], ["5000000001"]) == []
    w = main.po_warnings({}, ["123"], [])
    assert any("No valid PO" in x and "123" in x for x in w)
    assert {"Invoice number missing", "Invoice date missing"} <= set(w)
    assert any("Gross amount" in x for x in main.po_warnings({**ok, "gross_amount": 0}, ["5000000001"], ["5000000001"]))


def test_non_po_warnings():
    ok = {"invoice_number": "I", "invoice_date": "x", "gross_amount": 5}
    assert main.non_po_warnings(ok, [{"item_text": "a"}], "1100COMN") == []
    w = main.non_po_warnings(ok, [], "")
    assert any("line items" in x for x in w) and any("VENDOR-PLANT" in x for x in w)


# ------------------------------------------------------------- run logging
def test_invoice_records_one_per_file_even_with_many_line_items():
    npo = [{"_file_id": "f1", "_file_name": "f1.pdf", "_folder": "A/Nov-26", "_received": "2026-11-09T10:00:00+05:30",
            "_warnings": [], "Invoice No": "I1", "Vendor Code": "V", "Plant": "1100", "Gross Amount": "1,234.50",
            "Document Date": "09.11.2026", "URL Path": "https://x", "GRN Number": "g"} for _ in range(3)]
    po = [{"_file_id": "p1", "_file_name": "p1.pdf", "_folder": "R/Nov-26/1. Capex", "_received": None,
           "_warnings": ["Invoice date missing"], "PO numbers - PO1": "5000000001", "PO numbers - PO2": "5000000002",
           "Reference": "R1", "Gross Value": 99, "URL": "https://y"}]
    recs = {r["file_id"]: r for r in main.build_invoice_records(po, npo)}
    assert len(recs) == 2
    assert recs["f1"]["line_items"] == 3 and recs["f1"]["gross_amount"] == 1234.5 and recs["f1"]["kind"] == "NonPO"
    assert recs["p1"]["po_numbers"] == "5000000001, 5000000002" and recs["p1"]["warnings"] == "Invoice date missing"


def test_persist_run_writes_everything_and_marks_run_finished():
    db = HistoryDB(Config.DB_FILE)
    run_id = db.start_run("both"); db.close()
    main.FAILED_FILES.append({"name": "bad.pdf", "id": "bad", "error": "boom", "folder": "A/Nov-26",
                              "url": "https://u", "received": "2026-11-09T10:00:00+05:30", "kind": "PO"})
    main.SKIPPED_FILES.append({"id": "img", "name": "x.jpg", "folder": "A/Nov-26", "url": None, "received": None,
                               "mime": "image/jpeg", "kind": "NonPO"})
    main.SCAN_ERRORS.append("Could not list Regions/VIZ/Nov-26: HttpError 503")
    main.RUN_STATS.update(folders_scanned=4, pdfs_seen=12)
    ok = main.persist_run(run_id, [], [{"_file_id": "f1", "_file_name": "f1.pdf", "_folder": "A/Nov-26",
                                        "_warnings": [], "Invoice No": "I"}])
    assert ok
    conn = sqlite3.connect(Config.DB_FILE)
    assert conn.execute("SELECT count(*) FROM invoice_log").fetchone()[0] == 1
    kinds = dict(conn.execute("SELECT kind, file_kind FROM issue_log").fetchall())
    assert kinds == {"failed_file": "PO", "skipped_non_pdf": "NonPO", "folder_error": None}
    finished, folders, seen, new, failed, skipped, errs = conn.execute(
        "SELECT finished_at, folders_scanned, pdfs_seen, new_files, failed_files, skipped_files, folder_errors FROM runs"
    ).fetchone()
    assert finished and (folders, seen, new, failed, skipped, errs) == (4, 12, 1, 1, 1, 1)
    assert conn.execute("SELECT folder FROM issue_log WHERE kind='folder_error'").fetchone()[0] == "Regions/VIZ/Nov-26"


def test_persist_run_never_raises_when_the_database_is_unusable(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "DB_FILE", str(tmp_path))      # a directory, not a file
    assert main.persist_run(1, [], []) is False                  # reports, does not crash the bot
    main.refresh_dashboard()                                     # also swallows errors


def test_no_run_id_means_no_logging():
    assert main.persist_run(None, [], []) is False


# ------------------------------------------------------------- collect_data
def test_collect_data_statuses_legacy_and_resolution(tmp_path):
    path = str(tmp_path / "c.db")
    db = HistoryDB(path)
    db.log_success("old1", "old1.pdf")                                         # legacy row
    r1 = db.start_run("both")
    db.log_invoice(r1, rec("clean"))
    db.log_invoice(r1, rec("odd", warnings="Invoice number missing"))
    db.log_invoice(r1, rec("recovered"))
    db.log_issue(r1, "failed_file", "recovered", file_id="recovered", file_name="recovered.pdf", detail="429")
    db.log_issue(r1, "failed_file", "stuck", file_id="stuck", file_name="stuck.pdf", detail="boom", file_kind="PO")
    db.log_issue(r1, "skipped_non_pdf", "img", file_id="img", file_name="a.jpg", detail="Not a PDF")
    db.log_issue(r1, "folder_error", "Could not list OLD", folder="OLD", detail="gone since")
    r2 = db.start_run("both")                                                  # the latest run
    db.log_issue(r2, "folder_error", "Could not list NEW", folder="NEW", detail="still broken")
    db.finish_run(r2)
    db.close()

    d = collect_data(path, stale_hours=24)
    by_id = {f["id"]: f for f in d["files"]}
    assert by_id["clean"]["status"] == "ok" and not by_id["clean"]["legacy"]
    assert by_id["odd"]["status"] == "review" and by_id["odd"]["notes"] == "Invoice number missing"
    assert by_id["old1"]["status"] == "ok" and by_id["old1"]["legacy"] is True and by_id["old1"]["kind"] is None
    assert by_id["stuck"]["status"] == "failed" and by_id["stuck"]["kind"] == "PO"
    assert by_id["img"]["status"] == "skipped"
    assert sum(1 for f in d["files"] if f["id"] == "recovered") == 1           # failure resolved, shown once
    assert by_id["recovered"]["status"] == "ok"
    assert [e["folder"] for e in d["folder_errors"]] == ["NEW"]                # only what the latest run saw
    assert [r["id"] for r in d["runs"]] == [r2, r1]                            # newest first
    assert d["stale_hours"] == 24 and d["demo"] is False


def test_collect_data_works_on_a_database_that_has_never_been_run(tmp_path):
    path = str(tmp_path / "empty.db")
    HistoryDB(path).close()
    d = collect_data(path)
    assert d["files"] == [] and d["runs"] == [] and d["folder_errors"] == []


# ------------------------------------------------------------- html output
def test_html_embeds_valid_json_and_cannot_be_broken_out_of(tmp_path):
    path = str(tmp_path / "x.db")
    db = HistoryDB(path)
    run_id = db.start_run("both")
    evil = '</script><script>alert(1)</script><img src=x onerror=alert(2)>.pdf'
    db.log_invoice(run_id, rec("e1", file_name=evil, invoice_no='"><svg onload=alert(3)>'))
    db.close()
    html = render_html(collect_data(path))
    assert "</script><script>alert(1)" not in html and "<img src=x" not in html
    assert html.count("<script") == 2 and html.count("</script>") == 2          # data + app, nothing injected
    data = embedded_data(html)
    assert data["files"][0]["name"] == evil                                      # round-trips exactly
    assert "__DATA__" not in html


def test_build_dashboard_creates_folders_and_replaces_atomically(tmp_path):
    path = str(tmp_path / "x.db")
    HistoryDB(path).close()
    out = tmp_path / "deep" / "er" / "index.html"
    assert build_dashboard(path, str(out), stale_hours=24) == str(out)
    first = out.read_text(encoding="utf-8")
    build_dashboard(path, str(out), stale_hours=24)
    assert out.exists() and not (tmp_path / "deep" / "er" / "index.html.tmp").exists()
    assert "Invoice Bot" in first


def test_demo_database_builds_and_contains_every_state(tmp_path):
    db = make_demo_db(str(tmp_path / "demo.db"))
    d = collect_data(db, demo=True)
    statuses = {f["status"] for f in d["files"]}
    assert {"ok", "review", "failed", "skipped"} <= statuses
    assert any(f["legacy"] for f in d["files"]) and d["folder_errors"] and d["runs"][0]["finished"]
    assert {f["kind"] for f in d["files"] if f["status"] in ("failed", "skipped")} <= {"PO", "NonPO"}


def test_the_team_page_never_leaks_into_git():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ignored = open(os.path.join(root, ".gitignore"), encoding="utf-8").read()
    assert "dashboard/" in ignored            # generated pages hold real invoice data
