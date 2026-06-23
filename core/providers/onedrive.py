import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

import msal

from ..utils import make_session, encode_sharing_url
from ..manifest import PENDING
from .base import Provider

CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
AUTHORITY = "https://login.microsoftonline.com/common"
SCOPES = ["Files.Read", "Files.Read.All"]
GRAPH = "https://graph.microsoft.com/v1.0"


class OneDriveProvider(Provider):
    key = "onedrive"
    label = "OneDrive"
    supports_streaming = True

    def __init__(self, script_dir):
        self.cache_file = os.path.join(script_dir, ".msal_cache.json")
        self.cache = msal.SerializableTokenCache()
        if os.path.exists(self.cache_file):
            self.cache.deserialize(open(self.cache_file, encoding="utf-8").read())
        self.app = msal.PublicClientApplication(
            CLIENT_ID, authority=AUTHORITY, token_cache=self.cache)
        self.session = make_session()
        self.flow = None
        self._last = None

    # -- token / device flow ------------------------------------------------

    def _persist(self):
        if self.cache.has_state_changed:
            with open(self.cache_file, "w", encoding="utf-8") as f:
                f.write(self.cache.serialize())

    def _silent(self):
        accounts = self.app.get_accounts()
        if accounts:
            r = self.app.acquire_token_silent(SCOPES, account=accounts[0])
            if r and "access_token" in r:
                self._persist()
                return r["access_token"]
        return None

    def has_cached_account(self):
        return bool(self.app.get_accounts())

    def begin_device_flow(self):
        """Start interactive sign-in; returns the flow with user_code/uri/message."""
        self.flow = self.app.initiate_device_flow(scopes=SCOPES)
        return self.flow

    def complete_device_flow(self):
        """Block until the user finishes signing in. Returns True on success."""
        self._last = self.app.acquire_token_by_device_flow(self.flow)
        self._persist()
        return "access_token" in (self._last or {})

    def is_authenticated(self):
        return self._silent() is not None

    def authenticate(self):
        if self._silent():
            return
        self.begin_device_flow()
        print(self.flow["message"])
        if not self.complete_device_flow():
            raise RuntimeError("OneDrive sign-in failed.")
        print("Authentication successful.\n")

    def token(self):
        tok = self._silent()
        if tok:
            return tok
        if self._last and "access_token" in self._last:
            return self._last["access_token"]
        raise RuntimeError("OneDrive not authenticated.")

    # -- api helpers --------------------------------------------------------

    def _api_get(self, url):
        for attempt in range(5):
            try:
                r = self.session.get(
                    url, headers={"Authorization": f"Bearer {self.token()}"}, timeout=30)
                r.raise_for_status()
                return r.json()
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(2 ** attempt)

    # -- scan ---------------------------------------------------------------

    def scan(self, source, on_entry=None):
        """Enumerate the share. Folders are listed in parallel for speed.
        on_entry(entry) is called for every file as it is discovered."""
        share_id = encode_sharing_url(source)
        root = self._api_get(f"{GRAPH}/shares/{share_id}/driveItem")
        if "error" in root:
            raise RuntimeError(root["error"]["message"])
        drive_id = root["parentReference"]["driveId"]
        entries = []
        if "folder" not in root:
            e = self._entry(root, "", drive_id)
            entries.append(e)
            if on_entry:
                on_entry(e)
            return entries

        lock = threading.Lock()
        with ThreadPoolExecutor(max_workers=10) as ex:
            pending = {ex.submit(self._list_folder, root["id"], drive_id, "",
                                 entries, lock, on_entry)}
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for fut in done:
                    for cid, cdrive, cprefix in fut.result():
                        pending.add(ex.submit(self._list_folder, cid, cdrive,
                                              cprefix, entries, lock, on_entry))
        return entries

    def _list_folder(self, item_id, drive_id, prefix, entries, lock, on_entry):
        """List one folder (all pages). Returns its subfolders as
        (id, drive_id, rel_prefix) tuples for the caller to recurse into."""
        url = (f"{GRAPH}/drives/{drive_id}/items/{item_id}/children"
               f"?$top=200&$select=id,name,size,folder,parentReference")
        subfolders = []
        while url:
            data = self._api_get(url)
            for child in data.get("value", []):
                rel = f"{prefix}/{child['name']}" if prefix else child["name"]
                if "folder" in child:
                    cdrive = child.get("parentReference", {}).get("driveId", drive_id)
                    subfolders.append((child["id"], cdrive, rel))
                else:
                    e = self._entry(child, prefix, drive_id)
                    with lock:
                        entries.append(e)
                    if on_entry:
                        on_entry(e)
            url = data.get("@odata.nextLink")
        return subfolders

    def _entry(self, item, prefix, drive_id):
        rel = f"{prefix}/{item['name']}" if prefix else item["name"]
        return {
            "status": PENDING,
            "size": item.get("size", 0),
            "rel_path": rel,
            "item_id": item["id"],
            "extra": {"drive_id": item.get("parentReference", {}).get("driveId", drive_id)},
        }

    # -- download -----------------------------------------------------------

    def get_download(self, entry):
        drive_id = entry["extra"]["drive_id"]
        resp = self.session.get(
            f"{GRAPH}/drives/{drive_id}/items/{entry['item_id']}/content",
            headers={"Authorization": f"Bearer {self.token()}"},
            allow_redirects=False, timeout=30)
        url = resp.headers.get("Location")
        if not url:
            raise RuntimeError("No download URL returned by OneDrive.")
        return url, {}
