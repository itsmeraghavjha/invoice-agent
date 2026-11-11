# config.py
import os
from datetime import date, datetime, time
from dotenv import load_dotenv

load_dotenv()


def parse_since_date(text):
    """'2026-11-09' -> midnight at the start of that day, in this machine's local timezone."""
    return datetime.strptime(text.strip(), "%Y-%m-%d").astimezone()


def today_start():
    """Midnight at the start of today, in this machine's local timezone."""
    return datetime.combine(date.today(), time.min).astimezone()


class Config:
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    
    # Input Folders (Google Drive)
    ROOT_FOLDER_ID = os.getenv("ROOT_FOLDER_ID")       # PO Input
    NON_PO_FOLDER_ID = os.getenv("NON_PO_FOLDER_ID")   # Non-PO Input
    
    # Output (Staging) Folders for SAP (LOCAL PATHS)
    # Example: C:\SAP_Data\Invoices\PO
    PO_STAGING_PATH = os.getenv("PO_STAGING_PATH")         
    NON_PO_STAGING_PATH = os.getenv("NON_PO_STAGING_PATH") 
    
    GMAIL_USER = os.getenv("GMAIL_USER")
    GMAIL_PASS = os.getenv("GMAIL_PASSWORD")
    
    # Email parsing
    _email_raw = os.getenv("RECEIVER_EMAILS", "")
    _email_clean = _email_raw.replace("[", "").replace("]", "").replace('"', "").replace("'", "")
    RECEIVER_EMAILS = [e.strip() for e in _email_clean.split(",") if e.strip()]
    
    # How many of the most recent "Month-YY" folders to scan per location (min 1).
    # 2 = current + previous month, so late-filed invoices are still picked up.
    # Already-processed files are skipped via the history DB, so a higher value is safe.
    try:
        MONTHS_LOOKBACK = max(1, int(os.getenv("MONTHS_LOOKBACK", "2")))
    except ValueError:
        MONTHS_LOOKBACK = 2

    # Dry run: list what would be processed without calling Gemini or writing anything.
    # Can also be switched on per run with:  python main.py --dry-run
    DRY_RUN = os.getenv("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

    # Hard cutoff: only files CREATED or MODIFIED on/after this moment are considered.
    # Set per run with  --today  or  --since=YYYY-MM-DD. Deliberately NOT an environment
    # variable, so a forgotten setting can never silently hide new invoices on later runs.
    SINCE = None

    # Team dashboard: a single HTML file rewritten after every run. Point DASHBOARD_PATH at a
    # shared folder so the team can open it. It shows a warning when the bot has not run for
    # more than DASHBOARD_STALE_HOURS.
    DASHBOARD_PATH = os.getenv("DASHBOARD_PATH", "dashboard/index.html")
    try:
        DASHBOARD_STALE_HOURS = max(1, int(os.getenv("DASHBOARD_STALE_HOURS", "24")))
    except ValueError:
        DASHBOARD_STALE_HOURS = 24

    # Live dashboard server (python serve_dashboard.py). By default it listens on this computer
    # only; set DASHBOARD_HOST=0.0.0.0 (and a DASHBOARD_PASSWORD) to let the team reach it.
    DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1"
    DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD") or None
    try:
        DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8080"))
    except ValueError:
        DASHBOARD_PORT = 8080
    try:
        DASHBOARD_REFRESH_SECONDS = max(5, int(os.getenv("DASHBOARD_REFRESH_SECONDS", "30")))
    except ValueError:
        DASHBOARD_REFRESH_SECONDS = 30

    DB_FILE = "invoice_history.db"
    SCOPES = ['https://www.googleapis.com/auth/drive.readonly']