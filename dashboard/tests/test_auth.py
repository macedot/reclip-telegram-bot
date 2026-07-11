"""Tests for dashboard/auth.py fail-closed behavior."""
import importlib
import os

import pytest


def _reload_auth(monkeypatch, **env):
    """Set env vars, drop cached auth module, reimport."""
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    # Make sure no other env vars leak in.
    for k in ("ADMIN_USER", "ADMIN_PASSWORD_HASH", "SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    import auth
    importlib.reload(auth)
    return auth


# ---------------------------------------------------------------------------
# C3 — SECRET_KEY fail-closed
# ---------------------------------------------------------------------------


def test_secret_key_missing_raises_runtime_error(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    import auth
    importlib.reload(auth)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        auth._secret_key()


def test_secret_key_known_insecure_default_rejected(monkeypatch):
    for bad in ("insecure-default-key-change-me", "change-me-in-production", "change-me"):
        auth = _reload_auth(monkeypatch, SECRET_KEY=bad)
        with pytest.raises(RuntimeError, match="known-insecure default"):
            auth._secret_key()


def test_secret_key_accepts_random_value(monkeypatch):
    auth = _reload_auth(monkeypatch, SECRET_KEY="x" * 64)
    assert auth._secret_key() == "x" * 64


# ---------------------------------------------------------------------------
# M11/N3 — ADMIN_USER fail-closed
# ---------------------------------------------------------------------------


def test_admin_user_missing_raises_runtime_error(monkeypatch):
    monkeypatch.delenv("ADMIN_USER", raising=False)
    import auth
    importlib.reload(auth)
    with pytest.raises(RuntimeError, match="ADMIN_USER"):
        auth._admin_user()


def test_admin_user_default_rejected(monkeypatch):
    auth = _reload_auth(monkeypatch, ADMIN_USER="admin")
    with pytest.raises(RuntimeError, match="known-insecure default"):
        auth._admin_user()


def test_admin_user_accepts_other_value(monkeypatch):
    auth = _reload_auth(monkeypatch, ADMIN_USER="alice")
    assert auth._admin_user() == "alice"


# ---------------------------------------------------------------------------
# H7 — bcrypt password verification
# ---------------------------------------------------------------------------


def test_password_hash_missing_raises(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
    import auth
    importlib.reload(auth)
    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD_HASH"):
        auth._admin_password_hash()


def test_verify_credentials_with_correct_bcrypt(monkeypatch):
    import bcrypt as _bcrypt
    h = _bcrypt.hashpw(b"correctpw", _bcrypt.gensalt()).decode()
    auth = _reload_auth(monkeypatch, ADMIN_USER="alice", ADMIN_PASSWORD_HASH=h, SECRET_KEY="k" * 32)
    assert auth.verify_credentials("alice", "correctpw") is True


def test_verify_credentials_with_wrong_password(monkeypatch):
    import bcrypt as _bcrypt
    h = _bcrypt.hashpw(b"correctpw", _bcrypt.gensalt()).decode()
    auth = _reload_auth(monkeypatch, ADMIN_USER="alice", ADMIN_PASSWORD_HASH=h, SECRET_KEY="k" * 32)
    assert auth.verify_credentials("alice", "wrongpw") is False


def test_verify_credentials_with_wrong_user(monkeypatch):
    import bcrypt as _bcrypt
    h = _bcrypt.hashpw(b"correctpw", _bcrypt.gensalt()).decode()
    auth = _reload_auth(monkeypatch, ADMIN_USER="alice", ADMIN_PASSWORD_HASH=h, SECRET_KEY="k" * 32)
    assert auth.verify_credentials("bob", "correctpw") is False


# ---------------------------------------------------------------------------
# N5 — Secure cookie flag
# ---------------------------------------------------------------------------


def test_cookie_secure_flag_default_on(monkeypatch):
    monkeypatch.delenv("DASHBOARD_SECURE_COOKIES", raising=False)
    import auth
    importlib.reload(auth)
    from starlette.responses import Response
    resp = Response()
    auth.create_session_cookie(resp, "alice")
    set_cookie = resp.headers.get("set-cookie", "")
    assert "Secure" in set_cookie


def test_cookie_secure_flag_can_opt_out(monkeypatch):
    monkeypatch.setenv("DASHBOARD_SECURE_COOKIES", "false")
    import auth
    importlib.reload(auth)
    from starlette.responses import Response
    resp = Response()
    auth.create_session_cookie(resp, "alice")
    set_cookie = resp.headers.get("set-cookie", "")
    assert "Secure" not in set_cookie