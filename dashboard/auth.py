"""Authentication module for the reclip_bot admin dashboard."""
import hmac
import os
from typing import Optional

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from fastapi import Request, Response

COOKIE_NAME = "reclip_session"
_MAX_AGE_SECONDS = 86400  # 24 hours

_KNOWN_INSECURE_ADMIN = "admin"
_KNOWN_INSECURE_SECRETS = {
    "insecure-default-key-change-me",
    "change-me-in-production",
    "change-me",
}


def _admin_user() -> str:
    # M11/N3 — fail-closed; reject the default 'admin' username
    user = os.environ.get("ADMIN_USER")
    if not user:
        raise RuntimeError(
            "ADMIN_USER environment variable is required. "
            "Set it to a non-default value (not 'admin')."
        )
    if user == _KNOWN_INSECURE_ADMIN:
        raise RuntimeError(
            "ADMIN_USER is set to the known-insecure default 'admin'. "
            "Choose a different username."
        )
    return user


def _admin_password_hash() -> bytes:
    # H7 — bcrypt hash required
    raw = os.environ.get("ADMIN_PASSWORD_HASH")
    if not raw:
        raise RuntimeError(
            "ADMIN_PASSWORD_HASH environment variable is required. "
            "Generate with: python -c \"import bcrypt; print(bcrypt.hashpw(b'changeme', bcrypt.gensalt()).decode())\""
        )
    try:
        return raw.encode()
    except Exception as e:
        raise RuntimeError(f"ADMIN_PASSWORD_HASH must be a valid bcrypt hash: {e}") from e


def _secret_key() -> str:
    # C3 — fail-closed; reject known-insecure defaults
    key = os.environ.get("SECRET_KEY")
    if not key:
        raise RuntimeError(
            "SECRET_KEY environment variable is required. "
            "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    if key in _KNOWN_INSECURE_SECRETS:
        raise RuntimeError(
            "SECRET_KEY is set to a known-insecure default. "
            "Generate a fresh value with secrets.token_urlsafe(32)."
        )
    return key


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret_key())


def verify_credentials(username: str, password: str) -> bool:
    """Return True if credentials match env-configured admin user/password."""
    user = _admin_user()
    # constant-time-ish username compare
    if not hmac.compare_digest(username.encode(), user.encode()):
        # still hash-compare against a dummy to keep timing uniform
        try:
            bcrypt.checkpw(password.encode(), _admin_password_hash())
        except (ValueError, TypeError):
            pass
        return False
    try:
        return bcrypt.checkpw(password.encode(), _admin_password_hash())
    except (ValueError, TypeError):
        return False


def create_session_cookie(response: Response, username: str) -> None:
    """Sign and set the session cookie on the response."""
    token = _serializer().dumps(username)
    secure = os.environ.get("DASHBOARD_SECURE_COOKIES", "true").lower() == "true"
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=_MAX_AGE_SECONDS,
        httponly=True,
        secure=secure,  # N5
        samesite="lax",
    )


def get_current_user(request: Request) -> Optional[str]:
    """Read the session cookie and return the username, or None if invalid/missing."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        username = _serializer().loads(token, max_age=_MAX_AGE_SECONDS)
        return username
    except (BadSignature, SignatureExpired):
        return None
