import os
import re
import time
import threading
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

from ..utils import make_session
from ..manifest import PENDING
from .base import Provider

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
FILES_URL = "https://www.googleapis.com/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"

# Native Google formats have no binary; export them instead.
EXPORT_MAP = {
    "application/vnd.google-apps.document":
        ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet":
        ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation":
        ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    "application/vnd.google-apps.drawing":
        ("image/png", ".png"),
}

SETUP_HELP = (
    "Google Drive (private) needs a one-time OAuth client:\n"
    "  1. https://console.cloud.google.com  ->  create/select a project\n"
    "  2. Enable the 'Google Drive API'\n"
    "  3. APIs & Services -> Credentials -> Create OAuth client ID -> Desktop app\n"
    "  4. Download the JSON and save it as 'client_secret.json' in the script folder."
)


class GDriveApiProvider(Provider):
    key = "gdrive_api"
    label = "Google Drive (private — OAuth)"
    supports_streaming = True

    def __init__(self, script_dir):
        self.script_dir = script_dir
        self.secret_file = os.path.join(script_dir, "client_secret.json")
        self.token_file = os.path.join(script_dir, ".gdrive_token.json")
        self.session = make_session()
        self.creds = None

    # -- auth ---------------------------------------------------------------

    def has_secret(self):
        return os.path.exists(self.secret_file)

    def _load(self):
        from google.oauth2.credentials import Credentials
        if self.creds is None and os.path.exists(self.token_file):
            self.creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)

    def _save(self):
        with open(self.token_file, "w", encoding="utf-8") as f:
            f.write(self.creds.to_json())

    def is_authenticated(self):
        from google.auth.transport.requests import Request
        self._load()
        if not self.creds:
            return False
        if self.creds.valid:
            return True
        if self.creds.expired and self.creds.refresh_token:
            try:
                self.creds.refresh(Request())
                self._save()
                return True
            except Exception:
                return False
        return False

    def authenticate(self):
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        self._load()
        if self.creds and self.creds.valid:
            return
        if self.creds and self.creds.expired and self.creds.refresh_token:
            self.creds.refresh(Request())
            self._save()
            return
        if not self.has_secret():
            raise FileNotFoundError(SETUP_HELP)
        flow = InstalledAppFlow.from_client_secrets_file(self.secret_file, SCOPES)
        self.creds = flow.run_local_server(port=0, prompt="consent")
        self._save()

    def token(self):
        from google.auth.transport.requests import Request
        self._load()
        if self.creds and self.creds.expired and self.creds.refresh_token:
            self.creds.refresh(Request())
            self._save()
        return self.creds.token

    # -- scan ---------------------------------------------------------------

    @staticmethod
    def extract_id(url):
        for pat in (r"/folders/([\w-]+)", r"/file/d/([\w-]+)", r"[?&]id=([\w-]+)"):
            m = re.search(pat, url)
            if m:
                return m.group(1)
        if re.fullmatch(r"[\w-]{20,}", url.strip()):
            return url.strip()
        raise ValueError("Could not parse a Google Drive file/folder ID from the link.")

    def _api(self, params):
        for attempt in range(5):
            try:
                r = self.session.get(
                    FILES_URL, params=params,
                    headers={"Authorization": f"Bearer {self.token()}"}, timeout=30)
                r.raise_for_status()
                return r.json()
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(2 ** attempt)

    def _meta(self, file_id):
        for attempt in range(5):
            try:
                r = self.session.get(
                    f"{FILES_URL}/{file_id}",
                    params={"fields": "id,name,size,mimeType",
                            "supportsAllDrives": "true"},
                    headers={"Authorization": f"Bearer {self.token()}"}, timeout=30)
                r.raise_for_status()
                return r.json()
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(2 ** attempt)

    def scan(self, source, on_entry=None):
        """Enumerate the folder. Subfolders are listed in parallel for speed.
        on_entry(entry) is called for every file as it is discovered."""
        root_id = self.extract_id(source)
        meta = self._meta(root_id)
        entries = []
        if meta.get("mimeType") != FOLDER_MIME:
            e = self._entry(meta, "")
            entries.append(e)
            if on_entry:
                on_entry(e)
            return entries

        lock = threading.Lock()
        with ThreadPoolExecutor(max_workers=10) as ex:
            pending = {ex.submit(self._list_folder, root_id, "", entries, lock, on_entry)}
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for fut in done:
                    for cid, cprefix in fut.result():
                        pending.add(ex.submit(self._list_folder, cid, cprefix,
                                              entries, lock, on_entry))
        return entries

    def _list_folder(self, folder_id, prefix, entries, lock, on_entry):
        """List one folder (all pages). Returns subfolders as (id, prefix)."""
        subfolders = []
        page = None
        while True:
            params = {
                "q": f"'{folder_id}' in parents and trashed=false",
                "fields": "nextPageToken,files(id,name,size,mimeType)",
                "pageSize": 1000,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page:
                params["pageToken"] = page
            data = self._api(params)
            for f in data.get("files", []):
                rel = f"{prefix}/{f['name']}" if prefix else f["name"]
                if f["mimeType"] == FOLDER_MIME:
                    subfolders.append((f["id"], rel))
                else:
                    e = self._entry(f, prefix)
                    with lock:
                        entries.append(e)
                    if on_entry:
                        on_entry(e)
            page = data.get("nextPageToken")
            if not page:
                break
        return subfolders

    def _entry(self, f, prefix):
        rel = f"{prefix}/{f['name']}" if prefix else f["name"]
        export = EXPORT_MAP.get(f["mimeType"])
        native = export is not None
        if native:
            rel += export[1]
        return {
            "status": PENDING,
            "size": int(f.get("size", 0)),
            "rel_path": rel,
            "item_id": f["id"],
            "extra": {"native": native, "export_mime": export[0] if export else None},
        }

    # -- download -----------------------------------------------------------

    def get_download(self, entry):
        headers = {"Authorization": f"Bearer {self.token()}"}
        if entry["extra"].get("native"):
            mime = quote(entry["extra"]["export_mime"])
            url = f"{FILES_URL}/{entry['item_id']}/export?mimeType={mime}"
        else:
            url = f"{FILES_URL}/{entry['item_id']}?alt=media&supportsAllDrives=true"
        return url, headers
