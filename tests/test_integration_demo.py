"""Full flow on the generated demo data: upload, auto-mapping, compare, exports."""

import io
import json
import re

import openpyxl
import pytest

from filematch.config import Settings
from tests.conftest import session_of


@pytest.fixture
def settings():
    return Settings()  # default 200 MB limit


@pytest.fixture
def expected(demo_dir):
    return json.loads((demo_dir / "expected.json").read_text(encoding="utf-8"))


def selected_values(page: str) -> dict[str, str]:
    """Read each <select>'s selected option from the rendered mapping page."""
    values = {}
    for name, body in re.findall(r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', page, re.S):
        match = re.search(r'<option value="([^"]*)"\s+selected', body)
        values[name] = match.group(1) if match else ""
    return values


def upload_demo(client, demo_dir, with_mapping_file=False):
    files = {
        "file_a": ("legacy_customers.csv", (demo_dir / "legacy_customers.csv").read_bytes()),
        "file_b": ("target_customers.xlsx", (demo_dir / "target_customers.xlsx").read_bytes()),
    }
    if with_mapping_file:
        files["file_c"] = ("mapping.csv", (demo_dir / "mapping.csv").read_bytes())
    r = client.post("/upload", files=files, follow_redirects=False)
    assert r.status_code == 303, r.text


def run_compare(client, options: dict) -> dict:
    form = selected_values(client.get("/mapping").text)
    if options["trim"]:
        form["trim"] = "on"
    if options["case_insensitive"]:
        form["case_insensitive"] = "on"
    if options["empty_equals_null"]:
        form["empty_equals_null"] = "on"
    form["tolerance"] = str(options["tolerance"])
    r = client.post("/mapping", data=form, follow_redirects=False)
    assert r.status_code == 303, r.text
    return session_of(client).state["result"].counts


def test_upload_counts_match_expected(client, demo_dir, expected):
    upload_demo(client, demo_dir)
    files = session_of(client).files
    for slot, key in (("file_a", "file_a"), ("file_b", "file_b")):
        table = files[slot]
        stats = table.stats
        want = expected[key]
        assert table.df.height == want["rows"]
        assert table.df.width == want["columns"]
        assert stats["empty_cells"] == want["empty_cells"]
        assert stats["duplicate_keys"] == want["duplicate_keys"]
        assert stats["duplicate_key_rows"] == want["duplicate_key_rows"]
    page = client.get("/").text
    assert "UTF-8" in page and "comma-separated" in page


def test_auto_mapping_on_demo(client, demo_dir):
    upload_demo(client, demo_dir)
    form = selected_values(client.get("/mapping").text)
    names_b = [
        "entity_id", "firstname", "lastname", "email", "telephone", "postcode", "city",
        "country_id", "dob", "credit_limit",
    ]  # fmt: skip
    chosen = [names_b[int(form[f"map_{i}"])] for i in range(10)]
    assert chosen == names_b  # every legacy header found its Magento-style match
    assert form["key_a_0"] == "0" and form["key_b_0"] == "0"


@pytest.mark.parametrize("scenario", ["default_options", "all_options"])
def test_full_flow_counts_match_expected(client, demo_dir, expected, scenario):
    upload_demo(client, demo_dir)
    counts = run_compare(client, expected[scenario]["options"])
    assert counts == expected[scenario]["counts"]
    summary = client.get("/results").text
    for n in expected[scenario]["counts"].values():
        assert f"<strong>{n:,}</strong>" in summary


def test_mapping_file_on_demo(client, demo_dir, expected):
    upload_demo(client, demo_dir, with_mapping_file=True)
    page = client.get("/mapping").text
    assert page.count('<span class="badge">mapping file</span>') == 5
    assert "alert-error" not in page
    counts = run_compare(client, expected["default_options"]["options"])
    assert counts == expected["default_options"]["counts"]


def test_demo_exports(client, demo_dir, expected):
    upload_demo(client, demo_dir)
    counts = run_compare(client, expected["default_options"]["options"])
    issues = sum(counts.values()) - counts["matched"]
    wb = openpyxl.load_workbook(io.BytesIO(client.get("/download/issues.xlsx").content))
    assert wb.active.max_row == issues + 1
    csv_text = client.get("/download/issues.csv").content.decode("utf-8-sig")
    assert len(csv_text.strip().splitlines()) == issues + 1
    wb = openpyxl.load_workbook(io.BytesIO(client.get("/download/all.xlsx").content))
    assert wb.active.max_row == sum(counts.values()) + 1
