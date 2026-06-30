"""Tier-1 UI: the admin Platform page tabs actually load.

This is the regression guard for the bug report: "Most of the tabs under
Platform forever say Loading…". A human-like Chromium session logs in as admin,
opens /admin/platform, clicks EVERY tab, and for each asserts:

  1. it does NOT stay stuck on "Loading…" (the reported symptom),
  2. no admin API call it made returned 4xx/5xx (catches a missing/!mounted
     route — exactly what a stale backend produced),
  3. a tab-specific sentinel piece of content is visible (real data rendered),
  4. no uncaught page error was thrown.

All tab failures are collected and reported together, so one run shows every
broken tab rather than stopping at the first.

Run (with the dev stack up):
  set AIHUB_RUN_UI_TESTS=1
  cd backend && ../.venv/Scripts/python.exe -m pytest tests/test_ui/test_ui_06_platform_tabs.py -v
"""
from __future__ import annotations

import time

import pytest

from .conftest import FRONTEND_BASE

pytestmark = pytest.mark.ui_smoke

# (tab button label, a sentinel string that only appears once the tab has data)
PLATFORM_TABS = [
    ("Health", "Connection health"),
    ("System", "System status"),
    ("LLM Cost", "LLM cost"),
    ("License", "Current license"),
    ("Auth Providers", "Identity providers"),
    ("Teams", "named groups"),
    ("Audit Log", "event(s)"),
    ("Backups", "Back up now"),
    ("Settings", "AI generation"),
]


def _open_platform(page):
    page.goto(f"{FRONTEND_BASE}/admin/platform", timeout=20000)
    page.wait_for_load_state("networkidle", timeout=15000)
    # The tab bar renders the "Platform" page header.
    page.wait_for_selector("text=Platform", timeout=10000)


def _loading_gone(page, timeout=12.0) -> bool:
    """Wait until no 'Loading…' placeholder remains visible in the content area."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        # The exact placeholder string the tabs render (ellipsis char).
        if page.get_by_text("Loading…", exact=False).count() == 0:
            return True
        time.sleep(0.25)
    return page.get_by_text("Loading…", exact=False).count() == 0


def test_platform_tabs_all_load(admin_page):
    page = admin_page

    # Capture any failed API call + any uncaught page error.
    api_failures: list[str] = []
    page_errors: list[str] = []
    page.on("response", lambda r: (
        api_failures.append(f"{r.status} {r.url}")
        if ("/api/" in r.url and r.status >= 400) else None
    ))
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    _open_platform(page)

    problems: list[str] = []
    for label, sentinel in PLATFORM_TABS:
        api_failures.clear()
        page_errors.clear()
        try:
            page.get_by_role("button", name=label, exact=True).first.click()
        except Exception as e:
            problems.append(f"[{label}] could not click tab: {e}")
            continue

        # Let the tab fetch + render.
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        # 1. Not stuck on "Loading…"
        if not _loading_gone(page):
            problems.append(f"[{label}] STUCK on 'Loading…' (the reported bug)")

        # 2. No failed admin API call while this tab loaded
        admin_fails = [f for f in api_failures if "/api/admin" in f or "/api/auth/saml" in f or "/api/auth/oidc" in f]
        if admin_fails:
            problems.append(f"[{label}] API failures: {admin_fails}")

        # 3. Real content rendered (sentinel) — unless an error panel is shown,
        #    which we treat as its own failure below.
        has_sentinel = page.get_by_text(sentinel, exact=False).count() > 0
        has_error_panel = page.get_by_text("Couldn't load this section", exact=False).count() > 0
        if has_error_panel:
            problems.append(f"[{label}] shows the error panel (endpoint failed)")
        elif not has_sentinel:
            problems.append(f"[{label}] sentinel '{sentinel}' not found (tab did not render real content)")

        # 4. No uncaught JS error
        if page_errors:
            problems.append(f"[{label}] page errors: {page_errors}")

    assert not problems, "Platform tab failures:\n  - " + "\n  - ".join(problems)


def test_platform_first_tab_has_no_console_404(admin_page):
    """A focused, fast check: opening Platform (Health tab) makes a successful
    admin call — i.e. the backend running has the route (not a stale build)."""
    page = admin_page
    statuses: list[int] = []
    page.on("response", lambda r: statuses.append(r.status) if "/api/admin/connections/health/all" in r.url else None)
    _open_platform(page)
    page.wait_for_timeout(2500)
    assert statuses, "Health tab never called /api/admin/connections/health/all"
    assert all(s < 400 for s in statuses), f"health/all returned {statuses} (stale/missing route?)"
