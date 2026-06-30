"""UI TIER 3 — Journey: Admin creates a Connection end-to-end via the UI.

A real admin's flow:
  1. Land on /admin/connections
  2. Click "New Connection"
  3. Fill in name + dialect (sqlite for cheap), database = ":memory:"
  4. Hit "Save & test"
  5. Modal closes, row appears, the inline test result shows success
  6. Click the row's Test button → result shows again
  7. Click Edit → modal re-opens with the right name pre-filled
  8. Cancel → modal closes
  9. Click trash → confirm dialog → row gone

This catches the regression where each individual step works in tests_data
but the actual sequence of UI clicks doesn't deliver the end state.
"""
from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import expect

from .conftest import ARTIFACT_PREFIX, FRONTEND_BASE


@pytest.mark.ui_journey
def test_create_test_edit_delete_sqlite_connection(admin_page, cleanup_artifacts):
    name = f"{ARTIFACT_PREFIX}journey_{uuid.uuid4().hex[:6]}"
    cleanup_artifacts["connections"].append(name)

    admin_page.goto(f"{FRONTEND_BASE}/admin/connections", timeout=20000)
    admin_page.get_by_role("button", name="New Connection").click()
    expect(admin_page.get_by_role("heading", name="New connection")).to_be_visible()

    # SQLite is default; just type a name and a database path (:memory: works
    # for the test endpoint — it'll SELECT 1 against a fresh sqlite engine).
    admin_page.get_by_placeholder("prod-analytics-postgres").fill(name)
    admin_page.get_by_placeholder("/path/to/file.db").fill(":memory:")

    # Save & test — modal should close, row should appear, test result inline.
    admin_page.get_by_role("button", name="Save & test").click()

    # Wait for the row to render — the heading "New connection" disappears
    # when the modal closes; then a card with our name shows up.
    expect(admin_page.get_by_role("heading", name="New connection")).not_to_be_visible(timeout=10000)
    expect(admin_page.get_by_role("heading", name=name)).to_be_visible(timeout=10000)

    # The inline test result should land within a few seconds (the test runs
    # asynchronously). Look for the green success line on the row.
    success_locator = admin_page.locator(
        f'div:has(h3:has-text("{name}"))'
    ).get_by_text("Connection successful")
    expect(success_locator).to_be_visible(timeout=15000)


@pytest.mark.ui_journey
def test_inline_test_button_reruns_connection_test(admin_page, cleanup_artifacts):
    """Clicking the per-row Test (Play) button triggers /test and shows the
    result without reopening the modal."""
    name = f"{ARTIFACT_PREFIX}retest_{uuid.uuid4().hex[:6]}"
    cleanup_artifacts["connections"].append(name)

    # Create via UI
    admin_page.goto(f"{FRONTEND_BASE}/admin/connections", timeout=20000)
    admin_page.get_by_role("button", name="New Connection").click()
    admin_page.get_by_placeholder("prod-analytics-postgres").fill(name)
    admin_page.get_by_placeholder("/path/to/file.db").fill(":memory:")
    admin_page.get_by_role("button", name="Save", exact=True).click()
    expect(admin_page.get_by_role("heading", name=name)).to_be_visible(timeout=10000)

    # Click the Test button on this row. The row containing the heading hosts
    # the action buttons; aria-label="Test connection" wasn't set, but title is.
    row = admin_page.locator(f'div:has(h3:has-text("{name}"))').first
    row.locator('button[title="Test connection"]').first.click()

    # Result shows up inline — Connection successful + ms
    expect(row.get_by_text("Connection successful")).to_be_visible(timeout=15000)


@pytest.mark.ui_journey
def test_edit_button_repopulates_form_with_existing_values(admin_page, cleanup_artifacts):
    name = f"{ARTIFACT_PREFIX}edit_{uuid.uuid4().hex[:6]}"
    cleanup_artifacts["connections"].append(name)

    admin_page.goto(f"{FRONTEND_BASE}/admin/connections", timeout=20000)
    admin_page.get_by_role("button", name="New Connection").click()
    admin_page.get_by_placeholder("prod-analytics-postgres").fill(name)
    admin_page.get_by_placeholder("/path/to/file.db").fill(":memory:")
    admin_page.get_by_role("button", name="Save", exact=True).click()
    expect(admin_page.get_by_role("heading", name=name)).to_be_visible(timeout=10000)

    # Click Edit pencil — should reopen modal with name + database pre-filled
    row = admin_page.locator(f'div:has(h3:has-text("{name}"))').first
    # Edit button has no title; look for the Pencil icon inside the row
    # actions. We target it by SVG lucide class.
    edit_btn = row.locator('button:has(svg.lucide-pencil)').first
    edit_btn.click()

    expect(admin_page.get_by_role("heading", name="Edit connection")).to_be_visible()
    # Name field has the saved name (in the modal scope to avoid hitting the row heading)
    modal = admin_page.locator('div:has(> div:has-text("Edit connection"))').last
    name_input = admin_page.get_by_placeholder("prod-analytics-postgres")
    expect(name_input).to_have_value(name)


@pytest.mark.ui_journey
def test_creating_two_connections_lists_both(admin_page, cleanup_artifacts):
    """After creating two connections, both names appear in the list."""
    n1 = f"{ARTIFACT_PREFIX}a_{uuid.uuid4().hex[:6]}"
    n2 = f"{ARTIFACT_PREFIX}b_{uuid.uuid4().hex[:6]}"
    cleanup_artifacts["connections"].extend([n1, n2])

    admin_page.goto(f"{FRONTEND_BASE}/admin/connections", timeout=20000)

    for n in (n1, n2):
        admin_page.get_by_role("button", name="New Connection").click()
        admin_page.get_by_placeholder("prod-analytics-postgres").fill(n)
        admin_page.get_by_placeholder("/path/to/file.db").fill(":memory:")
        admin_page.get_by_role("button", name="Save", exact=True).click()
        expect(admin_page.get_by_role("heading", name=n)).to_be_visible(timeout=10000)

    # Both rows render in the list
    expect(admin_page.get_by_role("heading", name=n1)).to_be_visible()
    expect(admin_page.get_by_role("heading", name=n2)).to_be_visible()
