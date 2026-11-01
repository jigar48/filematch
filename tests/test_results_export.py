import csv
import io

import openpyxl
import pytest

from filematch import export
from filematch.export import escape_csv_cell
from tests.conftest import make_csv, make_xlsx, session_of, upload

A_CSV = make_csv(
    [["CustID", "FName", "Note"]]
    + [[f"{i:03d}", f"Name{i}", "ok"] for i in range(1, 231)]
    + [["900", "Only A", '=HYPERLINK("http://example.com")']]
)
B_ROWS = [["entity_id", "firstname", "note"]] + [
    [f"{i:03d}", f"Name{i}" if i % 10 else f"NAME{i}", "ok"] for i in range(1, 231)
]
B_ROWS += [["901", "Only B", "@cmd"], ["902", "Dup", "x"], ["902", "Dup", "y"]]
B_XLSX = make_xlsx(B_ROWS)


def compare_flow(client, **options):
    upload(client, file_a=("a.csv", A_CSV), file_b=("b.xlsx", B_XLSX))
    data = {"map_0": "0", "map_1": "1", "map_2": "2", "key_a_0": "0", "key_b_0": "0"}
    data.update(options)
    r = client.post("/mapping", data=data, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/results"


def test_results_summary_tabs_and_pagination(client):
    compare_flow(client)
    page = client.get("/results").text
    assert "Page 1 of 3" in page
    assert "matched" in page and "duplicate key" in page
    assert "A: FName" in page and "B: firstname" in page
    assert 'class="mismatch"' in page
    assert '<span class="visually-hidden">Mismatch: </span>' in page
    last = client.get("/results?tab=all&page=99").text
    assert "Page 3 of 3" in last
    issues = client.get("/results?tab=issues").text
    # 23 case mismatches + only A + only B + 2 duplicate rows
    assert "27 rows" in issues and "Page 1 of 1" in issues
    assert "only in A" in issues and "only in B" in issues and "duplicate key (B)" in issues
    assert client.get("/results?tab=issues&page=abc").status_code == 200


def test_case_option_clears_case_mismatches(client):
    compare_flow(client, case_insensitive="on")
    assert "4 rows" in client.get("/results?tab=issues").text


def test_results_without_compare_redirects(client):
    assert client.get("/results", follow_redirects=False).status_code == 303


def load_xlsx(content: bytes):
    return openpyxl.load_workbook(io.BytesIO(content))


def test_xlsx_issues_export(client):
    compare_flow(client)
    r = client.get("/download/issues.xlsx")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    ws = load_xlsx(r.content).active
    assert ws.freeze_panes == "A2"
    assert ws.auto_filter.ref == "A1:F28"
    header = [c.value for c in ws[1]]
    assert header == [
        "Status",
        "CustID / entity_id",
        "A: FName",
        "B: firstname",
        "A: Note",
        "B: note",
    ]
    rows = list(ws.iter_rows(min_row=2, values_only=False))
    assert len(rows) == 27
    mismatched = next(r for r in rows if r[0].value == "mismatched")
    fills = [c.fill.fgColor.rgb for c in mismatched]
    # A and B cells of the mismatching pair are both red; others are not
    assert fills[2].endswith("F4C7C3") and fills[3].endswith("F4C7C3")
    assert not fills[4].endswith("F4C7C3")
    # leading zeros kept, formula text stored as text
    assert mismatched[1].value == "010" and mismatched[1].data_type == "s"
    only_a = next(r for r in rows if r[0].value == "only in A")
    assert only_a[4].value.startswith("=HYPERLINK")
    assert only_a[4].data_type == "s"


def test_xlsx_all_export(client):
    compare_flow(client)
    ws = load_xlsx(client.get("/download/all.xlsx").content).active
    assert ws.max_row == 1 + 234
    assert ws.freeze_panes == "A2"


def test_xlsx_refused_over_excel_limit(client, monkeypatch):
    compare_flow(client)
    monkeypatch.setattr(export, "EXCEL_MAX_ROWS", 100)
    r = client.get("/download/all.xlsx")
    assert r.status_code == 413
    assert "more than Excel" in r.text
    assert "/download/all.csv" in r.text
    assert client.get("/download/all.csv").status_code == 200
    assert client.get("/download/issues.xlsx").status_code == 200  # 27 rows still fine


def test_csv_issues_export(client):
    compare_flow(client)
    r = client.get("/download/issues.csv")
    assert r.status_code == 200
    text = r.content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0][:2] == ["Status", "CustID / entity_id"]
    assert len(rows) == 28
    only_a = next(r for r in rows if r[0] == "only in A")
    assert only_a[4] == '\'=HYPERLINK("http://example.com")'
    only_b = next(r for r in rows if r[0] == "only in B")
    assert only_b[5] == "'@cmd"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("=SUM(A1:A2)", "'=SUM(A1:A2)"),
        ("+cmd", "'+cmd"),
        ("-cmd", "'-cmd"),
        ("@cmd", "'@cmd"),
        ("\tx", "'\tx"),
        ("\rx", "'\rx"),
        ("-5", "-5"),
        ("+1.5", "+1.5"),
        ("-1e3", "-1e3"),
        ("-.5", "-.5"),
        ("plain", "plain"),
        ("a=b", "a=b"),
        (None, ""),
    ],
)
def test_csv_formula_escaping(value, expected):
    assert escape_csv_cell(value) == expected


def test_clear_after_results_deletes_everything(client):
    compare_flow(client)
    client.get("/results?tab=issues")
    client.get("/download/issues.xlsx")
    directory = session_of(client).directory
    assert (directory / "result.parquet").exists()
    client.post("/clear")
    assert not directory.exists()


def test_reupload_invalidates_results(client):
    compare_flow(client)
    upload(client, file_a=("a.csv", A_CSV))
    assert client.get("/results", follow_redirects=False).status_code == 303
    assert not (session_of(client).directory / "result.parquet").exists()
