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
  database.py           # SQLite processed-files tracking
  email_tool.py         # Gmail SMTP report sender
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
- **Sub-folders are searched.** Non-PO invoices are found directly in a month folder and also in any sub-folder below it (up to 3 levels), e.g. `HO- F&A/Nov-26/2. Stock and Service PO's/`.
- **Failures are isolated and reported.** One unreadable folder or failing PDF no longer stops the run. After each run an **ATTENTION** email lists unreadable folders, PDFs that failed (retried automatically next run, since they are not logged as done) and non-PDF files that were skipped.
- **Gemini retries.** Rate limits (429), server errors (5xx), network errors and truncated JSON are retried with exponential back-off.
- **No silent loss.** If no staging path is set, the local output `.txt` is kept (never deleted after the invoices are logged as processed).

After changing `MONTHS_LOOKBACK`, run once with `--dry-run` to see which previously-unseen files it will pick up.

## Tests

```
pip install pytest
python -m pytest tests/ -v
```

The tests use fakes for Drive and Gemini, so no credentials are needed.
