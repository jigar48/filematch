import asyncio
import io
import zipfile

import pytest

from filematch.uploads import UploadError, receive_files
from tests.conftest import make_csv, make_xlsx, session_of, upload

CSV = make_csv([["id", "name"], ["1", "Ann"], ["2", "Bo"]])


def test_upload_ok_shows_counts(client):
    r = upload(
        client, file_a=("legacy.csv", CSV), file_b=("target.xlsx", make_xlsx([["id"], ["1"]]))
    )
    assert r.status_code == 303
    page = client.get("/").text
    assert "legacy.csv" in page and "target.xlsx" in page
    assert "UTF-8" in page and "comma-separated" in page


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("old.xls", "old .xls"),
        ("macro.xlsm", ".xlsm"),
        ("notes.txt", "only .xlsx and .csv"),
        ("noext", "only .xlsx and .csv"),
    ],
)
def test_rejects_extension(client, name, message):
    r = upload(client, file_a=(name, b"id\n1\n"))
    assert r.status_code == 400
    assert message in r.text


def test_rejects_xlsx_that_is_not_zip(client):
    r = upload(client, file_a=("fake.xlsx", b"id,name\n1,Ann\n"))
    assert r.status_code == 400
    assert "not a zip package" in r.text


def test_rejects_zip_without_content_types(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("hello.txt", "hi")
    r = upload(client, file_a=("fake.xlsx", buf.getvalue()))
    assert r.status_code == 400
    assert "[Content_Types].xml missing" in r.text


def test_rejects_xlsx_with_macros(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("xl/vbaProject.bin", "x")
    r = upload(client, file_a=("renamed.xlsx", buf.getvalue()))
    assert r.status_code == 400
    assert "macros" in r.text


def zip_bomb(size: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("xl/worksheets/sheet1.xml", b"\0" * size)
    return buf.getvalue()


def test_rejects_zip_bomb_by_ratio(client):
    data = zip_bomb(5 * 1024 * 1024)
    assert len(data) < 1024 * 1024
    r = upload(client, file_a=("bomb.xlsx", data))
    assert r.status_code == 400
    assert "compression ratio is over 100:1" in r.text


def test_rejects_zip_by_total_uncompressed_size(tmp_path):
    from filematch.uploads import check_xlsx_content

    path = tmp_path / "big.xlsx"
    path.write_bytes(zip_bomb(3000))
    with pytest.raises(UploadError, match="expand to more than"):
        check_xlsx_content(path, "big.xlsx", max_uncompressed=2000, max_ratio=1e9)


def test_rejects_undecodable_csv(client):
    r = upload(client, file_a=("bad.csv", b"id,name\n1,\x81\x8d\x90\n"))
    assert r.status_code == 400
    assert "UTF-8 or Windows-1252" in r.text


def test_rejects_too_many_columns(client):
    header = [f"c{i}" for i in range(501)]
    r = upload(client, file_a=("wide.csv", make_csv([header, ["x"] * 501])))
    assert r.status_code == 400
    assert "limit is 500" in r.text


def test_rejects_oversize_file_and_removes_partial(client):
    big = b"id,name\n" + b"1,abcdefghij\n" * 120_000  # ~1.5 MB, limit is 1 MB
    r = upload(client, file_a=("big.csv", big))
    assert r.status_code == 413
    assert "1 MB upload limit" in r.text
    session = session_of(client)
    assert list(session.directory.iterdir()) == []


def test_size_limit_enforced_while_streaming(tmp_path):
    """The parser must stop reading as soon as a file crosses the limit."""
    boundary = "XyZ"
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file_a"; '
        'filename="a.csv"\r\nContent-Type: text/csv\r\n\r\n'
    ).encode()
    consumed = 0
    total_chunks = 1000

    async def body():
        nonlocal consumed
        yield head
        for _ in range(total_chunks):
            consumed += 1
            yield b"x" * 1024

    async def run():
        return await receive_files(
            f"multipart/form-data; boundary={boundary}", body(), tmp_path, {"file_a"}, 10 * 1024
        )

    with pytest.raises(UploadError) as info:
        asyncio.run(run())
    assert info.value.status_code == 413
    assert consumed < 20  # stopped after ~10 KB, not after the full 1 MB
    assert list(tmp_path.iterdir()) == []


def test_empty_upload_rejected(client):
    r = client.post(
        "/upload", files={"file_a": ("", b"", "application/octet-stream")}, follow_redirects=False
    )
    assert r.status_code == 400
    assert "Choose at least one file" in r.text


def test_reupload_replaces_previous_file(client):
    upload(client, file_a=("first.csv", CSV))
    upload(client, file_a=("second.xlsx", make_xlsx([["id"], ["1"]])))
    session = session_of(client)
    names = sorted(p.name for p in session.directory.iterdir())
    assert names == ["file_a.xlsx"]
    assert session.files["file_a"].original_name == "second.xlsx"
