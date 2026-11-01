"""Generate the fake demo dataset in demo/ (seeded, deterministic, no Faker).

    python scripts/make_demo.py

Writes legacy_customers.csv (File A), target_customers.xlsx (File B), mapping.csv
(File C) and expected.json with the exact planted counts. All people are made up;
emails use example.com / example.org and phone numbers use the 555-01xx range.
"""

from __future__ import annotations

import csv
import json
import random
from datetime import date, timedelta
from pathlib import Path

import xlsxwriter

SEED = 20261101
OUT = Path(__file__).resolve().parent.parent / "demo"

FIRST = [
    "Aarav", "Maya", "Liam", "Sofia", "Noah", "Isha", "Ethan", "Zara", "Lucas", "Anya",
    "Omar", "Elena", "Kai", "Priya", "Leo", "Mila", "Arjun", "Nora", "Ravi", "Chloe",
    "Felix", "Hana", "Ivan", "Jade", "Theo", "Lina", "Marco", "Tara", "Victor", "Wren",
]  # fmt: skip
LAST = [
    "Shah", "Patel", "Smith", "Garcia", "Kumar", "Nguyen", "Brown", "Rossi", "Khan", "Silva",
    "Mehta", "Jones", "Novak", "Haddad", "Ito", "Costa", "Weber", "Joshi", "Ali", "Moreau",
    "Fischer", "Dubois", "Kowalski", "Lopez", "Sato", "Reddy", "Olsen", "Murphy", "Rao", "Kim",
]  # fmt: skip
CITIES = [
    ("Ahmedabad", "IN", "380001"), ("Pune", "IN", "411001"), ("Austin", "US", "73301"),
    ("Boston", "US", "02108"), ("Leeds", "GB", "LS1 1UR"), ("Bristol", "GB", "BS1 4DJ"),
    ("Lyon", "FR", "69001"), ("Porto", "PT", "4000-322"), ("Hamburg", "DE", "20095"),
    ("Toronto", "CA", "M5H 2N2"), ("Perth", "AU", "6000"), ("Dublin", "IE", "D02 X285"),
]  # fmt: skip
CREDIT = ["500", "1000", "1500", "2000", "2500", "5000"]

A_HEADERS = [
    "CustID", "FName", "LName", "E-Mail", "Mobile", "Zip", "City", "Country", "DOB",
    "Credit_Limit",
]  # fmt: skip
B_HEADERS = [
    "entity_id", "firstname", "lastname", "email", "telephone", "postcode", "city",
    "country_id", "dob", "credit_limit",
]  # fmt: skip

N_COMMON, N_ONLY_A, N_ONLY_B, N_DUP_A, N_DUP_B = 1965, 25, 15, 5, 3

# Planted value differences between A and B, one per row, on common keys.
PLANT = {
    "whitespace": 8,  # cleared by "trim whitespace"
    "case": 8,  # cleared by "case-insensitive"
    "numeric_within": 6,  # credit differs by 0.01: cleared by tolerance 0.01
    "numeric_outside": 4,  # credit differs by more than 0.01
    "value": 14,  # real differences
}
N_EMPTY_VS_NULL = 6  # A has "" where B has an empty cell: cleared by "empty = null"


def person(rng: random.Random, key: int) -> dict[str, str]:
    first, last = rng.choice(FIRST), rng.choice(LAST)
    city, country, zipcode = rng.choice(CITIES)
    domain = rng.choice(["example.com", "example.org"])
    born = date(1950, 1, 1) + timedelta(days=rng.randrange(0, 365 * 54))
    return {
        "id": f"{key:06d}",
        "first": first,
        "last": last,
        "email": f"{first}.{last}{key}@{domain}".lower(),
        "phone": f"555-01{rng.randrange(100):02d}",
        "zip": zipcode,
        "city": city,
        "country": country,
        "dob": born.isoformat(),
        "credit": rng.choice(CREDIT),
    }


def a_row(p: dict[str, str]) -> list[str]:
    credit = p["credit"] if "." in p["credit"] else p["credit"] + ".00"
    return [p["id"], p["first"], p["last"], p["email"], p["phone"], p["zip"], p["city"],
            p["country"], p["dob"], credit]  # fmt: skip


def b_row(p: dict[str, str]) -> list[object]:
    return [p["id"], p["first"], p["last"], p["email"], p["phone"], p["zip"], p["city"],
            p["country"], p["dob"], float(p["credit"])]  # fmt: skip


def other(rng: random.Random, options: list[str], current: str) -> str:
    return rng.choice([o for o in options if o != current])


def plant(rng: random.Random, kind: str, a: dict, b: dict) -> None:
    """Change one cell so A and B differ in the given way."""
    if kind == "whitespace":
        field = rng.choice(["first", "last", "city"])
        a[field] = rng.choice([" ", "  "]) + a[field] + rng.choice(["", " "])
    elif kind == "case":
        field = rng.choice(["email", "city", "last"])
        a[field] = a[field].upper() if field != "email" else a[field].title()
    elif kind == "numeric_within":
        b["credit"] = f"{float(a['credit']) + 0.01:.2f}"
    elif kind == "numeric_outside":
        b["credit"] = str(int(float(a["credit"])) + rng.choice([50, 250, 1000]))
    elif kind == "value":
        field = rng.choice(["phone", "last", "zip", "country", "dob"])
        if field == "phone":
            b["phone"] = other(rng, [f"555-01{i:02d}" for i in range(100)], a["phone"])
        elif field == "last":
            b["last"] = other(rng, LAST, a["last"])
        elif field == "zip":
            b["zip"] = a["zip"] + "9"
        elif field == "country":
            b["country"] = other(rng, sorted({c for _, c, _ in CITIES}), a["country"])
        else:
            b["dob"] = (date.fromisoformat(a["dob"]) + timedelta(days=1)).isoformat()


def main() -> None:
    rng = random.Random(SEED)
    OUT.mkdir(exist_ok=True)
    keys = iter(range(1, 10_000))
    common = [person(rng, next(keys)) for _ in range(N_COMMON)]
    only_a = [person(rng, next(keys)) for _ in range(N_ONLY_A)]
    only_b = [person(rng, next(keys)) for _ in range(N_ONLY_B)]
    dup_a = [person(rng, next(keys)) for _ in range(N_DUP_A)]
    dup_b = [person(rng, next(keys)) for _ in range(N_DUP_B)]

    a_people = [dict(p) for p in common]
    b_people = [dict(p) for p in common]
    kinds = [k for k, n in PLANT.items() for _ in range(n)] + ["empty"] * N_EMPTY_VS_NULL
    planted_rows = rng.sample(range(N_COMMON), len(kinds))
    empty_cells = 0
    for kind, idx in zip(kinds, planted_rows, strict=True):
        if kind == "empty":
            field = rng.choice(["phone", "zip", "dob"])
            a_people[idx][field] = ""
            b_people[idx][field] = None
            empty_cells += 1
        else:
            plant(rng, kind, a_people[idx], b_people[idx])

    rows_a = [a_row(p) for p in a_people + only_a]
    for p in dup_a:  # same key twice, slightly different rows
        rows_a.append(a_row(p))
        rows_a.append(a_row({**p, "phone": other(rng, ["555-0100", "555-0101"], p["phone"])}))
    rows_b = [b_row(p) for p in b_people + only_b]
    for p in dup_b:
        rows_b.append(b_row(p))
        rows_b.append(b_row({**p, "city": other(rng, [c for c, _, _ in CITIES], p["city"])}))
    rng.shuffle(rows_a)
    rng.shuffle(rows_b)

    with (OUT / "legacy_customers.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(A_HEADERS)
        writer.writerows(rows_a)

    wb = xlsxwriter.Workbook(str(OUT / "target_customers.xlsx"), {"strings_to_formulas": False})
    ws = wb.add_worksheet("customers")
    for c, name in enumerate(B_HEADERS):
        ws.write_string(0, c, name)
    for r, row in enumerate(rows_b, start=1):
        for c, value in enumerate(row):
            if value is None:
                continue
            if isinstance(value, float):
                ws.write_number(r, c, value)
            else:
                ws.write_string(r, c, value)
    wb.close()

    with (OUT / "mapping.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["source_column", "target_column"])
        writer.writerows(
            [
                ["FName", "firstname"],
                ["LName", "lastname"],
                ["Mobile", "telephone"],
                ["Zip", "postcode"],
                ["Country", "country_id"],
            ]  # fmt: skip
        )

    n_diff = sum(PLANT.values())
    default_mismatched = n_diff + N_EMPTY_VS_NULL
    lenient_mismatched = PLANT["numeric_outside"] + PLANT["value"]
    duplicate_rows = 2 * (N_DUP_A + N_DUP_B)
    expected = {
        "seed": SEED,
        "file_a": {
            "name": "legacy_customers.csv",
            "rows": len(rows_a),
            "columns": len(A_HEADERS),
            "empty_cells": empty_cells,
            "duplicate_keys": N_DUP_A,
            "duplicate_key_rows": 2 * N_DUP_A,
        },
        "file_b": {
            "name": "target_customers.xlsx",
            "rows": len(rows_b),
            "columns": len(B_HEADERS),
            "empty_cells": empty_cells,
            "duplicate_keys": N_DUP_B,
            "duplicate_key_rows": 2 * N_DUP_B,
        },
        "planted": {**PLANT, "empty_vs_null": N_EMPTY_VS_NULL},
        "default_options": {
            "options": {
                "trim": False,
                "case_insensitive": False,
                "tolerance": 0,
                "empty_equals_null": False,
            },  # fmt: skip
            "counts": {
                "matched": N_COMMON - default_mismatched,
                "mismatched": default_mismatched,
                "only_in_a": N_ONLY_A,
                "only_in_b": N_ONLY_B,
                "duplicate_key": duplicate_rows,
            },
        },
        "all_options": {
            "options": {
                "trim": True,
                "case_insensitive": True,
                "tolerance": 0.01,
                "empty_equals_null": True,
            },  # fmt: skip
            "counts": {
                "matched": N_COMMON - lenient_mismatched,
                "mismatched": lenient_mismatched,
                "only_in_a": N_ONLY_A,
                "only_in_b": N_ONLY_B,
                "duplicate_key": duplicate_rows,
            },
        },
    }
    (OUT / "expected.json").write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(rows_a)} A rows, {len(rows_b)} B rows to {OUT}")


if __name__ == "__main__":
    main()
