"""Column matching: normalisation, synonyms, fuzzy one-to-one assignment, mapping files."""

from __future__ import annotations

import re
from dataclasses import dataclass

import polars as pl
from rapidfuzz import fuzz, process

FUZZY_THRESHOLD = 60
SYNONYM_SCORE = 95

# Small on purpose: common legacy-vs-target names for customer data.
SYNONYM_GROUPS: list[list[str]] = [
    ["email", "e-mail", "email_address"],
    ["first_name", "fname", "firstname", "given_name"],
    ["last_name", "lname", "lastname", "surname"],
    ["phone", "mobile", "telephone", "telephone_number"],
    ["zip", "postcode", "postal_code"],
    ["dob", "date_of_birth", "birthdate"],
    ["address", "street", "street_address"],
    ["city", "town"],
    ["country", "country_id", "country_code"],
    ["id", "customer_id", "entity_id", "cust_id"],
]

# Lower number wins when two suggestions compete for the same column.
SOURCE_RANK = {"mapping file": 0, "exact": 1, "synonym": 2, "AI": 3, "fuzzy": 4}

_CAMEL_1 = re.compile(r"([a-z0-9])([A-Z])")
_CAMEL_2 = re.compile(r"([A-Z]+)([A-Z][a-z])")
_SEPARATORS = re.compile(r"[\s_\-.]+")


def normalise(header: str) -> str:
    """Lowercase, trim, split camelCase, treat _ - . and whitespace as one separator."""
    text = _CAMEL_2.sub(r"\1 \2", _CAMEL_1.sub(r"\1 \2", header.strip()))
    return _SEPARATORS.sub(" ", text.lower()).strip()


def compact(header: str) -> str:
    return normalise(header).replace(" ", "")


_SYNONYM_INDEX = {compact(name): i for i, group in enumerate(SYNONYM_GROUPS) for name in group}


def synonym_group(header: str) -> int | None:
    return _SYNONYM_INDEX.get(compact(header))


@dataclass
class Suggestion:
    a: str
    b: str | None
    score: int
    source: str | None


@dataclass
class Candidate:
    a: str
    b: str
    score: int
    source: str


def candidates(columns_a: list[str], columns_b: list[str]) -> list[Candidate]:
    """Every exact, synonym and fuzzy (>= 60) pair between the two header lists."""
    out: list[Candidate] = []
    if not columns_a or not columns_b:
        return out
    norm_a = [normalise(c) for c in columns_a]
    norm_b = [normalise(c) for c in columns_b]
    scores = process.cdist(norm_a, norm_b, scorer=fuzz.token_sort_ratio)
    for i, a in enumerate(columns_a):
        for j, b in enumerate(columns_b):
            if norm_a[i] == norm_b[j] or compact(a) == compact(b):
                out.append(Candidate(a, b, 100, "exact"))
                continue
            group_a, group_b = synonym_group(a), synonym_group(b)
            if group_a is not None and group_a == group_b:
                out.append(Candidate(a, b, SYNONYM_SCORE, "synonym"))
                continue
            score = round(float(scores[i][j]))
            if score >= FUZZY_THRESHOLD:
                out.append(Candidate(a, b, min(score, 99), "fuzzy"))
    return out


def assign(columns_a: list[str], pool: list[Candidate]) -> list[Suggestion]:
    """Greedy one-to-one assignment: best source first, then highest score."""
    order = {a: i for i, a in enumerate(columns_a)}
    ranked = sorted(pool, key=lambda c: (SOURCE_RANK[c.source], -c.score, order.get(c.a, 0), c.b))
    chosen: dict[str, Candidate] = {}
    used_b: set[str] = set()
    for cand in ranked:
        if cand.a in chosen or cand.b in used_b or cand.a not in order:
            continue
        chosen[cand.a] = cand
        used_b.add(cand.b)
    return [
        Suggestion(a, chosen[a].b, chosen[a].score, chosen[a].source)
        if a in chosen
        else Suggestion(a, None, 0, None)
        for a in columns_a
    ]


def suggest(
    columns_a: list[str],
    columns_b: list[str],
    file_pairs: list[tuple[str, str]] | None = None,
    ai_pairs: list[Candidate] | None = None,
) -> list[Suggestion]:
    pool = candidates(columns_a, columns_b)
    pool += [Candidate(a, b, 100, "mapping file") for a, b in file_pairs or []]
    pool += ai_pairs or []
    return assign(columns_a, pool)


@dataclass
class MappingFileResult:
    pairs: list[tuple[str, str]]
    errors: list[str]


def _resolve(name: str, columns: list[str]) -> str | None:
    if name in columns:
        return name
    folded = [c for c in columns if c.strip().casefold() == name.casefold()]
    return folded[0] if len(folded) == 1 else None


def import_mapping_file(
    df: pl.DataFrame, file_name: str, columns_a: list[str], columns_b: list[str]
) -> MappingFileResult:
    """Read (source A, target B) pairs from the first two columns of a mapping file."""
    if df.width < 2:
        return MappingFileResult(
            [], [f"Mapping file {file_name}: needs at least two columns (A column, B column)."]
        )
    pairs: list[tuple[str, str]] = []
    bad: list[str] = []
    seen_a: set[str] = set()
    seen_b: set[str] = set()
    for offset, (raw_a, raw_b) in enumerate(df.select(df.columns[:2]).iter_rows()):
        row = offset + 2  # row 1 is the header
        src = (raw_a or "").strip()
        dst = (raw_b or "").strip()
        if not src and not dst:
            continue
        if not src or not dst:
            bad.append(f"row {row}: both a File A and a File B column name are required")
            continue
        a = _resolve(src, columns_a)
        b = _resolve(dst, columns_b)
        problems = []
        if a is None:
            problems.append(f"'{src}' is not a column in File A")
        if b is None:
            problems.append(f"'{dst}' is not a column in File B")
        if problems:
            bad.append(f"row {row}: " + " and ".join(problems))
            continue
        if a in seen_a or b in seen_b:
            bad.append(f"row {row}: '{src}' or '{dst}' is already mapped by an earlier row")
            continue
        seen_a.add(a)
        seen_b.add(b)
        pairs.append((a, b))
    errors = []
    if bad:
        errors.append(
            f"Mapping file {file_name}: {len(bad)} row(s) were skipped. "
            + "; ".join(bad[:20])
            + (f"; and {len(bad) - 20} more" if len(bad) > 20 else "")
        )
    return MappingFileResult(pairs, errors)
