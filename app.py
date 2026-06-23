import os
import time
import threading

import streamlit as st

from core import make_provider, SCRIPT_DIR
from core import manifest as M
from core.downloader import Progress, download_all, scan_and_download, init_for_manifest
from core.utils import fmt_size, fmt_time, make_session

fragment = getattr(st, "fragment", None) or st.experimental_fragment
LOG_FILE = os.path.join(SCRIPT_DIR, "download.log")

PROVIDERS = {
    "onedrive": "OneDrive",
    "gdrive_api": "Google Drive — private (OAuth)",
    "gdrive_public": "Google Drive — public link (gdown)",
}

GDRIVE_SETUP = (
    "1.  https://console.cloud.google.com  ->  create or pick a project\n"
    "2.  Enable the 'Google Drive API'\n"
    "3.  APIs & Services -> Credentials -> Create OAuth client ID -> Desktop app\n"
    "4.  Download the JSON, rename to 'client_secret.json',\n"
    f"    and place it in:  {SCRIPT_DIR}"
)

PROVIDER_HELP = (
    "**OneDrive** — Microsoft OneDrive/SharePoint shares. Sign in with a code.\n\n"
    "**Google Drive (public)** — any 'Anyone with the link' Drive item. No setup.\n\n"
    "**Google Drive (private)** — your own / privately-shared Drive. One-time setup."
)

LINK_HELP = (
    "The share URL of the folder or file you want.\n\n"
    "- OneDrive: `https://1drv.ms/…` or `…sharepoint.com/…`\n\n"
    "- Google folder: `https://drive.google.com/drive/folders/<id>`\n\n"
    "- Google file: `https://drive.google.com/file/d/<id>/view`\n\n"
    "Not sure how to get it? Open the **How to get your link** guide above."
)

DEST_HELP = (
    "Where files are saved on this PC. It's created if it doesn't exist. "
    "A `download_manifest.json` is written here so you can see what's left."
)

# Short inline hint shown under the link box, specific to the chosen provider.
PROVIDER_HINTS = {
    "onedrive":
        "🔗 In OneDrive (web): right-click the file/folder → **Share** → "
        "**Copy link**. Sign in with the Microsoft account that has access.",
    "gdrive_public":
        "🔗 In Google Drive: right-click → **Share** → set **General access** to "
        "**Anyone with the link** → **Copy link**. No sign-in needed.",
    "gdrive_api":
        "🔗 In Google Drive: right-click → **Share** → **Copy link** "
        "(access can stay Restricted — you'll sign in with your Google account).",
}

GUIDE = """\
### Which source should I pick?

| Provider | Sign-in | One-time setup | Use it for |
|---|---|---|---|
| **OneDrive** | Paste a code | None | OneDrive / SharePoint links |
| **Google Drive — public** | None | None | Drive items shared as *Anyone with the link* |
| **Google Drive — private** | Browser pop-up | `client_secret.json` | Your own or privately-shared Drive |

If a Google link is shared publicly, use **public** (zero setup). If it's private
(restricted to your account), use **private**. OneDrive handles both via sign-in.

---

### 📘 OneDrive — getting the link
1. Go to **onedrive.com** and find the file or folder.
2. Right-click it → **Share** (or the *Share* button).
3. Choose **Copy link**. If you see *"Anyone with the link"*, that's ideal — but
   a link shared directly to your account works too.
4. Paste it into **Sharing link**.
5. When you start, you'll get a short code + a link — open the link, paste the
   code, approve. Sign in with the account that can see the files.

### 📗 Google Drive — public (no setup)
1. In **drive.google.com**, right-click the file or folder → **Share**.
2. Under **General access**, pick **Anyone with the link**.
3. Click **Copy link** and paste it in.
4. No sign-in — downloading starts right away.
   *(Large files or very big folders can be flaky in this mode; for those, use
   the private option below.)*

### 📙 Google Drive — private (OAuth, one-time setup)
Use this for content that must stay **Restricted**, or when public mode struggles.
1. Right-click → **Share** → **Copy link** (access can stay Restricted).
2. One-time: create an OAuth client and drop `client_secret.json` in the app
   folder — the **Sign in** step shows the exact 4 steps if it's missing.
3. Click **Sign in with Google** — a browser opens, approve, and it returns here.

---

### Tips
- **Resume:** the `download_manifest.json` in your destination tracks every file's
  status. Re-run with the same link + folder and it continues where it left off.
- **Folder or single file:** both work for every provider.
- **Where am I in the process?** Open the manifest file any time to see exactly
  which files are still *pending*.
"""


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def ss_init():
    d = st.session_state
    d.setdefault("phase", "idle")          # idle|scanning|downloading|done
    d.setdefault("provider_key", "onedrive")
    d.setdefault("start_mode", "scan_first")  # scan_first | immediate
    d.setdefault("provider", None)
    d.setdefault("manifest", None)
    d.setdefault("progress", None)
    d.setdefault("stop_event", None)
    d.setdefault("worker", None)
    d.setdefault("device_flow", None)
    d.setdefault("log_lines", [])
    d.setdefault("ctx", {"scan_count": 0, "scanning": False, "manifest": None,
                         "auth_running": False, "error": None})


def get_provider():
    d = st.session_state
    if d.provider is None or d.provider.key != d.provider_key:
        d.provider = make_provider(d.provider_key, SCRIPT_DIR)
        d.device_flow = None
        d.manifest = None
        d.phase = "idle"
        d.ctx.update(auth_running=False, scanning=False, error=None)
    return d.provider


def file_log(msg):
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 2_000_000:
            open(LOG_FILE, "w").close()
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# Background workers (no st.* calls inside threads)
# --------------------------------------------------------------------------

def _onedrive_auth_worker(p, ctx):
    try:
        p.complete_device_flow()
    except Exception as e:
        ctx["error"] = str(e)
    finally:
        ctx["auth_running"] = False


def _google_auth_worker(p, ctx):
    try:
        p.authenticate()
    except Exception as e:
        ctx["error"] = str(e)
    finally:
        ctx["auth_running"] = False


def _scan_worker(p, source, dest, ctx):
    try:
        ctx["error"] = None
        count = [0]

        def on_entry(_e):
            count[0] += 1
            ctx["scan_count"] = count[0]

        entries = p.scan(source, on_entry)
        m = M.new_manifest(p.key, source, dest, entries)
        M.save(m, dest)
        ctx["manifest"] = m
    except Exception as e:
        ctx["error"] = str(e)
    finally:
        ctx["scanning"] = False


def _stream_worker(p, source, dest, progress, stop, logger, ctx):
    try:
        ctx["manifest"] = scan_and_download(
            p, source, dest, progress, stop_event=stop, log=logger,
            session=make_session())
    except Exception as e:
        ctx["error"] = str(e)
        progress.status = "error"


# --------------------------------------------------------------------------
# Auth panel
# --------------------------------------------------------------------------

def auth_panel(p):
    d = st.session_state
    ctx = d.ctx

    if p.key == "gdrive_public":
        st.info("No sign-in needed. The link must be shared as **Anyone with the link**.")
        try:
            import gdown  # noqa: F401
        except ImportError:
            st.error("`gdown` is not installed.  Run:  `pip install gdown`")
            return False
        return True

    if p.key == "gdrive_api":
        try:
            import google_auth_oauthlib  # noqa: F401
        except ImportError:
            st.error("Google libraries missing.  Run:  "
                     "`pip install google-auth google-auth-oauthlib google-api-python-client`")
            return False
        if not p.has_secret():
            st.warning("`client_secret.json` not found — one-time setup:")
            st.code(GDRIVE_SETUP, language="text")
            st.button("I've added it — re-check")
            return False
        if p.is_authenticated():
            st.success("Signed in to Google Drive.")
            return True
        if ctx["auth_running"]:
            st.info("A browser window opened — finish signing in there. "
                    "This page continues automatically.")
            return False
        if st.button("Sign in with Google", type="primary"):
            ctx["auth_running"] = True
            threading.Thread(target=_google_auth_worker, args=(p, ctx), daemon=True).start()
            st.rerun()
        return False

    # OneDrive (device flow)
    if p.is_authenticated():
        st.success("Signed in to OneDrive.")
        return True
    if ctx["auth_running"] and d.device_flow:
        flow = d.device_flow
        st.markdown(f"**Step 1 — Open this link:**  [{flow['verification_uri']}]"
                    f"({flow['verification_uri']})")
        st.markdown("**Step 2 — Enter this code:**")
        st.code(flow["user_code"], language="text")
        st.caption("Step 3 — Approve access. This page continues automatically once done.")
        return False
    if st.button("Get OneDrive sign-in code", type="primary"):
        d.device_flow = p.begin_device_flow()
        ctx["auth_running"] = True
        threading.Thread(target=_onedrive_auth_worker, args=(p, ctx), daemon=True).start()
        st.rerun()
    return False


@fragment(run_every=2.0)
def live_auth():
    if not st.session_state.ctx["auth_running"]:
        st.rerun()
    st.caption("Waiting for sign-in…")


# --------------------------------------------------------------------------
# Scan
# --------------------------------------------------------------------------

@fragment(run_every=0.5)
def live_scan():
    ctx = st.session_state.ctx
    st.progress(0.5, text=f"Scanning… {ctx['scan_count']} files found")
    if not ctx["scanning"]:
        if not ctx["error"]:
            st.session_state.manifest = ctx["manifest"]
        st.session_state.phase = "idle"
        st.rerun()


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------

def start_download(p, manifest, dest):
    d = st.session_state
    M.reset_failed(manifest)
    progress = Progress()
    init_for_manifest(progress, manifest)
    stop = threading.Event()
    loglist = d.log_lines

    def logger(msg):
        loglist.append(f"{time.strftime('%H:%M:%S')} {msg}")
        del loglist[:-200]
        file_log(msg)

    sess = make_session()

    def work():
        download_all(p, manifest, dest, progress, stop_event=stop, log=logger, session=sess)

    d.progress = progress
    d.stop_event = stop
    d.phase = "downloading"
    t = threading.Thread(target=work, daemon=True)
    t.start()
    d.worker = t


def start_immediate(p, source, dest):
    d = st.session_state
    progress = Progress()
    progress.status = "scanning"
    stop = threading.Event()
    loglist = d.log_lines

    def logger(msg):
        loglist.append(f"{time.strftime('%H:%M:%S')} {msg}")
        del loglist[:-200]
        file_log(msg)

    d.ctx["manifest"] = None
    d.progress = progress
    d.stop_event = stop
    d.phase = "downloading"
    t = threading.Thread(target=_stream_worker,
                         args=(p, source, dest, progress, stop, logger, d.ctx),
                         daemon=True)
    t.start()
    d.worker = t


def render_progress(snap):
    total = snap["files_total"] or 1
    suffix = "  ·  still scanning…" if snap["status"] == "scanning" else ""
    st.progress(min(snap["processed"] / total, 1.0),
                text=f"{snap['processed']} / {snap['files_total']} files{suffix}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Downloaded", snap["succeeded"])
    c2.metric("Skipped", snap["skipped"])
    c3.metric("Failed", snap["failed"])
    c4.metric("Data", fmt_size(snap["bytes_done"]))
    if snap["status"] == "downloading" and snap["current_name"]:
        st.caption(f"Current file: {snap['current_name']}")
        st.progress(
            min(snap["current_pct"] / 100, 1.0),
            text=f"{snap['current_pct']:.1f}%  ·  {fmt_size(snap['current_speed'])}/s"
                 f"  ·  ETA {fmt_time(snap['current_eta'])}")


@fragment(run_every=1.0)
def live_download():
    d = st.session_state
    if d.progress is None:
        return
    render_progress(d.progress.snapshot())
    if st.button("Stop", key="stop_live"):
        d.stop_event.set()
    if d.worker is not None and not d.worker.is_alive():
        if d.ctx.get("manifest") is not None:
            d.manifest = d.ctx["manifest"]
        d.phase = "done"
        st.rerun()


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    st.set_page_config(page_title="Cloud Folder Downloader", page_icon="📥", layout="centered")
    ss_init()
    d = st.session_state

    st.title("📥 Cloud Folder Downloader")
    st.caption("Download an entire OneDrive or Google Drive share, with resume.")

    with st.expander("📖 How to get your link & which source to pick"):
        st.markdown(GUIDE)

    # ---- Sidebar: provider ----
    with st.sidebar:
        st.header("Source")
        st.radio("Provider", list(PROVIDERS), key="provider_key",
                 format_func=lambda k: PROVIDERS[k], help=PROVIDER_HELP)
        st.divider()
        st.caption("OneDrive uses a code-based sign-in. Google private folders open "
                   "a browser. Google public links need no sign-in.")
        with st.popover("ℹ️ About the sources"):
            st.markdown(PROVIDER_HELP)

    provider = get_provider()

    source = st.text_input("Sharing link", key="source", help=LINK_HELP,
                           placeholder="Paste the OneDrive / Google Drive share URL")
    st.caption(PROVIDER_HINTS[d.provider_key])
    dest = st.text_input("Destination folder", key="dest", help=DEST_HELP,
                         placeholder=r"e.g. C:\Users\You\Downloads\MyFiles")

    if d.ctx["error"]:
        st.error(d.ctx["error"])

    if not (source and dest):
        st.info("Enter a link and a destination folder to begin.")
        return

    # ---- Manifest location notice ----
    mpath = M.manifest_path(dest)
    st.info(f"📄 **File list is stored at:**\n\n`{mpath}`\n\n"
            "It updates live as files finish (status: pending / done / skip / fail), "
            "so you can open it any time to see exactly what's left.")

    # Load any existing manifest from disk for this destination.
    if d.manifest is None and os.path.isdir(dest):
        existing = M.load(dest)
        if existing and existing.get("source") == source:
            d.manifest = existing

    # ---- Live phases ----
    if d.phase == "scanning":
        live_scan()
        return
    if d.phase == "downloading":
        st.subheader("Downloading")
        live_download()
        return

    # ---- Start mode (only relevant for a fresh run) ----
    if d.manifest is None:
        st.subheader("1 · How do you want to start?")
        st.radio(
            "start mode", ["scan_first", "immediate"], key="start_mode",
            label_visibility="collapsed",
            format_func=lambda k: {
                "scan_first": "🔍 Scan first — see total files & size, then download",
                "immediate": "⚡ Start immediately — download as files are found (no waiting)",
            }[k])

    # ---- Auth ----
    st.subheader("2 · Sign in")
    ready = auth_panel(provider)
    if d.ctx["auth_running"]:
        live_auth()
    if not ready:
        return

    # ---- Fresh run: honor start mode ----
    if d.manifest is None:
        st.subheader("3 · Go")
        if d.start_mode == "immediate":
            st.caption("Files start downloading the moment they're found — no waiting "
                       "for a full scan. Totals fill in as it goes.")
            if st.button("⚡ Start downloading now", type="primary"):
                start_immediate(provider, source, dest)
                st.rerun()
        else:
            st.caption("Scanning is parallelized, so this is quick even for big folders.")
            if st.button("🔍 Scan folder", type="primary"):
                d.ctx.update(scan_count=0, scanning=True, manifest=None, error=None)
                d.phase = "scanning"
                threading.Thread(target=_scan_worker,
                                 args=(provider, source, dest, d.ctx), daemon=True).start()
                st.rerun()
        return

    # ---- Have a manifest: show totals + download / resume ----
    m = d.manifest
    counts = M.summary(m)
    total_bytes = sum(e["size"] for e in m["files"])
    st.subheader("3 · Files")
    st.success(f"Found **{len(m['files'])} files** ({fmt_size(total_bytes)}).")
    cc = st.columns(4)
    cc[0].metric("Done", counts[M.DONE])
    cc[1].metric("Skipped", counts[M.SKIP])
    cc[2].metric("Pending", counts[M.PENDING])
    cc[3].metric("Failed", counts[M.FAIL])
    if st.button("Re-scan (discard this list)"):
        d.manifest = None
        st.rerun()

    st.subheader("4 · Download")
    todo = M.remaining(m)
    if not todo:
        st.success("🎉 Everything is already downloaded. Nothing to do.")
    else:
        todo_bytes = sum(e["size"] for e in todo)
        done_before = counts[M.DONE] + counts[M.SKIP]
        label = (f"▶ Resume download — {len(todo)} files left ({fmt_size(todo_bytes)})"
                 if done_before or counts[M.FAIL]
                 else f"▶ Start download — {len(todo)} files ({fmt_size(todo_bytes)})")
        if counts[M.FAIL]:
            st.warning(f"{counts[M.FAIL]} file(s) failed previously — they'll be retried.")
        if st.button(label, type="primary"):
            start_download(provider, m, dest)
            st.rerun()

    # ---- Done summary ----
    if d.phase == "done" and d.progress is not None:
        snap = d.progress.snapshot()
        st.divider()
        st.subheader("Result")
        render_progress(snap)
        if snap["failed"]:
            st.error(f"{snap['failed']} file(s) failed. Click the button above to retry them.")
        else:
            st.success("All done!")

    # ---- Log ----
    if d.log_lines:
        with st.expander("Log"):
            st.code("\n".join(d.log_lines[-100:]), language="text")


if __name__ == "__main__":
    main()
