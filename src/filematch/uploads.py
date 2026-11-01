"""Streaming multipart upload handling with size, type and zip-bomb checks.

Files are streamed straight into the session directory; nothing is spooled to the
framework's temp area, and the per-file size limit is enforced chunk by chunk.
"""

from __future__ import annotations

import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

import python_multipart
from python_multipart.multipart import parse_options_header

ALLOWED_EXTENSIONS = {".csv", ".xlsx"}
REJECTED_HINTS = {
    ".xls": "old .xls workbooks are not supported; save it as .xlsx",
    ".xlsm": "macro-enabled .xlsm workbooks are not accepted; save it as .xlsx",
}
MAX_FIELD_BYTES = 64 * 1024


class UploadError(ValueError):
    """Rejected upload; the message is shown to the user."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class ReceivedFile:
    field: str
    filename: str
    path: Path
    size: int


@dataclass
class _Part:
    headers: dict[str, str] = field(default_factory=dict)
    name: str = ""
    filename: str | None = None
    fh: BinaryIO | None = None
    path: Path | None = None
    size: int = 0
    data: bytearray = field(default_factory=bytearray)


def check_extension(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in REJECTED_HINTS:
        raise UploadError(f"{filename}: {REJECTED_HINTS[suffix]}")
    if suffix not in ALLOWED_EXTENSIONS:
        raise UploadError(f"{filename}: only .xlsx and .csv files are accepted")
    return suffix


def check_xlsx_content(path: Path, filename: str, max_uncompressed: int, max_ratio: float) -> None:
    """An .xlsx must be a zip with [Content_Types].xml and must not look like a zip bomb."""
    if not zipfile.is_zipfile(path):
        raise UploadError(f"{filename}: not a valid .xlsx file (it is not a zip package)")
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile as exc:
        raise UploadError(f"{filename}: not a valid .xlsx file ({exc})") from exc
    names = {i.filename for i in infos}
    if "[Content_Types].xml" not in names:
        raise UploadError(f"{filename}: not a valid .xlsx file ([Content_Types].xml missing)")
    if any(n.lower().endswith("vbaproject.bin") for n in names):
        raise UploadError(f"{filename}: contains macros; save it as a plain .xlsx")
    uncompressed = sum(i.file_size for i in infos)
    compressed = sum(i.compress_size for i in infos)
    if uncompressed > max_uncompressed:
        raise UploadError(
            f"{filename}: rejected, it would expand to more than "
            f"{max_uncompressed // 1024**2} MB when unpacked"
        )
    if compressed > 0 and uncompressed / compressed > max_ratio:
        raise UploadError(
            f"{filename}: rejected, its compression ratio is over {max_ratio:.0f}:1 "
            "(possible zip bomb)"
        )


async def receive_files(
    content_type: str,
    stream: AsyncIterator[bytes],
    directory: Path,
    allowed_fields: set[str],
    max_file_bytes: int,
) -> tuple[dict[str, ReceivedFile], dict[str, str]]:
    """Parse a multipart body, writing each file part into `directory`.

    Returns (files by field name, small text fields). Raises UploadError, after
    deleting any partial files, when a limit or check fails.
    """
    ctype, params = parse_options_header(content_type)
    if ctype != b"multipart/form-data" or b"boundary" not in params:
        raise UploadError("expected a multipart/form-data upload")

    files: dict[str, ReceivedFile] = {}
    fields: dict[str, str] = {}
    written: list[Path] = []
    current = _Part()
    header_field = bytearray()
    header_value = bytearray()
    error: list[UploadError] = []

    def on_part_begin() -> None:
        nonlocal current
        current = _Part()

    def on_header_field(data: bytes, start: int, end: int) -> None:
        header_field.extend(data[start:end])

    def on_header_value(data: bytes, start: int, end: int) -> None:
        header_value.extend(data[start:end])

    def on_header_end() -> None:
        current.headers[header_field.decode("latin-1").lower()] = header_value.decode("utf-8")
        header_field.clear()
        header_value.clear()

    def on_headers_finished() -> None:
        _, disp = parse_options_header(current.headers.get("content-disposition", ""))
        current.name = disp.get(b"name", b"").decode("utf-8")
        if b"filename" in disp:
            current.filename = Path(disp[b"filename"].decode("utf-8")).name
        if current.filename is None or current.name not in allowed_fields:
            return
        if not current.filename:
            return  # empty file input: the browser sends a part with filename=""
        suffix = check_extension(current.filename)
        current.path = directory / f"incoming-{current.name}{suffix}"
        current.fh = current.path.open("wb")
        written.append(current.path)

    def on_part_data(data: bytes, start: int, end: int) -> None:
        chunk = data[start:end]
        if current.fh is not None:
            current.size += len(chunk)
            if current.size > max_file_bytes:
                raise UploadError(
                    f"{current.filename}: larger than the "
                    f"{max_file_bytes // (1024 * 1024)} MB upload limit",
                    status_code=413,
                )
            current.fh.write(chunk)
        elif current.filename is None:
            current.data.extend(chunk)
            if len(current.data) > MAX_FIELD_BYTES:
                raise UploadError("form field too large", status_code=413)

    def on_part_end() -> None:
        if current.fh is not None:
            current.fh.close()
            assert current.path is not None and current.filename is not None
            if current.size == 0:
                raise UploadError(f"{current.filename}: the file is empty")
            files[current.name] = ReceivedFile(
                current.name, current.filename, current.path, current.size
            )
        elif current.filename is None and current.name:
            fields[current.name] = current.data.decode("utf-8", errors="replace")

    callbacks = {
        "on_part_begin": on_part_begin,
        "on_part_data": on_part_data,
        "on_part_end": on_part_end,
        "on_header_field": on_header_field,
        "on_header_value": on_header_value,
        "on_header_end": on_header_end,
        "on_headers_finished": on_headers_finished,
    }
    parser = python_multipart.MultipartParser(params[b"boundary"], callbacks)
    try:
        async for chunk in stream:
            parser.write(chunk)
        parser.finalize()
    except UploadError as exc:
        error.append(exc)
    except Exception as exc:  # malformed multipart body
        error.append(UploadError(f"could not read the upload ({type(exc).__name__})"))
    finally:
        if current.fh is not None and not current.fh.closed:
            current.fh.close()
    if error:
        for path in written:
            path.unlink(missing_ok=True)
        raise error[0]
    return files, fields
