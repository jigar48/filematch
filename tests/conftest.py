from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import pytest
import xlsxwriter
from fastapi.testclient import TestClient

from filematch.app import create_app
from filematch.config import Settings


def make_xlsx(rows: list[list[object]]) -> bytes:
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True, "strings_to_formulas": False})
    ws = wb.add_worksheet()
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            if value is None:
                continue
            if isinstance(value, int | float):
                ws.write_number(r, c, value)
            else:
                ws.write_string(r, c, str(value))
    wb.close()
    return buf.getvalue()


def make_csv(rows: list[list[str]], sep: str = ",", encoding: str = "utf-8") -> bytes:
    return ("\n".join(sep.join(r) for r in rows) + "\n").encode(encoding)


@pytest.fixture
def settings() -> Settings:
    return Settings(max_upload_mb=1)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    with TestClient(app, base_url="http://localhost") as c:
        yield c


def upload(client: TestClient, **files: tuple[str, bytes]):
    payload = {
        slot: (name, data, "application/octet-stream") for slot, (name, data) in files.items()
    }
    return client.post("/upload", files=payload, follow_redirects=False)


def session_of(client: TestClient):
    store = client.app.state.store
    return store.get(client.cookies.get("filematch_session"))


@pytest.fixture
def demo_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "demo"
