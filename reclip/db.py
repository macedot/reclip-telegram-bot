# SQLite-backed job store for reclip.
#
# Replaces the old in-memory `jobs = OrderedDict()` (lost on every container
# restart). All public functions acquire `_lock` so they are safe to call
# from any thread — Flask request threads, the per-download thread, and the
# stderr-reader daemon thread inside `_do_download`.
#
# Concurrency model: single sqlite3.Connection with check_same_thread=False,
# WAL mode, one module-level reentrant Lock. SQLite's internal locking
# handles file-level concurrency; the Python lock is just to keep the
# sqlite3 module itself happy about cross-thread connection use.

import json
import logging
import os
import sqlite3
import threading
import time

logger = logging.getLogger(__name__)

# --- env-driven config -------------------------------------------------------

DB_PATH = os.environ.get("RECLIP_DB_PATH", "/data/reclip_jobs.db")
MAX_JOBS = int(os.environ.get("MAX_JOBS", "1000"))
SWEEP_INTERVAL_SECONDS = int(os.environ.get("SWEEP_INTERVAL_SECONDS", "3600"))
JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", str(7 * 24 * 3600)))
STALE_DOWNLOAD_SECONDS = int(os.environ.get("STALE_DOWNLOAD_SECONDS", "60"))


# --- module-level state ------------------------------------------------------

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


# --- schema ------------------------------------------------------------------

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    url           TEXT NOT NULL,
    title         TEXT,
    format_choice TEXT NOT NULL,
    format_id     TEXT,
    status        TEXT NOT NULL CHECK (status IN ('downloading','done','error')),
    error         TEXT,
    progress_json TEXT,
    file          TEXT,
    filename      TEXT,
    width         INTEGER,
    height        INTEGER,
    duration      REAL,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status     ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at);
"""


# --- connection lifecycle ----------------------------------------------------

def _connect() -> sqlite3.Connection:
    """Open the module-level connection (idempotent)."""
    global _conn
    if _conn is not None:
        return _conn
    parent = os.path.dirname(os.path.abspath(DB_PATH))
    if parent:
        os.makedirs(parent, exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    _conn = c
    return c


def init_db() -> None:
    """Open the DB, apply schema, ready for use. Idempotent."""
    with _lock:
        _connect()
        logger.info("reclip db ready at %s (max_jobs=%d, job_ttl=%ds)",
                    DB_PATH, MAX_JOBS, JOB_TTL_SECONDS)


def close_db() -> None:
    """Close the connection (test cleanup)."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


# --- helpers -----------------------------------------------------------------

def _now() -> int:
    return int(time.time())


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    progress_json = d.pop("progress_json", None)
    d["progress"] = json.loads(progress_json) if progress_json else None
    return d


# --- public API --------------------------------------------------------------

def create_job(job_id: str, url: str, title: str | None,
               format_choice: str, format_id: str | None) -> None:
    """Insert a new row at status='downloading'. Enforces MAX_JOBS cap by
    evicting the oldest job first (deletes its file from disk + row from DB).
    """
    now = _now()
    with _lock:
        c = _connect()
        # Evict oldest while we are over the cap.
        while True:
            row = c.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()
            if row["n"] < MAX_JOBS:
                break
            _evict_oldest_locked(c)
        c.execute(
            """
            INSERT INTO jobs
                (job_id, url, title, format_choice, format_id,
                 status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'downloading', ?, ?)
            """,
            (job_id, url, title, format_choice, format_id, now, now),
        )


def get_job(job_id: str) -> dict | None:
    """Return a plain dict for the row, or None if not found.

    The returned dict includes a deserialized `progress` field (parsed
    from `progress_json`).
    """
    with _lock:
        c = _connect()
        row = c.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return _row_to_dict(row)


def update_progress(job_id: str, progress_dict: dict) -> None:
    """Replace the progress JSON for the row and bump updated_at.

    Hot path — called from the stderr_reader thread on every progress
    line emitted by yt-dlp.
    """
    payload = json.dumps(progress_dict, separators=(",", ":"))
    with _lock:
        c = _connect()
        c.execute(
            "UPDATE jobs SET progress_json = ?, updated_at = ? WHERE job_id = ?",
            (payload, _now(), job_id),
        )


def mark_done(job_id: str, *, file_path: str, filename: str,
              width: int | None, height: int | None, duration: float | None) -> None:
    """Atomic UPDATE setting all success fields + status='done'."""
    with _lock:
        c = _connect()
        c.execute(
            """
            UPDATE jobs
               SET status = 'done',
                   file = ?, filename = ?,
                   width = ?, height = ?, duration = ?,
                   updated_at = ?
             WHERE job_id = ?
            """,
            (file_path, filename, width, height, duration, _now(), job_id),
        )


def mark_error(job_id: str, error_message: str) -> None:
    """Set status='error' and store the error message."""
    with _lock:
        c = _connect()
        c.execute(
            "UPDATE jobs SET status = 'error', error = ?, updated_at = ? WHERE job_id = ?",
            (error_message, _now(), job_id),
        )


def count_active() -> int:
    """Number of rows currently status='downloading'."""
    with _lock:
        c = _connect()
        row = c.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status = 'downloading'"
        ).fetchone()
        return int(row["n"])


def evict_oldest() -> bool:
    """Delete the oldest job row + its file. Returns True if something was
    evicted, False if the table is empty.
    """
    with _lock:
        return _evict_oldest_locked(_connect())


def _evict_oldest_locked(c: sqlite3.Connection) -> bool:
    """Caller MUST hold _lock."""
    row = c.execute(
        "SELECT job_id, file FROM jobs ORDER BY created_at ASC LIMIT 1"
    ).fetchone()
    if row is None:
        return False
    file = row["file"]
    if file:
        try:
            os.remove(file)
        except OSError:
            pass
    c.execute("DELETE FROM jobs WHERE job_id = ?", (row["job_id"],))
    return True


def sweep_stale(ttl_seconds: int | None = None,
                stale_download_seconds: int | None = None) -> int:
    """Two passes:
    1. DELETE rows with status IN ('done','error') AND updated_at < now-ttl.
    2. UPDATE rows with status='downloading' AND updated_at < now-stale
       to status='error' with error='interrupted by restart'.

    Returns the number of rows deleted (does not count the marked-error
    rows; they're still in the table).
    """
    ttl = JOB_TTL_SECONDS if ttl_seconds is None else ttl_seconds
    stale = (STALE_DOWNLOAD_SECONDS
             if stale_download_seconds is None else stale_download_seconds)
    cutoff_delete = _now() - ttl
    cutoff_stale = _now() - stale
    with _lock:
        c = _connect()
        # Pass 1: clean up old done/error rows.
        cur = c.execute(
            "DELETE FROM jobs WHERE status IN ('done','error') AND updated_at < ?",
            (cutoff_delete,),
        )
        deleted = cur.rowcount
        # Pass 2: mark long-stale downloading rows as error.
        c.execute(
            """
            UPDATE jobs
               SET status = 'error',
                   error = 'interrupted by restart',
                   updated_at = ?
             WHERE status = 'downloading' AND updated_at < ?
            """,
            (_now(), cutoff_stale),
        )
        return deleted


# --- background sweeper ------------------------------------------------------

def start_sweeper() -> threading.Thread:
    """Start the background sweeper thread. Returns the thread (daemon)."""
    t = threading.Thread(target=_sweeper_loop, name="reclip-sweeper", daemon=True)
    t.start()
    return t


def _sweeper_loop() -> None:
    while True:
        try:
            time.sleep(SWEEP_INTERVAL_SECONDS)
            deleted = sweep_stale()
            if deleted:
                logger.info("sweeper deleted %d stale job row(s)", deleted)
        except Exception:
            logger.exception("sweeper iteration failed")