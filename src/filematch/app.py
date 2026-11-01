"""FastAPI application factory, middleware and routes."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

import jinja2
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from filematch import __version__
from filematch.config import Settings
from filematch.readers import ReadError, TableInfo, read_table
from filematch.routes_mapping import router as mapping_router
from filematch.sessions import COOKIE_NAME, Session, SessionStore
from filematch.uploads import UploadError, check_xlsx_content, receive_files
from filematch.web import SLOTS, current_session, render, set_session_cookie

PACKAGE_DIR = Path(__file__).parent
# Starlette keeps the brackets on IPv6 Host headers, so "[::1]" is what it compares.
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "::1", "[::1]"]
SECURITY_HEADERS = [
    (b"content-security-policy", b"default-src 'self'"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
]


class SecurityHeadersMiddleware:
    """Adds CSP, nosniff and referrer headers to every HTTP response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    h for h in message.get("headers", []) if h[0] not in dict(SECURITY_HEADERS)
                ]
                message["headers"] = headers + SECURITY_HEADERS
            await send(message)

        await self.app(scope, receive, send_with_headers)


def make_templates() -> Jinja2Templates:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(PACKAGE_DIR / "templates"),
        autoescape=True,  # every template, regardless of extension
        undefined=jinja2.StrictUndefined,
    )
    return Jinja2Templates(env=env)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = SessionStore(ttl_seconds=settings.session_ttl_seconds)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async def sweeper() -> None:
            while True:
                await asyncio.sleep(settings.sweep_interval_seconds)
                store.sweep()

        task = asyncio.create_task(sweeper())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            store.clear_all()

    app = FastAPI(
        title="filematch",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.templates = make_templates()
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(mapping_router)
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        session = current_session(request)
        return render(request, "upload.html", session)

    @app.post("/upload")
    async def upload(request: Request) -> Response:
        session = current_session(request)
        is_new = session is None
        if session is None:
            session = store.create()

        limit = settings.max_upload_bytes
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > len(SLOTS) * limit + 1024**2:
            return upload_error(
                request, session, is_new, ["The upload is larger than the limit."], 413
            )

        try:
            received, _ = await receive_files(
                request.headers.get("content-type", ""),
                request.stream(),
                session.directory,
                set(SLOTS),
                limit,
            )
        except UploadError as exc:
            return upload_error(request, session, is_new, [str(exc)], exc.status_code)
        if not received:
            return upload_error(request, session, is_new, ["Choose at least one file."], 400)

        errors: list[str] = []
        for slot, item in received.items():
            try:
                table = await run_in_threadpool(load_upload, item.path, item.filename, settings)
            except (UploadError, ReadError) as exc:
                item.path.unlink(missing_ok=True)
                errors.append(f"{SLOTS[slot]}: {exc}")
                continue
            with session.lock:
                previous = session.files.get(slot)
                final = session.directory / f"{slot}{item.path.suffix}"
                if previous is not None and previous.path != final:
                    previous.path.unlink(missing_ok=True)
                item.path.replace(final)
                table.path = final
                session.files[slot] = table
                session.state.clear()  # inputs changed: mapping and results are stale
        if errors:
            return upload_error(request, session, is_new, errors, 400)
        response = RedirectResponse("/", status_code=303)
        if is_new:
            set_session_cookie(response, session)
        return response

    @app.post("/clear")
    def clear(request: Request) -> Response:
        session_id = request.cookies.get(COOKIE_NAME)
        if session_id:
            store.delete(session_id)
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(COOKIE_NAME, httponly=True, samesite="strict")
        return response

    return app


def load_upload(path: Path, filename: str, settings: Settings) -> TableInfo:
    if path.suffix.lower() == ".xlsx":
        check_xlsx_content(path, filename, settings.max_xlsx_uncompressed, settings.max_xlsx_ratio)
    return read_table(path, filename, settings.max_columns)


def upload_error(
    request: Request, session: Session, is_new: bool, errors: list[str], status: int
) -> Response:
    response = render(request, "upload.html", session, {"errors": errors}, status)
    if is_new:
        set_session_cookie(response, session)
    return response
