# Cloud Folder Downloader — Context

Downloads an entire OneDrive / Google Drive share to a local folder, with
scan -> manifest -> resumable download. Streamlit UI + CLI over a shared core.

## Layout
- `core/utils.py`: session, formatting, `safe_join` (sanitizes Windows-illegal
  chars, blocks path traversal).
- `core/manifest.py`: JSON manifest `download_manifest.json` written **into the
  destination folder** (atomic). Statuses: pending / done / skip / fail.
- `core/downloader.py`: `Progress` (shared thread state) + `download_all`
  streaming engine (`.part` temp file -> atomic rename, per-file retry x5) +
  `scan_and_download` (download files as they're discovered; throttled saves).
- `core/providers/`: `base.Provider`, `onedrive.py` (MSAL device flow),
  `gdrive_api.py` (OAuth `client_secret.json`, streams via REST, exports native
  Google docs), `gdrive_public.py` (gdown; `download_folder(skip_download=True)`
  lists, per-file `gdown.download(resume=True)`).
- `core/__init__.py`: `make_provider(key)`.
- `cli.py`: `python cli.py <link> <dest> -p onedrive|gdrive_api|gdrive_public`.
- `app.py`: Streamlit UI (`streamlit run app.py`). Background threads + session
  state; progress/scan/auth polled via `st.fragment(run_every=...)`.

## Provider notes
- **OneDrive**: device flow - UI shows link + code to paste. Public CLIENT_ID.
- **Google private**: `client_secret.json` in this folder; browser OAuth popup.
- **Google public**: no setup; only "anyone with link"; resume = gdown skip.

## Start mode (UI + CLI `--stream`)
- **Scan first**: full enumeration -> totals -> download.
- **Start immediately**: `scan_and_download` downloads as files are found.
- Scans are **concurrent** (ThreadPoolExecutor BFS over folders, 10 workers;
  OneDrive uses `$top=200&$select=...`) so they're fast on big trees.

## Decisions
- Manifest moved txt -> JSON, stored in dest (user can open it to see remaining).
- Streaming engine shared; gdown providers use `bulk_download` (non-streaming).
- `provider.scan(source, on_entry)` emits each file as discovered (live count +
  stream download); returns full list too.
- `download_onedrive.py` is the original monolith, superseded by core+cli.py,
  left in place (not deleted) as the user's reference.

## Status
Core + CLI + app tested: streaming download/resume verified against a local
server; public-folder listing verified live (213 files); app renders + provider
switching verified via Streamlit AppTest. Python 3.14; all deps install.
