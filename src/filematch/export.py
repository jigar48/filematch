"""Excel and CSV exports of the comparison result."""

from __future__ import annotations

import csv
import re
from collections.abc import Iterator
from pathlib import Path

import polars as pl
import xlsxwriter

from filematch.compare import CLASS_LABELS, CompareResult, issues_filter

# Excel allows 1,048,576 rows per sheet; one is the header row.
EXCEL_MAX_ROWS = 1_048_576
BATCH_ROWS = 50_000
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


class ExportTooLarge(ValueError):
    pass


def headers(result: CompareResult) -> list[str]:
    out = ["Status", *result.key_labels]
    for a, b in result.pairs:
        out += [f"A: {a}", f"B: {b}"]
    return out


def status_label(status: str, dup_side: str | None) -> str:
    label = CLASS_LABELS.get(status, status)
    return f"{label} ({dup_side})" if dup_side else label


def tab_frame(result: CompareResult, tab: str) -> pl.LazyFrame:
    lf = pl.scan_parquet(result.path)
    return lf.filter(issues_filter()) if tab == "issues" else lf


def tab_rows(result: CompareResult, tab: str) -> int:
    return result.issues if tab == "issues" else result.total


def iter_rows(result: CompareResult, tab: str) -> Iterator[tuple[list, list[bool]]]:
    """Yield (cells, mismatch flags per cell) in display order, in batches."""
    n_keys, n_pairs = len(result.key_a), len(result.pairs)
    total = tab_rows(result, tab)
    for offset in range(0, total, BATCH_ROWS):
        batch = tab_frame(result, tab).slice(offset, BATCH_ROWS).collect()
        for row in batch.iter_rows(named=True):
            cells = [status_label(row["status"], row["dup_side"])]
            flags = [False]
            cells += [row[f"k{i}"] for i in range(n_keys)]
            flags += [False] * n_keys
            for i in range(n_pairs):
                mismatch = bool(row[f"m{i}"])
                cells += [row[f"a{i}"], row[f"b{i}"]]
                flags += [mismatch, mismatch]
            yield cells, flags


def write_xlsx(result: CompareResult, tab: str, out_path: Path) -> Path:
    rows = tab_rows(result, tab)
    if rows + 1 > EXCEL_MAX_ROWS:
        raise ExportTooLarge(
            f"This tab has {rows:,} rows, more than Excel's limit of "
            f"{EXCEL_MAX_ROWS:,} rows per sheet (including the header). "
            "Download the CSV instead."
        )
    names = headers(result)
    workbook = xlsxwriter.Workbook(
        str(out_path), {"constant_memory": True, "strings_to_formulas": False}
    )
    try:
        sheet = workbook.add_worksheet("Issues" if tab == "issues" else "All rows")
        bold = workbook.add_format({"bold": True, "bg_color": "#EFEFEC", "border": 1})
        red = workbook.add_format({"bg_color": "#F4C7C3", "font_color": "#9C0006"})
        for col, name in enumerate(names):
            sheet.write_string(0, col, name, bold)
        sheet.freeze_panes(1, 0)
        sheet.autofilter(0, 0, max(rows, 1), len(names) - 1)
        for r, (cells, flags) in enumerate(iter_rows(result, tab), start=1):
            for c, (value, flagged) in enumerate(zip(cells, flags, strict=True)):
                fmt = red if flagged else None
                if value is None:
                    if flagged:
                        sheet.write_blank(r, c, None, fmt)
                    continue
                # Source values are text; keep them text so "007" stays "007".
                sheet.write_string(r, c, value, fmt)
        sheet.set_column(0, 0, 18)
        sheet.set_column(1, len(names) - 1, 16)
    finally:
        workbook.close()
    return out_path


def escape_csv_cell(value: str | None) -> str:
    """Neutralise spreadsheet formulas: prefix ' unless the value is a plain number."""
    if value is None:
        return ""
    if value.startswith(FORMULA_PREFIXES) and not _NUMBER.match(value):
        return "'" + value
    return value


def write_csv(result: CompareResult, tab: str, out_path: Path) -> Path:
    # utf-8-sig: still UTF-8, with a BOM so Excel detects the encoding on open.
    with out_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([escape_csv_cell(h) for h in headers(result)])
        for cells, _ in iter_rows(result, tab):
            writer.writerow([escape_csv_cell(v) for v in cells])
    return out_path
