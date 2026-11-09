# modules/drive_tool.py
import io
import time
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.oauth2.credentials import Credentials
from tenacity import retry, stop_after_attempt, wait_exponential

class DriveTool:
    def __init__(self, config):
        self.service = self._auth(config)

    def _auth(self, config):
        # ... (Insert your existing auth logic here using config.SCOPES) ...
        # For brevity, assuming creds are loaded
        creds = Credentials.from_authorized_user_file('token.json', config.SCOPES)
        return build('drive', 'v3', credentials=creds)

    def list_files(self, folder_id, mime_type):
        query = f"'{folder_id}' in parents and mimeType = '{mime_type}' and trashed = false"
        # We explicitly ask for the webViewLink here
        results = self.service.files().list(
            q=query, fields="files(id, name, webViewLink)", 
            supportsAllDrives=True, includeItemsFromAllDrives=True
        ).execute()
        return results.get('files', [])

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def download_pdf(self, file_id):
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, self.service.files().get_media(fileId=file_id))
        done = False
        while not done: _, done = downloader.next_chunk()
        fh.seek(0)
        return fh