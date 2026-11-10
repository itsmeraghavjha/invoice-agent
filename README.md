# invoice-agent

Reads vendor invoice PDFs from Google Drive, extracts the key fields with Gemini, writes SAP-ready text files to a staging folder, and emails an Excel report.

## How it works

1. `DriveTool` walks the configured Drive folders and downloads new PDFs.
2. `InvoiceExtractor` sends each PDF to Gemini (`gemini-2.5-flash`) and gets structured JSON back.
3. Results are written as text files for SAP and emailed as an `.xlsx` report.
4. Processed Drive file IDs are stored in a local SQLite database (`invoice_history.db`) so they are skipped on later runs.

Two pipelines:

| Pipeline | Output | Notes |
|---|---|---|
| PO invoices | One row per invoice, pipe-delimited | PO numbers must be 10 digits in the range 3300000000-8259999999 |
| Non-PO invoices | One row per line item, comma-delimited | Vendor code and plant are parsed from the filename (`VENDOR-PLANT-...`) |

## Project layout

```
main.py                 # orchestration, folder scanning, reporting
config.py               # environment-based configuration
modules/
  ai_tool.py            # Gemini prompts and extraction
  drive_tool.py         # Google Drive listing and download
  database.py           # SQLite processed-files tracking + run log for the dashboard
  email_tool.py         # Gmail SMTP report sender
  dashboard.py          # builds the team dashboard (one self-contained HTML file)
  dashboard_template.html   # the page itself (no server, no internet needed)
  demo_data.py          # invented data for previewing the dashboard
build_dashboard.py      # rebuild the dashboard without running the bot
```

## Setup

1. Python 3.13 is recommended.
2. Install dependencies: `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill in the values.
4. Place a Google OAuth `token.json` (Drive read-only scope) in the project root.

`.env`, `token.json`, `credentials.json` and `invoice_history.db` are git-ignored and must never be committed.

## Usage

```
python main.py                  # run both pipelines
python main.py po               # PO invoices only
python main.py nonpo            # Non-PO invoices only
python main.py both --dry-run   # list what WOULD be processed; nothing is called, written or logged
python main.py both --today     # only files created/modified today (hard cutoff for this run)
python main.py both --since=2026-11-09   # only files created/modified on/after that date
```

`--today` / `--since=` filter on the Drive **created or modified** time, so older files are ignored even if they sit in a month folder that is being scanned. They apply to one run only (not an env setting), so a later normal run is never limited by accident. Combine with `--dry-run` to preview first.

## Not missing invoices

- **Full pagination.** Drive folder listings follow every page (1000 per page), with retries. A listing that still fails raises instead of returning a partial list.
- **Month lookback.** The latest `MONTHS_LOOKBACK` month folders (default 2) are scanned in every location, so invoices filed late into last month's folder are still picked up. Processed files are skipped via the history DB.
- **Failures are isolated and reported.** One unreadable folder or failing PDF no longer stops the run. After each run an **ATTENTION** email lists unreadable folders, PDFs that failed (retried automatically next run, since they are not logged as done) and non-PDF files that were skipped.
- **Gemini retries.** Rate limits (429), server errors (5xx), network errors and truncated JSON are retried with exponential back-off.
- **No silent loss.** If no staging path is set, the local output `.txt` is kept (never deleted after the invoices are logged as processed).

After changing `MONTHS_LOOKBACK`, run once with `--dry-run` to see which previously-unseen files it will pick up.

## Team dashboard

A read-only page for the accounts team to see what arrived, what the bot processed, and what needs a look. It is one HTML file that is **rewritten after every bot run**; there is no server to run and nothing to install.

**Sharing it:** set `DASHBOARD_PATH` in `.env` to a folder the team can open (a shared network folder works), for example `DASHBOARD_PATH=\\fileserver\invoices\dashboard\index.html`. The team opens the file in a browser and bookmarks it. Press F5 to see the latest run. The default is `dashboard/index.html` next to the bot.

**What the team sees**

- **Day / Week / Month / All time**, with previous and next arrows, and a strip showing files received each day. A weekday with no files stands out as a gap.
- **Search** (file name, invoice no., vendor, PO, folder, notes), **filters** (type, location, status) and **sorting** on any column. Press `/` to jump to the search box. Click a row's arrow for its details and a link to the file in Drive.
- **Download this list (CSV)** exports exactly what is on screen. The current view and filters are kept in the page address, so a link or a reload returns to the same view.
- **Status of every file:** *Processed*, *Needs review* (processed, but something looked off: no valid PO, missing invoice number, date or amount, no line items, file name not `VENDOR-PLANT-...`), *Failed* (the bot will retry it on every run), *Not a PDF* (skipped; needs a PDF or manual handling).
- **Warnings at the top** when a folder could not be read in the last run, when files failed or were skipped, when the last run did not finish, or when the bot has not run for more than `DASHBOARD_STALE_HOURS` (default 24). This last one matters most: a bot that silently stopped is the biggest way to miss invoices.
- **Bot runs:** when each run happened, how many folders and PDFs it saw, and how many were new, failed or skipped.

Files processed before the dashboard existed appear as *Older records* with only a name and date. Everything from the next run on has full details.

```
python build_dashboard.py          # rebuild from the database without running the bot
python build_dashboard.py --demo   # preview with INVENTED data (dashboard/demo.html)
python build_dashboard.py --demo --stale   # preview the "bot stopped" warning
```

**Privacy.** The page contains invoice numbers, vendors and amounts, so keep it in a folder only the team can read. `dashboard/` is git-ignored; never commit a generated page. Links to files open in Google Drive, which still checks the viewer's own Drive access. The dashboard never changes anything: it only reads the history database.

If the dashboard cannot be updated (for example the shared folder is offline), the bot logs a warning and still processes and reports invoices as normal.

## Tests

```
pip install pytest
python -m pytest tests/ -v
```

The tests use fakes for Drive and Gemini, so no credentials are needed. The dashboard's browser tests need `pip install playwright` and `playwright install chromium`; without them those tests are skipped.
