import polars as pl
import pytest

from filematch.compare import CompareOptions, compare, read_page

A = pl.DataFrame(
    {
        "id": ["1", "2", "3", "4", "5", "7", "7", "9"],
        "name": ["Ann", "Bo", "Cy", "Di", "Ed", "G1", "G2", "Ivy"],
        "amt": ["10", "5", "1", "1.00", "3", "0", "0", "2"],
        "note": ["x", "  y", "Z", "", "e", "g", "g", None],
    }
)
B = pl.DataFrame(
    {
        "entity_id": ["1", "2", "3", "4", "6", "7", "8", "8", "9"],
        "firstname": ["Ann", "Bo", "Cy", "Di", "Fay", "G", "H1", "H2", "Ivy"],
        "credit": ["10", "5", "1.0", "1.01", "4", "0", "0", "0", "2"],
        "remark": ["x", "y", "z", None, "f", "g", "h", "h", None],
    }
)
PAIRS = [("name", "firstname"), ("amt", "credit"), ("note", "remark")]


def run(tmp_path, options=None, a=A, b=B, key_a=("id",), key_b=("entity_id",), pairs=PAIRS):
    result = compare(
        a, b, list(key_a), list(key_b), pairs, options or CompareOptions(), tmp_path / "r.parquet"
    )
    frame = pl.read_parquet(result.path)
    return result, frame


def status_of(frame, key):
    rows = frame.filter(pl.col("k0") == key)
    return rows["status"].to_list()


def test_row_classes_default_options(tmp_path):
    result, frame = run(tmp_path)
    assert result.counts == {
        "matched": 2,  # 1 and 9 (null vs null is equal)
        "mismatched": 3,  # 2 (whitespace), 3 (case), 4 (1.00 vs 1.01, "" vs null)
        "only_in_a": 1,  # 5
        "only_in_b": 1,  # 6
        "duplicate_key": 5,  # key 7: twice in A + once in B; key 8: twice in B
    }
    assert status_of(frame, "1") == ["matched"]
    assert status_of(frame, "5") == ["only_in_a"]
    assert status_of(frame, "6") == ["only_in_b"]
    assert status_of(frame, "7") == ["duplicate_key"] * 3
    dups = frame.filter(pl.col("status") == "duplicate_key")
    assert sorted(dups["dup_side"].to_list()) == ["A", "A", "B", "B", "B"]
    assert result.total == 12 and result.issues == 10


def test_per_cell_mismatch_flags(tmp_path):
    _, frame = run(tmp_path)
    row = frame.filter(pl.col("k0") == "3").row(0, named=True)
    assert (row["m0"], row["m1"], row["m2"]) == (False, False, True)  # "1" == "1.0" numerically
    row = frame.filter(pl.col("k0") == "4").row(0, named=True)
    assert (row["m0"], row["m1"], row["m2"]) == (False, True, True)
    assert (row["a1"], row["b1"]) == ("1.00", "1.01")


def test_trim_option(tmp_path):
    _, frame = run(tmp_path, CompareOptions(trim=True))
    assert status_of(frame, "2") == ["matched"]


def test_case_option(tmp_path):
    _, frame = run(tmp_path, CompareOptions(case_insensitive=True))
    assert status_of(frame, "3") == ["matched"]


def test_tolerance_option(tmp_path):
    _, frame = run(tmp_path, CompareOptions(tolerance=0.01, empty_equals_null=True))
    assert status_of(frame, "4") == ["matched"]
    _, frame = run(tmp_path, CompareOptions(tolerance=0.009, empty_equals_null=True))
    assert status_of(frame, "4") == ["mismatched"]


def test_empty_equals_null_option(tmp_path):
    _, frame = run(tmp_path, CompareOptions(tolerance=0.01))
    row = frame.filter(pl.col("k0") == "4").row(0, named=True)
    assert row["m1"] is False and row["m2"] is True
    _, frame = run(tmp_path, CompareOptions(tolerance=0.01, empty_equals_null=True))
    assert frame.filter(pl.col("k0") == "4").row(0, named=True)["m2"] is False


def test_numeric_compare_at_zero_tolerance(tmp_path):
    a = pl.DataFrame({"k": ["1", "2", "3", "4"], "v": ["1", "2.50", "abc", "1e3"]})
    b = pl.DataFrame({"k": ["1", "2", "3", "4"], "v": ["1.0", "2.5", "ABC", "1000"]})
    _, frame = run(tmp_path, a=a, b=b, key_a=["k"], key_b=["k"], pairs=[("v", "v")])
    assert frame.sort("k0")["status"].to_list() == ["matched", "matched", "mismatched", "matched"]


def test_trim_and_case_apply_to_keys(tmp_path):
    a = pl.DataFrame({"k": [" K1", "k2"], "v": ["a", "b"]})
    b = pl.DataFrame({"k": ["k1", "K2 "], "v": ["a", "b"]})
    result, _ = run(tmp_path, a=a, b=b, key_a=["k"], key_b=["k"], pairs=[("v", "v")])
    assert result.counts["matched"] == 0
    result, _ = run(
        tmp_path,
        CompareOptions(trim=True, case_insensitive=True),
        a=a,
        b=b,
        key_a=["k"],
        key_b=["k"],
        pairs=[("v", "v")],
    )
    assert result.counts["matched"] == 2


def test_composite_key(tmp_path):
    a = pl.DataFrame({"store": ["1", "1", "2"], "sku": ["A", "B", "A"], "qty": ["5", "6", "7"]})
    b = pl.DataFrame(
        {"site": ["1", "2", "2"], "code": ["A", "A", "B"], "quantity": ["5", "70", "8"]}
    )
    result, frame = run(
        tmp_path,
        a=a,
        b=b,
        key_a=["store", "sku"],
        key_b=["site", "code"],
        pairs=[("qty", "quantity")],
    )
    assert result.counts["matched"] == 1
    assert result.counts["mismatched"] == 1
    assert result.counts["only_in_a"] == 1
    assert result.counts["only_in_b"] == 1
    assert result.key_labels == ["store / site", "sku / code"]
    mismatch = frame.filter(pl.col("status") == "mismatched").row(0, named=True)
    assert (mismatch["k0"], mismatch["k1"], mismatch["a0"], mismatch["b0"]) == ("2", "A", "7", "70")


def test_composite_key_duplicates(tmp_path):
    a = pl.DataFrame({"s": ["1", "1", "1"], "k": ["A", "A", "B"], "v": ["x", "y", "z"]})
    b = pl.DataFrame({"s": ["1", "1"], "k": ["A", "B"], "v": ["x", "z"]})
    result, _ = run(tmp_path, a=a, b=b, key_a=["s", "k"], key_b=["s", "k"], pairs=[("v", "v")])
    assert result.counts == {
        "matched": 1,
        "mismatched": 0,
        "only_in_a": 0,
        "only_in_b": 0,
        "duplicate_key": 3,  # (1, A) twice in A and once in B
    }


def test_key_twice_in_a_once_in_b(tmp_path):
    a = pl.DataFrame({"k": ["1", "1", "2"], "v": ["x", "y", "z"]})
    b = pl.DataFrame({"k": ["1", "2"], "v": ["x", "z"]})
    result, frame = run(tmp_path, a=a, b=b, key_a=["k"], key_b=["k"], pairs=[("v", "v")])
    assert result.counts == {
        "matched": 1,
        "mismatched": 0,
        "only_in_a": 0,
        "only_in_b": 0,
        "duplicate_key": 3,
    }
    dups = frame.filter(pl.col("k0") == "1")
    assert dups["status"].to_list() == ["duplicate_key"] * 3
    assert sorted(dups["dup_side"].to_list()) == ["A", "A", "B"]


def test_key_twice_in_b_once_in_a(tmp_path):
    a = pl.DataFrame({"k": ["1"], "v": ["x"]})
    b = pl.DataFrame({"k": ["1", "1"], "v": ["x", "y"]})
    result, _ = run(tmp_path, a=a, b=b, key_a=["k"], key_b=["k"], pairs=[("v", "v")])
    assert result.counts["duplicate_key"] == 3
    assert result.counts["only_in_a"] == 0 and result.counts["only_in_b"] == 0


def test_key_twice_in_a_absent_in_b(tmp_path):
    a = pl.DataFrame({"k": ["1", "1", "2"], "v": ["x", "y", "z"]})
    b = pl.DataFrame({"k": ["2"], "v": ["z"]})
    result, _ = run(tmp_path, a=a, b=b, key_a=["k"], key_b=["k"], pairs=[("v", "v")])
    assert result.counts == {
        "matched": 1,
        "mismatched": 0,
        "only_in_a": 0,
        "only_in_b": 0,
        "duplicate_key": 2,
    }


def test_ambiguous_key_respects_trim_and_case(tmp_path):
    a = pl.DataFrame({"k": ["K1", " k1 "], "v": ["x", "y"]})
    b = pl.DataFrame({"k": ["k1"], "v": ["x"]})
    opts = CompareOptions(trim=True, case_insensitive=True)
    result, _ = run(tmp_path, opts, a=a, b=b, key_a=["k"], key_b=["k"], pairs=[("v", "v")])
    assert result.counts["duplicate_key"] == 3
    result, _ = run(tmp_path, a=a, b=b, key_a=["k"], key_b=["k"], pairs=[("v", "v")])
    assert result.counts["duplicate_key"] == 0  # without trim/case the keys differ


def test_key_columns_must_pair(tmp_path):
    with pytest.raises(ValueError):
        compare(A, B, ["id"], [], PAIRS, CompareOptions(), tmp_path / "r.parquet")


def test_pagination(tmp_path):
    n = 250
    a = pl.DataFrame({"k": [str(i) for i in range(n)], "v": ["x"] * n})
    b = pl.DataFrame({"k": [str(i) for i in range(n)], "v": ["x"] * 200 + ["y"] * 50})
    result, _ = run(tmp_path, a=a, b=b, key_a=["k"], key_b=["k"], pairs=[("v", "v")])
    assert read_page(result, "all", 1).height == 100
    assert read_page(result, "all", 3).height == 50
    assert read_page(result, "all", 3)["k0"][0] == "200"
    assert read_page(result, "issues", 1).height == 50
    assert read_page(result, "issues", 2).height == 0
