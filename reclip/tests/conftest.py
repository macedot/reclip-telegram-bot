"""Pytest configuration: set env vars BEFORE app import.

The reclip app reads RECLIP_API_TOKEN at import time and raises if missing,
so every test must pre-set this.
"""
import os
import tempfile

_tmpdir = tempfile.mkdtemp()
os.environ.setdefault("RECLIP_API_TOKEN", "test-reclip-token")
os.environ.setdefault("DOWNLOADS_PATH", _tmpdir)
os.environ.setdefault("MAX_FILESIZE", "2G")
os.environ.setdefault("MIN_FREE_DISK_MB", "0")  # don't gate tests on disk space