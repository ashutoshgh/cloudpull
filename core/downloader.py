import os
import time
import threading

from . import manifest as M
from .utils import safe_join, make_session


class Stopped(Exception):
    pass


class Progress:
    """Mutable snapshot of a download, shared between the worker thread and the
    UI/CLI. Plain attribute writes; reads are display-only so races are benign."""

    def __init__(self):
        self.lock = threading.Lock()
        self.status = "idle"          # idle|scanning|downloading|done|stopped|error
        self.message = ""
        self.files_total = 0
        self.processed = 0            # succeeded + skipped + failed
        self.succeeded = 0
        self.skipped = 0
        self.failed = 0
        self.bytes_total = 0
        self.bytes_done = 0
        self.current_name = ""
        self.current_pct = 0.0
        self.current_speed = 0.0
        self.current_eta = 0.0
        self.started = time.time()

    def snapshot(self):
        with self.lock:
            return dict(self.__dict__, lock=None)


def init_for_manifest(progress, manifest):
    todo = M.remaining(manifest)
    progress.files_total = len(manifest["files"])
    progress.processed = len(manifest["files"]) - len(todo)
    progress.bytes_total = sum(e["size"] for e in manifest["files"])
    progress.bytes_done = sum(e["size"] for e in manifest["files"]
                              if e["status"] in (M.DONE, M.SKIP))
    progress.started = time.time()


def download_all(provider, manifest, dest, progress, stop_event=None,
                 on_tick=None, log=None, session=None):
    progress.status = "downloading"
    if not provider.supports_streaming:
        provider.bulk_download(manifest, dest, progress, stop_event, on_tick, log)
        M.save(manifest, dest)
        _finish(progress, stop_event)
        return

    session = session or make_session()
    for entry in M.remaining(manifest):
        if stop_event and stop_event.is_set():
            break
        try:
            _download_one(provider, entry, dest, session, progress,
                          stop_event, on_tick, log)
        except Stopped:
            break
        M.save(manifest, dest)
        if on_tick:
            on_tick(progress)
    M.save(manifest, dest)
    _finish(progress, stop_event)


def scan_and_download(provider, source, dest, progress, stop_event=None,
                      on_tick=None, log=None, session=None):
    """Download files as they are discovered — no upfront scan wait. Returns the
    completed manifest. Non-streaming providers fall back to scan-then-download."""
    session = session or make_session()
    manifest = M.new_manifest(provider.key, source, dest, [])
    files = manifest["files"]

    if not provider.supports_streaming:
        progress.status = "scanning"
        files.extend(provider.scan(source))
        progress.files_total = len(files)
        progress.bytes_total = sum(e["size"] for e in files)
        M.save(manifest, dest)
        download_all(provider, manifest, dest, progress, stop_event, on_tick, log, session)
        return manifest

    lock = threading.Lock()
    scan_state = {"done": False}
    progress.status = "scanning"

    def on_entry(e):
        with lock:
            files.append(e)
            progress.files_total += 1
            progress.bytes_total += e["size"]

    def run_scan():
        try:
            provider.scan(source, on_entry=on_entry)
        except Exception as ex:
            if log:
                log(f"Scan error: {ex}")
        finally:
            scan_state["done"] = True

    scanner = threading.Thread(target=run_scan, daemon=True)
    scanner.start()

    progress.status = "downloading"
    idx = 0
    while True:
        if stop_event and stop_event.is_set():
            break
        with lock:
            entry = files[idx] if idx < len(files) else None
        if entry is None:
            if scan_state["done"]:
                break
            time.sleep(0.1)
            continue
        try:
            _download_one(provider, entry, dest, session, progress,
                          stop_event, on_tick, log)
        except Stopped:
            break
        idx += 1
        if idx % 20 == 0:
            _save_snapshot(manifest, files, lock, dest)
            if on_tick:
                on_tick(progress)

    scanner.join(timeout=2)
    _save_snapshot(manifest, files, lock, dest)
    _finish(progress, stop_event)
    return manifest


def _save_snapshot(manifest, files, lock, dest):
    """Save the manifest from a consistent copy (scanner may still be appending)."""
    with lock:
        snap = dict(manifest, files=list(files))
    M.save(snap, dest)


def _finish(progress, stop_event):
    if stop_event and stop_event.is_set():
        progress.status = "stopped"
    else:
        progress.status = "done"


def _download_one(provider, entry, dest, session, progress, stop_event, on_tick, log):
    rel = entry["rel_path"]
    size = entry["size"]
    local = safe_join(dest, rel)
    name = os.path.basename(rel)
    os.makedirs(os.path.dirname(local), exist_ok=True)

    if size and os.path.exists(local) and os.path.getsize(local) == size:
        entry["status"] = M.SKIP
        progress.skipped += 1
        progress.processed += 1
        return

    try:
        url, headers = provider.get_download(entry)
    except Exception as e:
        if log:
            log(f"No download URL for {rel}: {e}")
        entry["status"] = M.FAIL
        progress.failed += 1
        progress.processed += 1
        return

    tmp = local + ".part"
    for attempt in range(5):
        downloaded = 0
        file_start = time.time()
        try:
            with session.get(url, headers=headers, stream=True, timeout=60) as r:
                r.raise_for_status()
                total = size or int(r.headers.get("Content-Length", 0))
                progress.current_name = name
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        if stop_event and stop_event.is_set():
                            raise Stopped
                        f.write(chunk)
                        downloaded += len(chunk)
                        progress.bytes_done += len(chunk)
                        elapsed = time.time() - file_start
                        progress.current_speed = downloaded / elapsed if elapsed > 0 else 0
                        progress.current_pct = (downloaded / total * 100) if total else 0
                        progress.current_eta = (
                            (total - downloaded) / progress.current_speed
                            if progress.current_speed > 0 and total else 0)
                        if on_tick:
                            on_tick(progress)
            os.replace(tmp, local)
            entry["status"] = M.DONE
            progress.succeeded += 1
            progress.processed += 1
            return
        except Stopped:
            _cleanup(tmp)
            progress.bytes_done -= downloaded
            raise
        except Exception as e:
            progress.bytes_done -= downloaded
            if attempt == 4:
                if log:
                    log(f"Giving up on {rel} after 5 attempts: {e}")
                _cleanup(tmp)
                entry["status"] = M.FAIL
                progress.failed += 1
                progress.processed += 1
                return
            if log:
                log(f"Retry {name} ({attempt+1}/5): {e}")
            time.sleep(2 ** attempt)


def _cleanup(tmp):
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except OSError:
        pass
