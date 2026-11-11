"""Run the LIVE team dashboard: a web page that keeps itself up to date.

    python serve_dashboard.py                     # this computer only:  http://localhost:8080
    python serve_dashboard.py --host 0.0.0.0      # let the team open it (needs DASHBOARD_PASSWORD)
    python serve_dashboard.py --demo              # try it with INVENTED data that keeps changing

It only READS the history database; it never changes it and it does not run the bot. Run the bot
as you do today (by hand or on a schedule) and the open page shows the results by itself.
"""
import argparse
import os
import random
import tempfile
import threading
from datetime import datetime

from config import Config
from modules.dashboard_server import DashboardApp, check_bind, lan_address, make_server
from modules.database import HistoryDB


def simulate_bot(db_file, stop, every=20):
    """Demo only: pretend the bot runs now and then, so the page visibly updates by itself."""
    rnd, n = random.Random(), 0
    while not stop.wait(every):
        n += 1
        now = datetime.now().astimezone()
        db = HistoryDB(db_file)
        try:
            run = db.start_run("both", None, 2)
            vendor, plant = rnd.choice(["30017314", "30011876", "30022540"]), rnd.choice(["1100", "1200"])
            name = f"{vendor}-{plant}-{rnd.randint(1000, 9999)}.pdf"
            db.log_invoice(run, {
                "file_id": f"live{n}-{rnd.randint(0, 10**6)}", "kind": "NonPO", "file_name": name,
                "folder": f"Head Office/HO- F&A/{now:%b-%y}", "url": "https://drive.google.com/",
                "received_at": now.isoformat(timespec="seconds"), "invoice_no": f"INV/{rnd.randint(1000, 99999)}",
                "vendor": vendor, "plant": plant, "invoice_date": now.strftime("%d.%m.%Y"),
                "gross_amount": round(rnd.uniform(2000, 90000), 2), "line_items": 1,
                "warnings": rnd.choice(["", "", "", "Invoice number missing"])})
            db.finish_run(run, folders_scanned=58, pdfs_seen=300 + n, new_files=1)
        finally:
            db.close()


def main():
    ap = argparse.ArgumentParser(description="Live, read-only team dashboard.")
    ap.add_argument("--host", default=Config.DASHBOARD_HOST,
                    help="127.0.0.1 = this computer only (default); 0.0.0.0 = the whole network")
    ap.add_argument("--port", type=int, default=Config.DASHBOARD_PORT)
    ap.add_argument("--refresh", type=int, default=Config.DASHBOARD_REFRESH_SECONDS,
                    help="how often the open page checks for new data, in seconds (min 5)")
    ap.add_argument("--demo", action="store_true", help="use invented data that keeps changing")
    ap.add_argument("--no-password", action="store_true", help="allow network access without a password")
    args = ap.parse_args()

    password = Config.DASHBOARD_PASSWORD
    check_bind(args.host, password, args.no_password)

    stop, tmp = threading.Event(), None
    if args.demo:
        from modules.demo_data import make_demo_db
        tmp = tempfile.TemporaryDirectory()
        db_file = make_demo_db(os.path.join(tmp.name, "demo.db"))
        threading.Thread(target=simulate_bot, args=(db_file, stop), daemon=True).start()
    else:
        db_file = Config.DB_FILE
        if not os.path.exists(db_file):
            print(f"Note: no history database at {os.path.abspath(db_file)} yet. "
                  "The page will say so until the bot has run once.")

    app = DashboardApp(db_file, Config.DASHBOARD_STALE_HOURS, args.refresh, password, demo=args.demo)
    try:
        server = make_server(app, args.host, args.port)
    except OSError as e:
        raise SystemExit(f"Could not start on port {args.port}: {e}\n  Is it already running, or in use by "
                         "another program? Try --port 8081.")

    shown = "localhost" if args.host in ("127.0.0.1", "localhost", "::1") else (
        lan_address() if args.host in ("0.0.0.0", "::") else args.host)
    print("Team dashboard is running (read-only).")
    print(f"  Open:      http://{shown}:{args.port}")
    if args.host in ("127.0.0.1", "localhost", "::1"):
        print("  Reachable from this computer only. To share it, set DASHBOARD_HOST=0.0.0.0 and a DASHBOARD_PASSWORD.")
    else:
        print("  Password:  " + ("required (username can be anything)" if password else "NONE: anyone on the network can read it"))
        print("  If others cannot connect, allow this port in the Windows Firewall.")
    print(f"  The page refreshes itself every {app.refresh_seconds} s. Press Ctrl+C to stop.")
    if args.demo:
        print("  DEMO: invented data; a new invoice appears about every 20 seconds.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        stop.set()
        server.server_close()
        if tmp:
            tmp.cleanup()


if __name__ == "__main__":
    main()
