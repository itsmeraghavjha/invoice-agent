"""Builds the read-only team dashboard: ONE self-contained HTML file (no server, no
internet needed) that the team can open from a shared folder. It is rewritten after
every bot run from the data in the history database."""
import json
import os
import sqlite3
from datetime import datetime

from modules.database import DASHBOARD_SCHEMA

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard_template.html")
MAX_FILES = 20000   # newest first; keeps the page small and fast on very old databases
MAX_RUNS = 60


def _rows(conn, sql, params=()):
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _legacy_to_iso(text):
    """Older versions stored a naive local time ('2026-11-09 11:40:00'); make it ISO + offset."""
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return None


def collect_data(db_file, stale_hours=24, demo=False):
    """Everything the page shows, as plain JSON-able data."""
    conn = sqlite3.connect(db_file)
    try:
        conn.executescript(DASHBOARD_SCHEMA)  # old databases get the (empty) new tables
        has_legacy = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='processed_files'"
        ).fetchone()

        invoices = _rows(conn, "SELECT * FROM invoice_log")
        issues = _rows(conn, "SELECT * FROM issue_log")
        runs = _rows(conn, "SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (MAX_RUNS,))
        legacy = _rows(conn, "SELECT file_id, file_name, processed_at FROM processed_files") if has_legacy else []
        latest_run_id = conn.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]
    finally:
        conn.close()

    files, logged_ids = [], set()

    for r in invoices:
        logged_ids.add(r["file_id"])
        notes = r["warnings"] or ""
        files.append({
            "id": r["file_id"], "status": "review" if notes else "ok", "kind": r["kind"],
            "name": r["file_name"], "folder": r["folder"] or "", "url": r["url"],
            "received": r["received_at"] or r["processed_at"], "processed": r["processed_at"],
            "invoice_no": r["invoice_no"], "vendor": r["vendor"], "plant": r["plant"],
            "invoice_date": r["invoice_date"], "po": r["po_numbers"], "gross": r["gross_amount"],
            "net": r["net_amount"], "lines": r["line_items"], "grn": r["grn"],
            "notes": notes, "legacy": False, "seen": 1,
        })

    for i in issues:
        if i["kind"] == "failed_file":
            if i["file_id"] in logged_ids:   # failed once, processed since: no longer a problem
                continue
            status, notes = "failed", f"Could not be processed: {i['detail']}"
        elif i["kind"] == "skipped_non_pdf":
            status, notes = "skipped", i["detail"] or "Not a PDF, so it was skipped"
        else:
            continue
        files.append({
            "id": i["file_id"] or f"issue-{i['issue_id']}", "status": status, "kind": i["file_kind"],
            "name": i["file_name"], "folder": i["folder"] or "", "url": i["url"],
            "received": i["received_at"] or i["last_seen"], "processed": None,
            "invoice_no": None, "vendor": None, "plant": None, "invoice_date": None, "po": None,
            "gross": None, "net": None, "lines": None, "grn": None, "notes": notes,
            "legacy": False, "seen": i["times_seen"],
        })

    # History from before the run log existed: we only know the name and when it was done.
    for r in legacy:
        if r["file_id"] in logged_ids:
            continue
        iso = _legacy_to_iso(r["processed_at"])
        if not iso:
            continue
        files.append({
            "id": r["file_id"], "status": "ok", "kind": None, "name": r["file_name"] or r["file_id"],
            "folder": "", "url": None, "received": iso, "processed": iso, "invoice_no": None,
            "vendor": None, "plant": None, "invoice_date": None, "po": None, "gross": None,
            "net": None, "lines": None, "grn": None, "notes": "", "legacy": True, "seen": 1,
        })

    files.sort(key=lambda f: f["received"] or "", reverse=True)
    files = files[:MAX_FILES]

    # Folder errors only count while they are still happening, i.e. seen in the latest run.
    folder_errors = [
        {"folder": i["folder"], "detail": i["detail"], "last_seen": i["last_seen"], "times": i["times_seen"]}
        for i in issues if i["kind"] == "folder_error" and i["run_id"] == latest_run_id
    ]

    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "stale_hours": stale_hours,
        "demo": demo,
        "files": files,
        "runs": [{
            "id": r["run_id"], "started": r["started_at"], "finished": r["finished_at"],
            "mode": r["mode"], "since": r["since"], "folders": r["folders_scanned"],
            "seen": r["pdfs_seen"], "new": r["new_files"], "failed": r["failed_files"],
            "skipped": r["skipped_files"], "errors": r["folder_errors"],
        } for r in runs],
        "folder_errors": folder_errors,
    }


def render_html(data):
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        template = f.read()
    # "<" is escaped so no value (e.g. a file name) can ever close the <script> tag early.
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    return template.replace("__DATA__", payload, 1)


def build_dashboard(db_file, out_path, stale_hours=None, demo=False):
    """Write the dashboard to out_path (atomically, so nobody opens a half-written page)."""
    if stale_hours is None:
        from config import Config
        stale_hours = Config.DASHBOARD_STALE_HOURS
    html = render_html(collect_data(db_file, stale_hours, demo))
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, out_path)
    return out_path
