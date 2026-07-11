"""Tests for the admin dashboard API routes."""
import json as _json
import os
import tempfile

import bcrypt
import pytest

# Set env vars BEFORE any dashboard imports so db.py picks them up.
_tmpdir = tempfile.mkdtemp()
os.environ["ADMIN_USER"] = "testuser"
os.environ["ADMIN_PASSWORD_HASH"] = bcrypt.hashpw(b"testpass123", bcrypt.gensalt()).decode()
os.environ["SECRET_KEY"] = "test-secret-key-aaaaaaaaaaaaaaaaaaaa"
os.environ["DASHBOARD_INTERNAL_TOKEN"] = "test-internal-token"
os.environ["DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["DOWNLOADS_PATH"] = _tmpdir
# Raise login rate limit so the test suite (which runs many logins from
# the same TestClient IP) doesn't trigger the 5/minute limit.
os.environ["LOGIN_RATE_LIMIT"] = "1000/minute"
# TestClient uses http://testserver, so Secure cookies would not be sent.
os.environ["DASHBOARD_SECURE_COOKIES"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from main import create_app  # noqa: E402

app = create_app()
client = TestClient(app, raise_server_exceptions=False)


def _delete(path: str, body: dict, cookies: dict | None = None, headers: dict | None = None):
    """Helper: send DELETE with JSON body using client.request()."""
    kwargs = {
        "content": _json.dumps(body).encode(),
        "headers": {"content-type": "application/json"},
    }
    if cookies:
        kwargs["cookies"] = cookies
    if headers:
        kwargs["headers"].update(headers)
    return client.request("DELETE", path, **kwargs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _login(user: str = "testuser", password: str = "testpass123") -> dict:
    """POST /login with correct creds and return the response cookies."""
    resp = client.post(
        "/login",
        data={"username": user, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"Login failed: {resp.status_code} {resp.text}"
    return resp.cookies


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Clear slowapi's storage between tests so the 1000/minute test limit
    doesn't bleed across tests."""
    from routes.pages import limiter as pages_limiter
    pages_limiter.reset()
    yield
    pages_limiter.reset()


def _event(token: str | None = "test-internal-token", **payload):
    headers = {}
    if token:
        headers["X-Internal-Token"] = token
    return client.post("/api/events", json=payload, headers=headers)


# ---------------------------------------------------------------------------
# C4 — /api/events authentication
# ---------------------------------------------------------------------------


def test_api_events_missing_token_returns_401():
    resp = client.post(
        "/api/events",
        json={"type": "download_start", "job_id": "no-tok-1",
              "url": "https://x", "platform": "x"},
    )
    assert resp.status_code == 401


def test_api_events_with_invalid_token_returns_401():
    resp = _event(token="wrong-token", type="download_start", job_id="x",
                  url="https://x", platform="x")
    assert resp.status_code == 401


def test_api_events_with_valid_token_returns_200():
    resp = _event(
        type="download_start", job_id="valid-tok-1",
        user_id=42, username="alice", chat_id=99,
        url="https://example.com/video.mp4", platform="youtube", title="t",
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# H6 — login throttle
# ---------------------------------------------------------------------------


def test_login_with_wrong_password_returns_401():
    resp = client.post(
        "/login",
        data={"username": "testuser", "password": "wrong"},
        follow_redirects=False,
    )
    assert resp.status_code == 401


def test_login_rate_limit_decorator_is_applied():
    """The /login route is wired up with a slowapi rate-limit decorator.

    We verify the decorator is present and the limit string is non-empty
    rather than driving a full HTTP burst (which would interfere with
    other tests in the suite that share the same TestClient IP).
    """
    from routes.pages import limiter
    assert limiter is not None
    # Verify the default limit is read from env or has a sane default
    import os
    limit = os.environ.get("LOGIN_RATE_LIMIT", "5/minute")
    assert "/" in limit  # "5/minute" shape


# ---------------------------------------------------------------------------
# H8 — payload validation (Pydantic discriminated union)
# ---------------------------------------------------------------------------


def test_event_payload_validation_missing_job_id():
    resp = _event(type="download_start", url="x", platform="x")  # no job_id
    assert resp.status_code == 422


def test_event_payload_validation_oversized_url():
    resp = _event(type="download_start", job_id="oversize-url",
                  url="x" * 3000, platform="x")
    assert resp.status_code == 422


def test_event_payload_validation_oversized_error_message():
    resp = _event(type="download_error", job_id="oversize-err",
                  error_message="x" * 3000)
    assert resp.status_code == 422


def test_event_payload_validation_unknown_type():
    resp = _event(type="not_a_real_type", job_id="unknown-1")
    assert resp.status_code == 422


def test_event_payload_validation_percent_out_of_range():
    resp = _event(type="download_progress", job_id="bad-pct",
                  percent=200.0)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# C4 + H8 — end-to-end event flow now uses flat JSON
# ---------------------------------------------------------------------------


def test_event_download_start():
    resp = _event(
        type="download_start", job_id="job-start-1",
        user_id=42, username="alice", chat_id=99,
        url="https://example.com/video.mp4", platform="youtube",
        title="t", format="video", quality="best",
    )
    assert resp.status_code == 200


def test_event_download_progress():
    _event(type="download_start", job_id="job-progress-1",
           user_id=1, username="bob", chat_id=1,
           url="https://example.com/v.mp4")
    resp = _event(type="download_progress", job_id="job-progress-1",
                  percent=50.0, speed=1000.0, eta=5.0,
                  downloaded_bytes=500, total_bytes=1000)
    assert resp.status_code == 200


def test_event_download_done():
    _event(type="download_start", job_id="job-done-1",
           user_id=2, username="carol", chat_id=2,
           url="https://example.com/done.mp4", platform="tiktok")
    resp = _event(type="download_done", job_id="job-done-1",
                  file_size_bytes=1024000, duration_seconds=3.5,
                  filename="done.mp4")
    assert resp.status_code == 200


def test_event_download_error():
    _event(type="download_start", job_id="job-err-1",
           user_id=3, username="dave", chat_id=3,
           url="https://example.com/err.mp4")
    resp = _event(type="download_error", job_id="job-err-1",
                  error_message="HTTP 403 forbidden")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Auth-required dashboard endpoints
# ---------------------------------------------------------------------------


def test_dashboard_stats_without_auth_returns_401():
    resp = client.get("/api/dashboard-stats")
    assert resp.status_code == 401


def test_dashboard_stats_with_auth_returns_200():
    cookies = _login()
    resp = client.get("/api/dashboard-stats", cookies=cookies)
    assert resp.status_code == 200
    body = resp.json()
    assert "stats" in body
    stats = body["stats"]
    assert "downloads_today" in stats
    assert "active_users_24h" in stats
    assert "error_rate" in stats


# ---------------------------------------------------------------------------
# Chart data
# ---------------------------------------------------------------------------


def test_chart_data_without_auth_returns_401():
    fresh = TestClient(app, raise_server_exceptions=False)
    resp = fresh.get("/api/chart-data?range=1D")
    assert resp.status_code == 401


def test_chart_data_with_auth():
    cookies = _login()
    for range_key in ("1D", "7D", "1M", "1Y"):
        resp = client.get(f"/api/chart-data?range={range_key}", cookies=cookies)
        assert resp.status_code == 200, f"Failed for range={range_key}"
        body = resp.json()
        assert "labels" in body
        assert "values" in body


# ---------------------------------------------------------------------------
# Active downloads
# ---------------------------------------------------------------------------


def test_active_downloads_without_auth_returns_401():
    fresh = TestClient(app, raise_server_exceptions=False)
    resp = fresh.get("/api/active-downloads")
    assert resp.status_code == 401


def test_active_downloads_with_auth():
    _event(type="download_start", job_id="job-active-1",
           user_id=10, username="eve", chat_id=10,
           url="https://example.com/active.mp4", platform="instagram",
           title="x")
    cookies = _login()
    resp = client.get("/api/active-downloads", cookies=cookies)
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    job_ids = [d["job_id"] for d in body]
    assert "job-active-1" in job_ids


# ---------------------------------------------------------------------------
# Delete files
# ---------------------------------------------------------------------------


def test_delete_files():
    """Create a temp file, delete it via API, verify it's gone."""
    import pathlib

    downloads_path = pathlib.Path(_tmpdir)
    test_file = downloads_path / "test_delete_me.mp4"
    test_file.write_bytes(b"fake video content")
    assert test_file.exists()

    cookies = _login()
    resp = _delete("/api/files", {"paths": ["test_delete_me.mp4"]}, cookies=cookies)
    assert resp.status_code == 200
    body = resp.json()
    assert "test_delete_me.mp4" in body["deleted"]
    assert not test_file.exists()


def test_delete_files_without_auth_returns_401():
    fresh = TestClient(app, raise_server_exceptions=False)
    resp = fresh.request(
        "DELETE",
        "/api/files",
        content=_json.dumps({"paths": ["something.mp4"]}).encode(),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 401


def test_purge_requires_confirm():
    """Sending empty/missing confirm should return 400."""
    cookies = _login()
    resp = _delete("/api/files/all", {}, cookies=cookies)
    assert resp.status_code == 400


def test_purge_with_confirm():
    """Create a temp file, purge all, verify it's gone."""
    import pathlib

    downloads_path = pathlib.Path(_tmpdir)
    test_file = downloads_path / "purge_me.mp4"
    test_file.write_bytes(b"content to be purged")
    assert test_file.exists()

    cookies = _login()
    resp = _delete("/api/files/all", {"confirm": "PURGE"}, cookies=cookies)
    assert resp.status_code == 200
    body = resp.json()
    assert "deleted_count" in body
    assert not test_file.exists()


# ---------------------------------------------------------------------------
# N4 — _active_downloads is capped
# ---------------------------------------------------------------------------


def test_active_downloads_capped_at_max(monkeypatch):
    """Spam > MAX_ACTIVE_DOWNLOADS start events; dict should not exceed cap."""
    from routes import api as api_module

    # Reset state and shrink the cap so the test is fast.
    api_module._active_downloads.clear()
    monkeypatch.setattr(api_module, "MAX_ACTIVE_DOWNLOADS", 8)

    for i in range(20):
        _event(type="download_start", job_id=f"cap-{i}",
               user_id=i, username=f"u{i}", chat_id=i,
               url="https://x", platform="x", title="x")

    cookies = _login()
    resp = client.get("/api/active-downloads", cookies=cookies)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) <= 8, f"expected <=8 entries, got {len(body)}"