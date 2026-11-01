"""Benchmark filematch on a synthetic 500,000-row x 10-column pair.

    python scripts/bench.py [--rows 500000] [--runs 3]

File A is a CSV, File B an XLSX, with about 1% of rows changed in one column.
Each step is timed separately, the whole sequence runs --runs times, and the
median per step is printed as a Markdown table along with the environment.
"""

from __future__ import annotations

import argparse
import os
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

import polars as pl
import xlsxwriter

from filematch.compare import CompareOptions, compare
from filematch.export import write_xlsx
from filematch.readers import read_table
from filematch.routes_results import PAGE_SIZE, page_rows

COLUMNS = ["id", "first_name", "last_name", "email", "city", "country", "zip", "phone", "dob",
           "amount"]  # fmt: skip


def synthetic(rows: int) -> tuple[pl.DataFrame, pl.DataFrame]:
    i = pl.int_range(rows, eager=True).alias("i")
    base = pl.DataFrame(i).select(
        pl.format("{}", pl.col("i") + 1).str.zfill(7).alias("id"),
        pl.format("First{}", pl.col("i") % 97).alias("first_name"),
        pl.format("Last{}", pl.col("i") % 89).alias("last_name"),
        pl.format("user{}@example.com", pl.col("i")).alias("email"),
        pl.format("City{}", pl.col("i") % 50).alias("city"),
        pl.format("C{}", pl.col("i") % 20).alias("country"),
        pl.format("{}", 10000 + pl.col("i") % 80000).alias("zip"),
        pl.format("555-01{}", pl.col("i") % 100).str.zfill(2).alias("phone"),
        pl.format(
            "19{}-0{}-1{}", 50 + pl.col("i") % 50, 1 + pl.col("i") % 9, pl.col("i") % 10
        ).alias("dob"),
        pl.format("{}.50", pl.col("i") % 5000).alias("amount"),
        pl.col("i"),
    )
    a = base.drop("i")
    # ~1% of rows get one changed cell, spread over three columns.
    changed = (pl.col("i") % 100) == 7
    b = base.with_columns(
        pl.when(changed & (pl.col("i") % 3 == 0))
        .then(pl.concat_str(pl.col("city"), pl.lit("x")))
        .otherwise(pl.col("city"))
        .alias("city"),
        pl.when(changed & (pl.col("i") % 3 == 1))
        .then(pl.lit("555-0199"))
        .otherwise(pl.col("phone"))
        .alias("phone"),
        pl.when(changed & (pl.col("i") % 3 == 2))
        .then(pl.lit("1.00"))
        .otherwise(pl.col("amount"))
        .alias("amount"),
    ).drop("i")
    return a, b


def write_inputs(directory: Path, rows: int) -> tuple[Path, Path]:
    a, b = synthetic(rows)
    path_a = directory / "a.csv"
    path_b = directory / "b.xlsx"
    a.write_csv(path_a)
    wb = xlsxwriter.Workbook(str(path_b), {"constant_memory": True, "strings_to_formulas": False})
    ws = wb.add_worksheet()
    for c, name in enumerate(b.columns):
        ws.write_string(0, c, name)
    for r, row in enumerate(b.iter_rows(), start=1):
        for c, value in enumerate(row):
            ws.write_string(r, c, value)
    wb.close()
    return path_a, path_b


def timed(fn):
    start = time.perf_counter()
    value = fn()
    return value, time.perf_counter() - start


def run_once(path_a: Path, path_b: Path, workdir: Path) -> dict[str, float]:
    times: dict[str, float] = {}
    table_a, times["Read A (CSV)"] = timed(lambda: read_table(path_a, "a.csv"))
    table_b, times["Read B (XLSX)"] = timed(lambda: read_table(path_b, "b.xlsx"))
    pairs = [(c, c) for c in COLUMNS[1:]]
    result, times["Compare + Parquet write"] = timed(
        lambda: compare(
            table_a.df,
            table_b.df,
            ["id"],
            ["id"],
            pairs,
            CompareOptions(),
            workdir / "result.parquet",
        )  # fmt: skip
    )
    rows, times["First results page (100 rows)"] = timed(lambda: page_rows(result, "all", 1))
    assert len(rows) == PAGE_SIZE
    _, times["XLSX export, tab (b) not matched"] = timed(
        lambda: write_xlsx(result, "issues", workdir / "issues.xlsx")
    )
    _, times["XLSX export, tab (a) all rows"] = timed(
        lambda: write_xlsx(result, "all", workdir / "all.xlsx")
    )
    times["_counts"] = result.counts  # type: ignore[assignment]
    return times


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=500_000)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="filematch-bench-") as tmp:
        workdir = Path(tmp)
        print(f"Generating {args.rows:,} x {len(COLUMNS)} synthetic pair...", flush=True)
        path_a, path_b = write_inputs(workdir, args.rows)
        sizes = (path_a.stat().st_size / 1e6, path_b.stat().st_size / 1e6)
        print(f"A.csv {sizes[0]:.1f} MB, B.xlsx {sizes[1]:.1f} MB", flush=True)
        runs = []
        for n in range(args.runs):
            times = run_once(path_a, path_b, workdir)
            counts = times.pop("_counts")
            runs.append(times)
            line = ", ".join(f"{k}: {v:.2f}s" for k, v in times.items())
            print(f"run {n + 1}: {line}", flush=True)

    print("\nCounts:", counts)
    print("\n| Step | Median of", args.runs, "runs (s) |")
    print("|---|---:|")
    for step in runs[0]:
        print(f"| {step} | {statistics.median(r[step] for r in runs):.2f} |")
    print(f"\nPython {platform.python_version()}, Polars {pl.__version__}, "
          f"{platform.system()} {platform.release()}, {os.cpu_count()} logical CPUs, "
          f"{platform.machine()}")  # fmt: skip
    print("Command:", " ".join([Path(sys.executable).name, *sys.argv]))


if __name__ == "__main__":
    main()
