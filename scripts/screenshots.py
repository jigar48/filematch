"""Capture README screenshots with Playwright, using the demo data.

    pip install -e ".[dev]" && python -m playwright install chromium
    python scripts/screenshots.py

Starts the app on 127.0.0.1:8766 in a background thread, drives a 1280x800
Chromium window through upload, mapping and both results tabs, and saves PNGs
to docs/screenshots/.
"""

from __future__ import annotations

import threading
import time
import urllib.request
from pathlib import Path

import uvicorn
from playwright.sync_api import sync_playwright

from filematch.app import create_app
from filematch.config import Settings

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo"
OUT = ROOT / "docs" / "screenshots"
PORT = 8766
BASE = f"http://127.0.0.1:{PORT}"


def start_server() -> uvicorn.Server:
    config = uvicorn.Config(
        create_app(Settings()), host="127.0.0.1", port=PORT, log_level="warning"
    )
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            urllib.request.urlopen(BASE + "/", timeout=1)  # noqa: S310 - local URL
            return server
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("server did not start")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    server = start_server()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(BASE + "/")
            page.set_input_files("#file_a", str(DEMO / "legacy_customers.csv"))
            page.set_input_files("#file_b", str(DEMO / "target_customers.xlsx"))
            page.set_input_files("#file_c", str(DEMO / "mapping.csv"))
            page.click("form.upload-form button[type=submit]")
            page.wait_for_selector("table.summary")
            page.locator("h2", has_text="Uploaded files").scroll_into_view_if_needed()
            page.screenshot(path=OUT / "1-upload.png")

            page.goto(BASE + "/mapping")
            # Deliberately not the defaults: trim on, case-insensitive on, tolerance 0.01,
            # empty = null off (81 not-matched rows). The README captions state this.
            page.check("input[name=trim]")
            page.check("input[name=case_insensitive]")
            page.fill("input[name=tolerance]", "0.01")
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=OUT / "2-mapping.png")

            page.click("form.mapping-form button[type=submit]")
            page.wait_for_url("**/results")
            page.screenshot(path=OUT / "3-results-all.png")

            page.goto(BASE + "/results?tab=issues")
            page.screenshot(path=OUT / "4-results-not-matched.png")
            browser.close()
    finally:
        server.should_exit = True
    for png in sorted(OUT.glob("*.png")):
        print(png.relative_to(ROOT))


if __name__ == "__main__":
    main()
