import asyncio
import logging

from .apps import registry

logger = logging.getLogger(__name__)


async def health_loop(interval: float = 10.0) -> None:
    while True:
        try:
            for app in registry.list():
                if app.status in ("running", "error"):
                    await registry.probe(app)
        except Exception:
            logger.exception("health loop iteration failed")
        await asyncio.sleep(interval)
