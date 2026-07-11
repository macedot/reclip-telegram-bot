"""Tests that event_client functions are fire-and-forget (never raise)."""
import asyncio
import os

# Point at an unreachable URL before importing event_client
os.environ["DASHBOARD_URL"] = "http://localhost:99999"
# C4 — token set, but the URL is unreachable so no real request is sent.
os.environ["DASHBOARD_INTERNAL_TOKEN"] = "test-token"

import event_client


def test_send_download_start_no_raise():
    asyncio.run(event_client.send_download_start(
        job_id="job-1",
        user_id=12345,
        username="testuser",
        chat_id=67890,
        url="https://example.com/video",
        platform="youtube",
        format="video",
        quality="best",
        title="Test Video",
    ))


def test_send_progress_no_raise():
    asyncio.run(event_client.send_progress(
        job_id="job-1",
        percent=42.5,
        speed=1024000.0,
        eta=30.0,
        downloaded_bytes=10485760,
        total_bytes=25165824,
    ))


def test_send_download_done_no_raise():
    asyncio.run(event_client.send_download_done(
        job_id="job-1",
        file_size_bytes=25165824,
        duration_seconds=12.5,
        filename="video.mp4",
    ))


def test_send_download_error_no_raise():
    asyncio.run(event_client.send_download_error(
        job_id="job-1",
        error_message="Download timed out",
    ))


def test_internal_token_is_set():
    """The module must read DASHBOARD_INTERNAL_TOKEN from env at import time."""
    assert event_client.DASHBOARD_INTERNAL_TOKEN == "test-token"


def test_headers_include_token_when_set():
    headers = event_client._headers()
    assert headers.get("X-Internal-Token") == "test-token"
