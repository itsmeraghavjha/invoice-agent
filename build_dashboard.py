"""Rebuild the team dashboard from the history database, without running the bot.

    python build_dashboard.py                    # writes Config.DASHBOARD_PATH
    python build_dashboard.py --demo             # writes dashboard/demo.html with INVENTED data
    python build_dashboard.py --demo --stale     # same, but pretend the bot stopped 3 days ago
"""
import os
import sys
import tempfile

from config import Config
from modules.dashboard import build_dashboard


def main():
    if "--demo" in sys.argv:
        from modules.demo_data import make_demo_db
        hours = 72 if "--stale" in sys.argv else 2
        out = os.path.join(os.path.dirname(Config.DASHBOARD_PATH) or ".", "demo.html")
        with tempfile.TemporaryDirectory() as tmp:
            db = make_demo_db(os.path.join(tmp, "demo.db"), last_run_hours_ago=hours)
            path = build_dashboard(db, out, stale_hours=Config.DASHBOARD_STALE_HOURS, demo=True)
        print(f"Demo dashboard (invented data) written to: {path}")
        return
    if not os.path.exists(Config.DB_FILE):
        print(f"No database found at {Config.DB_FILE}. Run the bot first (python main.py).")
        return
    print(f"Team dashboard written to: {build_dashboard(Config.DB_FILE, Config.DASHBOARD_PATH)}")


if __name__ == "__main__":
    main()
