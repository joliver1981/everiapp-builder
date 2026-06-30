"""UI TIER 1 — Smoke tests.

The "does the app even load?" canary tier. If anything here fails, none of
the higher tiers will produce useful signal. Keep them fast and surgical.

Coverage:
  1. Unauth root URL bounces to /login
  2. Login page renders the username + password fields
  3. Login with wrong creds shows an error
  4. Login with admin creds lands somewhere non-/login
  5. Admin sidebar shows Connections + Datasets entries
  6. Click Connections → URL becomes /admin/connections
  7. Click Datasets → URL becomes /admin/datasets
  8. Click Marketplace → URL becomes /marketplace
  9. Click App Builder → URL becomes /builder
  10. Logout returns to /login

These are clickthrough sentinels — assertions are pinned to URL + a single
visible string. We're NOT checking layout / styling here.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

from .conftest import FRONTEND_BASE, TEST_ADMIN_PASS, TEST_ADMIN_USER


@pytest.mark.ui_smoke
def test_unauthed_root_bounces_to_login(fresh_page):
    fresh_page.goto(FRONTEND_BASE, timeout=20000)
    fresh_page.wait_for_url(lambda url: "/login" in url, timeout=10000)
    assert "/login" in fresh_page.url


@pytest.mark.ui_smoke
def test_login_form_has_required_fields(fresh_page):
    fresh_page.goto(f"{FRONTEND_BASE}/login", timeout=20000)
    expect(fresh_page.locator("#username")).to_be_visible()
    expect(fresh_page.locator("#password")).to_be_visible()
    expect(fresh_page.locator('button[type="submit"]')).to_be_visible()


@pytest.mark.ui_smoke
def test_login_with_wrong_password_shows_error(fresh_page):
    fresh_page.goto(f"{FRONTEND_BASE}/login", timeout=20000)
    fresh_page.fill("#username", TEST_ADMIN_USER)
    fresh_page.fill("#password", "definitely-not-the-password")
    fresh_page.click('button[type="submit"]')
    # Error region in LoginPage uses .text-destructive — wait for any visible
    # text to appear in the error band.
    fresh_page.wait_for_selector(".text-destructive", timeout=10000)
    err_text = fresh_page.locator(".text-destructive").first.inner_text().strip()
    assert err_text, "error region rendered but empty"
    # Must NOT have navigated to home
    assert "/login" in fresh_page.url


@pytest.mark.ui_smoke
def test_login_with_admin_creds_lands_on_dashboard(fresh_page):
    fresh_page.goto(f"{FRONTEND_BASE}/login", timeout=20000)
    fresh_page.fill("#username", TEST_ADMIN_USER)
    fresh_page.fill("#password", TEST_ADMIN_PASS)
    fresh_page.click('button[type="submit"]')
    fresh_page.wait_for_url(lambda url: "/login" not in url, timeout=15000)
    # AIHub sidebar header is always present after login
    expect(fresh_page.locator("text=EveriApp").first).to_be_visible()


@pytest.mark.ui_smoke
def test_admin_sidebar_includes_data_section_entries(admin_page):
    admin_page.goto(FRONTEND_BASE, timeout=20000)
    # Both new sidebar entries must be present for admin role
    expect(admin_page.get_by_role("link", name="Connections", exact=True)).to_be_visible()
    expect(admin_page.get_by_role("link", name="Datasets", exact=True)).to_be_visible()
    # Some standard admin entries
    expect(admin_page.get_by_role("link", name="Secrets", exact=True)).to_be_visible()
    expect(admin_page.get_by_role("link", name="AI Providers", exact=True)).to_be_visible()


@pytest.mark.ui_smoke
def test_clicking_connections_navigates_there(admin_page):
    admin_page.goto(FRONTEND_BASE, timeout=20000)
    admin_page.get_by_role("link", name="Connections", exact=True).click()
    admin_page.wait_for_url(lambda url: "/admin/connections" in url, timeout=10000)
    # Page header should read "Connections"
    expect(admin_page.get_by_role("heading", name="Connections")).to_be_visible()


@pytest.mark.ui_smoke
def test_clicking_datasets_navigates_there(admin_page):
    admin_page.goto(FRONTEND_BASE, timeout=20000)
    admin_page.get_by_role("link", name="Datasets", exact=True).click()
    admin_page.wait_for_url(lambda url: "/admin/datasets" in url, timeout=10000)
    expect(admin_page.get_by_role("heading", name="Datasets")).to_be_visible()


@pytest.mark.ui_smoke
def test_clicking_marketplace_navigates_there(admin_page):
    admin_page.goto(FRONTEND_BASE, timeout=20000)
    admin_page.get_by_role("link", name="Marketplace").click()
    admin_page.wait_for_url(lambda url: "/marketplace" in url, timeout=10000)


@pytest.mark.ui_smoke
def test_clicking_app_builder_navigates_there(admin_page):
    admin_page.goto(FRONTEND_BASE, timeout=20000)
    admin_page.get_by_role("link", name="App Builder").click()
    admin_page.wait_for_url(lambda url: "/builder" in url, timeout=10000)


@pytest.mark.ui_smoke
def test_logout_returns_to_login(admin_page):
    admin_page.goto(FRONTEND_BASE, timeout=20000)
    # Logout button is in the sidebar footer, title="Sign out"
    admin_page.locator('button[title="Sign out"]').click()
    admin_page.wait_for_url(lambda url: "/login" in url, timeout=10000)
    # Sidebar should be gone now that we're unauthed
    expect(admin_page.locator("#username")).to_be_visible()


@pytest.mark.ui_smoke
def test_no_console_errors_on_dashboard(admin_page):
    """Sentinel for a typical class of JS errors that the user wouldn't see
    immediately but would be a sign of a broken build."""
    errors: list[str] = []

    def _collect(msg):
        if msg.type == "error":
            errors.append(msg.text)

    admin_page.on("console", _collect)
    admin_page.goto(FRONTEND_BASE, timeout=20000)
    admin_page.wait_for_load_state("networkidle", timeout=15000)

    # Vite's HMR ping connections can spew benign Network errors — filter those.
    real_errors = [
        e for e in errors
        if not any(skip in e for skip in (
            "Failed to load resource",  # 404 fetches in dev
            "[vite]",                    # HMR noise
            "WebSocket",                 # dev-server probes
        ))
    ]
    assert not real_errors, (
        f"unexpected console errors on dashboard: {real_errors[:3]}"
    )
