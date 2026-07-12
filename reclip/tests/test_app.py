"""Tests for reclip/app.py — security hardening."""
import sys
import os
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as reclip_app


@pytest.fixture
def client():
    reclip_app.app.config["TESTING"] = True
    with reclip_app.app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# C1 — SSRF guard
# ---------------------------------------------------------------------------


def test_validate_url_rejects_file_scheme():
    with pytest.raises(ValueError, match="scheme"):
        reclip_app._validate_url("file:///etc/passwd")


def test_validate_url_rejects_ftp_scheme():
    with pytest.raises(ValueError, match="scheme"):
        reclip_app._validate_url("ftp://example.com/x")


def test_validate_url_rejects_javascript():
    with pytest.raises(ValueError, match="scheme"):
        reclip_app._validate_url("javascript:alert(1)")


def test_validate_url_rejects_loopback_literal():
    with pytest.raises(ValueError, match="blocked"):
        reclip_app._validate_url("http://127.0.0.1/")


def test_validate_url_rejects_rfc1918():
    with pytest.raises(ValueError, match="blocked"):
        reclip_app._validate_url("http://10.0.0.1/")


def test_validate_url_rejects_link_local_metadata():
    with pytest.raises(ValueError, match="blocked"):
        reclip_app._validate_url("http://169.254.169.254/latest/meta-data")


def test_validate_url_rejects_ipv6_loopback():
    with pytest.raises(ValueError, match="blocked"):
        reclip_app._validate_url("http://[::1]/")


def test_validate_url_rejects_ipv4_mapped_ipv6_loopback():
    """Regression: ::ffff:127.0.0.1 must hit the IPv4 deny-list, not bypass it."""
    with pytest.raises(ValueError, match="blocked"):
        reclip_app._validate_url("http://[::ffff:127.0.0.1]/")


def test_validate_url_rejects_ipv4_mapped_ipv6_metadata():
    """Regression: ::ffff:169.254.169.254 (AWS metadata via mapped v6)."""
    with pytest.raises(ValueError, match="blocked"):
        reclip_app._validate_url("http://[::ffff:169.254.169.254]/latest/meta-data")


def test_validate_url_rejects_empty_hostname():
    with pytest.raises(ValueError):
        reclip_app._validate_url("http://")


def test_validate_url_accepts_public_host():
    # example.com is a public host; should not raise
    reclip_app._validate_url("http://example.com/")


def test_validate_url_accepts_https():
    reclip_app._validate_url("https://www.youtube.com/watch?v=abc")


# ---------------------------------------------------------------------------
# C2 — X-Reclip-Token required
# ---------------------------------------------------------------------------


def test_api_info_requires_token(client):
    resp = client.post("/api/info", json={"url": "https://example.com"})
    assert resp.status_code == 401


def test_api_download_requires_token(client):
    resp = client.post("/api/download", json={"url": "https://example.com"})
    assert resp.status_code == 401


def test_api_status_requires_token(client):
    resp = client.get("/api/status/abc123")
    assert resp.status_code == 401


def test_api_file_requires_token(client):
    resp = client.get("/api/file/abc123")
    assert resp.status_code in (401, 404)  # 401 takes priority


def test_api_info_rejects_wrong_token(client):
    resp = client.post(
        "/api/info",
        json={"url": "https://example.com"},
        headers={"X-Reclip-Token": "wrong"},
    )
    assert resp.status_code == 401


def test_api_info_rejects_file_scheme(client, monkeypatch):
    """Even with a valid token, a file:// URL must be rejected (C1)."""
    # Patch _validate_url so we don't actually try DNS for the test
    def boom(url):
        raise ValueError("blocked destination: 127.0.0.0")
    monkeypatch.setattr(reclip_app, "_validate_url", boom)
    resp = client.post(
        "/api/info",
        json={"url": "file:///etc/passwd"},
        headers={"X-Reclip-Token": "test-reclip-token"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# M4 — format_id whitelist
# ---------------------------------------------------------------------------


def test_sanitize_format_id_accepts_legal():
    assert reclip_app._sanitize_format_id("22") == "22"
    assert reclip_app._sanitize_format_id("137+251") == "137+251"
    assert reclip_app._sanitize_format_id("abc_def-123") == "abc_def-123"


def test_sanitize_format_id_rejects_path_traversal():
    assert reclip_app._sanitize_format_id("../../etc/passwd") is None


def test_sanitize_format_id_rejects_shell_metachars():
    assert reclip_app._sanitize_format_id("22;rm -rf /") is None


def test_sanitize_format_id_rejects_too_long():
    assert reclip_app._sanitize_format_id("a" * 100) is None


def test_sanitize_format_id_rejects_non_string():
    assert reclip_app._sanitize_format_id(22) is None  # type: ignore[arg-type]
    assert reclip_app._sanitize_format_id(None) is None


# ---------------------------------------------------------------------------
# M5 — title sanitization
# ---------------------------------------------------------------------------


def test_sanitize_title_strips_control_chars():
    assert reclip_app._sanitize_title("Hello\x00\x07World") == "HelloWorld"


def test_sanitize_title_strips_path_separators():
    assert "/" not in reclip_app._sanitize_title("a/b\\c:d")
    assert reclip_app._sanitize_title("a/b\\c:d") == "abcd"


def test_sanitize_title_strips_html():
    assert "<script>" not in reclip_app._sanitize_title("<script>alert(1)</script>")


def test_sanitize_title_truncates_long():
    long = "a" * 1000
    out = reclip_app._sanitize_title(long)
    assert len(out) <= 200


def test_sanitize_title_falls_back():
    assert reclip_app._sanitize_title("///") == "download"
    assert reclip_app._sanitize_title("") == "download"


# ---------------------------------------------------------------------------
# M5 — thumbnail sanitization
# ---------------------------------------------------------------------------


def test_sanitize_thumbnail_accepts_https():
    assert reclip_app._sanitize_thumbnail("https://x/y.jpg") == "https://x/y.jpg"


def test_sanitize_thumbnail_accepts_http():
    assert reclip_app._sanitize_thumbnail("http://x/y.jpg") == "http://x/y.jpg"


def test_sanitize_thumbnail_rejects_javascript():
    assert reclip_app._sanitize_thumbnail("javascript:alert(1)") == ""


def test_sanitize_thumbnail_rejects_data():
    assert reclip_app._sanitize_thumbnail("data:text/html,<x>") == ""


def test_sanitize_thumbnail_rejects_empty():
    assert reclip_app._sanitize_thumbnail("") == ""


def test_sanitize_thumbnail_rejects_non_string():
    assert reclip_app._sanitize_thumbnail(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# H1 — stderr sanitization
# ---------------------------------------------------------------------------


def test_friendly_error_known_codes():
    assert reclip_app._friendly_error(1, "") == "download failed"
    assert reclip_app._friendly_error(2, "") == "invalid URL or unsupported site"
    assert reclip_app._friendly_error(-9, "") == "download timed out"


def test_friendly_error_scrambles_arbitrary_stderr():
    """Generic yt-dlp stderr must not leak through."""
    out = reclip_app._friendly_error(99, "/home/u/secret-path: Traceback (most recent call last)")
    assert "secret-path" not in out
    assert "Traceback" not in out


def test_friendly_error_allows_benign_info_line():
    out = reclip_app._friendly_error(0, "[info] Test video page")
    assert "Test video page" in out


# ---------------------------------------------------------------------------
# H2 — LRU eviction (now backed by SQLite in db.py)
# ---------------------------------------------------------------------------


def test_register_job_evicts_oldest_when_over_cap(monkeypatch):
    import db as reclip_db
    monkeypatch.setattr(reclip_db, "MAX_JOBS", 3)
    for i in range(4):
        reclip_app._register_job(f"job{i}", "u", None, "video", None)
    assert reclip_db.get_job("job0") is None
    assert reclip_db.get_job("job3") is not None
    c = reclip_db._connect()
    n = c.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
    assert n == 3


# ---------------------------------------------------------------------------
# M8 — active download count (delegates to db.count_active)
# ---------------------------------------------------------------------------


def test_active_download_count():
    import db as reclip_db
    reclip_db._connect().execute("DELETE FROM jobs")
    reclip_db._connect().commit()
    reclip_db.create_job("a", "u", None, "video", None)
    reclip_db.create_job("b", "u", None, "video", None)
    reclip_db.mark_done("b", file_path="x", filename="x",
                        width=None, height=None, duration=None)
    reclip_db.create_job("c", "u", None, "video", None)
    assert reclip_app._active_download_count() == 2


# ---------------------------------------------------------------------------
# L2 — security headers present on every response
# ---------------------------------------------------------------------------


def test_security_headers_present(client):
    resp = client.get("/")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert "Referrer-Policy" in resp.headers
    assert "Content-Security-Policy" in resp.headers


def test_security_headers_on_api(client):
    # Even a 401 should include the headers
    resp = client.post("/api/info", json={})
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "DENY"


# ---------------------------------------------------------------------------
# Index page is protected and does not leak the API token
# ---------------------------------------------------------------------------


def test_index_requires_token(client):
    resp = client.get("/")
    assert resp.status_code == 401


def test_index_does_not_leak_token_when_authorized(client):
    resp = client.get("/", headers={"X-Reclip-Token": "test-reclip-token"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'name="reclip-api-token"' not in body
    assert "test-reclip-token" not in body