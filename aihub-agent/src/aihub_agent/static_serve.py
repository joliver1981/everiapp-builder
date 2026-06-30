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


def build_app(dist_dir: str) -> FastAPI:
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
