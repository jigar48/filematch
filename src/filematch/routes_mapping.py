"""Mapping screen: column suggestions, manual edits, key choice and AI suggestions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from filematch import ai
from filematch.mapping import Candidate, Suggestion, import_mapping_file, suggest
from filematch.readers import TableInfo
from filematch.sessions import Session
from filematch.web import current_session, render

router = APIRouter()
MAX_KEY_COLUMNS = 4
DEFAULT_OPTIONS: dict[str, Any] = {
    "trim": False,
    "case_insensitive": False,
    "empty_equals_null": False,
    "tolerance": 0.0,
}


@dataclass
class MappingConfig:
    pairs: list[tuple[str, str]]
    key_a: list[str]
    key_b: list[str]
    options: dict[str, Any] = field(default_factory=dict)


def tables(session: Session | None) -> tuple[TableInfo, TableInfo] | None:
    if session is None:
        return None
    a, b = session.files.get("file_a"), session.files.get("file_b")
    return (a, b) if a is not None and b is not None else None


def mapping_file_state(session: Session, a: TableInfo, b: TableInfo) -> tuple[list, list[str]]:
    if "file_pairs" not in session.state:
        c: TableInfo | None = session.files.get("file_c")
        if c is None:
            session.state["file_pairs"], session.state["file_errors"] = [], []
        else:
            result = import_mapping_file(c.df, c.original_name, a.columns, b.columns)
            session.state["file_pairs"], session.state["file_errors"] = result.pairs, result.errors
    return session.state["file_pairs"], session.state["file_errors"]


def current_suggestions(session: Session, a: TableInfo, b: TableInfo) -> list[Suggestion]:
    file_pairs, _ = mapping_file_state(session, a, b)
    ai_pairs: list[Candidate] = session.state.get("ai_pairs") or []
    return suggest(a.columns, b.columns, file_pairs, ai_pairs)


def default_selection(session: Session, a: TableInfo, b: TableInfo) -> dict[str, Any]:
    config: MappingConfig | None = session.state.get("config")
    if config is not None and not session.state.get("ai_newer_than_config"):
        chosen = dict(config.pairs)
        return {
            "map": [chosen.get(col) for col in a.columns],
            "key_a": config.key_a,
            "key_b": config.key_b,
            "options": config.options,
        }
    suggestions = current_suggestions(session, a, b)
    return {
        "map": [s.b for s in suggestions],
        "key_a": a.columns[:1],
        "key_b": b.columns[:1],
        "options": dict(DEFAULT_OPTIONS),
    }


def first_value(table: TableInfo, column: str) -> str:
    series = table.df[column].drop_nulls()
    for value in series.head(50):
        if value.strip():
            return value[:40]
    return ""


def render_mapping(
    request: Request,
    session: Session,
    selection: dict[str, Any] | None = None,
    errors: list[str] | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> Response:
    a, b = tables(session)  # type: ignore[misc]
    _, file_errors = mapping_file_state(session, a, b)
    suggestions = current_suggestions(session, a, b)
    selection = selection or default_selection(session, a, b)
    rows = [
        {
            "index": i,
            "column": s.a,
            "sample": first_value(a, s.a),
            "suggested": s.b,
            "selected": selection["map"][i],
            "score": s.score,
            "source": s.source,
        }
        for i, s in enumerate(suggestions)
    ]
    keys = [
        {
            "n": n,
            "a": selection["key_a"][n] if n < len(selection["key_a"]) else None,
            "b": selection["key_b"][n] if n < len(selection["key_b"]) else None,
        }
        for n in range(MAX_KEY_COLUMNS)
    ]
    context = {
        "rows": rows,
        "keys": keys,
        "columns_a": a.columns,
        "columns_b": b.columns,
        "options": selection["options"],
        "errors": (errors or []) + file_errors,
        "notice": notice or session.state.pop("notice", None),
        "ai_available": ai.available(),
        "ai_message": session.state.pop("ai_message", None),
        "file_a": a.original_name,
        "file_b": b.original_name,
        "has_mapping_file": "file_c" in session.files,
    }
    return render(request, "mapping.html", session, context, status_code)


def parse_selection(form, a: TableInfo, b: TableInfo) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    mapped: list[str | None] = []
    for i in range(len(a.columns)):
        value = form.get(f"map_{i}", "")
        if value == "":
            mapped.append(None)
        elif value.isdigit() and int(value) < len(b.columns):
            mapped.append(b.columns[int(value)])
        else:
            errors.append(f"Unknown File B column for '{a.columns[i]}'.")
            mapped.append(None)
    seen: dict[str, str] = {}
    for col_a, col_b in zip(a.columns, mapped, strict=True):
        if col_b is None:
            continue
        if col_b in seen:
            errors.append(
                f"File B column '{col_b}' is mapped twice ('{seen[col_b]}' and '{col_a}'). "
                "Each column can be used once."
            )
        seen[col_b] = col_a

    key_a: list[str] = []
    key_b: list[str] = []
    for n in range(MAX_KEY_COLUMNS):
        va, vb = form.get(f"key_a_{n}", ""), form.get(f"key_b_{n}", "")
        if va == "" and vb == "":
            continue
        if va == "" or vb == "":
            errors.append(f"Key column {n + 1}: choose a column on both sides, or neither.")
            continue
        if not (va.isdigit() and int(va) < len(a.columns)):
            errors.append(f"Key column {n + 1}: unknown File A column.")
            continue
        if not (vb.isdigit() and int(vb) < len(b.columns)):
            errors.append(f"Key column {n + 1}: unknown File B column.")
            continue
        key_a.append(a.columns[int(va)])
        key_b.append(b.columns[int(vb)])
    if not key_a:
        errors.append("Choose at least one key column pair.")
    if len(set(key_a)) != len(key_a) or len(set(key_b)) != len(key_b):
        errors.append("A key column can only be used once per side.")

    selection = {"map": mapped, "key_a": key_a, "key_b": key_b, "options": parse_options(form)}
    if selection["options"].get("tolerance_error"):
        errors.append(selection["options"].pop("tolerance_error"))
    return selection, errors


def parse_options(form) -> dict[str, Any]:
    options: dict[str, Any] = {
        "trim": form.get("trim") == "on",
        "case_insensitive": form.get("case_insensitive") == "on",
        "empty_equals_null": form.get("empty_equals_null") == "on",
        "tolerance": 0.0,
    }
    raw = (form.get("tolerance") or "0").strip()
    try:
        tolerance = float(raw)
        if tolerance < 0 or tolerance != tolerance or tolerance == float("inf"):
            raise ValueError
        options["tolerance"] = tolerance
    except ValueError:
        options["tolerance_error"] = "Numeric tolerance must be a number of 0 or more."
    return options


def config_from_selection(selection: dict[str, Any], a: TableInfo) -> MappingConfig:
    key_pairs = set(zip(selection["key_a"], selection["key_b"], strict=True))
    pairs = [
        (col_a, col_b)
        for col_a, col_b in zip(a.columns, selection["map"], strict=True)
        if col_b is not None and (col_a, col_b) not in key_pairs
    ]
    return MappingConfig(pairs, selection["key_a"], selection["key_b"], selection["options"])


@router.get("/mapping")
def mapping_page(request: Request) -> Response:
    session = current_session(request)
    if tables(session) is None:
        return RedirectResponse("/", status_code=303)
    with session.lock:  # type: ignore[union-attr]
        return render_mapping(request, session)  # type: ignore[arg-type]


@router.post("/mapping")
async def mapping_submit(request: Request) -> Response:
    session = current_session(request)
    pair = tables(session)
    if session is None or pair is None:
        return RedirectResponse("/", status_code=303)
    a, b = pair
    form = await request.form()
    selection, errors = parse_selection(form, a, b)
    if errors:
        return render_mapping(request, session, selection, errors, status_code=400)
    config = config_from_selection(selection, a)
    if not config.pairs:
        return render_mapping(
            request, session, selection, ["Map at least one non-key column."], status_code=400
        )
    with session.lock:
        session.state["config"] = config
        session.state.pop("ai_newer_than_config", None)
    return await after_confirm(request, session, config)


async def after_confirm(request: Request, session: Session, config: MappingConfig) -> Response:
    from filematch.routes_results import run_compare

    result = await run_in_threadpool(run_compare, session, config)
    with session.lock:
        session.state["result"] = result
    return RedirectResponse("/results", status_code=303)


@router.get("/mapping/ai")
def ai_preview(request: Request) -> Response:
    session = current_session(request)
    pair = tables(session)
    if session is None or pair is None:
        return RedirectResponse("/", status_code=303)
    if not ai.available():
        session.state["ai_message"] = "AI suggestions are not configured on this server."
        return RedirectResponse("/mapping", status_code=303)
    a, b = pair
    payload = ai.build_payload(a.df, b.df)
    context = {
        "request_text": ai.request_text(payload),
        "model": request.app.state.settings.ai_model,
        "column_count": len(a.columns) + len(b.columns),
    }
    return render(request, "ai_preview.html", session, context)


@router.post("/mapping/ai")
async def ai_run(request: Request) -> Response:
    session = current_session(request)
    pair = tables(session)
    if session is None or pair is None:
        return RedirectResponse("/", status_code=303)
    if not ai.available():
        session.state["ai_message"] = "AI suggestions are not configured on this server."
        return RedirectResponse("/mapping", status_code=303)
    a, b = pair
    payload = ai.build_payload(a.df, b.df)
    model = request.app.state.settings.ai_model
    try:
        pairs = await run_in_threadpool(ai.suggest_pairs, payload, model, a.columns, b.columns)
    except ai.AIError as exc:
        session.state["ai_message"] = f"{exc} Showing the automatic suggestions instead."
        return RedirectResponse("/mapping", status_code=303)
    with session.lock:
        session.state["ai_pairs"] = pairs
        session.state["ai_newer_than_config"] = True
        session.state["ai_message"] = (
            f"AI suggested {len(pairs)} pair(s). Exact, synonym and mapping-file matches "
            "still take precedence. Review every row before you continue."
        )
    return RedirectResponse("/mapping", status_code=303)
