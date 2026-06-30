"""Tier 3 journey — App Builder Data panel + Attach flow (real browser).

Directly covers the "Unauthorized in the Data panel" report: with a VALID
session the Data panel must load its bound-datasets view (empty state or a
list) and the Attach sheet must open the discoverable list — NEVER the
"Unauthorized"/error box. This is the regression guard for a broken auth/route
on GET /api/apps/{id}/datasets, and confirms the panel itself is healthy (the
original report was an expired session, not a panel bug).

Opt-in: set AIHUB_RUN_UI_TESTS=1 and have the stack running (start.bat).

Generous timeouts on purpose: the first browser navigation pays Vite's
on-demand compile, which can take a while on a loaded dev box.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from .conftest import ARTIFACT_PREFIX, BACKEND_BASE, FRONTEND_BASE

pytestmark = pytest.mark.ui_journey

NAV_TIMEOUT = 60000      # cold Vite compile + loaded machine
SELECTOR_TIMEOUT = 40000


def _api_post(token: str, path: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{BACKEND_BASE}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, json.loads(r.read())


def _api_delete(token: str, path: str) -> int:
    req = urllib.request.Request(
        f"{BACKEND_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.fixture
def ui_app(admin_http_token):
    """A throwaway app created via API; deleted on teardown (best-effort)."""
    status, body = _api_post(
        admin_http_token, "/api/apps",
        {"name": f"{ARTIFACT_PREFIX}DataFlow", "description": "ui e2e data panel"},
    )
    assert status in (200, 201), f"could not create test app: HTTP {status}"
    app_id = body["id"]
    yield app_id
    _api_delete(admin_http_token, f"/api/apps/{app_id}")


def _no_error_box(page) -> bool:
    """The Data panel / Attach sheet render errors in a red box (text-red-400).
    A healthy authed session has none."""
    return page.locator("div.text-red-400").count() == 0


def test_builder_data_flow_authed_no_unauthorized(admin_page, ui_app):
    page = admin_page
    page.set_default_timeout(SELECTOR_TIMEOUT)

    # 1. Open the builder for the throwaway app.
    page.goto(f"{FRONTEND_BASE}/builder/{ui_app}", timeout=NAV_TIMEOUT)
    page.wait_for_selector('button[title="Data sources"]', timeout=NAV_TIMEOUT)

    # 2. Toggle the Data panel open → it fires GET /api/apps/{id}/datasets.
    page.click('button[title="Data sources"]')
    page.wait_for_selector('button:has-text("Attach")', timeout=SELECTOR_TIMEOUT)
    # Let the bound-datasets fetch settle (empty state, since nothing is bound).
    page.wait_for_selector('text=No datasets attached', timeout=SELECTOR_TIMEOUT)

    body = page.inner_text("body")
    assert "Unauthorized" not in body, (
        "Data panel showed 'Unauthorized' with a valid session — auth/session or "
        "GET /api/apps/{id}/datasets regression"
    )
    assert _no_error_box(page), "Data panel rendered an error box with a valid session"
    # The panel header itself is proof it rendered (subtitle mentions useDataset()).
    assert "useDataset()" in body

    # 3. Open the Attach sheet → it fires GET /api/datasets/discoverable.
    page.locator('button:has-text("Attach")').first.click()
    page.wait_for_selector('text=Attach a dataset', timeout=SELECTOR_TIMEOUT)
    # The sheet's static helper line is a reliable "sheet body rendered" marker
    # regardless of whether any datasets exist to list.
    page.wait_for_selector('text=shared with the org', timeout=SELECTOR_TIMEOUT)

    body2 = page.inner_text("body")
    assert "Unauthorized" not in body2, "Attach sheet showed 'Unauthorized'"
    assert _no_error_box(page), "Attach sheet rendered an error box (discoverable fetch failed?)"
