"""Job manager: background preparation workers, progress, history,
24-hour auto-delete sweeper, and the optional per-IP prepare cooldown.
"""
import json
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import store as store_mod

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA_DIR = os.path.join(BASE_DIR, "data")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "2"))
RETENTION_SECONDS = int(os.environ.get("RETENTION_SECONDS", str(24 * 3600)))
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "60"))

_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
_jobs = {}
_lock = threading.Lock()
_cooldown = {}          # ip -> last prepare epoch
_history_lock = threading.Lock()


class Job:
    def __init__(self, kind, title=None, video_id=None):
        self.id = uuid.uuid4().hex[:16]
        self.kind = kind                # video | audio | subtitle | thumbnail | zip
        self.title = title
        self.video_id = video_id
        self.status = "queued"          # queued|preparing|ready|error
        self.progress = {"stage": "Queued…", "percent": 0.0,
                         "done_bytes": 0, "total_bytes": None,
                         "speed": "", "eta": ""}
        self.error = None
        self.filename = None
        self.mime = None
        self.size = None
        self.key = None                 # local path or R2 key
        self.created_at = time.time()
        self.extra = {}

    def to_dict(self):
        return {
            "job_id": self.id, "kind": self.kind, "title": self.title,
            "video_id": self.video_id, "status": self.status,
            "progress": self.progress, "error": self.error,
            "filename": self.filename, "mime": self.mime,
            "size": self.size, "created_at": self.created_at,
            "extra": self.extra,
        }


def get_job(job_id):
    with _lock:
        return _jobs.get(job_id)


def create_job(kind, title=None, video_id=None):
    job = Job(kind, title=title, video_id=video_id)
    with _lock:
        _jobs[job.id] = job
    return job


def submit(fn, job):
    def wrapper():
        try:
            job.status = "preparing"
            fn(job)
        except Exception as ex:  # noqa: BLE001 - surface as job error
            job.status = "error"
            job.error = str(ex)[:500]
    _executor.submit(wrapper)
    return job


def check_cooldown(ip):
    """60-second per-IP cooldown between prepares (ytdown.to parity).

    Returns seconds to wait, or 0. Disabled when COOLDOWN_SECONDS=0.
    """
    if COOLDOWN_SECONDS <= 0 or not ip:
        return 0
    now = time.time()
    last = _cooldown.get(ip)
    if last and now - last < COOLDOWN_SECONDS:
        return int(COOLDOWN_SECONDS - (now - last)) + 1
    _cooldown[ip] = now
    return 0


# ---------------------------------------------------------------- history
def _load_history():
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _save_history(items):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(items[-20:], f, ensure_ascii=False)
    os.replace(tmp, HISTORY_FILE)


def record_history(job):
    with _history_lock:
        items = _load_history()
        items.append({"job_id": job.id, "kind": job.kind,
                      "title": job.title, "filename": job.filename,
                      "size": job.size, "created_at": job.created_at})
        _save_history(items)


def get_history():
    with _history_lock:
        items = _load_history()
    out = []
    for it in reversed(items):
        j = get_job(it["job_id"])
        it = dict(it)
        it["available"] = bool(j and j.status == "ready")
        out.append(it)
    return out


# ---------------------------------------------------------------- sweeper
def cleanup_orphans():
    """Delete job dirs on disk older than RETENTION_SECONDS.

    After a restart the in-memory registry is empty but the files
    remain; without this they would leak forever (history only drops
    its own rows). Only touches dirs older than the retention window.
    """
    base = store_mod.LOCAL_DIR
    now = time.time()
    try:
        names = os.listdir(base)
    except OSError:
        return
    for name in names:
        d = os.path.join(base, name)
        try:
            if os.path.isdir(d) and now - os.path.getmtime(d) > RETENTION_SECONDS:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def _sweep_once():
    now = time.time()
    st = store_mod.Store()
    with _lock:
        stale = [j for j in list(_jobs.values())
                 if now - j.created_at > RETENTION_SECONDS]
        for j in stale:
            del _jobs[j.id]
    for j in stale:
        if j.key:
            st.delete(j.key)
    with _history_lock:
        items = [it for it in _load_history()
                 if now - it.get("created_at", 0) <= RETENTION_SECONDS]
        _save_history(items)
    cleanup_orphans()


def sweeper_loop():
    while True:
        time.sleep(600)
        try:
            _sweep_once()
        except Exception:
            pass


def start_sweeper():
    t = threading.Thread(target=sweeper_loop, daemon=True)
    t.start()
