import os
import re

from ..utils import safe_join
from ..manifest import PENDING, DONE, SKIP, FAIL
from .base import Provider


class GDrivePublicProvider(Provider):
    """Zero-setup downloads of *public* ('anyone with the link') Drive items via
    gdown. Per-file when the folder can be listed; otherwise a coarse bulk
    folder download. No byte-level resume — gdown skips files already present."""

    key = "gdrive_public"
    label = "Google Drive (public link — gdown)"
    supports_streaming = False

    def authenticate(self):
        try:
            import gdown  # noqa: F401
        except ImportError:
            raise RuntimeError("The 'gdown' package is required: pip install gdown")

    @staticmethod
    def is_folder(url):
        return "/folders/" in url or "/drive/folders/" in url

    @staticmethod
    def _file_id(url):
        for pat in (r"/file/d/([\w-]+)", r"[?&]id=([\w-]+)", r"/folders/([\w-]+)"):
            m = re.search(pat, url)
            if m:
                return m.group(1)
        return ""

    # -- scan ---------------------------------------------------------------

    def scan(self, source, on_entry=None):
        if not self.is_folder(source):
            e = {
                "status": PENDING, "size": 0,
                "rel_path": self._file_id(source) or "download",
                "item_id": self._file_id(source),
                "extra": {"file": True},
            }
            if on_entry:
                on_entry(e)
            return [e]
        entries = self._list_folder(source, on_entry)
        if entries:
            return entries
        # Could not enumerate -> one bulk entry handled by download_folder.
        return [{
            "status": PENDING, "size": 0,
            "rel_path": "(entire folder — downloaded together via gdown)",
            "item_id": "", "extra": {"bulk": True},
        }]

    def _list_folder(self, url, on_entry):
        """Enumerate a public folder without downloading, via gdown's
        skip_download mode. Returns None to fall back to bulk mode."""
        try:
            import gdown
        except Exception:
            return None
        try:
            items = gdown.download_folder(
                url=url, skip_download=True, quiet=True, use_cookies=False)
        except Exception:
            return None
        entries = []
        for it in items or []:
            rel = it.path.replace(os.sep, "/")
            e = {
                "status": PENDING, "size": 0, "rel_path": rel,
                "item_id": it.id, "extra": {"file": True},
            }
            entries.append(e)
            if on_entry:
                on_entry(e)
        return entries or None

    # -- download -----------------------------------------------------------

    def bulk_download(self, manifest, dest, progress, stop_event=None, on_tick=None, log=None):
        import gdown
        entries = manifest["files"]
        if entries and entries[0]["extra"].get("bulk"):
            progress.current_name = "(folder via gdown)"
            progress.status = "downloading"
            if on_tick:
                on_tick(progress)
            try:
                gdown.download_folder(
                    url=manifest["source"], output=dest,
                    quiet=True, use_cookies=False, remaining_ok=True)
                entries[0]["status"] = DONE
                progress.succeeded += 1
            except Exception as e:
                entries[0]["status"] = FAIL
                progress.failed += 1
                if log:
                    log(f"gdown folder failed: {e}")
            progress.processed += 1
            return

        for e in entries:
            if stop_event and stop_event.is_set():
                break
            if e["status"] in (DONE, SKIP):
                progress.processed += 1
                continue
            local = safe_join(dest, e["rel_path"])
            os.makedirs(os.path.dirname(local), exist_ok=True)
            if os.path.exists(local) and os.path.getsize(local) > 0:
                e["status"] = SKIP
                progress.skipped += 1
                progress.processed += 1
                if on_tick:
                    on_tick(progress)
                continue
            progress.current_name = os.path.basename(e["rel_path"])
            progress.current_pct = 0
            progress.status = "downloading"
            if on_tick:
                on_tick(progress)
            url = (f"https://drive.google.com/uc?id={e['item_id']}"
                   if e["item_id"] else manifest["source"])
            try:
                out = gdown.download(url=url, output=local, quiet=True,
                                     resume=True, fuzzy=True)
                if out:
                    e["status"] = DONE
                    progress.succeeded += 1
                    if os.path.exists(local):
                        progress.bytes_done += os.path.getsize(local)
                else:
                    e["status"] = FAIL
                    progress.failed += 1
            except Exception as ex:
                e["status"] = FAIL
                progress.failed += 1
                if log:
                    log(f"gdown failed for {e['rel_path']}: {ex}")
            progress.processed += 1
            if on_tick:
                on_tick(progress)
