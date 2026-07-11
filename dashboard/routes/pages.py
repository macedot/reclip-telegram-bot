"""Page routes for the reclip_bot admin dashboard."""
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from slowapi import Limiter
from slowapi.util import get_remote_address

import db
from auth import (
    COOKIE_NAME,
    create_session_cookie,
    get_current_user,
    verify_credentials,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_templates_dir = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


# M12 — only render http(s) URLs as clickable links; everything else is plain text.
def _safe_url(value):
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = urlparse(value)
    except Exception:
        return None
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return value
    return None


# Register the filter on the underlying Jinja2 env so {{ row.url | safe_url }}
# works inside templates.
templates.env.filters["safe_url"] = _safe_url


safe_url = _safe_url  # public re-export for tests


# H6 — per-IP login rate limit. The Limiter instance is created here and
# wired into app.state by main.create_app(). The threshold is configurable
# via LOGIN_RATE_LIMIT (default "5/minute") so tests can raise it.
_LOGIN_RATE_LIMIT = os.environ.get("LOGIN_RATE_LIMIT", "5/minute")
limiter = Limiter(key_func=get_remote_address, default_limits=[])


# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
@limiter.limit(_LOGIN_RATE_LIMIT)
async def login_submit(request: Request) -> HTMLResponse:
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""

    if verify_credentials(username, password):
        response = RedirectResponse(url="/", status_code=303)
        create_session_cookie(response, username)
        return response

    # L6 — log failed attempts server-side
    client = request.client
    ip = client.host if client else "unknown"
    logger.warning("login failed for username=%r from ip=%s", username, ip)

    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": "Invalid credentials"},
        status_code=401,
    )


@router.get("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


# ---------------------------------------------------------------------------
# Protected pages
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request) -> HTMLResponse:
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request, "dashboard.html", {"user": user})


@router.get("/history", response_class=HTMLResponse)
async def history_page(
    request: Request,
    page: int = 1,
    platform: str = None,
    status: str = None,
    user_filter: str = None,
    date_from: str = None,
    date_to: str = None,
) -> HTMLResponse:
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    data = await db.get_downloads_page(
        page=page,
        platform=platform,
        status=status,
        user=user_filter,
        date_from=date_from,
        date_to=date_to,
    )
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "user": user,
            "data": data,
            "platform": platform,
            "status": status,
            "user_filter": user_filter,
            "date_from": date_from,
            "date_to": date_to,
        },
    )


@router.get("/errors", response_class=HTMLResponse)
async def errors_page(
    request: Request,
    date_from: str = None,
    date_to: str = None,
) -> HTMLResponse:
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    errors = await db.get_error_downloads(date_from=date_from, date_to=date_to)
    return templates.TemplateResponse(
        request,
        "errors.html",
        {"user": user, "errors": errors},
    )


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request) -> HTMLResponse:
    import os
    import shutil

    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    downloads_path = Path(os.environ.get("DOWNLOADS_PATH", "/downloads"))
    files = []
    downloads_bytes = 0
    if downloads_path.exists():
        for f in sorted(downloads_path.iterdir()):
            try:
                stat = f.stat()
                if f.is_file():
                    downloads_bytes += stat.st_size
                files.append({
                    "name": f.name,
                    "size": stat.st_size,
                    "is_dir": f.is_dir(),
                })
            except OSError:
                pass

    max_disk_mb = int(os.environ.get("CLEANUP_MAX_DISK_MB", "5000"))
    max_disk_bytes = max_disk_mb * 1024 * 1024
    disk_pct = round(downloads_bytes / max_disk_bytes * 100, 1) if max_disk_bytes else 0

    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "user": user,
            "files": files,
            "downloads_bytes": downloads_bytes,
            "max_disk_mb": max_disk_mb,
            "disk_pct": disk_pct,
        },
    )
