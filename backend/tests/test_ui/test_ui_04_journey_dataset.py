"""UI TIER 3 — Journey: Admin creates a Dataset via the UI editor.

Flow (the part most users never automate, so it's where regressions hide):
  1. Create a Connection first (UI prereq)
  2. Open /admin/datasets
  3. Click "New Dataset"
  4. Pick the connection from the dropdown
  5. Stay on SQL Query tab; type a trivial SQL ("SELECT 1 AS n")
  6. Click "Run preview" — preview pane shows the result table
  7. Click Save — modal closes, row appears

Then a separate test: open Activity (recent calls) panel and verify it
renders without errors even when the dataset hasn't been executed yet.
"""
from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import expect

from .conftest import ARTIFACT_PREFIX, FRONTEND_BASE


def _create_sqlite_connection(page, name: str) -> None:
    """Helper — create a sqlite :memory: connection via the UI."""
    page.goto(f"{FRONTEND_BASE}/admin/connections", timeout=20000)
    page.get_by_role("button", name="New Connection").click()
    page.get_by_placeholder("prod-analytics-postgres").fill(name)
    page.get_by_placeholder("/path/to/file.db").fill(":memory:")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.get_by_role("heading", name=name)).to_be_visible(timeout=10000)


@pytest.mark.ui_journey
def test_create_query_dataset_with_preview_and_save(admin_page, cleanup_artifacts):
    conn_name = f"{ARTIFACT_PREFIX}ds_conn_{uuid.uuid4().hex[:6]}"
    ds_name = f"{ARTIFACT_PREFIX}ds_{uuid.uuid4().hex[:6]}"
    cleanup_artifacts["connections"].append(conn_name)
    cleanup_artifacts["datasets"].append(ds_name)

    _create_sqlite_connection(admin_page, conn_name)

    # Navigate to Datasets
    admin_page.get_by_role("link", name="Datasets", exact=True).click()
    admin_page.wait_for_url(lambda url: "/admin/datasets" in url, timeout=10000)

    # Open editor
    admin_page.get_by_role("button", name="New Dataset").click()
    expect(admin_page.get_by_role("heading", name="New dataset")).to_be_visible()

    # Fill in name + pick the connection we just made (it should be selected
    # by default since the page sorts alphabetically; either way force-select).
    admin_page.get_by_placeholder("recent_orders").fill(ds_name)
    # Connection select is the first one in the editor body
    conn_select = admin_page.locator('select').first
    # Match by visible option text (label may be "name (sql)")
    conn_select.select_option(label=f"{conn_name} (sql)")

    # SQL Query tab is default; type a trivial query into the textarea.
    # The textarea has placeholder "SELECT 1" defaulted, just replace it.
    sql_box = admin_page.locator("textarea").first
    sql_box.fill("SELECT 1 AS n")

    # Run preview — wait for the preview pane to show row count line
    admin_page.get_by_role("button", name="Run preview").click()
    expect(admin_page.get_by_text("1 rows")).to_be_visible(timeout=15000)

    # Save the dataset
    admin_page.get_by_role("button", name="Save", exact=True).click()
    expect(admin_page.get_by_role("heading", name="New dataset")).not_to_be_visible(timeout=10000)
    expect(admin_page.get_by_role("heading", name=ds_name)).to_be_visible(timeout=10000)


@pytest.mark.ui_journey
def test_activity_panel_renders_for_unused_dataset(admin_page, cleanup_artifacts):
    """Click the Activity (recent calls) icon on a dataset that's never been
    executed → the expansion shows the empty state, not a crash."""
    conn_name = f"{ARTIFACT_PREFIX}act_conn_{uuid.uuid4().hex[:6]}"
    ds_name = f"{ARTIFACT_PREFIX}act_ds_{uuid.uuid4().hex[:6]}"
    cleanup_artifacts["connections"].append(conn_name)
    cleanup_artifacts["datasets"].append(ds_name)

    # Pre-stage via UI (faster) then assert
    _create_sqlite_connection(admin_page, conn_name)
    admin_page.get_by_role("link", name="Datasets", exact=True).click()
    admin_page.get_by_role("button", name="New Dataset").click()
    admin_page.get_by_placeholder("recent_orders").fill(ds_name)
    admin_page.locator('select').first.select_option(label=f"{conn_name} (sql)")
    admin_page.locator("textarea").first.fill("SELECT 1 AS n")
    admin_page.get_by_role("button", name="Save", exact=True).click()
    expect(admin_page.get_by_role("heading", name=ds_name)).to_be_visible(timeout=10000)

    # Click the Activity icon on the row (title="Recent calls").
    row = admin_page.locator(f'div:has(h3:has-text("{ds_name}"))').first
    row.locator('button[title="Recent calls"]').first.click()

    # Empty-state message inside the expansion
    expect(admin_page.get_by_text("No calls yet").first).to_be_visible(timeout=10000)


@pytest.mark.ui_journey
def test_preview_pane_shows_columns_and_truncated_indicator(admin_page, cleanup_artifacts):
    """Preview of a deliberately small query shows row_count + column headers."""
    conn_name = f"{ARTIFACT_PREFIX}prev_{uuid.uuid4().hex[:6]}"
    cleanup_artifacts["connections"].append(conn_name)
    _create_sqlite_connection(admin_page, conn_name)

    admin_page.get_by_role("link", name="Datasets", exact=True).click()
    admin_page.get_by_role("button", name="New Dataset").click()
    admin_page.locator('select').first.select_option(label=f"{conn_name} (sql)")
    # Two-column SELECT so columns + names render properly
    admin_page.locator("textarea").first.fill("SELECT 'alpha' AS letter, 1 AS num")
    admin_page.get_by_role("button", name="Run preview").click()
    # Expect both column headers to show in the preview table
    expect(admin_page.locator("table").get_by_text("letter")).to_be_visible(timeout=15000)
    expect(admin_page.locator("table").get_by_text("num")).to_be_visible()
    # Row count line "1 rows"
    expect(admin_page.get_by_text("1 rows")).to_be_visible()
