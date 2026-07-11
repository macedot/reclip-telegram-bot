"""FastAPI application factory for the reclip_bot admin dashboard."""
import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import db
from routes.api import router as api_router
from routes.pages import limiter as pages_limiter, router as pages_router

_STATIC_DIR = Path(__file__).parent / "static"
_DOWNLOADS_PATH = Path(os.environ.get("DOWNLOADS_PATH", "/downloads"))


async def _disk_snapshot_loop() -> None:
    """Background task: scan DOWNLOADS_PATH every 5 minutes and record snapshot."""
    while True:
        try:
            downloads_path = Path(os.environ.get("DOWNLOADS_PATH", "/downloads"))
            total_bytes = 0
            file_count = 0
            if downloads_path.exists():
                for item in downloads_path.rglob("*"):
                    if item.is_file():
                        try:
                            total_bytes += item.stat().st_size
                            file_count += 1
                        except OSError:
                            pass
            await db.insert_disk_snapshot(total_bytes=total_bytes, file_count=file_count)
        except Exception:
            pass
        await asyncio.sleep(300)  # 5 minutes


@asynccontextmanager
async def _lifespan(app: FastAPI):
    await db.init_db()
    task = asyncio.create_task(_disk_snapshot_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def create_app() -> FastAPI:
    """Application factory."""
    app = FastAPI(title="reclip admin", lifespan=_lifespan)

    # H6 — wire slowapi into the app
    app.state.limiter = pages_limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    app.include_router(api_router)
    app.include_router(pages_router)

    return app


app = create_app()
