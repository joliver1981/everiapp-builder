"""Test the dist/index.html meta-tag injection that lets deployed apps
identify themselves to the SDK (and therefore the bug-report intake endpoint).
"""
from pathlib import Path

import pytest

from src.deployments.builder import inject_identity_meta


def _write(tmp_path: Path, html: str) -> Path:
    p = tmp_path / "index.html"
    p.write_text(html, encoding="utf-8")
    return p


def test_injects_meta_tags_into_head(tmp_path):
    path = _write(tmp_path, "<!doctype html><html><head><title>x</title></head><body></body></html>")
    inject_identity_meta(path, app_id="abc-123", version=4)
    out = path.read_text(encoding="utf-8")
    assert '<meta name="aihub-app-id" content="abc-123">' in out
    assert '<meta name="aihub-version" content="4">' in out
    # Both meta tags appear before the existing <title>
    assert out.index("aihub-app-id") < out.index("<title>")


def test_handles_head_with_attributes(tmp_path):
    path = _write(tmp_path, '<html><head data-x="y"><title>x</title></head></html>')
    inject_identity_meta(path, app_id="id-1", version=1)
    out = path.read_text(encoding="utf-8")
    # head's attributes preserved, meta tags added
    assert 'data-x="y"' in out
    assert 'aihub-app-id' in out


def test_idempotent_replaces_existing_meta(tmp_path):
    path = _write(tmp_path, "<html><head></head><body></body></html>")
    inject_identity_meta(path, app_id="id-1", version=1)
    inject_identity_meta(path, app_id="id-2", version=2)
    out = path.read_text(encoding="utf-8")
    assert out.count('aihub-app-id') == 1
    assert out.count('aihub-version') == 1
    # And the latest values won
    assert 'content="id-2"' in out
    assert 'content="2"' in out
    # The stale ones are gone
    assert 'content="id-1"' not in out


def test_missing_file_is_noop(tmp_path):
    # Should not raise even though the file doesn't exist.
    inject_identity_meta(tmp_path / "nope.html", app_id="x", version=1)


def test_escapes_special_characters_in_app_id(tmp_path):
    """Defensive — app_ids are UUIDs, but if something weird ever slipped through
    we shouldn't allow tag injection."""
    path = _write(tmp_path, "<html><head></head><body></body></html>")
    inject_identity_meta(path, app_id='"><script>x</script>', version=1)
    out = path.read_text(encoding="utf-8")
    assert '<script>' not in out
    assert '&quot;' in out or '&lt;' in out


def test_no_head_tag_uses_fallback(tmp_path):
    """Some odd builds may not have <head>; we still inject something useful."""
    path = _write(tmp_path, "<html><body>raw</body></html>")
    inject_identity_meta(path, app_id="x", version=1)
    out = path.read_text(encoding="utf-8")
    assert 'aihub-app-id' in out
