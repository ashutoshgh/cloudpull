import os
import time
import argparse

from core import make_provider, SCRIPT_DIR
from core import manifest as M
from core.downloader import Progress, download_all, scan_and_download, init_for_manifest
from core.utils import fmt_size, fmt_time

PROVIDERS = {
    "onedrive": "OneDrive (device-flow sign-in)",
    "gdrive_api": "Google Drive private folder (OAuth client_secret.json)",
    "gdrive_public": "Google Drive public link (gdown, no setup)",
}


def ask(question):
    return input(f"\n{question} [Y/N]: ").strip().lower() == "y"


def cli_tick(p):
    if p.status != "downloading":
        return
    print(
        f"\r[{p.processed+1}/{p.files_total}] {p.current_name[:30]:<30} "
        f"{p.current_pct:5.1f}% | {fmt_size(p.current_speed)}/s | "
        f"ETA {fmt_time(p.current_eta)} | {fmt_size(p.bytes_done)} done   ",
        end="", flush=True)


def main():
    ap = argparse.ArgumentParser(description="Download a OneDrive / Google Drive share.")
    ap.add_argument("source", nargs="?", help="Sharing link")
    ap.add_argument("dest", nargs="?", help="Local destination folder")
    ap.add_argument("-p", "--provider", choices=PROVIDERS, default="onedrive")
    ap.add_argument("--stream", action="store_true",
                    help="Start downloading immediately while scanning (no upfront wait).")
    args = ap.parse_args()

    source = args.source or input("Sharing link: ").strip()
    dest = args.dest or input("Local destination folder: ").strip()
    os.makedirs(dest, exist_ok=True)

    provider = make_provider(args.provider, SCRIPT_DIR)
    print(f"\nProvider: {PROVIDERS[args.provider]}")

    # ---- Auth ----
    print("Authenticating...")
    provider.authenticate()

    # ---- Resume or rescan ----
    manifest = M.load(dest)
    if manifest:
        c = M.summary(manifest)
        print(f"\nExisting manifest: {M.manifest_path(dest)}")
        print(f"  done {c[M.DONE]} | skip {c[M.SKIP]} | pending {c[M.PENDING]} | fail {c[M.FAIL]}")
        if ask("Resume from this manifest? (N = discard and rescan)"):
            M.reset_failed(manifest)
        else:
            manifest = None

    # ---- Stream mode: download while scanning (fresh runs only) ----
    if manifest is None and args.stream:
        print("\nDownloading while scanning...\n")
        progress = Progress()
        scan_and_download(provider, source, dest, progress,
                          on_tick=cli_tick, log=lambda m: print(f"\n{m}"))
        _summary(progress, M.load(dest))
        return

    # ---- Scan ----
    if manifest is None:
        if not ask("Scan the folder now?"):
            print("Aborted.")
            return
        print("\nScanning...")
        count = [0]

        def on_entry(e):
            count[0] += 1
            if count[0] == 1 or count[0] % 10 == 0:
                print(f"\r  Scanned {count[0]} files...   ", end="", flush=True)

        entries = provider.scan(source, on_entry)
        manifest = M.new_manifest(provider.key, source, dest, entries)
        M.save(manifest, dest)
        total = sum(e["size"] for e in entries)
        print(f"\n\nFound {len(entries)} files ({fmt_size(total)}).")
        print(f"Manifest: {M.manifest_path(dest)}")

    todo = M.remaining(manifest)
    if not todo:
        print("\nNothing to download — all files are done.")
        return

    todo_bytes = sum(e["size"] for e in todo)
    print(f"\nReady to download {len(todo)} files ({fmt_size(todo_bytes)}).")
    if not ask("Start downloading now?"):
        print("Aborted. Re-run and choose Resume to continue.")
        return

    print()
    progress = Progress()
    init_for_manifest(progress, manifest)
    download_all(provider, manifest, dest, progress,
                 on_tick=cli_tick, log=lambda m: print(f"\n{m}"))
    _summary(progress, manifest)


def _summary(progress, manifest):
    c = M.summary(manifest)
    print(f"\n\nDone. {progress.succeeded} downloaded, {progress.skipped} skipped, "
          f"{progress.failed} failed | {fmt_size(progress.bytes_done)} in "
          f"{fmt_time(time.time() - progress.started)}")
    if c[M.FAIL]:
        print(f"Re-run and choose Resume to retry {c[M.FAIL]} failed file(s).")


if __name__ == "__main__":
    main()
