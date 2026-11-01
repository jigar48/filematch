"""Command-line entry point: `filematch [--port N]`."""

from __future__ import annotations

import argparse

import uvicorn

from filematch import __version__
from filematch.app import create_app
from filematch.config import DEFAULT_PORT, Settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="filematch",
        description="Compare legacy vs target Excel/CSV files in your browser, locally.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="port (default 8765)")
    parser.add_argument("--version", action="version", version=f"filematch {__version__}")
    args = parser.parse_args(argv)

    # The bind host is deliberately not a CLI flag: it comes only from FILEMATCH_HOST.
    settings = Settings.from_env()
    print(f"filematch {__version__} running at http://127.0.0.1:{args.port}/", flush=True)
    if settings.host != "127.0.0.1":
        print(f"(bound to {settings.host} via FILEMATCH_HOST)", flush=True)
    # Single worker: session state lives in this process.
    uvicorn.run(create_app(settings), host=settings.host, port=args.port, workers=1)
