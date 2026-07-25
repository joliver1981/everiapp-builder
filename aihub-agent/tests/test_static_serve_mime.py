"""Deployed-app static serving must not trust the host registry for MIME.

Same field bug as the platform's v0.17.1 black-page fix: a host whose HKCR
maps `.js` to text/plain makes browsers refuse the app bundle's module
script — assets 200, app never runs. The agent serves bundles on arbitrary
customer hosts, so static_serve pins web MIME types before mounting.
"""
import mimetypes

from fastapi.testclient import TestClient

from aihub_agent.static_serve import build_app, force_web_mime_types


def _pollute() -> None:
    for ext in (".js", ".mjs", ".css", ".svg"):
        mimetypes.add_type("text/plain", ext)


def test_pins_beat_polluted_registry():
    _pollute()
    force_web_mime_types()
    assert mimetypes.guess_type("index-abc123.js")[0] == "text/javascript"
    assert mimetypes.guess_type("index.css")[0] == "text/css"
    assert mimetypes.guess_type("favicon.svg")[0] == "image/svg+xml"


def test_served_bundle_gets_javascript_mime_on_the_wire(tmp_path):
    (tmp_path / "index.html").write_text("<html><body>app</body></html>")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index-abc123.js").write_text("console.log('hi')")
    (assets / "index-abc123.css").write_text("body{}")

    _pollute()  # build_app re-pins; pollution must not survive
    client = TestClient(build_app(str(tmp_path)))

    r = client.get("/assets/index-abc123.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/javascript"), (
        f"module script would be refused: {r.headers['content-type']}"
    )
    r2 = client.get("/assets/index-abc123.css")
    assert r2.status_code == 200
    assert r2.headers["content-type"].startswith("text/css")
    r3 = client.get("/")
    assert r3.status_code == 200
    assert r3.headers["content-type"].startswith("text/html")
