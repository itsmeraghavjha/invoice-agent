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
python main.py          # run both pipelines
python main.py po       # PO invoices only
python main.py nonpo    # Non-PO invoices only
```
