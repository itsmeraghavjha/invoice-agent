"""Tests for the "never miss an invoice" behaviour. They use fakes, so no Google or
Gemini access is needed:  python -m pytest tests/ -v
"""
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import pytest
from google.genai import errors as genai_errors

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from config import Config, parse_since_date, today_start
from modules import ai_tool
from modules.drive_tool import DriveTool, PAGE_SIZE


# --------------------------------------------------------------------------- fakes
class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeFilesApi:
    """Mimics service.files().list(): serves `items` in pages of `page_size`."""

    def __init__(self, items, page_size):
        self.items, self.page_size, self.calls = items, page_size, []

    def list(self, q, fields, pageSize, pageToken, **kw):
        self.calls.append({"q": q, "fields": fields, "pageSize": pageSize, "pageToken": pageToken})
        start = int(pageToken or 0)
        chunk = self.items[start:start + self.page_size]
        nxt = start + self.page_size
        out = {"files": chunk}
        if nxt < len(self.items):
            out["nextPageToken"] = str(nxt)
        return FakeRequest(out)


class FakeService:
    def __init__(self, api):
        self._api = api

    def files(self):
        return self._api


def make_drive(items, page_size=100):
    drive = DriveTool.__new__(DriveTool)  # skip real OAuth
    api = FakeFilesApi(items, page_size)
    drive.service = FakeService(api)
    return drive, api


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    for lst in (main.SCAN_ERRORS, main.SCAN_WARNINGS, main.FAILED_FILES):
        lst.clear()
    monkeypatch.setattr(Config, "DB_FILE", str(tmp_path / "test.db"))
    monkeypatch.setattr(Config, "DRY_RUN", False)
    monkeypatch.setattr(Config, "MONTHS_LOOKBACK", 2)
    monkeypatch.setattr(Config, "SINCE", None)
    monkeypatch.setattr("time.sleep", lambda *_: None)  # make retry back-offs instant


# ------------------------------------------------------------------ pagination
def test_list_files_returns_every_page():
    items = [{"id": f"f{i}", "name": f"inv{i}.pdf"} for i in range(2500)]
    drive, api = make_drive(items, page_size=1000)
    result = drive.list_files("folder", "application/pdf")
    assert len(result) == 2500
    assert [r["id"] for r in result] == [i["id"] for i in items]  # nothing lost, order kept
    assert len(api.calls) == 3
    assert all(c["pageSize"] == PAGE_SIZE for c in api.calls)
    assert "nextPageToken" in api.calls[0]["fields"]


def test_list_files_empty_folder():
    drive, _ = make_drive([])
    assert drive.list_files("folder", "application/pdf") == []


def test_list_files_retries_transient_error_then_succeeds(monkeypatch):
    drive, api = make_drive([{"id": "a", "name": "a.pdf"}])
    real = api.list
    state = {"n": 0}

    def flaky(**kw):
        state["n"] += 1
        return FakeRequest(OSError("network blip")) if state["n"] == 1 else real(**kw)

    api.list = lambda q, fields, pageSize, pageToken, **kw: flaky(
        q=q, fields=fields, pageSize=pageSize, pageToken=pageToken)
    monkeypatch.setattr(DriveTool._list_page.retry, "wait", lambda *_: 0, raising=False)
    assert [f["id"] for f in drive.list_files("folder", "application/pdf")] == ["a"]
    assert state["n"] == 2


def test_list_files_raises_instead_of_returning_partial_results():
    drive, api = make_drive([])
    api.list = lambda **kw: FakeRequest(OSError("down"))
    with pytest.raises(OSError):
        drive.list_files("folder", "application/pdf")


# ---------------------------------------------------------------- month lookback
def folders(*names):
    return [{"id": n, "name": n} for n in names]


def test_latest_month_folders_picks_newest_n_in_order():
    fs = folders("OCT-26", "NOV-26", "SEP-26", "Random Notes")
    assert [f["name"] for f in latest_names(fs, 2)] == ["NOV-26", "OCT-26"]
    assert [f["name"] for f in latest_names(fs, 1)] == ["NOV-26"]


def latest_names(fs, n):
    return main.latest_month_folders(fs, n)


def test_latest_month_folders_uses_config_default(monkeypatch):
    monkeypatch.setattr(Config, "MONTHS_LOOKBACK", 3)
    fs = folders("AUG-26", "SEP-26", "OCT-26", "NOV-26")
    assert [f["name"] for f in main.latest_month_folders(fs)] == ["NOV-26", "OCT-26", "SEP-26"]


# -------------------------------------------------------------- scan resilience
class FakeTree:
    """In-memory Drive. children[folder_id] = list of {id, name, mimeType}."""

    FOLDER, PDF = "application/vnd.google-apps.folder", "application/pdf"

    def __init__(self, children, broken=()):
        self.children, self.broken = children, set(broken)
        self.since_seen = []  # the `since` passed to every listing

    def list_files(self, folder_id, mime, since=None):
        self.since_seen.append(since)
        if folder_id in self.broken:
            raise OSError(f"boom {folder_id}")
        return [c for c in self.children.get(folder_id, []) if c["mimeType"] == mime]

    def list_other_files(self, folder_id, since=None):
        self.since_seen.append(since)
        return [c for c in self.children.get(folder_id, [])
                if c["mimeType"] not in (self.FOLDER, self.PDF)]


def node(i, name, mime):
    return {"id": i, "name": name, "mimeType": mime}


def test_non_po_scans_previous_month_too_and_reports_non_pdfs():
    F, P = FakeTree.FOLDER, FakeTree.PDF
    tree = FakeTree({
        "root": [node("cat", "Regions", F)],
        "cat": [node("grp", "North", F)],
        "grp": [node("m_nov", "NOV-26", F), node("m_oct", "OCT-26", F), node("m_sep", "SEP-26", F)],
        "m_nov": [node("p1", "V1-P1-inv.pdf", P)],
        "m_oct": [node("p2", "V2-P2-late.pdf", P), node("img", "scan.jpg", "image/jpeg")],
        "m_sep": [node("p3", "V3-P3-old.pdf", P)],  # outside the 2-month lookback
    })
    seen = []
    results = []
    main.scan_and_process_non_po(tree, "root", lambda p: seen.append(p["id"]) or {"id": p["id"]}, results)
    assert sorted(seen) == ["p1", "p2"]            # late October invoice is no longer missed
    assert "p3" not in seen
    assert any("scan.jpg" in w for w in main.SCAN_WARNINGS)  # non-PDF surfaced, not silent
    assert main.SCAN_ERRORS == []


def test_one_broken_folder_does_not_stop_the_rest_of_the_scan():
    F, P = FakeTree.FOLDER, FakeTree.PDF
    tree = FakeTree({
        "root": [node("cat", "Regions", F)],
        "cat": [node("g1", "Broken Region", F), node("g2", "Good Region", F)],
        "g2": [node("m", "NOV-26", F)],
        "m": [node("p", "V-P-inv.pdf", P)],
    }, broken={"g1"})
    seen = []
    main.scan_and_process_non_po(tree, "root", lambda p: seen.append(p["id"]) or {"id": p["id"]}, [])
    assert seen == ["p"]                                       # good region still processed
    assert any("Regions/Broken Region" in e for e in main.SCAN_ERRORS)  # and the failure is reported


def test_po_scan_finds_invoices_in_previous_month_and_only_allowed_subfolders():
    F, P = FakeTree.FOLDER, FakeTree.PDF
    tree = FakeTree({
        "root": [node("reg", "KSG REGION", F)],
        "reg": [node("nov", "NOV-26", F), node("oct", "OCT-26", F)],
        "nov": [node("nov_capex", "1. Capex", F), node("nov_misc", "Misc", F)],
        "oct": [node("oct_stock", "2. Stock and Service PO's", F)],
        "nov_capex": [node("a", "a.pdf", P)],
        "nov_misc": [node("ignored", "ignored.pdf", P)],
        "oct_stock": [node("b", "b.pdf", P)],
    })
    seen = []
    main.scan_and_process(tree, "root", lambda p: seen.append(p["id"]) or {"id": p["id"]}, [])
    assert sorted(seen) == ["a", "b"]


def test_worker_crash_is_recorded_not_swallowed():
    F, P = FakeTree.FOLDER, FakeTree.PDF
    tree = FakeTree({"root": [node("cat", "Regions", F)], "cat": [node("g", "N", F)],
                     "g": [node("m", "NOV-26", F)], "m": [node("p", "x.pdf", P)]})

    def crash(_):
        raise RuntimeError("worker died")

    main.scan_and_process_non_po(tree, "root", crash, [])
    assert [f["id"] for f in main.FAILED_FILES] == ["p"]


def test_dry_run_processes_nothing(monkeypatch):
    monkeypatch.setattr(Config, "DRY_RUN", True)
    F, P = FakeTree.FOLDER, FakeTree.PDF
    tree = FakeTree({"root": [node("cat", "Regions", F)], "cat": [node("g", "N", F)],
                     "g": [node("m", "NOV-26", F)], "m": [node("p", "x.pdf", P)]})
    seen = []
    main.scan_and_process_non_po(tree, "root", lambda p: seen.append(p) or {}, [])
    assert seen == []


# ---------------------------------------------------------------- Gemini retries
def api_error(code):
    return genai_errors.APIError(code, {"error": {"code": code, "message": "x", "status": "X"}})


@pytest.mark.parametrize("code,expected", [(429, True), (503, True), (500, True), (400, False), (404, False)])
def test_retry_policy_by_status_code(code, expected):
    assert ai_tool._is_retryable(api_error(code)) is expected


def test_retry_policy_covers_network_and_bad_json():
    assert ai_tool._is_retryable(json.JSONDecodeError("bad", "", 0))
    assert ai_tool._is_retryable(TimeoutError())
    assert not ai_tool._is_retryable(ValueError("empty file"))


def test_rate_limit_error_is_retried_until_it_succeeds(tmp_path, monkeypatch):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    ex = ai_tool.InvoiceExtractor.__new__(ai_tool.InvoiceExtractor)
    attempts = {"n": 0}

    class Resp:
        text = json.dumps({"invoice_number": "A1"})

    class Models:
        def generate_content(self, **kw):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise api_error(429)
            return Resp()

    class Files:
        def upload(self, file):
            return type("U", (), {"name": "files/1", "state": None})()

        def delete(self, name):
            pass

    ex.client = type("C", (), {"models": Models(), "files": Files()})()
    monkeypatch.setattr(ai_tool._generate.retry if False else ai_tool.InvoiceExtractor._generate.retry,
                        "wait", lambda *_: 0, raising=False)
    assert ex._generate(str(pdf), "prompt") == {"invoice_number": "A1"}
    assert attempts["n"] == 3


def test_empty_file_is_a_failure_not_a_silent_success(tmp_path):
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    ex = ai_tool.InvoiceExtractor.__new__(ai_tool.InvoiceExtractor)
    with pytest.raises(ValueError):
        ex._generate(str(empty), "prompt")


# ------------------------------------------------------------- date cutoff (--today)
CUTOFF = datetime(2026, 11, 9, 0, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))


def test_since_adds_created_or_modified_filter_to_drive_query():
    drive, api = make_drive([{"id": "a", "name": "a.pdf"}])
    drive.list_files("folder", "application/pdf", since=CUTOFF)
    q = api.calls[0]["q"]
    assert "createdTime >= '2026-11-09T00:00:00+05:30'" in q
    assert "modifiedTime >= '2026-11-09T00:00:00+05:30'" in q   # a moved-in file keeps its old createdTime
    assert " or " in q and q.count("(") == 1                    # OR is parenthesised, so it can't leak
    assert "'folder' in parents" in q and "trashed = false" in q


def test_since_also_filters_the_non_pdf_warning_listing():
    drive, api = make_drive([])
    drive.list_other_files("folder", since=CUTOFF)
    assert "createdTime >= '2026-11-09T00:00:00+05:30'" in api.calls[0]["q"]


def test_no_since_means_no_date_filter():
    drive, api = make_drive([])
    drive.list_files("folder", "application/pdf")
    drive.list_other_files("folder")
    assert all("createdTime" not in c["q"] and "modifiedTime" not in c["q"] for c in api.calls)


def test_cutoff_is_applied_to_every_file_listing_in_a_scan(monkeypatch):
    monkeypatch.setattr(Config, "SINCE", CUTOFF)
    F, P = FakeTree.FOLDER, FakeTree.PDF
    tree = FakeTree({"root": [node("cat", "Regions", F)], "cat": [node("g", "N", F)],
                     "g": [node("nov", "NOV-26", F), node("oct", "OCT-26", F)],
                     "nov": [node("p1", "a.pdf", P)], "oct": [node("p2", "b.pdf", P)]})
    main.scan_and_process_non_po(tree, "root", lambda p: {"id": p["id"]}, [])
    # folder listings carry no cutoff; every PDF / non-PDF listing (4 of them, 2 per month) does
    assert tree.since_seen.count(CUTOFF) == 4


def test_parse_since_date_is_local_midnight():
    d = parse_since_date("2026-11-09")
    assert (d.year, d.month, d.day, d.hour, d.minute) == (2026, 11, 9, 0, 0)
    assert d.tzinfo is not None                      # Drive requires a timezone offset
    assert d.isoformat().startswith("2026-11-09T00:00:00")


def test_parse_since_date_rejects_garbage():
    with pytest.raises(ValueError):
        parse_since_date("09/11/2026")


def test_today_start_is_midnight_today():
    t = today_start()
    assert t.date() == date.today() and (t.hour, t.minute, t.second) == (0, 0, 0)
    assert t.tzinfo is not None


# ------------------------------------------- PDFs filed in sub-folders of a month
def test_non_po_finds_pdf_inside_a_month_subfolder():
    """Real case: Head Office/HO- F&A/Nov-26/2. Stock and Service PO's/30017314-1100-6132.pdf
    was never read, because only the month folder itself was searched."""
    F, P = FakeTree.FOLDER, FakeTree.PDF
    tree = FakeTree({
        "root": [node("ho", "Head Office", F)],
        "ho": [node("fa", "HO- F&A", F)],
        "fa": [node("nov", "Nov-26", F), node("oct", "Oct-26", F)],
        "nov": [node("sub", "2. Stock and Service PO's", F)],
        "sub": [node("p_sub", "30017314-1100-6132.pdf", P)],
        "oct": [node("p_oct", "flat-file.pdf", P)],          # flat months still work
    })
    seen = []
    main.scan_and_process_non_po(tree, "root", lambda p: seen.append(p["id"]) or {"id": p["id"]}, [])
    assert sorted(seen) == ["p_oct", "p_sub"]


def test_non_po_finds_pdfs_in_month_folder_and_all_its_subfolders():
    F, P = FakeTree.FOLDER, FakeTree.PDF
    tree = FakeTree({
        "root": [node("c", "Regions", F)], "c": [node("g", "North", F)], "g": [node("m", "Nov-26", F)],
        "m": [node("p0", "top.pdf", P), node("s1", "1. Capex", F), node("s2", "2. Stock", F)],
        "s1": [node("p1", "capex.pdf", P)],
        "s2": [node("p2", "stock.pdf", P), node("s2a", "deeper", F)],
        "s2a": [node("p3", "deep.pdf", P)],
    })
    seen = []
    main.scan_and_process_non_po(tree, "root", lambda p: seen.append(p["id"]) or {"id": p["id"]}, [])
    assert sorted(seen) == ["p0", "p1", "p2", "p3"]


def test_subfolder_search_stops_at_max_depth(monkeypatch):
    monkeypatch.setattr(main, "MAX_SUBFOLDER_DEPTH", 1)
    F, P = FakeTree.FOLDER, FakeTree.PDF
    tree = FakeTree({
        "root": [node("c", "Regions", F)], "c": [node("g", "North", F)], "g": [node("m", "Nov-26", F)],
        "m": [node("s1", "level1", F)], "s1": [node("s2", "level2", F), node("p1", "a.pdf", P)],
        "s2": [node("p2", "too-deep.pdf", P)],
    })
    seen = []
    main.scan_and_process_non_po(tree, "root", lambda p: seen.append(p["id"]) or {"id": p["id"]}, [])
    assert seen == ["p1"]       # depth limit also protects against folder loops
