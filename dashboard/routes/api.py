"""API routes for the reclip_bot admin dashboard."""
import collections
import hmac
import os
import shutil
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, confloat

import db
from auth import get_current_user

router = APIRouter()

MAX_ACTIVE_DOWNLOADS = int(os.environ.get("MAX_ACTIVE_DOWNLOADS", "256"))

# In-memory active downloads dict: job_id -> dict (N4 — capped below)
_active_downloads: "OrderedDict[str, Dict[str, Any]]" = collections.OrderedDict()


def _downloads_path() -> Path:
    return Path(os.environ.get("DOWNLOADS_PATH", "/downloads"))


# ---------------------------------------------------------------------------
# Auth dependency — session cookie
# ---------------------------------------------------------------------------

def require_auth(request: Request) -> str:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


# ---------------------------------------------------------------------------
# C4 — internal-network token for /api/events (shared secret header)
# ---------------------------------------------------------------------------

_INTERNAL_TOKEN = os.environ.get("DASHBOARD_INTERNAL_TOKEN", "")
if not _INTERNAL_TOKEN:
    raise RuntimeError(
        "DASHBOARD_INTERNAL_TOKEN environment variable is required. "
        "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
    )


def _require_internal_token(request: Request) -> None:
    provided = request.headers.get("X-Internal-Token", "")
    if not hmac.compare_digest(_INTERNAL_TOKEN, provided):
        raise HTTPException(status_code=401, detail="invalid internal token")


# ---------------------------------------------------------------------------
# H8 — Pydantic discriminated-union event payloads
# ---------------------------------------------------------------------------

_JOB_ID = Field(min_length=1, max_length=64)
_USER_ID = Field(default=None)
_USERNAME = Field(default="", max_length=64)
_CHAT_ID = Field(default=None)
_URL = Field(default="", max_length=2048)
_PLATFORM = Field(default="", max_length=64)
_TITLE = Field(default="", max_length=200)
_ERROR_MESSAGE = Field(default="", max_length=2048)


class _DownloadStart(BaseModel):
    type: Literal["download_start"]
    job_id: str = _JOB_ID
    user_id: Optional[int] = None
    username: str = Field(default="", max_length=64)
    chat_id: Optional[int] = None
    url: str = Field(default="", max_length=2048)
    platform: str = Field(default="", max_length=64)
    title: str = Field(default="", max_length=200)
    format: str = Field(default="", max_length=32)
    quality: str = Field(default="", max_length=32)


class _DownloadProgress(BaseModel):
    type: Literal["download_progress"]
    job_id: str = _JOB_ID
    percent: confloat(ge=0, le=100) = 0
    speed: Optional[float] = None
    eta: Optional[float] = None
    downloaded_bytes: Optional[int] = None
    total_bytes: Optional[int] = None


class _DownloadDone(BaseModel):
    type: Literal["download_done"]
    job_id: str = _JOB_ID
    file_size_bytes: int = Field(default=0, ge=0, le=10**12)
    duration_seconds: Optional[float] = None
    filename: str = Field(default="", max_length=256)


class _DownloadError(BaseModel):
    type: Literal["download_error"]
    job_id: str = _JOB_ID
    error_message: str = Field(default="", max_length=2048)


Event = Annotated[
    Union[_DownloadStart, _DownloadProgress, _DownloadDone, _DownloadError],
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# N4 — bounded active-downloads dict (LRU)
# ---------------------------------------------------------------------------


def _track_active(entry: Dict[str, Any]) -> None:
    job_id = entry["job_id"]
    while len(_active_downloads) >= MAX_ACTIVE_DOWNLOADS:
        _active_downloads.popitem(last=False)
    _active_downloads[job_id] = entry
    _active_downloads.move_to_end(job_id)


# ---------------------------------------------------------------------------
# Event ingestion (C4 — internal token required)
# ---------------------------------------------------------------------------


@router.post("/api/events")
async def ingest_event(
    request: Request,
    event: Event,
    _token: None = Depends(_require_internal_token),
) -> Dict[str, str]:
    """Accept download lifecycle events from the bot. Requires X-Internal-Token."""
    if event.type == "download_start":
        await db.insert_download_start(
            job_id=event.job_id,
            user_id=event.user_id,
            username=event.username or None,
            chat_id=event.chat_id,
            url=event.url or "",
            platform=event.platform or None,
        )
        _track_active({
            "job_id": event.job_id,
            "user_id": event.user_id,
            "username": event.username,
            "url": event.url,
            "platform": event.platform,
            "title": event.title,
            "format": event.format,
            "quality": event.quality,
            "percent": 0,
            "speed": 0,
            "eta": 0,
        })

    elif event.type == "download_progress":
        existing = _active_downloads.get(event.job_id)
        if existing is not None:
            existing.update({
                "percent": event.percent,
                "speed": event.speed,
                "eta": event.eta,
                "downloaded_bytes": event.downloaded_bytes,
                "total_bytes": event.total_bytes,
            })

    elif event.type == "download_done":
        await db.update_download_done(
            job_id=event.job_id,
            file_size_bytes=event.file_size_bytes,
            download_duration_sec=event.duration_seconds,
        )
        _active_downloads.pop(event.job_id, None)

    elif event.type == "download_error":
        await db.update_download_error(
            job_id=event.job_id,
            error_message=event.error_message or "Unknown error",
        )
        _active_downloads.pop(event.job_id, None)

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Dashboard stats (auth required)
# ---------------------------------------------------------------------------


@router.get("/api/dashboard-stats")
async def dashboard_stats(user: str = Depends(require_auth)) -> Dict[str, Any]:
    stats = await db.get_dashboard_stats()
    disk = await db.get_latest_disk_snapshot()
    return {"stats": stats, "disk": disk}


@router.get("/api/chart-data")
async def chart_data(
    range: str = "1D",
    user: str = Depends(require_auth),
) -> Dict[str, Any]:
    valid_ranges = {"1D", "7D", "1M", "1Y"}
    if range not in valid_ranges:
        raise HTTPException(status_code=400, detail=f"Invalid range. Must be one of {valid_ranges}")
    return await db.get_chart_data(range)


@router.get("/api/active-downloads")
async def active_downloads(user: str = Depends(require_auth)) -> List[Dict[str, Any]]:
    return list(_active_downloads.values())


# ---------------------------------------------------------------------------
# Admin file operations (auth required)
# ---------------------------------------------------------------------------

class DeleteFilesBody(BaseModel):
    paths: List[str]


class PurgeBody(BaseModel):
    confirm: Optional[str] = None


@router.delete("/api/files")
async def delete_files(
    body: DeleteFilesBody,
    user: str = Depends(require_auth),
) -> Dict[str, Any]:
    """Delete selected files. Uses filename only to prevent path traversal."""
    downloads_path = _downloads_path()
    deleted = []
    errors = []
    for p in body.paths:
        # Prevent path traversal: use only the filename component
        safe_path = downloads_path / Path(p).name
        try:
            if safe_path.exists():
                safe_path.unlink()
                deleted.append(str(safe_path.name))
            else:
                errors.append({"file": p, "error": "not found"})
        except Exception as exc:
            errors.append({"file": p, "error": str(exc)})
    return {"deleted": deleted, "errors": errors}


@router.delete("/api/files/all")
async def purge_all_files(
    body: PurgeBody,
    user: str = Depends(require_auth),
) -> Dict[str, Any]:
    """Purge all files in DOWNLOADS_PATH. Requires confirm='PURGE' in body."""
    if body.confirm != "PURGE":
        raise HTTPException(status_code=400, detail='Body must contain {"confirm": "PURGE"}')
    downloads_path = _downloads_path()
    deleted_count = 0
    if downloads_path.exists():
        for item in downloads_path.iterdir():
            try:
                if item.is_file():
                    item.unlink()
                    deleted_count += 1
                elif item.is_dir():
                    shutil.rmtree(item)
                    deleted_count += 1
            except Exception:
                pass
    return {"deleted_count": deleted_count}
