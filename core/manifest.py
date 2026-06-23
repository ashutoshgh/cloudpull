import os
import json
import tempfile
from datetime import datetime

PENDING = "pending"
DONE = "done"
SKIP = "skip"
FAIL = "fail"

MANIFEST_NAME = "download_manifest.json"


def manifest_path(dest):
    return os.path.join(dest, MANIFEST_NAME)


def load(dest):
    p = manifest_path(dest)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save(manifest, dest):
    manifest["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    os.makedirs(dest, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        os.replace(tmp, manifest_path(dest))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def new_manifest(provider, source, dest, files):
    return {
        "provider": provider,
        "source": source,
        "dest": dest,
        "updated": None,
        "files": files,
    }


def summary(manifest):
    counts = {PENDING: 0, DONE: 0, SKIP: 0, FAIL: 0}
    for e in manifest["files"]:
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    return counts


def remaining(manifest):
    """Files still needing work (pending or previously failed)."""
    return [e for e in manifest["files"] if e["status"] in (PENDING, FAIL)]


def reset_failed(manifest):
    for e in manifest["files"]:
        if e["status"] == FAIL:
            e["status"] = PENDING
