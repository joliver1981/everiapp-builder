"""Unit tests for the AI prompt's `available_datasets_block`.

Pure formatter — no DB / HTTP — so we can use a SimpleNamespace stand-in for
the DatasetResponse shape.
"""
from types import SimpleNamespace

from src.ai.prompts import SYSTEM_PROMPT, available_datasets_block


def _ds(**kw):
    """Build a duck-typed dataset for prompt formatting."""
    defaults = dict(
        id="ds-1",
        name="recent_orders",
        description="",
        kind="query",
        definition={},
        parameter_schema={},
        output_schema={},
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def test_returns_none_when_no_datasets():
    assert available_datasets_block([]) is None


def test_includes_id_name_and_usage():
    block = available_datasets_block([
        _ds(id="abc-123", name="recent_orders", description="Orders for a customer"),
    ])
    assert block is not None
    assert "abc-123" in block
    assert "recent_orders" in block
    assert "Orders for a customer" in block
    assert "useDataset('abc-123'" in block


def test_parameter_schema_renders_as_ts_signature():
    block = available_datasets_block([
        _ds(parameter_schema={
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["customer_id"],
        }),
    ])
    assert block is not None
    # Required param has no '?', optional one does
    assert "customer_id: string" in block
    assert "limit?: number" in block


def test_output_schema_columns_render():
    block = available_datasets_block([
        _ds(output_schema={
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "total": {"type": "number"},
                },
            },
        }),
    ])
    assert block is not None
    assert "id: string" in block
    assert "total: number" in block


def test_table_kind_falls_back_to_allowlist_when_no_schema():
    block = available_datasets_block([
        _ds(
            kind="table",
            output_schema={},
            definition={"schema": "main", "table_name": "orders", "column_allowlist": ["id", "total"]},
        ),
    ])
    assert block is not None
    assert "id: unknown" in block
    assert "total: unknown" in block


def test_mentions_current_user_injection():
    """The model should know `current_user` is auto-injected."""
    block = available_datasets_block([_ds()])
    assert block is not None
    assert "current_user" in block


def test_multiple_datasets_all_appear():
    block = available_datasets_block([
        _ds(id="ds-a", name="alpha"),
        _ds(id="ds-b", name="beta"),
        _ds(id="ds-c", name="gamma"),
    ])
    assert block is not None
    for tag in ("ds-a", "ds-b", "ds-c", "alpha", "beta", "gamma"):
        assert tag in block


def test_block_warns_about_verbatim_column_casing():
    """The model must use the listed column names as-is — DB casing isn't
    normalized. Guessing snake_case/UPPERCASE is what made generated apps fail
    their `usable` check and fall back to sample data."""
    block = available_datasets_block([_ds()])
    assert block is not None
    low = block.lower()
    assert "verbatim" in low
    assert "snake_case" in low or "uppercase" in low


def test_block_forbids_reimplementing_or_inventing_globals():
    """The block must steer the model to the real SDK hook, not a hand-written
    shim or a hallucinated `window.__AIHUB_DATASET__` bridge."""
    block = available_datasets_block([_ds()])
    assert block is not None
    low = block.lower()
    assert "never reimplement" in low
    assert "__aihub_" in low
    # Don't paper over a real dataset error with fake rows.
    assert "error.message" in block


def test_system_prompt_documents_usedataset_contract():
    """The base prompt is ALWAYS present (even with no datasets bound), so it —
    not just the dynamic block — must teach the real useDataset contract. This is
    the gap that let the model guess the shape and hand-write a `window` shim."""
    low = SYSTEM_PROMPT.lower()
    assert "usedataset" in low
    # The exact return shape, so the model doesn't guess whether `data` is wrapped.
    assert "result.rows === data" in SYSTEM_PROMPT
    # No hand-rolled hooks / invented globals.
    assert "reimplement" in low and "never" in low
    assert "__aihub_dataset__" in low
    # Column-casing fidelity + explicit value coercion.
    assert "verbatim" in low
    assert "number(row" in low
