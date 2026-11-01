import polars as pl
import pytest

from filematch.mapping import (
    Candidate,
    assign,
    import_mapping_file,
    normalise,
    suggest,
    synonym_group,
)
from tests.conftest import make_csv, make_xlsx, upload


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  FirstName ", "first name"),
        ("CustID", "cust id"),
        ("E-Mail", "e mail"),
        ("postal.code", "postal code"),
        ("Credit_Limit", "credit limit"),
        ("credit  -_ limit", "credit limit"),
        ("customerID", "customer id"),
        ("HTMLParser", "html parser"),
        ("zip", "zip"),
    ],
)
def test_normalise(raw, expected):
    assert normalise(raw) == expected


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("FName", "firstname"),
        ("given_name", "First_Name"),
        ("Surname", "lastname"),
        ("Mobile", "telephone"),
        ("Zip", "postcode"),
        ("DOB", "date_of_birth"),
        ("Town", "city"),
        ("Country", "country_id"),
        ("CustID", "entity_id"),
        ("Street", "street_address"),
    ],
)
def test_synonyms(a, b):
    assert synonym_group(a) is not None
    assert synonym_group(a) == synonym_group(b)


def test_suggest_exact_synonym_fuzzy_and_threshold():
    cols_a = ["CustID", "E-Mail", "FName", "Credit_Limit", "Loyalty Points", "zzz"]
    cols_b = ["entity_id", "email", "firstname", "credit_limit", "loyalty_point_total", "qqq"]
    result = {s.a: s for s in suggest(cols_a, cols_b)}
    assert (result["E-Mail"].b, result["E-Mail"].source, result["E-Mail"].score) == (
        "email",
        "exact",
        100,
    )
    assert (result["CustID"].b, result["CustID"].source) == ("entity_id", "synonym")
    assert (result["FName"].b, result["FName"].source) == ("firstname", "synonym")
    assert result["Credit_Limit"].source == "exact"
    assert result["Loyalty Points"].source == "fuzzy"
    assert 60 <= result["Loyalty Points"].score < 100
    assert result["zzz"].b is None and result["zzz"].source is None


def test_assignment_is_one_to_one_and_greedy_by_score():
    pool = [
        Candidate("a1", "b1", 90, "fuzzy"),
        Candidate("a2", "b1", 95, "fuzzy"),
        Candidate("a1", "b2", 70, "fuzzy"),
    ]
    result = {s.a: s.b for s in assign(["a1", "a2"], pool)}
    assert result == {"a2": "b1", "a1": "b2"}


def test_source_precedence_beats_score():
    pool = [
        Candidate("a1", "b1", 99, "fuzzy"),
        Candidate("a1", "b2", 100, "mapping file"),
        Candidate("a2", "b2", 100, "exact"),
    ]
    result = {s.a: (s.b, s.source) for s in assign(["a1", "a2"], pool)}
    assert result == {"a1": ("b2", "mapping file"), "a2": (None, None)}


def test_no_b_column_used_twice_on_realistic_headers():
    cols_a = ["Name", "Name2", "Phone", "Mobile"]
    cols_b = ["name", "telephone"]
    result = suggest(cols_a, cols_b)
    used = [s.b for s in result if s.b]
    assert len(used) == len(set(used))


def test_mapping_file_import_and_errors():
    df = pl.DataFrame(
        {
            "source": ["FName", "Zip", "Nope", "", "lname", "FName", "City"],
            "target": ["firstname", "postcode", "email", "", "lastname", "city", ""],
        }
    )
    result = import_mapping_file(
        df,
        "map.csv",
        ["FName", "LName", "Zip", "City"],
        ["firstname", "lastname", "postcode", "city"],
    )
    assert result.pairs == [("FName", "firstname"), ("Zip", "postcode"), ("LName", "lastname")]
    assert len(result.errors) == 1
    message = result.errors[0]
    assert "map.csv" in message
    assert "row 4: 'Nope' is not a column in File A" in message
    assert "row 7: 'FName' or 'city' is already mapped" in message
    assert "row 8: both a File A and a File B column name are required" in message


def test_mapping_file_needs_two_columns():
    result = import_mapping_file(pl.DataFrame({"only": ["x"]}), "m.csv", ["x"], ["y"])
    assert result.pairs == []
    assert "needs at least two columns" in result.errors[0]


def test_mapping_file_takes_precedence():
    result = {s.a: s for s in suggest(["Email"], ["email", "contact"], [("Email", "contact")])}
    assert (result["Email"].b, result["Email"].source) == ("contact", "mapping file")


A_CSV = make_csv(
    [
        ["CustID", "FName", "E-Mail", "Notes"],
        ["001", "Ann", "ann@example.com", "x"],
        ["002", "Bo", "bo@example.org", "y"],
    ]
)
B_XLSX = make_xlsx(
    [
        ["entity_id", "firstname", "email", "website"],
        ["001", "Ann", "ann@example.com", "a"],
        ["002", "Bo", "bo@example.org", "b"],
    ]
)


def test_mapping_page_suggestions(client):
    upload(client, file_a=("a.csv", A_CSV), file_b=("b.xlsx", B_XLSX))
    page = client.get("/mapping").text
    assert "synonym" in page and "exact" in page
    assert "Suggest with AI" not in page  # no key in the test environment


def test_mapping_page_redirects_without_files(client):
    r = client.get("/mapping", follow_redirects=False)
    assert r.status_code == 303


def test_mapping_file_errors_shown_and_screen_works(client):
    mapping = make_csv([["source", "target"], ["FName", "firstname"], ["Ghost", "email"]])
    upload(
        client,
        file_a=("a.csv", A_CSV),
        file_b=("b.xlsx", B_XLSX),
        file_c=("mymap.csv", mapping),
    )
    page = client.get("/mapping").text
    assert "Mapping file mymap.csv" in page
    assert "row 3: &#39;Ghost&#39; is not a column in File A" in page
    assert "mapping file" in page  # the valid FName row was imported


def test_unreadable_mapping_file_is_reported(client):
    upload(client, file_a=("a.csv", A_CSV), file_b=("b.xlsx", B_XLSX))
    r = upload(client, file_c=("broken.xlsx", b"not a zip"))
    assert r.status_code == 400
    assert "File C (mapping)" in r.text
    assert client.get("/mapping").status_code == 200


def form(mapping: dict[int, str], key_a=("0",), key_b=("0",), **extra):
    data = {f"map_{i}": v for i, v in mapping.items()}
    for n, (ka, kb) in enumerate(zip(key_a, key_b, strict=True)):
        data[f"key_a_{n}"] = ka
        data[f"key_b_{n}"] = kb
    data.update(extra)
    return data


def test_save_mapping_with_edits_and_ignore(client):
    upload(client, file_a=("a.csv", A_CSV), file_b=("b.xlsx", B_XLSX))
    data = form({0: "0", 1: "1", 2: "2", 3: ""})
    r = client.post("/mapping", data=data, follow_redirects=False)
    assert r.status_code == 303
    config = client.app.state.store.get(client.cookies.get("filematch_session")).state["config"]
    assert config.key_a == ["CustID"] and config.key_b == ["entity_id"]
    # the key pair is not compared as a value column; Notes was ignored
    assert config.pairs == [("FName", "firstname"), ("E-Mail", "email")]


def test_composite_key_selection(client):
    upload(client, file_a=("a.csv", A_CSV), file_b=("b.xlsx", B_XLSX))
    data = form({0: "", 1: "", 2: "2", 3: ""}, key_a=("0", "1"), key_b=("0", "1"))
    r = client.post("/mapping", data=data, follow_redirects=False)
    assert r.status_code == 303
    config = client.app.state.store.get(client.cookies.get("filematch_session")).state["config"]
    assert config.key_a == ["CustID", "FName"]
    assert config.key_b == ["entity_id", "firstname"]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (form({0: "1", 1: "1"}), "is mapped twice"),
        (form({1: "1"}, key_a=("0",), key_b=("",)), "choose a column on both sides"),
        (form({1: "1"}, key_a=("",), key_b=("",)), "Choose at least one key column pair"),
        (form({1: "1"}, key_a=("0", "0"), key_b=("0", "1")), "only be used once per side"),
        (form({1: "99"}), "Unknown File B column"),
        (form({0: "0"}), "Map at least one non-key column"),
    ],
)
def test_mapping_validation_errors(client, data, message):
    upload(client, file_a=("a.csv", A_CSV), file_b=("b.xlsx", B_XLSX))
    r = client.post("/mapping", data=data, follow_redirects=False)
    assert r.status_code == 400
    assert message in r.text
