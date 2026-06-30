"""Out-of-process Playwright screenshot capture for marketplace publishing.

WHY A SEPARATE PROCESS: same constraint as ai/runtime_probe_child.py — the
server runs under uvicorn's Windows SelectorEventLoop, which cannot spawn the
Chromium subprocess. A fresh interpreter under asyncio.run gets a Proactor
loop where Playwright works.

Usage:   python screenshot_child.py <url> <out_dir>
Stdout:  one JSON line: {"files": ["<abs path>", ...], "error": str|null}
Captures a desktop (1280x800) and a narrow (480x800) screenshot.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

VIEWPORTS = [
    ("desktop", {"width": 1280, "height": 800}),
    ("narrow", {"width": 480, "height": 800}),
]


async def _capture(url: str, out_dir: Path) -> dict:
    out: dict = {"files": [], "error": None}
    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        out["error"] = f"playwright not installed: {e}"
        return out

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await (await browser.new_context()).new_page()
                await page.goto(url, wait_until="networkidle", timeout=60_000)
                await page.wait_for_timeout(1500)  # fonts/transitions settle
                for name, viewport in VIEWPORTS:
                    await page.set_viewport_size(viewport)
                    await page.wait_for_timeout(400)
                    path = out_dir / f"shot-{name}.png"
                    await page.screenshot(path=str(path))
                    out["files"].append(str(path))
            finally:
                await browser.close()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


if __name__ == "__main__":
    url, out_dir = sys.argv[1], Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(_capture(url, out_dir))
    print(json.dumps(result))
