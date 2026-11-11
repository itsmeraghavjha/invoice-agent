import os
import sys
import re
import time
import random
import pandas as pd
import concurrent.futures
import threading
# import google.generativeai as genai
from datetime import datetime
from dateutil import parser as date_parser 
from config import Config, parse_since_date, today_start
from modules.drive_tool import DriveTool
from modules.ai_tool import InvoiceExtractor 
from modules.database import HistoryDB
from modules.dashboard import build_dashboard
from modules.email_tool import EmailSender 

# --- HELPERS ---
def normalize_date(date_str):
    if not date_str: return None
    try:
        dt = date_parser.parse(str(date_str), dayfirst=True)
        return dt.strftime("%d.%m.%Y") 
    except:
        return date_str

def parse_smart_date(folder_name):
    match = re.search(r'([a-zA-Z]{3,})[^0-9]*(\d{2,4})', folder_name)
    if not match: return None
    month, year = match.groups()
    if len(year) == 2: year = "20" + year
    
    try: 
        # Using date_parser is much more resilient for names like FEB-26
        return date_parser.parse(f"{month} {year}")
    except: 
        return None

def parse_filename_metadata(filename):
    clean_name = os.path.splitext(filename)[0] 
    parts = clean_name.split('-')
    if len(parts) >= 2:
        vendor = parts[0].strip()
        cost_center = f"{parts[1].strip()}COMN"
        return vendor, cost_center
    else:
        return clean_name, ""
    

def extract_grn_number(filename):
    """GRN number is the portion of the filename before the first underscore."""
    clean_name = os.path.splitext(filename)[0]
    if '_' in clean_name:
        return clean_name.split('_', 1)[0].strip()
    return clean_name.strip()

def is_valid_po(po_str):
    """Validates SAP PO numbers for HFL & HNL based on strict rules."""
    if not po_str:
        return False
        
    # Clean up any accidental whitespace or hidden characters
    po_str = str(po_str).strip()
    
    # Rule 2: Must be exactly 10 digits
    if not po_str.isdigit() or len(po_str) != 10:
        return False
        
    # Rule 1: Range must be between 3300000000 and 8259999999
    po_num = int(po_str)
    if 3300000000 <= po_num <= 8259999999:
        return True
        
    return False

def commit_processed_files(results, db_file):
    """Log all successfully staged invoices to DB. Called only after SAP file is written."""
    db = HistoryDB(db_file)
    seen = set()
    for row in results:
        fid = row.get('_file_id')
        fname = row.get('_file_name')
        if fid and fid not in seen:
            db.log_success(fid, fname)
            seen.add(fid)
    db.close()


def po_warnings(data, pos, valid_pos):
    """Data-quality notes for a PO invoice the AI read. These do not block processing; they
    appear on the team dashboard as 'Needs review' so someone can check the invoice."""
    notes = []
    if not valid_pos:
        notes.append("No valid PO number found" + (f" (rejected: {', '.join(map(str, pos))})" if pos else ""))
    if not data.get('invoice_number'):
        notes.append("Invoice number missing")
    if not data.get('invoice_date'):
        notes.append("Invoice date missing")
    if not data.get('gross_amount'):
        notes.append("Gross amount missing or zero")
    return notes


def non_po_warnings(data, ai_line_items, plant):
    notes = []
    if not data.get('invoice_number'):
        notes.append("Invoice number missing")
    if not data.get('invoice_date'):
        notes.append("Invoice date missing")
    if not data.get('gross_amount'):
        notes.append("Gross amount missing or zero")
    if not ai_line_items:
        notes.append("No line items found (invoice header used instead)")
    if not plant:
        notes.append("File name is not VENDOR-PLANT-... so vendor and plant may be wrong")
    return notes


def sanitize_pipe(value, replacement=':'):
    """Replace pipe characters within field values to avoid delimiter conflicts."""
    if isinstance(value, str):
        return value.replace('|', replacement)
    return value

# --- WORKER: PO INVOICES ---
def process_po_invoice(pdf_info):
    db = HistoryDB(Config.DB_FILE)
    if db.is_processed(pdf_info['id']): 
        db.close()
        return None
    db.close()

    time.sleep(random.uniform(0.5, 1.5))
    print(f"  [PO] Processing: {pdf_info['name']}")
    temp_path = f"temp_po_{pdf_info['id']}.pdf"
    
    try:
        local_drive = DriveTool(Config) 
        local_ai = InvoiceExtractor(Config.GEMINI_API_KEY) 

        pdf_bytes = local_drive.download_pdf(pdf_info['id'])
        with open(temp_path, "wb") as f: f.write(pdf_bytes.getbuffer())
        
        data = local_ai.process_file(temp_path) 
        if isinstance(data, list): data = data[0] if data else {}

        # Extract raw POs
        pos = data.get('po_numbers', [])
        if isinstance(pos, str): pos = [pos]
        if not pos: pos = []

        # 🔥 FILTER: Apply the strict validation rules
        valid_pos = [po for po in pos if is_valid_po(po)]


        
        return {
            '_file_id': pdf_info['id'],
            '_file_name': pdf_info['name'],
            '_folder': pdf_info.get('_folder'),
            '_received': pdf_info.get('modifiedTime'),
            '_warnings': po_warnings(data, pos, valid_pos),
            'Document Date': normalize_date(data.get('invoice_date')),
            'PO numbers - PO1': valid_pos[0] if len(valid_pos)>0 else None,
            'PO numbers - PO2': valid_pos[1] if len(valid_pos)>1 else None, 
            'PO numbers - PO3': valid_pos[2] if len(valid_pos)>2 else None,
            'Reference': data.get('invoice_number'),
            'Document Header Test': data.get('first_line_item_description'),
            'Gross Value': data.get('gross_amount'),
            'Net value': data.get('net_amount'),
            'Status': '',
            'URL': pdf_info.get('webViewLink'),
            'File Name': pdf_info['name'],
            'GRN Number': extract_grn_number(pdf_info['name'])
        }
    except Exception as e:
        print(f"  ❌ Error on {pdf_info['name']}: {e}")
        record_failure(pdf_info['name'], pdf_info['id'], e, pdf_info)  # not logged as done -> retried next run
        return None
    finally:
        if os.path.exists(temp_path):
            try: os.remove(temp_path)
            except: pass

# --- WORKER: NON-PO INVOICES ---
def process_non_po_invoice(pdf_info):
    db = HistoryDB(Config.DB_FILE)
    if db.is_processed(pdf_info['id']): 
        db.close()
        return None
    db.close()

    time.sleep(random.uniform(0.5, 1.5))
    print(f"  [Non-PO] Processing: {pdf_info['name']}")
    temp_path = f"temp_npo_{pdf_info['id']}.pdf"
    
    try:
        local_drive = DriveTool(Config) 
        local_ai = InvoiceExtractor(Config.GEMINI_API_KEY)

        vendor_no, plant = parse_filename_metadata(pdf_info['name'])
        pdf_bytes = local_drive.download_pdf(pdf_info['id'])
        with open(temp_path, "wb") as f: f.write(pdf_bytes.getbuffer())
        
        data = local_ai.process_non_po_file(temp_path) 
        if isinstance(data, list): data = data[0] if data else {}


        
        # Look for the new line items list
        line_items = data.get('line_items', [])
        warnings = non_po_warnings(data, line_items, plant)

        # Fallback just in case the AI returns a flat response
        if not line_items:
            line_items = [{
                "item_text": data.get("header_text"),
                "item_gross_value": data.get("gross_amount"),
                "hsn_code": ""
            }]

        rows = []
        for idx, item in enumerate(line_items, 1):
            rows.append({
                '_file_id': pdf_info['id'],
                '_file_name': pdf_info['name'],
                '_folder': pdf_info.get('_folder'),
                '_received': pdf_info.get('modifiedTime'),
                '_warnings': warnings,
                'Document Date': normalize_date(data.get('invoice_date')),
                'Invoice No': data.get('invoice_number'),
                'Vendor Code': vendor_no,
                'Gross Amount': data.get('gross_amount'),
                'Plant': plant.replace('COMN', '') if plant else '', 
                'Header Text': data.get('header_text'),
                'Line Item No': idx,
                'Item Test': item.get('item_text'),
                'Item Gross Value': item.get('item_gross_value'),
                'HSN Code': item.get('hsn_code'),
                'WT Amt': data.get('withholding_tax_base'),
                'URL Path': pdf_info.get('webViewLink'),
                'File Name': pdf_info['name'],
                'GRN Number': extract_grn_number(pdf_info['name'])
            })
        
        return rows # Returning the LIST of rows 
        
    except Exception as e:
        print(f"  ❌ Error on {pdf_info['name']}: {e}")
        record_failure(pdf_info['name'], pdf_info['id'], e, pdf_info)  # not logged as done -> retried next run
        return None
    finally:
        if os.path.exists(temp_path):
            try: os.remove(temp_path)
            except: pass

# --- SMART SCANNER ---
# def scan_and_process(drive_tool, root_id, worker_func, results_list):
#     try:
#         root_subs = drive_tool.list_files(root_id, "application/vnd.google-apps.folder")
#         if not root_subs:
#             print("     (Folder is empty)")
#             return

#         date_folders = [f for f in root_subs if parse_smart_date(f['name'])]
#         targets = []
        
#         if date_folders:
#             print(f"     👉 Detected Simple Structure (Found {len(date_folders)} date folders)")
#             targets.append(sorted(date_folders, key=lambda x: parse_smart_date(x['name']), reverse=True)[0])
#         else:
#             print("     👉 Checking for Region Structure (e.g. Region -> Date Folder)...")
#             for region in root_subs:
#                 sub_subs = drive_tool.list_files(region['id'], "application/vnd.google-apps.folder")
#                 valid_dates = [s for s in sub_subs if parse_smart_date(s['name'])]
#                 if valid_dates:
#                     best_sub = sorted(valid_dates, key=lambda x: parse_smart_date(x['name']), reverse=True)[0]
#                     best_sub['name'] = f"{region['name']}/{best_sub['name']}"
#                     targets.append(best_sub)

#         if not targets:
#             print("     ❌ No valid 'Month Year' folders found.")
#             return

#         for target in targets:
#             print(f"     📍 Scanning: {target['name']}")
#             pdfs = drive_tool.list_files(target['id'], "application/pdf")
            
#             if not pdfs:
#                 print("        (No PDFs found)")
#                 continue
            
#             # Reduced to 2 max workers to prevent Gemini API 400 errors
#             with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
#                 futures = [ex.submit(worker_func, p) for p in pdfs]
#                 for f in concurrent.futures.as_completed(futures):
#                     res = f.result()
#                     if res:
#                         # THIS is Step 3 automatically handled!
#                         if isinstance(res, list):
#                             results_list.extend(res) # Unpacks the multiple lines from Non-PO
#                         else:
#                             results_list.append(res) # Normal PO single line

#     except Exception as e:
#         print(f"❌ Scan Error: {e}")


# --- SCAN HELPERS ---
# Anything that goes wrong during a scan is recorded here and reported at the end of
# the run (console + email), instead of being buried in the log or silently dropped.
FOLDER_MIME = "application/vnd.google-apps.folder"
SCAN_ERRORS = []      # folders we could not read -> invoices inside may have been missed
SCAN_WARNINGS = []    # non-PDF files that were skipped (photos, shortcuts, Word files...)
FAILED_FILES = []     # PDFs that failed to process; not logged as done, so retried next run
SKIPPED_FILES = []    # same as SCAN_WARNINGS but with details, for the team dashboard
RUN_STATS = {'folders_scanned': 0, 'pdfs_seen': 0}
_state_lock = threading.Lock()


def record_failure(name, file_id, error, pdf_info=None):
    info = pdf_info or {}
    with _state_lock:
        FAILED_FILES.append({
            'name': name, 'id': file_id, 'error': str(error),
            'folder': info.get('_folder'), 'url': info.get('webViewLink'),
            'received': info.get('modifiedTime'), 'kind': info.get('_kind'),
        })


def safe_list(drive_tool, folder_id, mime_type, label, since=None):
    """List a folder (all pages). On failure, record it and return None so the caller
    can skip just this folder instead of aborting the whole scan. Empty folder -> []."""
    try:
        return drive_tool.list_files(folder_id, mime_type, since=since)
    except Exception as e:
        msg = f"Could not list {label}: {e}"
        print(f"     ❌ {msg}")
        SCAN_ERRORS.append(msg)
        return None


def latest_month_folders(folders, n=None):
    """The n most recent 'Month-YY' folders, newest first. n defaults to Config.MONTHS_LOOKBACK.

    Looking back more than one month catches invoices that are filed late, e.g. dropped
    into last month's folder after the month ended. Already-processed files are skipped
    via the history DB, so re-scanning an older month is cheap.
    """
    n = n or Config.MONTHS_LOOKBACK
    dated = [f for f in folders if parse_smart_date(f['name'])]
    return sorted(dated, key=lambda x: parse_smart_date(x['name']), reverse=True)[:n]


def process_pdfs_in_folder(drive_tool, folder, label, worker_func, results_list):
    """Process every PDF in one folder. A failure here never affects other folders."""
    pdfs = safe_list(drive_tool, folder['id'], "application/pdf", label, since=Config.SINCE)
    if pdfs is None:
        return

    # Remember which folder each PDF came from (shown on the team dashboard) and count
    # what this run looked at, so "bot ran but saw nothing" is visible too.
    kind = 'PO' if worker_func is process_po_invoice else 'NonPO'
    pdfs = [{**p, '_folder': label, '_kind': kind} for p in pdfs]
    RUN_STATS['folders_scanned'] += 1
    RUN_STATS['pdfs_seen'] += len(pdfs)

    # Surface files we do NOT process, so they are not silently ignored.
    try:
        for o in drive_tool.list_other_files(folder['id'], since=Config.SINCE):
            SCAN_WARNINGS.append(f"{label}/{o['name']} ({o.get('mimeType')}) - not a PDF, skipped")
            SKIPPED_FILES.append({
                'id': o.get('id') or f"{label}/{o['name']}", 'name': o['name'], 'folder': label,
                'url': o.get('webViewLink'), 'received': o.get('modifiedTime'),
                'mime': o.get('mimeType'), 'kind': kind,
            })
    except Exception as e:
        SCAN_WARNINGS.append(f"{label}: could not check for non-PDF files ({e})")

    if not pdfs:
        print(f"           (No PDFs found in {label})")
        return

    if Config.DRY_RUN:
        db = HistoryDB(Config.DB_FILE)
        try:
            new = [p for p in pdfs if not db.is_processed(p['id'])]
        finally:
            db.close()
        print(f"           [DRY RUN] {label}: {len(new)} of {len(pdfs)} PDFs would be processed")
        for p in new:
            print(f"              - {p['name']}")
        return

    print(f"           Found {len(pdfs)} PDFs in {label}, processing...")
    # Reduced to 2 max workers to prevent Gemini API 400 errors
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        futures = {ex.submit(worker_func, p): p for p in pdfs}
        for f in concurrent.futures.as_completed(futures):
            try:
                res = f.result()
            except Exception as e:
                record_failure(futures[f]['name'], futures[f]['id'], e, futures[f])
                continue
            if res:
                if isinstance(res, list):
                    results_list.extend(res)  # Unpacks the multiple lines from Non-PO
                else:
                    results_list.append(res)  # Normal PO single line


# --- SMART SCANNER ---
def scan_and_process(drive_tool, root_id, worker_func, results_list):
    root_subs = safe_list(drive_tool, root_id, FOLDER_MIME, "PO root folder")
    if root_subs is None:
        return
    if not root_subs:
        print("     (Folder is empty)")
        return

    targets = []

    # 1. Stray month folders at the root (e.g. JAN-26)
    targets.extend(latest_month_folders(root_subs))

    # 2. ALWAYS check the Region folders (folders that are NOT dates, like KSG REGION)
    region_folders = [f for f in root_subs if not parse_smart_date(f['name'])]
    if region_folders:
        print("     👉 Checking Region folders...")
    for region in region_folders:
        sub_subs = safe_list(drive_tool, region['id'], FOLDER_MIME, region['name'])
        if not sub_subs:
            continue
        for month in latest_month_folders(sub_subs):
            # Copy with a combined name for clean logging
            targets.append({**month, 'name': f"{region['name']}/{month['name']}"})

    if not targets:
        print("     ❌ No valid 'Month Year' folders found anywhere.")
        SCAN_ERRORS.append("PO scan: no valid 'Month Year' folders found anywhere")
        return

    # Allowed subfolder names based on your exact Drive structure
    allowed_subfolders = ["1. Capex", "2. Stock and Service PO's"]

    for target in targets:
        print(f"     📍 Scanning Month/Year Folder: {target['name']}")

        subfolders = safe_list(drive_tool, target['id'], FOLDER_MIME, target['name'])
        if subfolders is None:
            continue

        # Only the allowed subfolders hold invoices
        target_subfolders = [sf for sf in subfolders if sf['name'] in allowed_subfolders]
        if not target_subfolders:
            print(f"        (None of the target subfolders {allowed_subfolders} found in this month)")
            continue

        for subfolder in target_subfolders:
            label = f"{target['name']}/{subfolder['name']}"
            print(f"        📂 Diving into Subfolder: {subfolder['name']}")
            process_pdfs_in_folder(drive_tool, subfolder, label, worker_func, results_list)


def scan_and_process_non_po(drive_tool, root_id, worker_func, results_list):
    """
    Structure (as of Aug 2026):

    Non PO based Invoice Processing/
    ├── Regions/<Region Name>/<Month-YY>/*.pdf
    ├── Sales Offices/<SO Name>/<Month-YY>/*.pdf
    └── Head Office/<HO Dept>/<Month-YY>/*.pdf

    i.e. Category -> Group -> recent Month folders -> PDFs.
    All three categories follow the identical pattern, so we just loop
    over whatever category folders exist at the root instead of hardcoding
    "Regions"/"Sales Offices"/"Head Office" by name (keeps it resilient if
    they rename/add a category again).
    """
    category_folders = safe_list(drive_tool, root_id, FOLDER_MIME, "Non-PO root folder")
    if category_folders is None:
        return
    if not category_folders:
        print("     (Non-PO root folder is empty)")
        return

    for category in category_folders:
        print(f"     📁 Category: {category['name']}")

        group_folders = safe_list(drive_tool, category['id'], FOLDER_MIME, category['name'])
        if group_folders is None:
            continue
        if not group_folders:
            print(f"        (No sub-folders found under {category['name']})")
            continue

        for group in group_folders:
            label = f"{category['name']}/{group['name']}"
            print(f"        👤 Group: {label}")

            month_folders = safe_list(drive_tool, group['id'], FOLDER_MIME, label)
            if month_folders is None:
                continue

            months = latest_month_folders(month_folders)
            if not months:
                print(f"           (No valid month folders found for {label})")
                continue

            for month in months:
                print(f"           📅 Month: {month['name']}")
                process_pdfs_in_folder(
                    drive_tool, month, f"{label}/{month['name']}", worker_func, results_list
                )


# --- TEAM DASHBOARD LOG ---
def _num(v):
    try:
        return float(str(v).replace(',', '')) if v not in (None, '') else None
    except (TypeError, ValueError):
        return None


def build_invoice_records(po_results, non_po_results):
    """One record per FILE (a Non-PO invoice has one result row per line item)."""
    recs = {}
    for r in po_results:
        pos = [r.get(k) for k in ('PO numbers - PO1', 'PO numbers - PO2', 'PO numbers - PO3') if r.get(k)]
        recs[r['_file_id']] = {
            'file_id': r['_file_id'], 'kind': 'PO', 'file_name': r.get('_file_name'),
            'folder': r.get('_folder'), 'url': r.get('URL'), 'received_at': r.get('_received'),
            'invoice_no': r.get('Reference'), 'invoice_date': r.get('Document Date'),
            'po_numbers': ', '.join(map(str, pos)), 'gross_amount': _num(r.get('Gross Value')),
            'net_amount': _num(r.get('Net value')), 'line_items': 1,
            'grn': r.get('GRN Number'), 'warnings': '; '.join(r.get('_warnings') or []),
        }
    for r in non_po_results:
        fid = r['_file_id']
        if fid in recs:
            recs[fid]['line_items'] += 1
            continue
        recs[fid] = {
            'file_id': fid, 'kind': 'NonPO', 'file_name': r.get('_file_name'),
            'folder': r.get('_folder'), 'url': r.get('URL Path'), 'received_at': r.get('_received'),
            'invoice_no': r.get('Invoice No'), 'vendor': r.get('Vendor Code'),
            'plant': r.get('Plant'), 'invoice_date': r.get('Document Date'),
            'gross_amount': _num(r.get('Gross Amount')), 'line_items': 1,
            'grn': r.get('GRN Number'), 'warnings': '; '.join(r.get('_warnings') or []),
        }
    return list(recs.values())


def persist_run(run_id, po_results, non_po_results):
    """Write this run to the dashboard log. NEVER raises: a logging problem must not
    stop invoices from being processed or reported."""
    if not run_id:
        return False
    try:
        db = HistoryDB(Config.DB_FILE)
        try:
            records = build_invoice_records(po_results, non_po_results)
            for rec in records:
                db.log_invoice(run_id, rec)
            for f in FAILED_FILES:
                db.log_issue(run_id, 'failed_file', f['id'], file_id=f['id'], file_name=f['name'],
                             folder=f.get('folder'), url=f.get('url'), received_at=f.get('received'),
                             detail=f['error'], file_kind=f.get('kind'))
            for s in SKIPPED_FILES:
                db.log_issue(run_id, 'skipped_non_pdf', s['id'], file_id=s['id'], file_name=s['name'],
                             folder=s.get('folder'), url=s.get('url'), received_at=s.get('received'),
                             detail=f"Not a PDF ({s.get('mime')}), so it was skipped",
                             file_kind=s.get('kind'))
            for msg in SCAN_ERRORS:
                key = msg.split(':')[0]
                db.log_issue(run_id, 'folder_error', key,
                             folder=key.replace('Could not list ', ''), detail=msg)
            db.finish_run(run_id, folders_scanned=RUN_STATS['folders_scanned'],
                          pdfs_seen=RUN_STATS['pdfs_seen'], new_files=len(records),
                          failed_files=len(FAILED_FILES), skipped_files=len(SKIPPED_FILES),
                          folder_errors=len(SCAN_ERRORS))
        finally:
            db.close()
        return True
    except Exception as e:
        print(f"⚠️ Could not update the dashboard log (invoices were still processed): {e}")
        return False


def refresh_dashboard():
    """Rebuild the team dashboard page. Never raises."""
    try:
        path = build_dashboard(Config.DB_FILE, Config.DASHBOARD_PATH)
        print(f"📊 Team dashboard updated: {path}")
    except Exception as e:
        print(f"⚠️ Could not update the team dashboard (invoices were still processed): {e}")


def report_problems(email):
    """Print and email everything that may have caused an invoice to be missed."""
    if not (FAILED_FILES or SCAN_ERRORS or SCAN_WARNINGS):
        return

    lines = []
    if SCAN_ERRORS:
        lines.append(f"FOLDERS THAT COULD NOT BE READ ({len(SCAN_ERRORS)}) - invoices in them may be missing:")
        lines += [f"  - {m}" for m in SCAN_ERRORS]
    if FAILED_FILES:
        lines.append(f"PDFs THAT FAILED TO PROCESS ({len(FAILED_FILES)}) - will be retried automatically next run:")
        lines += [f"  - {f['name']}: {f['error']}" for f in FAILED_FILES]
    if SCAN_WARNINGS:
        lines.append(f"FILES SKIPPED, NOT PDFs ({len(SCAN_WARNINGS)}) - process these manually or convert them to PDF:")
        lines += [f"  - {m}" for m in SCAN_WARNINGS]
    text = "\n".join(lines)

    print("\n⚠️  ATTENTION - some invoices may need a look:\n" + text)
    if Config.DRY_RUN:
        return
    try:
        email.send_alert(text)
    except Exception as e:
        print(f"❌ Could not send the alert email: {e}")


# --- MAIN ---
def main():
    print("🤖 Agent Starting...")
    
    if not Config.GEMINI_API_KEY:
        print("❌ Error: GEMINI_API_KEY not found in environment variables.")
        return
    
    # Using REST transport to bypass proxy/firewall blocks
    # genai.configure(api_key=Config.GEMINI_API_KEY, transport="rest")
    
    try:
        init_db = HistoryDB(Config.DB_FILE)
        init_db.close()
        master_drive = DriveTool(Config)
        email = EmailSender(Config) 
    except Exception as e:
        print(f"❌ Init Failed: {e}")
        return

    po_results = []
    non_po_results = []

    # Which mode to run: "po", "nonpo", or "both" (default). Add --dry-run to only
    # list what WOULD be processed (no Gemini calls, no files written, nothing logged as done).
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    mode = args[0].lower() if args else "both"
    if '--dry-run' in sys.argv:
        Config.DRY_RUN = True

    # Hard date cutoff for THIS run:  --today  or  --since=YYYY-MM-DD
    if '--today' in sys.argv:
        Config.SINCE = today_start()
    for a in sys.argv[1:]:
        if a.startswith('--since'):
            try:
                Config.SINCE = parse_since_date(a.split('=', 1)[1])
            except (IndexError, ValueError):
                print("❌ Use the date like this: --since=2026-11-09  (no spaces)")
                return

    if Config.DRY_RUN:
        print("🧪 DRY RUN: listing what would be processed; nothing will be written or emailed.")
    if Config.SINCE:
        print(f"📅 Only files created or modified on/after {Config.SINCE:%d-%b-%Y %H:%M} are considered.")
    print(f"🗓️  Scanning the latest {Config.MONTHS_LOOKBACK} month folder(s) per location.")

    # Start the run log (the team dashboard's heartbeat). Skipped for dry runs.
    run_id = None
    if not Config.DRY_RUN:
        try:
            log_db = HistoryDB(Config.DB_FILE)
            run_id = log_db.start_run(mode, Config.SINCE.isoformat() if Config.SINCE else None,
                                      Config.MONTHS_LOOKBACK)
            log_db.close()
        except Exception as e:
            print(f"⚠️ Could not start the dashboard log: {e}")

    # === CYCLE 1: PO INVOICES ===
    if mode in ("po", "both") and Config.ROOT_FOLDER_ID:
        print(f"\n📂 Scanning PO Folders (ID: {Config.ROOT_FOLDER_ID})...")
        scan_and_process(master_drive, Config.ROOT_FOLDER_ID, process_po_invoice, po_results)

    # === CYCLE 2: NON-PO INVOICES ===
    if mode in ("nonpo", "both") and Config.NON_PO_FOLDER_ID:
        print(f"\n📂 Scanning Non-PO Folders (ID: {Config.NON_PO_FOLDER_ID})...")
        scan_and_process_non_po(master_drive, Config.NON_PO_FOLDER_ID, process_non_po_invoice, non_po_results)

    # === REPORTING & SAVING TO LOCAL SAP FOLDERS ===
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # --- PO REPORT ---
    if po_results:
        df_po = pd.DataFrame(po_results)
        df_po = df_po.drop(columns=['_file_id', '_file_name'], errors='ignore')  # remove tracking fields
        cols_po = ['Document Date', 'PO numbers - PO1', 'PO numbers - PO2', 'PO numbers - PO3', 
                   'Reference', 'Document Header Test', 'Gross Value', 'Net value', 'Status', 'URL',
                   'File Name', 'GRN Number']
        df_po = df_po.reindex(columns=cols_po)
        
        # Add an empty dummy column to force the trailing pipe in the Text file
        df_po[''] = '' 
        
        # 1. Save TXT for Staging
        filename_po_txt = f"PO_Invoices_{timestamp}.txt"
        staging_path_po = getattr(Config, 'PO_STAGING_PATH', None)

        df_po = df_po.map(sanitize_pipe)  
        
        if staging_path_po:
            os.makedirs(staging_path_po, exist_ok=True)
            save_path_po_txt = os.path.join(staging_path_po, filename_po_txt)
            df_po.to_csv(save_path_po_txt, index=False, sep='|')
            print(f"✅ Saved PO Text File to Staging: {save_path_po_txt}")
            commit_processed_files(po_results, Config.DB_FILE)   # ← add this line
        else:
            save_path_po_txt = filename_po_txt
            df_po.to_csv(save_path_po_txt, index=False, sep='|')
            print(f"⚠️ PO Staging Path not set. Saved locally as: {save_path_po_txt}")
            commit_processed_files(po_results, Config.DB_FILE)   # ← add this line

        # 2. Create Excel file for Email (drop the empty dummy column so it looks clean)
        filename_po_excel = f"PO_Invoices_{timestamp}.xlsx"
        df_po_email = df_po.drop(columns=[''])
        df_po_email.to_excel(filename_po_excel, index=False)

        try: 
            email.send_report(filename_po_excel, len(po_results))
        except: 
            pass

        # 3. Cleanup temporary files
        if os.path.exists(filename_po_excel):
            os.remove(filename_po_excel) # Always delete the temp Excel file
        # NOTE: when no staging path is set the local .txt is deliberately KEPT. The
        # invoices are already logged as processed, so deleting it would lose them.

    # --- NON-PO REPORT ---
    if non_po_results:
        df_npo = pd.DataFrame(non_po_results)
        df_npo = df_npo.drop(columns=['_file_id', '_file_name'], errors='ignore')
        cols_npo = ['Document Date', 'Invoice No', 'Vendor Code', 'Gross Amount', 
                    'Plant', 'Header Text', 'Line Item No', 'Item Test', 
                    'Item Gross Value', 'HSN Code', 'WT Amt', 'URL Path',
                    'File Name', 'GRN Number']
        df_npo = df_npo.reindex(columns=cols_npo)

        # 1. Save TXT for Staging (comma-separated, all fields double-quoted)
        filename_npo_txt = f"NonPO_Invoices_{timestamp}.txt"
        staging_path_npo = getattr(Config, 'NON_PO_STAGING_PATH', None)

        if staging_path_npo:
            os.makedirs(staging_path_npo, exist_ok=True)
            save_path_npo_txt = os.path.join(staging_path_npo, filename_npo_txt)
        else:
            save_path_npo_txt = filename_npo_txt

        with open(save_path_npo_txt, 'w', newline='', encoding='utf-8') as f:
            # Header row - no quotes
            f.write(','.join(cols_npo) + '\n')
            # Data rows
            for _, row in df_npo.iterrows():
                fields = []
                for col in cols_npo:
                    val = row[col]
                    if pd.isna(val) or val is None:
                        val = ''
                    else:
                        val = str(val)
                    val = val.replace('"', '""')  # escape existing quotes
                    # Only wrap Item Test in double quotes
                    if col == 'Item Test':
                        fields.append(f'"{val}"')
                    else:
                        fields.append(val)
                f.write(','.join(fields) + '\n')

        if staging_path_npo:
            print(f"✅ Saved Non-PO Text File to Staging: {save_path_npo_txt}")
        else:
            print(f"⚠️ Non-PO Staging Path not set. Saved locally as: {save_path_npo_txt}")

        commit_processed_files(non_po_results, Config.DB_FILE)

        # 2. Create Excel file for Email
        filename_npo_excel = f"NonPO_Invoices_{timestamp}.xlsx"
        df_npo.to_excel(filename_npo_excel, index=False)

        try:
            email.send_report(filename_npo_excel, len(non_po_results))
        except:
            pass

        # 3. Cleanup temporary files
        if os.path.exists(filename_npo_excel):
            os.remove(filename_npo_excel)
        # NOTE: local .txt is deliberately KEPT when no staging path is set (see PO block).

    if not po_results and not non_po_results and not Config.DRY_RUN:
        print("✅ No new invoices found.")

    report_problems(email)

    if not Config.DRY_RUN:
        persist_run(run_id, po_results, non_po_results)
        refresh_dashboard()

if __name__ == "__main__":
    main()