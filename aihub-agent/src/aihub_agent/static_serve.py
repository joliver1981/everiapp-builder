"""Static file server spawned per app by the agent.

Runs as: `python -m aihub_agent.static_serve --dir <path> --port <port>`

Kept as its own module so PyInstaller bundles it and the agent doesn't
need a separate node/serve installation on the target host.
"""
import argparse
import sys
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles


def force_web_mime_types() -> None:
    """Pin web MIME types — never trust the host OS registry.

    Starlette's StaticFiles resolves Content-Type via stdlib `mimetypes`,
    which on Windows merges the MACHINE'S REGISTRY. Real hosts routinely map
    `.js` → text/plain, and browsers hard-refuse a <script type="module">
    with a non-JavaScript MIME type — the deployed app then renders as a
    black empty page (assets 200, bundle never executes). Same field bug the
    platform fixed in backend/src/main.py (v0.17.1); the agent serves app
    bundles on arbitrary customer hosts, so it needs the same pin.
    """
    import mimetypes

    for mime, ext in (
        ("text/javascript", ".js"),
        ("text/javascript", ".mjs"),
        ("text/css", ".css"),
        ("text/html", ".html"),
        ("image/svg+xml", ".svg"),
        ("application/json", ".json"),
        ("application/manifest+json", ".webmanifest"),
        ("application/wasm", ".wasm"),
        ("font/woff2", ".woff2"),
        ("font/woff", ".woff"),
        ("image/x-icon", ".ico"),
        ("image/png", ".png"),
    ):
        mimetypes.add_type(mime, ext)


def build_app(dist_dir: str) -> FastAPI:
    force_web_mime_types()
    app = FastAPI(title="aihub-agent-static")
    # html=True → serve index.html for / and SPA-style fallbacks for unknown paths
    app.mount("/", StaticFiles(directory=dist_dir, html=True), name="static")
    return app


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True, help="Directory to serve (the unpacked dist/)")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    uvicorn.run(
        build_app(args.dir),
        host=args.host,
        port=args.port,
        log_level="warning",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
