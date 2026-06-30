"""Fixtures for the UI test tiers.

Drives a real headless Chromium against the live AIHub stack:
  - Backend:   http://localhost:8800  (admin/password / developer/password / user/password)
  - Frontend:  http://localhost:5173  (Vite dev server, proxies /api to backend)

Skips the entire suite if either service is unreachable. To run:

  start.bat                                         # spin up both services
  cd backend && ../.venv/Scripts/python.exe -m pytest tests/test_ui/ -v

Conventions:
  - Every artifact created by a UI test starts with `UI_TEST_` so the
    module-scoped cleanup fixture can find it via API after a crash.
  - Login happens ONCE per session; we reuse storage_state across all tests
    so we're not hammering /api/auth/login.
  - Each test gets a fresh BrowserContext (cookies + localStorage isolated).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

# Defer playwright import — pytest_playwright is optional; if it's not there,
# this conftest still loads and only the actual test files would fail.
try:
    from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


FRONTEND_BASE = os.environ.get("AIHUB_FRONTEND_BASE", "http://localhost:5173")
BACKEND_BASE = os.environ.get("AIHUB_BACKEND_BASE", "http://localhost:8800")
TEST_ADMIN_USER = os.environ.get("UI_TEST_USER", "admin")
TEST_ADMIN_PASS = os.environ.get("UI_TEST_PASS", "password")
TEST_DEV_USER = os.environ.get("UI_DEV_USER", "developer")
TEST_DEV_PASS = os.environ.get("UI_DEV_PASS", "password")

# Every artifact a UI test creates starts with this prefix → cleanup sweep
# can find what to delete even after a crash.
ARTIFACT_PREFIX = "UI_TEST_"

# Standard viewport. 1366x768 is the desktop median.
VIEWPORT = {"width": 1366, "height": 768}


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------
def pytest_configure(config):
    config.addinivalue_line("markers", "ui_smoke: tier 1 - basic page-loads-and-renders sentinel")
    config.addinivalue_line("markers", "ui_form: tier 2 - form interactions and modal behavior")
    config.addinivalue_line("markers", "ui_journey: tier 3 - multi-step end-to-end user flows")
    config.addinivalue_line("markers", "ui_competency: tier 4 - adversarial / edge-case real-user behavior")


# ---------------------------------------------------------------------------
# Opt-in gate
# ---------------------------------------------------------------------------
# UI tests boot a real Chromium and hit live dev servers — they take ~2 min and
# don't fit the project's green-gate budget (180s for ALL backend tests). They
# also require both backend (:8800) and frontend (:5173) to be running, which
# isn't typical in CI. Skip them by default; opt in with AIHUB_RUN_UI_TESTS=1.
#
# To run:
#   set AIHUB_RUN_UI_TESTS=1
#   cd backend && ../.venv/Scripts/python.exe -m pytest tests/test_ui/ -v
RUN_UI_TESTS = os.environ.get("AIHUB_RUN_UI_TESTS", "").strip() in ("1", "true", "yes")


def pytest_collection_modifyitems(config, items):
    """Skip every test under tests/test_ui/ unless AIHUB_RUN_UI_TESTS is set."""
    if RUN_UI_TESTS:
        return
    skip_marker = pytest.mark.skip(
        reason="UI tests are opt-in — set AIHUB_RUN_UI_TESTS=1 to run them"
    )
    for item in items:
        if "test_ui" in str(item.fspath):
            item.add_marker(skip_marker)


# ---------------------------------------------------------------------------
# Service readiness — skip the whole suite if not running
# ---------------------------------------------------------------------------
def _services_up() -> tuple[bool, str]:
    """Probe both services. Returns (ok, reason_if_not)."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{BACKEND_BASE}/api/health", timeout=3) as r:
            if r.status >= 500:
                return False, f"backend {BACKEND_BASE} returned {r.status}"
    except (urllib.error.URLError, OSError) as e:
        return False, f"backend {BACKEND_BASE} unreachable: {e}"
    try:
        with urllib.request.urlopen(FRONTEND_BASE, timeout=3) as r:
            if r.status >= 500:
                return False, f"frontend {FRONTEND_BASE} returned {r.status}"
    except (urllib.error.URLError, OSError) as e:
        return False, f"frontend {FRONTEND_BASE} unreachable: {e}"
    return True, ""


@pytest.fixture(scope="session")
def services_ready():
    if not PLAYWRIGHT_AVAILABLE:
        pytest.skip("playwright is not installed (pip install pytest-playwright + playwright install chromium)")
    ok, reason = _services_up()
    if not ok:
        pytest.skip(f"UI tests require both services running. {reason}. Run start.bat first.")
    return True


# ---------------------------------------------------------------------------
# Playwright lifecycle
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def _playwright_instance(services_ready):
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def browser(_playwright_instance) -> "Browser":
    """One headless chromium for the whole session.
    Set AIHUB_UI_HEADED=1 to watch the tests run in a visible browser.
    """
    headless = os.environ.get("AIHUB_UI_HEADED", "").strip() not in ("1", "true", "yes")
    b = _playwright_instance.chromium.launch(headless=headless)
    yield b
    b.close()


# ---------------------------------------------------------------------------
# Login helpers — reused across fixtures
# ---------------------------------------------------------------------------
def _login_page(page: "Page", username: str, password: str) -> bool:
    """Drive the LoginPage to authenticate. Returns True on success."""
    try:
        page.goto(f"{FRONTEND_BASE}/login", timeout=20000)
        page.wait_for_load_state("networkidle", timeout=15000)
        # If we're already authed and the SPA bounced us back to / before we
        # got here, that counts as success.
        if "/login" not in page.url:
            return True
        page.fill("#username", username)
        page.fill("#password", password)
        page.click('button[type="submit"]')
        # Wait for client-side navigation away from /login.
        page.wait_for_url(lambda url: "/login" not in url, timeout=15000)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"[ui] login as {username} failed: {e}")
        return False


def _fresh_authed_page(browser: "Browser", username: str, password: str):
    """Open a fresh BrowserContext, log in via the form, return (ctx, page).

    We DON'T use Playwright's storage_state pattern here. AIHub uses a one-shot
    refresh-token rotation: the first context to use a captured refresh cookie
    consumes it, and any subsequent context loaded from the same storage_state
    finds the cookie invalid and gets bounced to /login. So we just log in
    fresh per test. ~1.5s extra per test but rock-solid auth.
    """
    ctx = browser.new_context(ignore_https_errors=True, viewport=VIEWPORT)
    page = ctx.new_page()
    page.set_default_timeout(15000)
    ok = _login_page(page, username, password)
    if not ok:
        ctx.close()
        pytest.skip(f"could not log in as {username}")
    return ctx, page


# ---------------------------------------------------------------------------
# Session-scoped tokens for HTTP setup/cleanup
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def admin_auth_state(browser, tmp_path_factory) -> str:
    """Capture a one-shot storage state ONLY so we can read cookies out for
    the requests-based http session. Not used to restore page auth (that's
    rotation-incompatible)."""
    dest = tmp_path_factory.mktemp("ui_admin_auth") / "storage.json"
    ctx = browser.new_context(ignore_https_errors=True, viewport=VIEWPORT)
    page = ctx.new_page()
    ok = _login_page(page, TEST_ADMIN_USER, TEST_ADMIN_PASS)
    if not ok:
        ctx.close()
        pytest.skip(f"could not log in as {TEST_ADMIN_USER} — is the backend seeded?")
    ctx.storage_state(path=str(dest))
    ctx.close()
    return str(dest)


# ---------------------------------------------------------------------------
# Per-test pages — fresh login each time (auth is one-shot)
# ---------------------------------------------------------------------------
@pytest.fixture
def admin_page(browser):
    """Fresh BrowserContext + fresh form-login per test. Slow (~1.5s) but
    each test owns its own non-rotated session."""
    ctx, page = _fresh_authed_page(browser, TEST_ADMIN_USER, TEST_ADMIN_PASS)
    yield page
    try:
        ctx.close()
    except Exception:
        pass


@pytest.fixture
def developer_page(browser):
    ctx, page = _fresh_authed_page(browser, TEST_DEV_USER, TEST_DEV_PASS)
    yield page
    try:
        ctx.close()
    except Exception:
        pass


@pytest.fixture
def fresh_page(browser):
    """An UNAUTHENTICATED browser context. Used by login + logout tests."""
    ctx = browser.new_context(
        ignore_https_errors=True,
        viewport=VIEWPORT,
    )
    page = ctx.new_page()
    page.set_default_timeout(15000)
    yield page
    try:
        ctx.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# HTTP session that mirrors the same admin cookies (for setup + sweep)
# ---------------------------------------------------------------------------
def _build_http_session(auth_state_path: str):
    """Build a requests.Session that uses the same JWT the browser uses."""
    import json
    import urllib.request

    # AIHub returns the JWT in the body; the storage_state from Playwright stores
    # whatever the SPA put in localStorage. We log in fresh against the backend
    # to get a clean Bearer token, mirroring what apiClient.setToken stores.
    payload = json.dumps({"username": TEST_ADMIN_USER, "password": TEST_ADMIN_PASS}).encode("utf-8")
    req = urllib.request.Request(
        f"{BACKEND_BASE}/api/auth/login",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = json.loads(resp.read())
    return body["access_token"]


@pytest.fixture(scope="session")
def admin_http_token(admin_auth_state) -> str:
    """JWT bearer token for the admin user — used for setup + cleanup over HTTP."""
    return _build_http_session(admin_auth_state)


def _api_get(token: str, path: str) -> tuple[int, dict | list | None]:
    import json
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        f"{BACKEND_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None


def _api_delete(token: str, path: str) -> int:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        f"{BACKEND_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


# ---------------------------------------------------------------------------
# Module-scoped cleanup — wipe anything prefixed UI_TEST_ at teardown
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def cleanup_artifacts(admin_http_token):
    """Yields a dict the tests can populate; on teardown, performs a final
    prefix-scan sweep regardless of what the dict contains. That's the
    safety net — a test that crashes mid-flow still gets its data cleared."""
    created = {"connections": [], "datasets": []}
    yield created

    # Datasets first (FK from binding tables references them)
    _, ds_body = _api_get(admin_http_token, "/api/admin/datasets")
    if isinstance(ds_body, list):
        for d in ds_body:
            if (d.get("name") or "").startswith(ARTIFACT_PREFIX):
                _api_delete(admin_http_token, f"/api/admin/datasets/{d['id']}")

    # Then connections (datasets must be gone or they'll block with 409)
    _, conn_body = _api_get(admin_http_token, "/api/admin/connections")
    if isinstance(conn_body, list):
        for c in conn_body:
            if (c.get("name") or "").startswith(ARTIFACT_PREFIX):
                _api_delete(admin_http_token, f"/api/admin/connections/{c['id']}")


# ---------------------------------------------------------------------------
# Small wait helpers
# ---------------------------------------------------------------------------
def wait_until(predicate, timeout: float = 15.0, interval: float = 0.25, desc: str = "condition") -> bool:
    """Poll predicate() until True or timeout. Returns whether it ever became True."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False
