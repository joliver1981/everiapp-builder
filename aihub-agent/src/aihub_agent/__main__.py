"""Entry point for `python -m aihub_agent` AND for the PyInstaller-built exe.

The agent exe needs to do two things depending on how it's invoked:

  aihub-agent[.exe]                       → start the main agent server (default)
  aihub-agent[.exe] serve                 → same as above (explicit)
  aihub-agent[.exe] static-serve --dir X --port Y
                                          → run as the per-app static file server
                                            (subprocess spawned by the agent itself
                                             to host each deployed app)

When frozen by PyInstaller, sys.executable IS the agent.exe, and `python -m
aihub_agent.static_serve` doesn't work because the source isn't on disk. The
spawner in apps.py detects frozen mode and uses the `static-serve` subcommand.
"""
import logging
import sys


def _run_server() -> int:
    import uvicorn
    # Use absolute imports — under PyInstaller this file runs as a top-level
    # script (no parent package), so `from .config` raises ImportError.
    from aihub_agent.config import settings
    from aihub_agent.server import app as agent_app

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    uvicorn.run(
        agent_app,
        host=settings.agent_host,
        port=settings.agent_port,
        log_level="info",
    )
    return 0


def _run_static_serve() -> int:
    """Dispatch to the per-app static server. Strips the subcommand from argv
    so static_serve.main()'s argparse sees a clean argv."""
    from aihub_agent import static_serve
    sys.argv = [sys.argv[0]] + sys.argv[2:]
    return static_serve.main()


def main() -> int:
    # If the first arg is a known subcommand, dispatch. Otherwise fall through
    # to the main server (the default and most common invocation).
    if len(sys.argv) > 1:
        sub = sys.argv[1]
        if sub == "static-serve":
            return _run_static_serve()
        if sub == "serve":
            # Strip the explicit subcommand so the server doesn't see it.
            sys.argv = [sys.argv[0]] + sys.argv[2:]
            return _run_server()
    return _run_server()


if __name__ == "__main__":
    sys.exit(main())
