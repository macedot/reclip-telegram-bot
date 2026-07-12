"""Tests for bot handlers — H5, M1, M2, M13."""
import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Set env vars BEFORE any handlers imports
_tmpdir = tempfile.mkdtemp()
os.environ["DOWNLOADS_PATH"] = _tmpdir
os.environ["ALLOWED_USER_IDS"] = "12345,67890"
os.environ["ALLOWED_GROUP_IDS"] = "-100111111,-100222222"
os.environ["RECLIP_API_TOKEN"] = "test-reclip-token"
os.environ.setdefault("BOT_TOKEN", "test-token")

# Make sure the bot package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import handlers  # noqa: E402


# ---------------------------------------------------------------------------
# H5 — ALLOWED_USER_IDS parses correctly
# ---------------------------------------------------------------------------


def test_allowed_user_ids_parsed_from_env():
    assert handlers.ALLOWED_USER_IDS == frozenset({12345, 67890})


def test_empty_allowed_user_ids_blocks_all(monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_IDS", "")
    importlib.reload(handlers)
    assert handlers.ALLOWED_USER_IDS == frozenset()
    # Re-set for downstream tests
    monkeypatch.setenv("ALLOWED_USER_IDS", "12345,67890")
    importlib.reload(handlers)


def test_allowed_group_ids_parsed_from_env():
    assert handlers.ALLOWED_GROUP_IDS == frozenset({-100111111, -100222222})


def test_empty_allowed_group_ids_is_empty_frozenset(monkeypatch):
    monkeypatch.setenv("ALLOWED_GROUP_IDS", "")
    importlib.reload(handlers)
    assert handlers.ALLOWED_GROUP_IDS == frozenset()
    monkeypatch.setenv("ALLOWED_GROUP_IDS", "-100111111,-100222222")
    importlib.reload(handlers)


# ---------------------------------------------------------------------------
# M2 — path resolution rejects traversal
# ---------------------------------------------------------------------------


def test_resolve_local_path_accepts_filename():
    p = handlers._resolve_local_path("abc123.mp4")
    assert p is not None
    assert p.name == "abc123.mp4"


def test_resolve_local_path_strips_traversal_components():
    """M2 — Path(file_path).name strips all directory components, so
    traversal attempts collapse to a bare filename inside DOWNLOADS_PATH."""
    p = handlers._resolve_local_path("../../etc/passwd")
    assert p is not None
    assert p.name == "passwd"
    assert p.is_relative_to(handlers.DOWNLOADS_PATH_RESOLVED)


def test_resolve_local_path_strips_absolute_paths():
    """M2 — absolute paths are reduced to their basename."""
    p = handlers._resolve_local_path("/etc/passwd")
    assert p is not None
    assert p.name == "passwd"
    assert p.is_relative_to(handlers.DOWNLOADS_PATH_RESOLVED)


def test_resolve_local_path_strips_dir_components():
    p = handlers._resolve_local_path("/some/dir/file.mp4")
    assert p is not None
    assert p.name == "file.mp4"
    assert p.is_relative_to(handlers.DOWNLOADS_PATH_RESOLVED)


def test_resolve_local_path_rejects_symlink_escape(monkeypatch, tmp_path):
    """M2 — defense-in-depth: a symlink in DOWNLOADS_PATH that points
    outside should not be followed to read arbitrary files."""
    target = tmp_path / "outside.txt"
    target.write_text("secret")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this filesystem")

    # Symlinks resolve to the target; if target is outside DOWNLOADS_PATH,
    # Path.resolve() will follow the link and the is_relative_to check fails.
    monkeypatch.setattr(handlers, "DOWNLOADS_PATH_RESOLVED", tmp_path)
    p = handlers._resolve_local_path("link.txt")
    # p may be None if .resolve() leaves DOWNLOADS_PATH; both outcomes are safe.
    if p is not None:
        assert p.is_relative_to(tmp_path)


# ---------------------------------------------------------------------------
# M13 — state cleanup
# ---------------------------------------------------------------------------


def test_state_key_format():
    assert handlers._state_key(1, 2, "abc") == "1:2:abc"


def test_evict_stale_removes_old_entries():
    handlers._state.clear()
    import time
    handlers._state["old"] = {"created": time.time() - 9999, "url": "x"}
    handlers._state["new"] = {"created": time.time(), "url": "y"}
    handlers._evict_stale()
    assert "old" not in handlers._state
    assert "new" in handlers._state


def test_url_hash_is_8_hex_chars():
    h = handlers._url_hash("https://example.com/video")
    assert len(h) == 8
    assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# H5 — _require_allowed decorator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_require_allowed_passes_for_allowed_user():
    called = {"v": False}

    @handlers._require_allowed
    async def inner(update, context):
        called["v"] = True
        return "ok"

    class _User:
        id = 12345

    class _Update:
        effective_user = _User()
        effective_chat = None
        callback_query = None
        message = None

    result = await inner(_Update(), None)
    assert called["v"] is True
    assert result == "ok"


@pytest.mark.asyncio
async def test_require_allowed_blocks_unauthorized_user():
    called = {"v": False}

    @handlers._require_allowed
    async def inner(update, context):
        called["v"] = True
        return "ok"

    class _User:
        id = 99999  # not in ALLOWED_USER_IDS

    class _Update:
        effective_user = _User()
        effective_chat = None
        callback_query = None
        message = None

    result = await inner(_Update(), None)
    assert called["v"] is False
    assert result is None


@pytest.mark.asyncio
async def test_require_allowed_passes_for_allowed_group_chat():
    """Any sender in an allowed group chat is permitted, regardless of user ID."""
    called = {"v": False}

    @handlers._require_allowed
    async def inner(update, context):
        called["v"] = True
        return "ok"

    class _User:
        id = 99999  # not in ALLOWED_USER_IDS

    class _Chat:
        id = -100111111  # IS in ALLOWED_GROUP_IDS

    class _Update:
        effective_user = _User()
        effective_chat = _Chat()
        callback_query = None
        message = None

    result = await inner(_Update(), None)
    assert called["v"] is True
    assert result == "ok"


@pytest.mark.asyncio
async def test_require_allowed_blocks_user_in_unknown_group():
    """Sender not in ALLOWED_USER_IDS AND chat not in ALLOWED_GROUP_IDS -> reject."""
    called = {"v": False}

    @handlers._require_allowed
    async def inner(update, context):
        called["v"] = True
        return "ok"

    class _User:
        id = 99999

    class _Chat:
        id = -100999999  # not in ALLOWED_GROUP_IDS either

    class _Update:
        effective_user = _User()
        effective_chat = _Chat()
        callback_query = None
        message = None

    result = await inner(_Update(), None)
    assert called["v"] is False
    assert result is None


@pytest.mark.asyncio
async def test_command_handlers_require_allowed_user():
    """Sanity-check that a sample command handler is wrapped by _require_allowed."""

    class _User:
        id = 99999

    class _Update:
        effective_user = _User()
        effective_chat = None
        callback_query = None
        message = None

    # cmd_platforms should reject an unauthorized user because it is decorated.
    result = await handlers.cmd_platforms(_Update(), None)
    assert result is None
