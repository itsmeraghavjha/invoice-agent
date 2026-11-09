import os
import sys
import re
import time
import random
import pandas as pd
import concurrent.futures
# import google.generativeai as genai
from datetime import datetime
from dateutil import parser as date_parser 
from config import Config
from modules.drive_tool import DriveTool
from modules.ai_tool import InvoiceExtractor 
from modules.database import HistoryDB
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


# --- SMART SCANNER ---
def scan_and_process(drive_tool, root_id, worker_func, results_list):
    try:
        # 1. Look for all folders at the root level
        root_subs = drive_tool.list_files(root_id, "application/vnd.google-apps.folder")
        if not root_subs:
            print("     (Folder is empty)")
            return

        targets = []
        
        # 2. Find Date folders at the root (if any stray ones exist like JAN-26)
        date_folders = [f for f in root_subs if parse_smart_date(f['name'])]
        if date_folders:
            latest_root_date = sorted(date_folders, key=lambda x: parse_smart_date(x['name']), reverse=True)[0]
            targets.append(latest_root_date)

        # 3. ALWAYS check the Region folders (folders that are NOT dates, like KSG REGION)
        region_folders = [f for f in root_subs if not parse_smart_date(f['name'])]
        if region_folders:
            print("     👉 Checking Region folders...")
            for region in region_folders:
                sub_subs = drive_tool.list_files(region['id'], "application/vnd.google-apps.folder")
                valid_dates = [s for s in sub_subs if parse_smart_date(s['name'])]
                if valid_dates:
                    # Grab the latest month folder inside this region
                    best_sub = sorted(valid_dates, key=lambda x: parse_smart_date(x['name']), reverse=True)[0]
                    # Update name for clean logging
                    best_sub['name'] = f"{region['name']}/{best_sub['name']}"
                    targets.append(best_sub)

        if not targets:
            print("     ❌ No valid 'Month Year' folders found anywhere.")
            return

        # Allowed subfolder names based on your exact Drive structure
        allowed_subfolders = ["1. Capex", "2. Stock and Service PO's"]

        for target in targets:
            print(f"     📍 Scanning Month/Year Folder: {target['name']}")
            
            # Step 1: List all subfolders INSIDE the 'Month Year' folder
            subfolders = drive_tool.list_files(target['id'], "application/vnd.google-apps.folder")
            
            # Step 2: Filter to strictly include only our allowed subfolders
            target_subfolders = [
                sf for sf in subfolders 
                if sf['name'] in allowed_subfolders
            ]

            if not target_subfolders:
                print(f"        (None of the target subfolders {allowed_subfolders} found in this month)")
                continue

            # Step 3: Loop through ONLY the allowed subfolders and pull their PDFs
            for subfolder in target_subfolders:
                print(f"        📂 Diving into Subfolder: {subfolder['name']}")
                pdfs = drive_tool.list_files(subfolder['id'], "application/pdf")
                
                if not pdfs:
                    print("           (No PDFs found here)")
                    continue
                
                # Reduced to 2 max workers to prevent Gemini API 400 errors
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
                    futures = [ex.submit(worker_func, p) for p in pdfs]
                    for f in concurrent.futures.as_completed(futures):
                        res = f.result()
                        if res:
                            if isinstance(res, list):
                                results_list.extend(res) # Unpacks the multiple lines from Non-PO
                            else:
                                results_list.append(res) # Normal PO single line

    except Exception as e:
        print(f"❌ Scan Error: {e}")



def scan_and_process_non_po(drive_tool, root_id, worker_func, results_list):
    """
    New structure (as of Aug 2026):

    Non PO based Invoice Processing/
    ├── Regions/<Region Name>/<Month-YY>/*.pdf
    ├── Sales Offices/<SO Name>/<Month-YY>/*.pdf
    └── Head Office/<HO Dept>/<Month-YY>/*.pdf

    i.e. Category -> Group -> latest Month folder -> PDFs.
    All three categories follow the identical pattern, so we just loop
    over whatever category folders exist at the root instead of hardcoding
    "Regions"/"Sales Offices"/"Head Office" by name (keeps it resilient if
    they rename/add a category again).
    """
    try:
        # Level 1: Category folders (Regions, Sales Offices, Head Office)
        category_folders = drive_tool.list_files(root_id, "application/vnd.google-apps.folder")
        if not category_folders:
            print("     (Non-PO root folder is empty)")
            return

        for category in category_folders:
            print(f"     📁 Category: {category['name']}")

            # Level 2: Group folders (Region name / SO name / HO dept)
            group_folders = drive_tool.list_files(category['id'], "application/vnd.google-apps.folder")
            if not group_folders:
                print(f"        (No sub-folders found under {category['name']})")
                continue

            for group in group_folders:
                label = f"{category['name']}/{group['name']}"
                print(f"        👤 Group: {label}")

                # Level 3: Month folders inside each group
                month_folders = drive_tool.list_files(group['id'], "application/vnd.google-apps.folder")
                valid_months = [f for f in month_folders if parse_smart_date(f['name'])]

                if not valid_months:
                    print(f"           (No valid month folders found for {label})")
                    continue

                # Take only the latest month
                latest = sorted(valid_months, key=lambda x: parse_smart_date(x['name']), reverse=True)[0]
                print(f"           📅 Latest month: {latest['name']}")

                # Level 4: PDFs directly inside the month folder
                pdfs = drive_tool.list_files(latest['id'], "application/pdf")
                if not pdfs:
                    print(f"              (No PDFs found in {latest['name']})")
                    continue

                print(f"              Found {len(pdfs)} PDFs, processing...")
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
                    futures = [ex.submit(worker_func, p) for p in pdfs]
                    for f in concurrent.futures.as_completed(futures):
                        res = f.result()
                        if res:
                            if isinstance(res, list):
                                results_list.extend(res)
                            else:
                                results_list.append(res)

    except Exception as e:
        print(f"❌ Non-PO Scan Error: {e}")


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

    # Which mode to run: "po", "nonpo", or "both" (default)
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else "both"

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
        if not staging_path_po and os.path.exists(save_path_po_txt):
            os.remove(save_path_po_txt)

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
        if not staging_path_npo and os.path.exists(save_path_npo_txt):
            os.remove(save_path_npo_txt)

    if not po_results and not non_po_results:
        print("✅ No new invoices found.")

if __name__ == "__main__":
    main()