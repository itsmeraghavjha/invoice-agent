import sqlite3
from datetime import datetime


def now_iso():
    """Local time with UTC offset, e.g. 2026-11-09T16:05:00+05:30 (what the dashboard reads)."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# Extra tables that feed the team dashboard. They are ADDITIVE: `processed_files` (the
# "already done" list the bot relies on) is never touched, and these are created lazily,
# only by the run log / dashboard, so the normal processing path is unchanged.
DASHBOARD_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT,
    finished_at     TEXT,
    mode            TEXT,
    since           TEXT,
    months_lookback INTEGER,
    folders_scanned INTEGER DEFAULT 0,
    pdfs_seen       INTEGER DEFAULT 0,
    new_files       INTEGER DEFAULT 0,
    failed_files    INTEGER DEFAULT 0,
    skipped_files   INTEGER DEFAULT 0,
    folder_errors   INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS invoice_log (
    file_id      TEXT PRIMARY KEY,
    run_id       INTEGER,
    kind         TEXT,      -- 'PO' or 'NonPO'
    file_name    TEXT,
    folder       TEXT,      -- e.g. Head Office/HO- F&A/Nov-26
    url          TEXT,
    received_at  TEXT,      -- Drive "date modified" of the file
    processed_at TEXT,
    invoice_no   TEXT,
    vendor       TEXT,
    plant        TEXT,
    invoice_date TEXT,
    po_numbers   TEXT,
    gross_amount REAL,
    net_amount   REAL,
    line_items   INTEGER,
    grn          TEXT,
    warnings     TEXT       -- '; '-joined data-quality notes, empty when clean
);
CREATE TABLE IF NOT EXISTS issue_log (
    issue_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT,       -- 'failed_file', 'skipped_non_pdf' or 'folder_error'
    key         TEXT,
    run_id      INTEGER,
    file_id     TEXT,
    file_name   TEXT,
    file_kind   TEXT,       -- 'PO' or 'NonPO': which pipeline the file belongs to
    folder      TEXT,
    url         TEXT,
    received_at TEXT,
    detail      TEXT,
    first_seen  TEXT,
    last_seen   TEXT,
    times_seen  INTEGER DEFAULT 1,
    UNIQUE (kind, key)
);
"""


class HistoryDB:
    def __init__(self, db_file):
        self.conn = sqlite3.connect(db_file, check_same_thread=False)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_files (
                file_id      TEXT PRIMARY KEY,
                file_name    TEXT,
                status       TEXT,
                processed_at TEXT
            )
        """)
        self.conn.commit()

    def is_processed(self, file_id):
        cur = self.conn.cursor()
        cur.execute("SELECT 1 FROM processed_files WHERE file_id=?", (file_id,))
        return cur.fetchone() is not None

    def log_success(self, file_id, file_name=None):
        processed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            "INSERT OR REPLACE INTO processed_files VALUES (?, ?, ?, ?)",
            (file_id, file_name, "SUCCESS", processed_at)
        )
        self.conn.commit()

    # ---- run log (feeds the team dashboard) -------------------------------------
    def ensure_dashboard_tables(self):
        self.conn.executescript(DASHBOARD_SCHEMA)
        self.conn.commit()

    def start_run(self, mode, since=None, months_lookback=None):
        self.ensure_dashboard_tables()
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, mode, since, months_lookback) VALUES (?, ?, ?, ?)",
            (now_iso(), mode, since, months_lookback),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id, folders_scanned=0, pdfs_seen=0, new_files=0,
                   failed_files=0, skipped_files=0, folder_errors=0):
        self.conn.execute(
            """UPDATE runs SET finished_at=?, folders_scanned=?, pdfs_seen=?, new_files=?,
                               failed_files=?, skipped_files=?, folder_errors=?
               WHERE run_id=?""",
            (now_iso(), folders_scanned, pdfs_seen, new_files,
             failed_files, skipped_files, folder_errors, run_id),
        )
        self.conn.commit()

    def log_invoice(self, run_id, rec):
        """One row per processed file. Re-processing the same file replaces its row."""
        self.conn.execute(
            """INSERT OR REPLACE INTO invoice_log
               (file_id, run_id, kind, file_name, folder, url, received_at, processed_at,
                invoice_no, vendor, plant, invoice_date, po_numbers, gross_amount,
                net_amount, line_items, grn, warnings)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rec["file_id"], run_id, rec.get("kind"), rec.get("file_name"), rec.get("folder"),
             rec.get("url"), rec.get("received_at"), now_iso(), rec.get("invoice_no"),
             rec.get("vendor"), rec.get("plant"), rec.get("invoice_date"), rec.get("po_numbers"),
             rec.get("gross_amount"), rec.get("net_amount"), rec.get("line_items"),
             rec.get("grn"), rec.get("warnings") or ""),
        )
        self.conn.commit()

    def log_issue(self, run_id, kind, key, file_id=None, file_name=None, folder=None,
                  url=None, received_at=None, detail=None, file_kind=None):
        """Something the team may need to act on. The same issue seen again just bumps
        last_seen / times_seen, so a stuck file shows once, not once per run."""
        ts = now_iso()
        self.conn.execute(
            """INSERT INTO issue_log
               (kind, key, run_id, file_id, file_name, file_kind, folder, url, received_at, detail,
                first_seen, last_seen, times_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)
               ON CONFLICT (kind, key) DO UPDATE SET
                   run_id=excluded.run_id, detail=excluded.detail, last_seen=excluded.last_seen,
                   times_seen=times_seen + 1""",
            (kind, key, run_id, file_id, file_name, file_kind, folder, url, received_at, detail, ts, ts),
        )
        self.conn.commit()

    def close(self):
        try:
            self.conn.close()
        except:
            pass
