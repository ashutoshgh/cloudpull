import os
import base64
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def make_session():
    s = requests.Session()
    retry = Retry(total=5, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def encode_sharing_url(url):
    b64 = base64.urlsafe_b64encode(url.encode()).rstrip(b"=").decode()
    return f"u!{b64}"


def fmt_size(b):
    b = float(b)
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def fmt_time(s):
    if s <= 0:
        return "—"
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s/60:.0f}m {s%60:.0f}s"
    return f"{s/3600:.0f}h {(s%3600)/60:.0f}m"


_ILLEGAL = '<>:"|?*'


def safe_join(dest, rel_path):
    """Join dest + rel_path into a Windows-safe path, rejecting any path that
    escapes dest. Illegal filename characters are replaced with '_'."""
    dest_abs = os.path.abspath(dest)
    segments = []
    for seg in rel_path.replace("\\", "/").split("/"):
        for ch in _ILLEGAL:
            seg = seg.replace(ch, "_")
        seg = seg.strip().rstrip(". ")  # Windows can't end a name with '.' or ' '
        if seg in ("", ".", ".."):
            continue
        segments.append(seg)
    if not segments:
        raise ValueError(f"Empty path after sanitizing: {rel_path!r}")
    local = os.path.abspath(os.path.join(dest_abs, *segments))
    if local != dest_abs and not local.startswith(dest_abs + os.sep):
        raise ValueError(f"Unsafe path outside destination: {rel_path}")
    return local
