"""Shared helpers for request handlers: sessions, cookies and template rendering."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from filematch import __version__
from filematch.config import Settings
from filematch.readers import DELIMITER_NAMES, TableInfo, table_stats
from filematch.sessions import COOKIE_NAME, Session, SessionStore

SLOTS = {"file_a": "File A (legacy)", "file_b": "File B (target)", "file_c": "File C (mapping)"}


def current_session(request: Request) -> Session | None:
    store: SessionStore = request.app.state.store
    return store.get(request.cookies.get(COOKIE_NAME))


def set_session_cookie(response: Response, session: Session) -> None:
    response.set_cookie(
        COOKIE_NAME,
        session.id,
        httponly=True,
        samesite="strict",
        path="/",
    )


def file_summaries(session: Session | None) -> list[dict[str, Any]]:
    if session is None:
        return []
    out = []
    for slot, label in SLOTS.items():
        table: TableInfo | None = session.files.get(slot)
        if table is None:
            continue
        if table.stats is None and slot != "file_c":
            table.stats = table_stats(table.df)
        out.append(
            {
                "slot": slot,
                "label": label,
                "name": table.original_name,
                "kind": table.kind,
                "encoding": table.encoding,
                "delimiter_name": DELIMITER_NAMES.get(table.delimiter or ""),
                "rows": table.df.height,
                "columns": table.df.width,
                "stats": table.stats if slot != "file_c" else None,
                "key": table.df.columns[0],
            }
        )
    return out


def render(
    request: Request,
    template: str,
    session: Session | None,
    context: dict[str, Any] | None = None,
    status_code: int = 200,
) -> Response:
    settings: Settings = request.app.state.settings
    templates: Jinja2Templates = request.app.state.templates
    ctx = {
        "session": session,
        "has_data": bool(session and session.files),
        "ready_for_mapping": bool(
            session and "file_a" in session.files and "file_b" in session.files
        ),
        "files": file_summaries(session),
        "slots": SLOTS,
        "max_upload_mb": settings.max_upload_mb,
        "version": __version__,
        "errors": [],
        "page": template.removesuffix(".html"),
    }
    ctx.update(context or {})
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)
