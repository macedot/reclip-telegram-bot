"""Tests for the dashboard Jinja filters (M12 — safe_url)."""

# This test only exercises the safe_url filter and does not need
# full app env vars. routes.pages is safe to import with no env setup.

from routes.pages import safe_url  # noqa: E402


def test_safe_url_allows_http():
    assert safe_url("http://example.com/x") == "http://example.com/x"


def test_safe_url_allows_https():
    assert safe_url("https://example.com/x") == "https://example.com/x"


def test_safe_url_rejects_javascript():
    assert safe_url("javascript:alert(1)") is None


def test_safe_url_rejects_data():
    assert safe_url("data:text/html,<script>alert(1)</script>") is None


def test_safe_url_rejects_vbscript():
    assert safe_url("vbscript:msgbox(1)") is None


def test_safe_url_rejects_empty():
    assert safe_url("") is None
    assert safe_url(None) is None


def test_safe_url_rejects_no_scheme():
    assert safe_url("example.com/x") is None


def test_safe_url_rejects_no_host():
    # urlparse will parse http:///path but no netloc
    assert safe_url("http:///path") is None