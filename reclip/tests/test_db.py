"""Tests for reclip/db.py — SQLite-backed job store.

Each test runs against a fresh temp DB file via the `fresh_db` fixture.
"""
import importlib
import json
import os
import threading
import time

import pytest


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Point reclip.db at a temp file and reload the module so init runs."""
    db_file = tmp_path / "test_jobs.db"
    monkeypatch.setenv("RECLIP_DB_PATH", str(db_file))
    # Tight intervals so sweeper tests don't have to wait long.
    monkeypatch.setenv("MAX_JOBS", "100")
    monkeypatch.setenv("SWEEP_INTERVAL_SECONDS", "60")
    monkeypatch.setenv("JOB_TTL_SECONDS", "3600")
    monkeypatch.setenv("STALE_DOWNLOAD_SECONDS", "60")
    import db
    importlib.reload(db)
    db.init_db()
    yield db
    db.close_db()


def test_init_db_creates_schema(fresh_db):
    c = fresh_db._connect()
    rows = c.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','index') ORDER BY name"
    ).fetchall()
    names = {r["name"] for r in rows}
    assert "jobs" in names
    assert "idx_jobs_status" in names
    assert "idx_jobs_created_at" in names
    # PRAGMA returns the mode; connection must have WAL.
    mode = c.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_create_and_get_job(fresh_db):
    fresh_db.create_job("j1", "https://example.com/v", "Title", "video", None)
    job = fresh_db.get_job("j1")
    assert job is not None
    assert job["job_id"] == "j1"
    assert job["url"] == "https://example.com/v"
    assert job["title"] == "Title"
    assert job["format_choice"] == "video"
    assert job["format_id"] is None
    assert job["status"] == "downloading"
    assert job["error"] is None
    assert job["progress"] is None
    assert job["file"] is None
    assert isinstance(job["created_at"], int)
    assert isinstance(job["updated_at"], int)


def test_get_missing_job_returns_none(fresh_db):
    assert fresh_db.get_job("does-not-exist") is None


def test_update_progress_writes_json(fresh_db):
    fresh_db.create_job("j1", "u", None, "video", None)
    fresh_db.update_progress("j1", {"percent": 42.5, "downloaded_bytes": 1000,
                                     "total_bytes": 2000, "speed": 50.0, "eta": 20})
    job = fresh_db.get_job("j1")
    assert job["progress"] == {
        "percent": 42.5, "downloaded_bytes": 1000,
        "total_bytes": 2000, "speed": 50.0, "eta": 20,
    }


def test_mark_done_sets_all_fields(fresh_db):
    fresh_db.create_job("j1", "u", None, "video", None)
    fresh_db.mark_done("j1", file_path="/downloads/j1.mp4", filename="video.mp4",
                       width=1920, height=1080, duration=120.5)
    job = fresh_db.get_job("j1")
    assert job["status"] == "done"
    assert job["file"] == "/downloads/j1.mp4"
    assert job["filename"] == "video.mp4"
    assert job["width"] == 1920
    assert job["height"] == 1080
    assert job["duration"] == 120.5


def test_mark_error_sets_status_and_error(fresh_db):
    fresh_db.create_job("j1", "u", None, "video", None)
    fresh_db.mark_error("j1", "yt-dlp returned exit code 1")
    job = fresh_db.get_job("j1")
    assert job["status"] == "error"
    assert job["error"] == "yt-dlp returned exit code 1"


def test_count_active_filters_by_status(fresh_db):
    fresh_db.create_job("a", "u", None, "video", None)
    fresh_db.create_job("b", "u", None, "video", None)
    fresh_db.create_job("c", "u", None, "video", None)
    fresh_db.mark_done("b", file_path="x", filename="x", width=None, height=None, duration=None)
    assert fresh_db.count_active() == 2
    fresh_db.mark_error("a", "x")
    assert fresh_db.count_active() == 1


def test_create_job_evicts_oldest_when_over_cap(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(fresh_db, "MAX_JOBS", 3)
    file_to_remove = tmp_path / "old.mp4"
    file_to_remove.write_bytes(b"x")
    # Insert 3 rows then mark them done so they have a file attached for eviction.
    for i in range(3):
        fresh_db.create_job(f"job{i}", "u", None, "video", None)
        fresh_db.mark_done(f"job{i}", file_path=str(file_to_remove),
                           filename="video.mp4", width=None, height=None, duration=None)
    # Insert a 4th — should evict job0 and delete its file.
    fresh_db.create_job("job3", "u", None, "video", None)
    assert fresh_db.get_job("job0") is None
    assert fresh_db.get_job("job3") is not None
    assert not file_to_remove.exists()
    c = fresh_db._connect()
    n = c.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
    assert n == 3


def test_sweep_stale_deletes_old_done_and_error(fresh_db):
    c = fresh_db._connect()
    fresh_db.create_job("j1", "u", None, "video", None)
    fresh_db.create_job("j2", "u", None, "video", None)
    fresh_db.mark_done("j1", file_path="x", filename="x", width=None, height=None, duration=None)
    fresh_db.mark_error("j2", "oops")
    # Backdate so JOB_TTL_SECONDS (default 3600) makes them eligible.
    past = int(time.time()) - 7200
    c.execute("UPDATE jobs SET updated_at = ?", (past,))
    deleted = fresh_db.sweep_stale()
    assert deleted == 2
    assert fresh_db.get_job("j1") is None
    assert fresh_db.get_job("j2") is None


def test_sweep_stale_marks_stale_downloading_as_error(fresh_db):
    fresh_db.create_job("j1", "u", None, "video", None)
    # Backdate updated_at past the STALE_DOWNLOAD_SECONDS threshold.
    c = fresh_db._connect()
    past = int(time.time()) - (fresh_db.STALE_DOWNLOAD_SECONDS + 60)
    c.execute("UPDATE jobs SET updated_at = ? WHERE job_id = ?", (past, "j1"))
    fresh_db.sweep_stale()
    job = fresh_db.get_job("j1")
    assert job["status"] == "error"
    assert job["error"] == "interrupted by restart"


def test_sweep_keeps_recent_downloading(fresh_db, monkeypatch):
    monkeypatch.setattr(fresh_db, "STALE_DOWNLOAD_SECONDS", 3600)
    fresh_db.create_job("j1", "u", None, "video", None)
    fresh_db.sweep_stale()
    assert fresh_db.get_job("j1")["status"] == "downloading"


def test_concurrent_writers_dont_corrupt(fresh_db):
    fresh_db.create_job("j1", "u", None, "video", None)

    def writer(thread_id: int):
        for i in range(50):
            fresh_db.update_progress(
                "j1",
                {"percent": thread_id * 1000 + i,
                 "downloaded_bytes": i * 100,
                 "total_bytes": 1000,
                 "speed": 1.0,
                 "eta": 1.0},
            )

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    job = fresh_db.get_job("j1")
    assert job is not None
    assert job["status"] == "downloading"
    # progress_json was last-write-wins; value must be a valid dict with our shape.
    assert "percent" in job["progress"]