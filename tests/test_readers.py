from pathlib import Path

import pytest

from filematch.readers import (
    ReadError,
    decode_csv_bytes,
    read_table,
    sniff_delimiter,
    table_stats,
)
from tests.conftest import make_csv, make_xlsx


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_csv_keeps_leading_zeros_and_text(tmp_path):
    path = write(tmp_path, "a.csv", make_csv([["id", "amount"], ["007", "1.50"], ["010", "2"]]))
    table = read_table(path, "a.csv")
    assert table.df["id"].to_list() == ["007", "010"]
    assert table.df["amount"].to_list() == ["1.50", "2"]
    assert all(str(d) == "String" for d in table.df.dtypes)


def test_csv_empty_field_is_empty_string_not_null(tmp_path):
    path = write(tmp_path, "a.csv", make_csv([["id", "x"], ["1", ""], ["2", "v"]]))
    table = read_table(path, "a.csv")
    assert table.df["x"].to_list() == ["", "v"]


def test_utf8_bom_detected(tmp_path):
    data = b"\xef\xbb\xbf" + make_csv([["id", "name"], ["1", "Zoë"]])
    table = read_table(write(tmp_path, "a.csv", data), "a.csv")
    assert table.encoding == "UTF-8 (BOM)"
    assert table.columns == ["id", "name"]
    assert table.df["name"][0] == "Zoë"


def test_cp1252_fallback(tmp_path):
    data = make_csv([["id", "name"], ["1", "Café – ok"]], encoding="cp1252")
    table = read_table(write(tmp_path, "a.csv", data), "a.csv")
    assert table.encoding == "Windows-1252"
    assert table.df["name"][0] == "Café – ok"


def test_undecodable_csv_rejected():
    with pytest.raises(ReadError, match="UTF-8 or Windows-1252"):
        decode_csv_bytes(b"id,name\n1,\x81\x8d\x90\n")


def test_binary_csv_rejected():
    with pytest.raises(ReadError, match="NUL"):
        decode_csv_bytes(b"id,name\n1,\x00\x00\n")


@pytest.mark.parametrize("sep", [",", ";", "\t", "|"])
def test_delimiter_sniffing(tmp_path, sep):
    data = make_csv([["id", "name", "city"], ["1", "Ann", "Pune"], ["2", "Bo", "Oslo"]], sep=sep)
    assert sniff_delimiter(data.decode()) == sep
    table = read_table(write(tmp_path, "a.csv", data), "a.csv")
    assert table.columns == ["id", "name", "city"]
    assert table.delimiter == sep


def test_xlsx_values_read_as_text(tmp_path):
    data = make_xlsx([["id", "amount", "name"], ["007", 1.0, "Ann"], ["8", 2.5, None]])
    table = read_table(write(tmp_path, "b.xlsx", data), "b.xlsx")
    assert table.df["id"].to_list() == ["007", "8"]
    assert table.df["amount"].to_list() == ["1", "2.5"]
    assert table.df["name"].to_list() == ["Ann", None]


def test_column_cap_csv(tmp_path):
    header = [f"c{i}" for i in range(6)]
    path = write(tmp_path, "a.csv", make_csv([header, ["x"] * 6]))
    with pytest.raises(ReadError, match="limit is 5"):
        read_table(path, "a.csv", max_columns=5)


def test_column_cap_xlsx(tmp_path):
    header = [f"c{i}" for i in range(6)]
    path = write(tmp_path, "b.xlsx", make_xlsx([header, ["x"] * 6]))
    with pytest.raises(ReadError, match="limit is 5"):
        read_table(path, "b.xlsx", max_columns=5)


def test_stats_counts(tmp_path):
    rows = [["id", "a", "b"], ["1", "", "x"], ["1", "y", ""], ["2", "z", "w"], ["3", "", ""]]
    table = read_table(write(tmp_path, "a.csv", make_csv(rows)), "a.csv")
    stats = table_stats(table.df)
    assert stats == {
        "rows": 4,
        "columns": 3,
        "empty_cells": 4,
        "duplicate_keys": 1,
        "duplicate_key_rows": 2,
    }
