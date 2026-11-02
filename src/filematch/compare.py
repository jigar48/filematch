"""Key-based comparison of two all-text DataFrames, written once to Parquet."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

CLASSES = ["matched", "mismatched", "only_in_a", "only_in_b", "duplicate_key"]
CLASS_LABELS = {
    "matched": "matched",
    "mismatched": "mismatched",
    "only_in_a": "only in A",
    "only_in_b": "only in B",
    "duplicate_key": "duplicate key",
}
# Relative slack so 1.00 vs 1.01 at tolerance 0.01 is not lost to float rounding.
FLOAT_EPSILON = 1e-9


@dataclass
class CompareOptions:
    trim: bool = False
    case_insensitive: bool = False
    tolerance: float = 0.0
    empty_equals_null: bool = False


@dataclass
class CompareResult:
    path: Path
    key_a: list[str]
    key_b: list[str]
    pairs: list[tuple[str, str]]
    options: CompareOptions
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def issues(self) -> int:
        return self.total - self.counts.get("matched", 0)

    @property
    def key_labels(self) -> list[str]:
        return [a if a == b else f"{a} / {b}" for a, b in zip(self.key_a, self.key_b, strict=True)]


def _clean(expr: pl.Expr, options: CompareOptions) -> pl.Expr:
    if options.trim:
        expr = expr.str.strip_chars()
    if options.case_insensitive:
        expr = expr.str.to_lowercase()
    return expr


def _values_equal(a: pl.Expr, b: pl.Expr, options: CompareOptions) -> pl.Expr:
    a, b = _clean(a, options), _clean(b, options)
    if options.empty_equals_null:
        a = pl.when(a == "").then(None).otherwise(a)
        b = pl.when(b == "").then(None).otherwise(b)
    na = a.cast(pl.Float64, strict=False)
    nb = b.cast(pl.Float64, strict=False)
    both_numeric = na.is_not_null() & nb.is_not_null() & na.is_finite() & nb.is_finite()
    slack = pl.max_horizontal(pl.lit(1.0), na.abs(), nb.abs()) * FLOAT_EPSILON
    numeric_equal = (na - nb).abs() <= (options.tolerance + slack)
    text_equal = pl.when(a.is_null() & b.is_null()).then(True).otherwise(a.eq_missing(b))
    return pl.when(both_numeric).then(numeric_equal).otherwise(text_equal).fill_null(False)


def _side(
    df: pl.DataFrame, keys: list[str], cols: list[str], prefix: str, options: CompareOptions
) -> pl.DataFrame:
    exprs = [pl.int_range(pl.len(), dtype=pl.UInt32).alias("_row")]
    exprs += [pl.col(k).alias(f"k{prefix}{i}") for i, k in enumerate(keys)]
    exprs += [_clean(pl.col(k), options).alias(f"j{i}") for i, k in enumerate(keys)]
    exprs += [pl.col(c).alias(f"{prefix}{i}") for i, c in enumerate(cols)]
    out = df.select(exprs)
    join_cols = [f"j{i}" for i in range(len(keys))]
    return out.with_columns((pl.len().over(join_cols) > 1).alias("_dup"))


def compare(
    df_a: pl.DataFrame,
    df_b: pl.DataFrame,
    key_a: list[str],
    key_b: list[str],
    pairs: list[tuple[str, str]],
    options: CompareOptions,
    out_path: Path,
) -> CompareResult:
    if not key_a or len(key_a) != len(key_b):
        raise ValueError("key columns must be paired one to one")
    n_keys, n_pairs = len(key_a), len(pairs)
    a = _side(df_a, key_a, [p[0] for p in pairs], "a", options)
    b = _side(df_b, key_b, [p[1] for p in pairs], "b", options)
    join_cols = [f"j{i}" for i in range(n_keys)]

    # A key is ambiguous if it repeats in A or in B. Every row with an ambiguous
    # key, in either file, is a duplicate and stays out of the join.
    ambiguous = (
        pl.concat(
            [a.filter(pl.col("_dup")).select(join_cols), b.filter(pl.col("_dup")).select(join_cols)]
        )
        .unique()
        .with_columns(pl.lit(True).alias("_amb"))
    )

    def mark(side: pl.DataFrame) -> pl.DataFrame:
        return (
            side.join(ambiguous, on=join_cols, how="left", nulls_equal=True, maintain_order="left")
            .with_columns(pl.col("_amb").fill_null(False).alias("_dup"))
            .drop("_amb")
        )

    a, b = mark(a), mark(b)

    joined = (
        a.filter(~pl.col("_dup"))
        .with_columns(pl.lit(True).alias("_in_a"))
        .join(
            b.filter(~pl.col("_dup"))
            .with_columns(pl.lit(True).alias("_in_b"))
            .rename({"_row": "_row_b"}),
            on=join_cols,
            how="full",
            coalesce=True,
            nulls_equal=True,
        )
        .with_columns(pl.col("_in_a").fill_null(False), pl.col("_in_b").fill_null(False))
    )
    both = pl.col("_in_a") & pl.col("_in_b")
    flags = [
        (both & ~_values_equal(pl.col(f"a{i}"), pl.col(f"b{i}"), options)).alias(f"m{i}")
        for i in range(n_pairs)
    ]
    joined = joined.with_columns(flags)
    any_mismatch = (
        pl.any_horizontal([pl.col(f"m{i}") for i in range(n_pairs)]) if n_pairs else pl.lit(False)
    )
    joined = joined.with_columns(
        pl.when(both & any_mismatch)
        .then(pl.lit("mismatched"))
        .when(both)
        .then(pl.lit("matched"))
        .when(pl.col("_in_a"))
        .then(pl.lit("only_in_a"))
        .otherwise(pl.lit("only_in_b"))
        .alias("status"),
        pl.lit(None, dtype=pl.String).alias("dup_side"),
        pl.when(pl.col("_in_a")).then(0).otherwise(1).alias("_group"),
        pl.coalesce("_row", "_row_b").alias("_order"),
        *[pl.coalesce(f"ka{i}", f"kb{i}").alias(f"k{i}") for i in range(n_keys)],
    )

    def duplicates(side: pl.DataFrame, prefix: str, group: int) -> pl.DataFrame:
        return side.filter(pl.col("_dup")).select(
            pl.lit("duplicate_key").alias("status"),
            pl.lit(prefix.upper()).alias("dup_side"),
            pl.lit(group).alias("_group"),
            pl.col("_row").alias("_order"),
            *[pl.col(f"k{prefix}{i}").alias(f"k{i}") for i in range(n_keys)],
            *[
                pl.col(f"{prefix}{i}") if p == prefix else pl.lit(None, pl.String).alias(f"{p}{i}")
                for i in range(n_pairs)
                for p in ("a", "b")
            ],
            *[pl.lit(False).alias(f"m{i}") for i in range(n_pairs)],
        )

    columns = ["status", "dup_side", "_group", "_order"]
    columns += [f"k{i}" for i in range(n_keys)]
    columns += [f"{p}{i}" for i in range(n_pairs) for p in ("a", "b")]
    columns += [f"m{i}" for i in range(n_pairs)]
    result = pl.concat(
        [
            joined.select(columns),
            duplicates(a, "a", 2).select(columns),
            duplicates(b, "b", 3).select(columns),
        ],
        how="vertical_relaxed",
    )
    result = (
        result.with_columns(pl.col("_group").cast(pl.Int8), pl.col("_order").cast(pl.UInt32))
        .sort(["_group", "_order"])
        .drop(["_group", "_order"])
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result.write_parquet(out_path)
    counts = dict.fromkeys(CLASSES, 0)
    for status, n in result.group_by("status").len().iter_rows():
        counts[status] = n
    return CompareResult(out_path, key_a, key_b, pairs, options, counts)


def issues_filter() -> pl.Expr:
    return pl.col("status") != "matched"


def read_page(result: CompareResult, tab: str, page: int, page_size: int = 100) -> pl.DataFrame:
    lf = pl.scan_parquet(result.path)
    if tab == "issues":
        lf = lf.filter(issues_filter())
    return lf.slice((page - 1) * page_size, page_size).collect()
