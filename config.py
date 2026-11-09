# config.py
import os
from dotenv import load_dotenv

load_dotenv()

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
    
    DB_FILE = "invoice_history.db"
    SCOPES = ['https://www.googleapis.com/auth/drive.readonly']