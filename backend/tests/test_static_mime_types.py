"""The SPA black-page regression: Windows-registry MIME pollution.

A real v0.17.0 install served ``index-*.js`` as ``text/plain`` because the
host's HKCR ``.js`` mapping was polluted; browsers refuse to execute a
``<script type="module">`` with a non-JavaScript MIME type, so the SPA
rendered as a black empty page (HTML+CSS applied, bundle delivered but never
run, zero /api calls). ``src.main._force_web_mime_types`` pins web MIME types
at import so the host registry can never decide what our assets are served as.

Production ordering guarantee under test: mimetypes' lazy ``init()`` (which
reads the registry on Windows) runs BEFORE our ``add_type`` calls, and
``add_type`` last-wins per extension — so we pollute first, re-apply, and
assert our values took precedence, exactly mirroring a boot on a bad machine.
"""
from __future__ import annotations

import mimetypes
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_static_mime.db"
if _DB.exists():
    _DB.unlink()
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_DB}")
os.environ.setdefault("APP_DATA_DIR", str(_TMP / "apps_static_mime"))
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "static-mime-test")

from src.main import _force_web_mime_types, app  # noqa: E402

_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def _pollute() -> None:
    """Simulate a machine whose registry maps web extensions to text/plain."""
    for ext in (".js", ".mjs", ".css", ".svg"):
        mimetypes.add_type("text/plain", ext)


def test_pinned_types_win_over_polluted_registry():
    _pollute()
    _force_web_mime_types()
    assert mimetypes.guess_type("index-xQbK5N10.js")[0] == "text/javascript"
    assert mimetypes.guess_type("chunk.mjs")[0] == "text/javascript"
    assert mimetypes.guess_type("index.css")[0] == "text/css"
    assert mimetypes.guess_type("favicon.svg")[0] == "image/svg+xml"
    assert mimetypes.guess_type("app.wasm")[0] == "application/wasm"


@pytest.mark.skipif(
    not (_DIST / "index.html").is_file(), reason="frontend/dist not built"
)
def test_spa_js_asset_served_with_javascript_mime_via_http():
    """The real serving path (StaticFiles mount) must emit a JS MIME type even
    after registry-style pollution — this is the exact request the customer's
    browser refused."""
    js_assets = sorted((_DIST / "assets").glob("index-*.js"))
    assert js_assets, "built dist has no index-*.js entry chunk"
    _pollute()
    _force_web_mime_types()
    # Deliberately NOT a `with` block: static serving needs no lifespan, and
    # lifespan shutdown has a latent nondeterministic hang (asyncio
    # _cancel_all_tasks waits forever with an aiosqlite worker alive) that
    # wedged this test twice — tracked separately; don't roll those dice here.
    client = TestClient(app)
    r = client.get(f"/assets/{js_assets[-1].name}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/javascript"), (
        f"module script would be refused by browsers: {r.headers['content-type']}"
    )
    # Catch-all real-file branch.
    r2 = client.get("/index.html")
    assert r2.status_code == 200
    assert r2.headers["content-type"].startswith("text/html")
    # SPA fallback for client-side routes stays index.html.
    r3 = client.get("/admin/python-packages")
    assert r3.status_code == 200
    assert r3.headers["content-type"].startswith("text/html")


@pytest.mark.skipif(
    not (_DIST / "favicon.svg").is_file(), reason="frontend/dist not built"
)
def test_favicon_served_as_svg_via_http():
    """/favicon.svg must be the real brand icon with an SVG MIME type — before
    frontend/public/favicon.svg existed, index.html pointed at /vite.svg, which
    fell through the catch-all to index.html, so installs had no favicon."""
    _pollute()
    _force_web_mime_types()
    client = TestClient(app)  # no lifespan needed; see hang note above
    r = client.get("/favicon.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert "<svg" in r.text
    assert 'id="root"' not in r.text, "got the SPA index fallback, not the icon"
