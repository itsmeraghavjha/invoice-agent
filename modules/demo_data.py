"""Synthetic data so the team (and the tests) can see the dashboard without real invoices.
Everything here is invented: vendors, invoice numbers and amounts are not real."""
import os
import random
import sqlite3
from datetime import datetime, timedelta

from modules.database import DASHBOARD_SCHEMA

NON_PO_PLACES = [
    ("Head Office/HO- F&A", 5), ("Head Office/HO- CS & Legal", 2), ("Head Office/HO- IT", 1),
    ("Head Office/HO- Marketing", 1), ("Sales Offices/SO KA", 2), ("Sales Offices/SO MH", 2),
    ("Sales Offices/SO TN", 1), ("Regions/KSG REGION", 2), ("Regions/HYD REGION", 2),
]
PO_PLACES = ["KSG REGION", "HYD REGION", "TPT REGION", "VJA Region"]
PO_SUBS = ["1. Capex", "2. Stock and Service PO's"]
VENDORS = ["30017314", "30011876", "30022540", "30009917", "30031205", "30014402", "30027761"]
PLANTS = ["1100", "1200", "1300", "2100"]
ITEMS = ["Courier charges", "Printing and stationery", "Annual maintenance", "Software subscription",
         "Hotel accommodation", "Freight inward", "Security services", "Repairs and maintenance"]


def _month_folder(d):
    return d.strftime("%b-%y")


def make_demo_db(path, last_run_hours_ago=2.0, now=None, days=75, seed=11):
    """Create a demo database. Weekdays are busy, weekends quiet, and a few things are
    deliberately wrong (a day with no files, failures, a non-PDF, an unreadable folder)."""
    rnd = random.Random(seed)
    now = (now or datetime.now()).astimezone()
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE processed_files (file_id TEXT PRIMARY KEY, file_name TEXT,
                    status TEXT, processed_at TEXT)""")
    conn.executescript(DASHBOARD_SCHEMA)

    def iso(dt):
        return dt.isoformat(timespec="seconds")

    quiet_day = (now - timedelta(days=9)).date()       # a weekday where nothing arrived
    while quiet_day.weekday() >= 5:
        quiet_day -= timedelta(days=1)

    last_run = now - timedelta(hours=last_run_hours_ago)
    run_times = []
    n_file = 0

    for back in range(days, -1, -1):
        day = (now - timedelta(days=back)).replace(hour=0, minute=0, second=0, microsecond=0)
        weekend = day.weekday() >= 5
        if day.date() == quiet_day:
            count = 0
        elif weekend:
            count = rnd.choice([0, 0, 1, 2])
        elif back == 0:
            count = rnd.randint(3, 7)
        else:
            count = rnd.randint(9, 34)
        # Only files that arrived before the last run can have been processed.
        for _ in range(count):
            when = day + timedelta(hours=rnd.randint(9, 18), minutes=rnd.randint(0, 59))
            if when > last_run:
                if back != 0:
                    continue
                # today: whatever arrived before the last run was processed in it
                when = max(day, last_run - timedelta(minutes=rnd.randint(20, 240)))
            n_file += 1
            fid = f"demo{n_file:05d}"
            is_po = rnd.random() < 0.32
            processed = when + timedelta(minutes=rnd.randint(5, 150))
            if processed > last_run:
                processed = last_run - timedelta(minutes=rnd.randint(1, 20))
            vendor, plant = rnd.choice(VENDORS), rnd.choice(PLANTS)
            gross = round(rnd.uniform(1800, 480000), 2)
            warn = ""
            if rnd.random() < 0.07:
                warn = rnd.choice(["Invoice number missing", "Invoice date missing",
                                   "No valid PO number found (rejected: 4500012345)",
                                   "No line items found (invoice header used instead)"])
            if is_po:
                region = rnd.choice(PO_PLACES)
                folder = f"{region}/{_month_folder(when)}/{rnd.choice(PO_SUBS)}"
                po = f"{rnd.randint(4500000000, 5200000000)}"
                grn = f"{rnd.randint(5100000000, 5199999999)}"
                name = f"{grn}_{rnd.choice('ABCDEF')}{rnd.randint(100, 9999)}.pdf"
                conn.execute("INSERT INTO invoice_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    fid, 1, "PO", name, folder, f"https://drive.google.com/file/d/{fid}/view",
                    iso(when), iso(processed), f"INV/{rnd.randint(1000, 99999)}", None, None,
                    when.strftime("%d.%m.%Y"), "" if "PO number" in warn else po, gross,
                    round(gross / 1.18, 2), 1, grn, warn))
            else:
                place = rnd.choices([p for p, _ in NON_PO_PLACES], [w for _, w in NON_PO_PLACES])[0]
                folder = f"{place}/{_month_folder(when)}"
                name = f"{vendor}-{plant}-{rnd.randint(1000, 9999)}.pdf"
                conn.execute("INSERT INTO invoice_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    fid, 1, "NonPO", name, folder, f"https://drive.google.com/file/d/{fid}/view",
                    iso(when), iso(processed), f"{rnd.choice(['TI', 'GST', 'B'])}-{rnd.randint(100, 99999)}",
                    vendor, plant, when.strftime("%d.%m.%Y"), None, gross, None,
                    rnd.randint(1, 4), name.split("_")[0].replace(".pdf", ""), warn))
            conn.execute("INSERT INTO processed_files VALUES (?,?,?,?)",
                         (fid, name, "SUCCESS", processed.strftime("%Y-%m-%d %H:%M:%S")))

    # Records from before the run log existed: name + date only.
    for k in range(60):
        when = now - timedelta(days=days + 1 + k // 3, hours=rnd.randint(0, 8))
        conn.execute("INSERT INTO processed_files VALUES (?,?,?,?)",
                     (f"old{k:04d}", f"{rnd.choice(VENDORS)}-{rnd.choice(PLANTS)}-{rnd.randint(1000, 9999)}.pdf",
                      "SUCCESS", when.strftime("%Y-%m-%d %H:%M:%S")))

    # Bot runs: a few per weekday, the most recent one `last_run_hours_ago` ago.
    t = last_run
    run_id = 0
    while t > now - timedelta(days=days) and run_id < 40:
        run_times.append(t)
        t -= timedelta(hours=rnd.choice([4, 5, 6, 19]))
    for i, t in enumerate(reversed(run_times), 1):
        latest = i == len(run_times)
        conn.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            i, iso(t), iso(t + timedelta(minutes=rnd.randint(3, 14))), "both", None, 2,
            rnd.randint(52, 60), rnd.randint(180, 420), rnd.randint(0, 25),
            3 if latest else 0, 2 if latest else 0, 1 if latest else 0))
    latest_id = len(run_times)

    # Problems the team should see.
    def issue(kind, key, fid, name, folder, detail, hours_ago, seen=1, file_kind=None):
        # nothing can have been seen by the bot after its last run
        ts = iso(now - timedelta(hours=max(hours_ago, last_run_hours_ago + 0.5)))
        conn.execute("""INSERT INTO issue_log (kind,key,run_id,file_id,file_name,file_kind,folder,url,received_at,
                        detail,first_seen,last_seen,times_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (kind, key, latest_id, fid, name, file_kind, folder,
                      f"https://drive.google.com/file/d/{fid}/view" if fid else None, ts, detail, ts, ts, seen))
    m = _month_folder(now)
    issue("failed_file", "fail1", "fail1", "30017314-1100-6132.pdf", f"Head Office/HO- F&A/{m}",
          "429 RESOURCE_EXHAUSTED: Gemini rate limit, gave up after 5 tries", 3, 3, "NonPO")
    issue("failed_file", "fail2", "fail2", "5105001234_scan0042.pdf", f"KSG REGION/{m}/1. Capex",
          "Gemini returned an empty or unreadable file", 29, 2, "PO")
    issue("failed_file", "fail3", "fail3", "30011876-1200-8841.pdf", f"Sales Offices/SO MH/{m}",
          "Download failed: connection reset", 53, 1, "NonPO")
    issue("skipped_non_pdf", "skip1", "skip1", "invoice photo.jpg", f"Sales Offices/SO KA/{m}",
          "Not a PDF (image/jpeg), so it was skipped", 20, 4, "NonPO")
    issue("skipped_non_pdf", "skip2", "skip2", "October bills.docx", f"Head Office/HO- IT/{m}",
          "Not a PDF (application/vnd.openxmlformats-officedocument.wordprocessingml.document), so it was skipped", 30, 6, "NonPO")
    issue("folder_error", "Could not list Regions/VIZ REGION/" + m, None, None, f"Regions/VIZ REGION/{m}",
          "Could not list Regions/VIZ REGION/%s: HttpError 503 backend error" % m, 2)
    conn.commit()
    conn.close()
    return path
