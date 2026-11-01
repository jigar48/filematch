"""Compare run, paginated results tabs and downloads."""

from __future__ import annotations

import math

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from filematch.compare import CLASS_LABELS, CLASSES, CompareOptions, CompareResult, read_page
from filematch.export import ExportTooLarge, headers, status_label, write_csv, write_xlsx
from filematch.sessions import Session
from filematch.web import current_session, render

router = APIRouter()
PAGE_SIZE = 100
TABS = {"all": "All rows", "issues": "Not matched"}
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def run_compare(session: Session, config) -> CompareResult:
    from filematch.compare import compare

    a, b = session.files["file_a"], session.files["file_b"]
    options = CompareOptions(
        trim=config.options.get("trim", False),
        case_insensitive=config.options.get("case_insensitive", False),
        tolerance=config.options.get("tolerance", 0.0),
        empty_equals_null=config.options.get("empty_equals_null", False),
    )
    out = session.directory / "result.parquet"
    return compare(a.df, b.df, config.key_a, config.key_b, config.pairs, options, out)


def get_result(session: Session | None) -> CompareResult | None:
    return session.state.get("result") if session is not None else None


def page_rows(result: CompareResult, tab: str, page: int) -> list[dict]:
    frame = read_page(result, tab, page, PAGE_SIZE)
    n_keys, n_pairs = len(result.key_a), len(result.pairs)
    rows = []
    for row in frame.iter_rows(named=True):
        cells = []
        for i in range(n_pairs):
            mismatch = bool(row[f"m{i}"])
            cells.append({"value": row[f"a{i}"], "mismatch": mismatch})
            cells.append({"value": row[f"b{i}"], "mismatch": mismatch})
        rows.append(
            {
                "status": row["status"],
                "label": status_label(row["status"], row["dup_side"]),
                "key_values": [row[f"k{i}"] for i in range(n_keys)],
                "cells": cells,
            }
        )
    return rows


def parse_page(raw: str | None) -> int:
    try:
        return max(1, int(raw or "1"))
    except ValueError:
        return 1


@router.get("/results")
def results_page(request: Request, tab: str = "all", page: str | None = None) -> Response:
    session = current_session(request)
    result = get_result(session)
    if result is None:
        return RedirectResponse("/mapping" if session else "/", status_code=303)
    return render_results(request, session, result, tab, parse_page(page))


def render_results(
    request: Request,
    session: Session | None,
    result: CompareResult,
    tab: str,
    page: int,
    errors: list[str] | None = None,
    status_code: int = 200,
    offer_csv: str | None = None,
) -> Response:
    tab = tab if tab in TABS else "all"
    total = result.issues if tab == "issues" else result.total
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(page, pages)
    context = {
        "tab": tab,
        "tabs": TABS,
        "page_no": page,
        "pages": pages,
        "total": total,
        "rows": page_rows(result, tab, page) if total else [],
        "headers": headers(result),
        "key_labels": result.key_labels,
        "pairs": result.pairs,
        "counts": [(c, CLASS_LABELS[c], result.counts.get(c, 0)) for c in CLASSES],
        "options": result.options,
        "errors": errors or [],
        "offer_csv": offer_csv,
    }
    return render(request, "results.html", session, context, status_code)


@router.get("/download/{tab}.xlsx")
async def download_xlsx(request: Request, tab: str) -> Response:
    session = current_session(request)
    result = get_result(session)
    if session is None or result is None or tab not in TABS:
        return RedirectResponse("/", status_code=303)
    out = session.directory / f"filematch-{tab}.xlsx"
    try:
        await run_in_threadpool(write_xlsx, result, tab, out)
    except ExportTooLarge as exc:
        return render_results(request, session, result, tab, 1, [str(exc)], 413, offer_csv=tab)
    return FileResponse(out, media_type=XLSX_TYPE, filename=f"filematch-{tab}.xlsx")


@router.get("/download/{tab}.csv")
async def download_csv(request: Request, tab: str) -> Response:
    session = current_session(request)
    result = get_result(session)
    if session is None or result is None or tab not in TABS:
        return RedirectResponse("/", status_code=303)
    out = session.directory / f"filematch-{tab}.csv"
    await run_in_threadpool(write_csv, result, tab, out)
    return FileResponse(out, media_type="text/csv; charset=utf-8", filename=f"filematch-{tab}.csv")
