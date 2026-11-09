# modules/drive_tool.py
import io
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
from google.oauth2.credentials import Credentials
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

# Errors worth retrying: Drive API errors (rate limits, 5xx) and network hiccups.
TRANSIENT_ERRORS = (HttpError, OSError)

# Drive returns at most 1000 items per page. The API default is only 100, which
# is what used to silently truncate folders with many files.
PAGE_SIZE = 1000


class DriveTool:
    def __init__(self, config):
        self.service = self._auth(config)

    def _auth(self, config):
        # ... (Insert your existing auth logic here using config.SCOPES) ...
        # For brevity, assuming creds are loaded
        creds = Credentials.from_authorized_user_file('token.json', config.SCOPES)
        return build('drive', 'v3', credentials=creds)

    @retry(
        retry=retry_if_exception_type(TRANSIENT_ERRORS),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=20),
        reraise=True,
    )
    def _list_page(self, query, page_token, fields="nextPageToken, files(id, name, webViewLink)"):
        # By default we explicitly ask for the webViewLink here
        return self.service.files().list(
            q=query,
            fields=fields,
            pageSize=PAGE_SIZE,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()

    def list_files(self, folder_id, mime_type):
        """Return ALL matching files in a folder, following every page of results.

        Raises if a page still fails after retries, so callers never act on a
        partial listing without knowing it.
        """
        query = f"'{folder_id}' in parents and mimeType = '{mime_type}' and trashed = false"
        files = []
        page_token = None
        while True:
            results = self._list_page(query, page_token)
            files.extend(results.get('files', []))
            page_token = results.get('nextPageToken')
            if not page_token:
                return files

    def list_other_files(self, folder_id):
        """Files in a folder that are neither PDFs nor sub-folders (images, Word files,
        Drive shortcuts, ...). The scanner never processes these, so it reports them
        instead of letting them be skipped silently."""
        query = (
            f"'{folder_id}' in parents and trashed = false "
            "and mimeType != 'application/pdf' "
            "and mimeType != 'application/vnd.google-apps.folder'"
        )
        files = []
        page_token = None
        while True:
            results = self._list_page(query, page_token, fields="nextPageToken, files(id, name, mimeType)")
            files.extend(results.get('files', []))
            page_token = results.get('nextPageToken')
            if not page_token:
                return files

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def download_pdf(self, file_id):
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, self.service.files().get_media(fileId=file_id))
        done = False
        while not done: _, done = downloader.next_chunk()
        fh.seek(0)
        return fh
