"""Out-of-process Playwright runtime probe.

WHY A SEPARATE PROCESS: the in-server verifier runs under uvicorn's Windows
SelectorEventLoop, which CANNOT spawn the Chromium subprocess (raises a bare
`NotImplementedError`). A fresh interpreter started via `subprocess.Popen` gets
the default ProactorEventLoop (Windows) under `asyncio.run`, where async
Playwright launches Chromium fine — and because it's Popen (not an asyncio
subprocess), the parent's event-loop policy is irrelevant.

This script is launched BY FILE PATH so it works no matter how the parent
package was imported (`backend.src.ai...` in the server, `src.ai...` in tests):
Python puts this file's directory on `sys.path[0]`, so `import probe_shared`
resolves the sibling module.

Usage:   python runtime_probe_child.py <url> <a11y:0|1>
Stdout:  exactly one JSON line of RAW observations (the parent turns these into
         VerifyError objects, keeping all error-shaping logic in one place):
  {"mounted": bool, "page_errors": [str], "console_errors": [str],
   "failed_requests": [str], "a11y_raw": [obj], "probe_crash": str|null}
"""
from __future__ import annotations

import asyncio
import json
import sys

import probe_shared  # sibling module; sys.path[0] is this file's directory


async def _probe(url: str, run_a11y: bool) -> dict:
    out: dict = {
        "mounted": False,
        "page_errors": [],
        "console_errors": [],
        "failed_requests": [],
        "a11y_raw": [],
        "probe_crash": None,
    }

    try:
        from playwright.async_api import async_playwright
    except Exception as e:  # Playwright not installed
        out["probe_crash"] = f"playwright not installed: {e}"
        return out

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await (await browser.new_context()).new_page()

                console_errors: list[str] = []
                page_errors: list[str] = []
                failed_requests: list[str] = []

                # Wire listeners BEFORE navigating so we don't miss errors thrown
                # during initial script execution.
                def on_console(msg):
                    if msg.type == "error":
                        console_errors.append(msg.text)

                def on_pageerror(err):
                    page_errors.append(str(err))

                def on_requestfailed(req):
                    if req.url.startswith(url):
                        if "favicon" in req.url.lower():
                            return
                        failed_requests.append(f"{req.method} {req.url} — {req.failure}")

                page.on("console", on_console)
                page.on("pageerror", on_pageerror)
                page.on("requestfailed", on_requestfailed)

                try:
                    await page.goto(url, wait_until="networkidle", timeout=15000)
                except Exception as e:
                    page_errors.append(f"page.goto failed: {e}")

                # Wait for #root to actually have content. If mount throws, this
                # never resolves and we report a blank page (parent synthesizes it).
                mounted = False
                try:
                    await page.wait_for_function(
                        """() => {
                            const root = document.getElementById('root');
                            return root && root.children.length > 0;
                        }""",
                        timeout=probe_shared.MOUNT_TIMEOUT_MS,
                    )
                    mounted = True
                except Exception:
                    pass

                # Accessibility audit — only when requested and the app mounted
                # (no point auditing a blank page).
                if run_a11y and mounted:
                    try:
                        out["a11y_raw"] = await page.evaluate(probe_shared.A11Y_AUDIT_JS) or []
                    except Exception:
                        pass

                out["mounted"] = mounted
                out["page_errors"] = page_errors
                out["console_errors"] = console_errors
                out["failed_requests"] = failed_requests
            finally:
                await browser.close()
    except Exception as e:
        # The probe itself failed to RUN (launch / teardown / timeout) —
        # infrastructure, NOT an app bug. Some exceptions str() to "", so keep
        # the type name for diagnosability.
        detail = str(e).strip()
        out["probe_crash"] = f"{type(e).__name__}: {detail}" if detail else type(e).__name__

    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(json.dumps({"probe_crash": "usage: runtime_probe_child.py <url> <a11y:0|1>"}))
        return 2
    url = sys.argv[1]
    run_a11y = len(sys.argv) > 2 and sys.argv[2] == "1"
    try:
        result = asyncio.run(_probe(url, run_a11y))
    except Exception as e:  # pragma: no cover - last-resort guard
        result = {"probe_crash": f"{type(e).__name__}: {e}"}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
