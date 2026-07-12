"""Pytest configuration: set env vars BEFORE app import.

The reclip app reads RECLIP_API_TOKEN at import time and raises if missing,
so every test must pre-set this. RECLIP_DB_PATH must also point to a
writable location — the production default /data is read-only on macOS.
"""
import os
import tempfile

_tmpdir = tempfile.mkdtemp()
_db_path = os.path.join(_tmpdir, "reclip_jobs.db")
os.environ.setdefault("RECLIP_API_TOKEN", "test-reclip-token")
os.environ.setdefault("DOWNLOADS_PATH", _tmpdir)
os.environ.setdefault("RECLIP_DB_PATH", _db_path)
os.environ.setdefault("MAX_FILESIZE", "2G")
os.environ.setdefault("MIN_FREE_DISK_MB", "0")  # don't gate tests on disk space