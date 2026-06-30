"""UI TIER 2 — Form interactions.

Tests modal open/close, field validation, kind tabs that toggle fields,
dialect dropdowns that show/hide host+port, and other micro-interactions
that don't yet involve hitting Save against a real DB.

These catch the "the form rendered but is unusable" bug class — disabled
Save when it should be enabled, fields stuck visible after kind change,
modal that won't close on backdrop click.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

from .conftest import ARTIFACT_PREFIX, FRONTEND_BASE


@pytest.mark.ui_form
def test_connections_new_button_opens_modal(admin_page):
    admin_page.goto(f"{FRONTEND_BASE}/admin/connections", timeout=20000)
    admin_page.get_by_role("button", name="New Connection").click()
    # Modal title appears
    expect(admin_page.get_by_role("heading", name="New connection")).to_be_visible()
    # Name input is in the modal
    expect(admin_page.get_by_placeholder("prod-analytics-postgres")).to_be_visible()


@pytest.mark.ui_form
def test_connections_modal_save_disabled_until_name_typed(admin_page):
    admin_page.goto(f"{FRONTEND_BASE}/admin/connections", timeout=20000)
    admin_page.get_by_role("button", name="New Connection").click()

    save_btn = admin_page.get_by_role("button", name="Save", exact=True)
    expect(save_btn).to_be_disabled()

    # Type a name → Save enables
    admin_page.get_by_placeholder("prod-analytics-postgres").fill(f"{ARTIFACT_PREFIX}wired")
    expect(save_btn).to_be_enabled()

    # Clear → Save disables again
    admin_page.get_by_placeholder("prod-analytics-postgres").fill("")
    expect(save_btn).to_be_disabled()


@pytest.mark.ui_form
def test_connections_kind_tabs_toggle_visible_fields(admin_page):
    """SQL tab shows dialect/database/host fields; REST tab shows base URL +
    auth-type fields. This is the kind of toggle that classically rots."""
    admin_page.goto(f"{FRONTEND_BASE}/admin/connections", timeout=20000)
    admin_page.get_by_role("button", name="New Connection").click()

    # Default is SQL — dialect dropdown is visible, REST base_url is not
    expect(admin_page.locator('select').first).to_be_visible()  # dialect select
    # SQLite is the default dialect — "Database path" label is shown
    expect(admin_page.get_by_text("Database path")).to_be_visible()
    # REST-only fields should not be in the DOM
    assert admin_page.get_by_placeholder("https://api.example.com/v1").count() == 0

    # Switch to REST
    admin_page.get_by_role("button", name="REST").click()
    # Base URL field appears, Database path field gone
    expect(admin_page.get_by_placeholder("https://api.example.com/v1")).to_be_visible()
    assert admin_page.get_by_text("Database path").count() == 0


@pytest.mark.ui_form
def test_connections_sqlite_dialect_hides_host_fields(admin_page):
    """SQLite has no host/port concept — those fields should hide; Postgres
    must show them."""
    admin_page.goto(f"{FRONTEND_BASE}/admin/connections", timeout=20000)
    admin_page.get_by_role("button", name="New Connection").click()

    # Default is SQLite — host field should not be present
    assert admin_page.get_by_placeholder("db.example.com").count() == 0

    # Switch dialect to PostgreSQL — host appears
    dialect_select = admin_page.locator('select').first
    dialect_select.select_option(value="postgres")
    expect(admin_page.get_by_placeholder("db.example.com")).to_be_visible()
    expect(admin_page.get_by_placeholder("5432")).to_be_visible()


@pytest.mark.ui_form
def test_connections_modal_cancel_closes_it(admin_page):
    admin_page.goto(f"{FRONTEND_BASE}/admin/connections", timeout=20000)
    admin_page.get_by_role("button", name="New Connection").click()
    expect(admin_page.get_by_role("heading", name="New connection")).to_be_visible()

    admin_page.get_by_role("button", name="Cancel").click()
    expect(admin_page.get_by_role("heading", name="New connection")).not_to_be_visible()


@pytest.mark.ui_form
def test_datasets_new_button_exists_and_is_wired(admin_page, cleanup_artifacts):
    """The 'New Dataset' button is always rendered. Whether it's enabled
    depends on connections existing; either way, the page must render the
    header without crashing."""
    admin_page.goto(f"{FRONTEND_BASE}/admin/datasets", timeout=20000)
    expect(admin_page.get_by_role("heading", name="Datasets")).to_be_visible()
    expect(admin_page.get_by_role("button", name="New Dataset")).to_be_visible()


@pytest.mark.ui_form
def test_datasets_modal_has_all_three_kind_tabs(admin_page, cleanup_artifacts, admin_http_token):
    """Editor shows kind tabs Query / Table-View / API Call. We have to make
    sure a connection exists first — use the API to set one up cheaply."""
    import json
    import urllib.request

    name = f"{ARTIFACT_PREFIX}smoke_conn"
    body = json.dumps({
        "name": name,
        "kind": "sql",
        "config": {"dialect": "sqlite", "database": ":memory:"},
    }).encode("utf-8")
    req = urllib.request.Request(
        "http://localhost:8800/api/admin/connections",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {admin_http_token}",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass  # if it already exists from a prior run, that's fine
    cleanup_artifacts["connections"].append(name)

    admin_page.goto(f"{FRONTEND_BASE}/admin/datasets", timeout=20000)
    new_btn = admin_page.get_by_role("button", name="New Dataset")
    expect(new_btn).to_be_enabled()
    new_btn.click()
    expect(admin_page.get_by_role("heading", name="New dataset")).to_be_visible()
    # All three kind tabs are present
    expect(admin_page.get_by_role("button", name="SQL Query")).to_be_visible()
    expect(admin_page.get_by_role("button", name="Table / View")).to_be_visible()
    expect(admin_page.get_by_role("button", name="API Call")).to_be_visible()


@pytest.mark.ui_form
def test_datasets_kind_tabs_swap_central_form(admin_page, cleanup_artifacts, admin_http_token):
    """Switching to Table/View hides the SQL textarea and shows table fields.
    Switching to API hides both and shows method/path."""
    # Make sure at least one connection exists so the New Dataset button is enabled.
    import json
    import urllib.request

    name = f"{ARTIFACT_PREFIX}swap_conn"
    cleanup_artifacts["connections"].append(name)
    body = json.dumps({
        "name": name,
        "kind": "sql",
        "config": {"dialect": "sqlite", "database": ":memory:"},
    }).encode("utf-8")
    req = urllib.request.Request(
        "http://localhost:8800/api/admin/connections",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {admin_http_token}"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass  # likely already exists from a prior test in this module

    admin_page.goto(f"{FRONTEND_BASE}/admin/datasets", timeout=20000)
    new_btn = admin_page.get_by_role("button", name="New Dataset")
    expect(new_btn).to_be_enabled(timeout=15000)
    new_btn.click()
    expect(admin_page.get_by_role("heading", name="New dataset")).to_be_visible(timeout=10000)

    # Default is SQL Query — the editor's SQL textarea is rendered.
    # Using locator("textarea").first instead of text=SQL to avoid ambiguity
    # with multiple labels that contain the word "SQL".
    expect(admin_page.locator("textarea").first).to_be_visible()

    # Table / View
    admin_page.get_by_role("button", name="Table / View").click()
    expect(admin_page.get_by_placeholder("orders").first).to_be_visible()
    expect(admin_page.get_by_placeholder("id, total, customer_id").first).to_be_visible()

    # API Call
    admin_page.get_by_role("button", name="API Call").click()
    expect(admin_page.get_by_placeholder("/customers/{{customer_id}}").first).to_be_visible()
