"""Read CSV/XLSX files into all-text Polars DataFrames and compute upload stats."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import polars as pl

DELIMITERS = [",", ";", "\t", "|"]
DELIMITER_NAMES = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}


class ReadError(ValueError):
    """A file could not be read; the message is shown to the user."""


@dataclass
class TableInfo:
    path: Path
    original_name: str
    kind: str  # "csv" or "xlsx"
    df: pl.DataFrame
    encoding: str | None = None
    delimiter: str | None = None
    stats: dict[str, int] | None = None

    @property
    def columns(self) -> list[str]:
        return self.df.columns


def decode_csv_bytes(raw: bytes) -> tuple[str, str]:
    """Decode CSV bytes as UTF-8 (BOM-aware), falling back to Windows-1252."""
    if b"\x00" in raw:
        raise ReadError("contains NUL bytes, so it does not look like a text CSV file")
    try:
        if raw.startswith(b"\xef\xbb\xbf"):
            return raw.decode("utf-8-sig"), "UTF-8 (BOM)"
        return raw.decode("utf-8"), "UTF-8"
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp1252"), "Windows-1252"
    except UnicodeDecodeError as exc:
        raise ReadError("is not valid UTF-8 or Windows-1252 text") from exc


def sniff_delimiter(text: str) -> str:
    sample = text[:65536]
    try:
        return csv.Sniffer().sniff(sample, delimiters="".join(DELIMITERS)).delimiter
    except csv.Error:
        pass
    header = sample.splitlines()[0] if sample else ""
    counts = {d: header.count(d) for d in DELIMITERS}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] > 0 else ","


def read_csv(path: Path, original_name: str, max_columns: int) -> TableInfo:
    raw = path.read_bytes()
    text, encoding = decode_csv_bytes(raw)
    if not text.strip():
        raise ReadError("is empty")
    delimiter = sniff_delimiter(text)
    _check_header_width(text, delimiter, max_columns)
    data = raw[3:] if encoding == "UTF-8 (BOM)" else raw
    if encoding == "Windows-1252":
        data = text.encode("utf-8")
    del text
    try:
        df = pl.read_csv(
            data,
            separator=delimiter,
            has_header=True,
            infer_schema=False,  # every column stays text; leading zeros survive
            empty_string_is_null=False,  # an empty CSV field is "", not null
            encoding="utf8",
        )
    except Exception as exc:  # polars raises several error types for malformed input
        raise ReadError(f"could not be parsed as CSV ({_short(exc)})") from exc
    return _finish(df, path, original_name, "csv", max_columns, encoding, delimiter)


def read_xlsx(path: Path, original_name: str, max_columns: int) -> TableInfo:
    try:
        df = pl.read_excel(
            path,
            engine="calamine",
            sheet_id=1,  # first sheet only
            has_header=True,
            read_options={"dtypes": "string"},  # every cell read as text
        )
    except Exception as exc:
        raise ReadError(f"could not be read as an Excel workbook ({_short(exc)})") from exc
    return _finish(df, path, original_name, "xlsx", max_columns)


def read_table(path: Path, original_name: str, max_columns: int = 500) -> TableInfo:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return read_csv(path, original_name, max_columns)
    if suffix == ".xlsx":
        return read_xlsx(path, original_name, max_columns)
    raise ReadError("must be a .csv or .xlsx file")


def _check_header_width(text: str, delimiter: str, max_columns: int) -> None:
    first_line = next(csv.reader([text.split("\n", 1)[0]], delimiter=delimiter), [])
    if len(first_line) > max_columns:
        raise ReadError(f"has {len(first_line)} columns; the limit is {max_columns}")


def _finish(
    df: pl.DataFrame,
    path: Path,
    original_name: str,
    kind: str,
    max_columns: int,
    encoding: str | None = None,
    delimiter: str | None = None,
) -> TableInfo:
    if df.width == 0:
        raise ReadError("has no columns")
    if df.width > max_columns:
        raise ReadError(f"has {df.width} columns; the limit is {max_columns}")
    df = df.with_columns(pl.all().cast(pl.String))
    return TableInfo(path, original_name, kind, df, encoding, delimiter)


def _short(exc: Exception) -> str:
    message = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return message[:200]


def key_expr(columns: list[str]) -> pl.Expr:
    """A single expression identifying a row's key (struct for composite keys)."""
    if len(columns) == 1:
        return pl.col(columns[0])
    return pl.struct([pl.col(c) for c in columns])


def table_stats(df: pl.DataFrame, key_columns: list[str] | None = None) -> dict[str, int]:
    key_columns = key_columns or df.columns[:1]
    empty_cells = 0
    if df.width and df.height:
        empty_cells = int(
            df.select(
                pl.sum_horizontal([(pl.col(c).is_null() | (pl.col(c) == "")) for c in df.columns])
                .sum()
                .alias("n")
            ).item()
        )
    counts = df.group_by(key_expr(key_columns).alias("_k")).len()
    dupes = counts.filter(pl.col("len") > 1)
    return {
        "rows": df.height,
        "columns": df.width,
        "empty_cells": empty_cells,
        "duplicate_keys": dupes.height,
        "duplicate_key_rows": int(dupes["len"].sum()) if dupes.height else 0,
    }
