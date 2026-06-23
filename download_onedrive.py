import os
import sys
import base64
import time
import requests
import msal
from datetime import datetime
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
AUTHORITY = "https://login.microsoftonline.com/common"
SCOPES = ["Files.Read", "Files.Read.All"]
GRAPH = "https://graph.microsoft.com/v1.0"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MANIFEST_FILE = os.path.join(SCRIPT_DIR, "manifest.txt")

PENDING = "PENDING"
DONE    = "DONE   "
FAIL    = "FAIL   "
SKIP    = "SKIP   "


# ---------------------------------------------------------------------------
# Auth / HTTP helpers
# ---------------------------------------------------------------------------

def make_session():
    s = requests.Session()
    retry = Retry(total=5, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def build_app():
    return msal.PublicClientApplication(CLIENT_ID, authority=AUTHORITY)


def get_token(app):
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            return result["access_token"]
    flow = app.initiate_device_flow(scopes=SCOPES)
    print(flow["message"])
    result = app.acquire_token_by_device_flow(flow)
    print("Authentication successful.\n")
    return result["access_token"]


def encode_sharing_url(url):
    b64 = base64.urlsafe_b64encode(url.encode()).rstrip(b"=").decode()
    return f"u!{b64}"


def fmt_size(b):
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def fmt_time(s):
    if s < 60:
        return f"{s:.0f}s"
    elif s < 3600:
        return f"{s/60:.0f}m {s%60:.0f}s"
    return f"{s/3600:.0f}h {(s%3600)/60:.0f}m"


def api_get(session, app, url):
    for attempt in range(5):
        try:
            token = get_token(app)
            r = session.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == 4:
                print(f"\nAPI error (gave up after 5 attempts): {e}")
                raise
            print(f"\nAPI error (attempt {attempt+1}/5): {e} — retrying...")
            time.sleep(2 ** attempt)


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------

def write_manifest(entries, sharing_url, dest):
    with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
        f.write(f"# OneDrive Download Manifest\n")
        f.write(f"# Source : {sharing_url}\n")
        f.write(f"# Dest   : {dest}\n")
        f.write(f"# Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"#\n")
        f.write(f"# STATUS  | size_bytes | rel_path | item_id | drive_id\n")
        f.write(f"# -------   ----------   --------   -------   --------\n")
        for e in entries:
            f.write(f"{e['status']} | {e['size']:>12} | {e['rel_path']} | {e['item_id']} | {e['drive_id']}\n")


def read_manifest():
    if not os.path.exists(MANIFEST_FILE):
        return None
    entries = []
    with open(MANIFEST_FILE, encoding="utf-8") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) != 5:
                continue
            entries.append({
                "status":   parts[0],
                "size":     int(parts[1]),
                "rel_path": parts[2],
                "item_id":  parts[3],
                "drive_id": parts[4],
            })
    return entries


def manifest_summary(entries):
    counts = {}
    for e in entries:
        s = e["status"].strip()
        counts[s] = counts.get(s, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Phase 1: Scan
# ---------------------------------------------------------------------------

def scan_folder(session, app, item_id, drive_id, rel_prefix, entries):
    url = f"{GRAPH}/drives/{drive_id}/items/{item_id}/children"
    while url:
        data = api_get(session, app, url)
        for child in data.get("value", []):
            rel = f"{rel_prefix}/{child['name']}" if rel_prefix else child["name"]
            if "folder" in child:
                child_drive_id = child.get("parentReference", {}).get("driveId", drive_id)
                scan_folder(session, app, child["id"], child_drive_id, rel, entries)
            else:
                entries.append({
                    "status":   PENDING,
                    "size":     child.get("size", 0),
                    "rel_path": rel,
                    "item_id":  child["id"],
                    "drive_id": child.get("parentReference", {}).get("driveId", drive_id),
                })
                print(f"\r  Scanned {len(entries)} files...   ", end="", flush=True)
        url = data.get("@odata.nextLink")


# ---------------------------------------------------------------------------
# Phase 2: Download
# ---------------------------------------------------------------------------

def download_file(session, app, entry, dest, stats, start_time, entries, sharing_url):
    rel_path = entry["rel_path"]
    local_path = os.path.join(dest, rel_path.replace("/", os.sep))
    total_size = entry["size"]
    name = os.path.basename(rel_path)

    os.makedirs(os.path.dirname(local_path), exist_ok=True)

    if os.path.exists(local_path) and os.path.getsize(local_path) == total_size:
        entry["status"] = SKIP
        stats["skipped"] += 1
        stats["done"] += 1
        write_manifest(entries, sharing_url, dest)
        return

    token = get_token(app)
    try:
        resp = session.get(
            f"{GRAPH}/drives/{entry['drive_id']}/items/{entry['item_id']}/content",
            headers={"Authorization": f"Bearer {token}"}, allow_redirects=False, timeout=30)
        dl_url = resp.headers.get("Location")
    except Exception as e:
        print(f"\nFailed to get download URL for {rel_path}: {e}")
        entry["status"] = FAIL
        write_manifest(entries, sharing_url, dest)
        return

    if not dl_url:
        print(f"\nNo download URL available, skipping: {rel_path}")
        entry["status"] = FAIL
        write_manifest(entries, sharing_url, dest)
        return

    downloaded = 0
    file_start = time.time()

    for attempt in range(5):
        try:
            with session.get(dl_url, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(local_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        f.write(chunk)
                        downloaded += len(chunk)
                        stats["bytes"] += len(chunk)

                        elapsed_f = time.time() - file_start
                        speed = downloaded / elapsed_f if elapsed_f > 0 else 0
                        pct = (downloaded / total_size * 100) if total_size else 0
                        eta = (total_size - downloaded) / speed if speed > 0 else 0
                        pending_left = sum(1 for e in entries if e["status"].strip() == PENDING.strip())

                        print(
                            f"\r[{stats['done']+1}] {name[:30]} "
                            f"{pct:.1f}% {fmt_size(downloaded)}/{fmt_size(total_size)} "
                            f"| {fmt_size(speed)}/s | ETA {fmt_time(eta)} "
                            f"| {pending_left} pending | {fmt_size(stats['bytes'])} total   ",
                            end="", flush=True
                        )
            break
        except Exception as e:
            if attempt == 4:
                print(f"\nGiving up on {rel_path} after 5 attempts: {e}")
                entry["status"] = FAIL
                write_manifest(entries, sharing_url, dest)
                return
            print(f"\nRetrying {name} (attempt {attempt+1}/5): {e}")
            time.sleep(2 ** attempt)
            stats["bytes"] -= downloaded
            downloaded = 0

    print()
    entry["status"] = DONE
    stats["done"] += 1
    write_manifest(entries, sharing_url, dest)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def prompt(question):
    return input(f"\n{question} [Y/N]: ").strip().lower() == "y"


def main():
    sharing_url = sys.argv[1] if len(sys.argv) > 1 else input("OneDrive sharing link: ").strip()
    dest        = sys.argv[2] if len(sys.argv) > 2 else input("Local destination folder: ").strip()

    session = make_session()
    app = build_app()
    get_token(app)

    share_id = encode_sharing_url(sharing_url)
    r = api_get(session, app, f"{GRAPH}/shares/{share_id}/driveItem")
    if "error" in r:
        print(f"Error: {r['error']['message']}")
        sys.exit(1)

    drive_id = r["parentReference"]["driveId"]
    os.makedirs(dest, exist_ok=True)

    # ---- Decide: resume or rescan ----
    existing = read_manifest()
    entries = []

    if existing:
        counts = manifest_summary(existing)
        print(f"\nExisting manifest found: {MANIFEST_FILE}")
        print(f"  DONE   : {counts.get('DONE', 0)}")
        print(f"  SKIP   : {counts.get('SKIP', 0)}")
        print(f"  PENDING: {counts.get('PENDING', 0)}")
        print(f"  FAIL   : {counts.get('FAIL', 0)}")
        if prompt("Resume from this manifest? (N = discard and rescan)"):
            entries = existing
            for e in entries:
                if e["status"].strip() == "FAIL":
                    e["status"] = PENDING
        # else entries stays empty → triggers scan below

    # ---- Phase 1: Scan ----
    if not entries:
        if not prompt("Start scanning the OneDrive folder now?"):
            print("Aborted.")
            sys.exit(0)

        print("\nScanning OneDrive folder structure...")
        if "folder" in r:
            scan_folder(session, app, r["id"], drive_id, "", entries)
        else:
            entries.append({
                "status":   PENDING,
                "size":     r.get("size", 0),
                "rel_path": r["name"],
                "item_id":  r["id"],
                "drive_id": drive_id,
            })

        total_bytes = sum(e["size"] for e in entries)
        print(f"\n\nScan complete: {len(entries)} files found, {fmt_size(total_bytes)} total.")
        write_manifest(entries, sharing_url, dest)
        print(f"Manifest saved to: {MANIFEST_FILE}")

    # ---- Phase 1b: Check which files already exist on disk ----
    if prompt("Check which files are already downloaded on disk?"):
        print("\nChecking local files...")
        newly_skipped = 0
        for e in entries:
            if e["status"].strip() != PENDING.strip():
                continue
            local_path = os.path.join(dest, e["rel_path"].replace("/", os.sep))
            if os.path.exists(local_path) and os.path.getsize(local_path) == e["size"]:
                e["status"] = SKIP
                newly_skipped += 1
        write_manifest(entries, sharing_url, dest)
        remaining = sum(1 for e in entries if e["status"].strip() == PENDING.strip())
        print(f"  {newly_skipped} files already on disk → marked SKIP.")
        print(f"  {remaining} files still pending download.")

    # ---- Phase 2: Download ----
    to_download = [e for e in entries if e["status"].strip() == PENDING.strip()]
    if not to_download:
        print("\nNo files left to download. All done!")
        sys.exit(0)

    total_pending_bytes = sum(e["size"] for e in to_download)
    print(f"\nReady to download {len(to_download)} files ({fmt_size(total_pending_bytes)}).")
    if not prompt("Start downloading now?"):
        print("Aborted. Run the script again and choose Resume to start.")
        sys.exit(0)

    print()
    stats = {"done": 0, "bytes": 0, "skipped": 0}
    start_time = time.time()

    for entry in to_download:
        download_file(session, app, entry, dest, stats, start_time, entries, sharing_url)

    elapsed = time.time() - start_time
    fail_count = sum(1 for e in entries if e["status"].strip() == "FAIL")
    print(f"\nDone. {stats['done']} processed ({stats['skipped']} skipped, {fail_count} failed) | {fmt_size(stats['bytes'])} in {fmt_time(elapsed)}")
    if fail_count:
        print(f"Re-run the script and choose Resume to retry {fail_count} failed file(s).")


if __name__ == "__main__":
    main()
