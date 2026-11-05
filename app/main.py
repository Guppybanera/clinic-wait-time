"""FastAPI application. Run with:  uvicorn app.main:app --reload"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from . import jobs
from .auth import Forbidden, LoginRequired
from .config import settings
from .db import init_db
from .routes import dev, patient, staff

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if not settings.dev_mode and settings.secret_key == "dev-secret-change-me":
        raise RuntimeError("Set SECRET_KEY (and DATA_KEY) before running with DEV_MODE=false")
    task = asyncio.create_task(jobs.run_forever()) if settings.run_jobs else None
    yield
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def create_app() -> FastAPI:
    app = FastAPI(title="Clinic Wait Time", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie="staff",
                       max_age=14 * 3600, same_site="strict", https_only=settings.https_only_cookies)

    @app.exception_handler(LoginRequired)
    async def _login(request: Request, exc: LoginRequired):
        url = f"/staff/login?next={quote(exc.next_url)}" + ("&locked=1" if exc.locked else "")
        return RedirectResponse(url, status_code=303)

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden):
        return HTMLResponse("Managers only. <a href='/staff'>Back</a>", status_code=403)

    @app.middleware("http")
    async def _headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path.startswith("/staff") or request.url.path.startswith("/dev"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.get("/")
    def root():
        return RedirectResponse("/staff", status_code=303)

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz():
        return "ok"

    app.include_router(patient.router)
    app.include_router(staff.router)
    if settings.dev_mode:
        app.include_router(dev.router)
    return app


app = create_app()
