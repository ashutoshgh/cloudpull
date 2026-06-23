<div align="center">

# 📥 Cloud Folder Downloader

Grab a whole OneDrive or Google Drive share to a local folder — and pick up where you left off if it dies halfway.

<img src="https://readme-typing-svg.demolab.com?font=JetBrains+Mont&size=20&duration=3000&pause=900&color=4F8DFD&center=true&vCenter=true&width=560&lines=Scan+%E2%86%92+manifest+%E2%86%92+resumable+download;OneDrive+%2B+Google+Drive%2C+one+tool;Streamlit+UI+or+plain+CLI" alt="typing banner" />

<br/>

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Streamlit](https://img.shields.io/badge/UI-Streamlit-FF4B4B?logo=streamlit&logoColor=white)
![OneDrive](https://img.shields.io/badge/OneDrive-supported-0078D4?logo=microsoftonedrive&logoColor=white)
![Google Drive](https://img.shields.io/badge/Google%20Drive-supported-1A73E8?logo=googledrive&logoColor=white)
![Platform](https://img.shields.io/badge/Windows%20%7C%20macOS%20%7C%20Linux-grey)
![License](https://img.shields.io/badge/License-MIT-green)

</div>

---

## Why this exists

I had a OneDrive share with a few hundred files and no good way to pull all of it down. The web UI zips everything (and chokes past a couple of GB), and `gdown` alone doesn't touch OneDrive. So this wraps both behind one tool: point it at a share link, give it a folder, walk away. If your connection drops at file 180 of 213, the next run skips the 179 you already have.

It writes a `download_manifest.json` straight into your destination folder, so at any moment you can open that file and see exactly what's done and what's still pending. That's the whole trick behind resume — the manifest is the source of truth, not some hidden database.

## What it does

- **Three sources, one interface**
  - OneDrive / SharePoint links — sign in with a short device code
  - Google Drive *public* links (anyone-with-the-link) — zero setup, runs on `gdown`
  - Google Drive *private* folders — one-time OAuth with your own `client_secret.json`
- **Resume that actually works.** Files are matched by path + size; finished ones are skipped, failed ones are retried on the next run.
- **Two ways to start:** scan first (see total file count and size before committing), or stream — start pulling files the moment they're discovered, no upfront wait.
- **Folders or single files**, for every provider.
- **Per-file retry** (5 attempts, exponential backoff) and atomic writes — downloads go to a `.part` temp file and only get renamed into place once complete, so a half-written file never looks finished.
- **Use the GUI or don't.** Same core engine powers a Streamlit app and a no-frills CLI.

## Install

```bash
git clone https://github.com/ashutoshgh/cloudpull.git
cd cloudpull
pip install -r requirements.txt
```

Needs Python 3.10 or newer. Tested on 3.14.

## Running it

### Web UI (Streamlit)

```bash
streamlit run app.py
```

Opens at `http://localhost:8501`. Pick a source in the sidebar, paste your link, choose a destination, sign in if needed, and go. Progress, current-file speed, and ETA update live; there's a **Stop** button if you change your mind, and a built-in guide for finding your share link.

### Command line

```bash
# OneDrive folder
python cli.py "https://1drv.ms/f/..." "D:\Backups\Photos" -p onedrive

# Public Google Drive folder, download while scanning
python cli.py "https://drive.google.com/drive/folders/<id>" ./out -p gdrive_public --stream

# Private Google Drive folder (needs client_secret.json, see below)
python cli.py "https://drive.google.com/drive/folders/<id>" ./out -p gdrive_api
```

Run it again with the same link and folder to resume — it'll find the existing manifest and ask whether to continue or start fresh.

## Google Drive private folders (one-time setup)

Only needed for the `gdrive_api` provider. Public links and OneDrive don't need any of this.

1. Open the [Google Cloud Console](https://console.cloud.google.com) and create (or pick) a project.
2. Enable the **Google Drive API**.
3. Under **APIs & Services → Credentials**, create an **OAuth client ID** of type **Desktop app**.
4. Download the JSON, rename it to `client_secret.json`, and drop it in the project folder.

First run opens a browser to approve access. The token is cached locally so you only do it once.

## How it's put together

```
app.py            Streamlit UI — background threads, st.fragment polling for live progress
cli.py            Argparse CLI over the same core
core/
  __init__.py     make_provider(key) factory
  manifest.py     JSON manifest in the destination (atomic writes; pending/done/skip/fail)
  downloader.py   Progress state + streaming engine + scan-and-download
  utils.py        sessions, size/time formatting, safe_join (path-traversal guard)
  providers/
    base.py          Provider interface
    onedrive.py      MSAL device-flow auth, Graph API listing
    gdrive_api.py    OAuth, streams via REST, exports native Google Docs
    gdrive_public.py gdown wrapper for anyone-with-the-link items
```

The UI and CLI are thin. All the real work — scanning, manifests, the retrying download loop — lives in `core/` and is shared, so the two front ends can't drift apart in behaviour.

## A few honest caveats

- **Public Google Drive mode (`gdown`) can be flaky** on very large files or huge folders — Google rate-limits anonymous access. If a public folder keeps stalling, set it to your account and use `gdrive_api` instead.
- Native Google Docs/Sheets/Slides get **exported** (e.g. to `.docx`/`.xlsx`), since they have no raw bytes to download.
- The manifest matches files by size. If a file changes on the server but keeps the same byte count, it won't be re-fetched.
- `download_onedrive.py` is the original single-file version, kept around for reference. The `core/` + `cli.py` split superseded it.

## Security note

Your auth tokens (`.msal_cache.json`), Google `client_secret.json`/`token.json`, and the local `download.log` stay on your machine. They're listed in `.gitignore` and won't be committed — keep it that way.

## License

MIT. Do what you like with it.
