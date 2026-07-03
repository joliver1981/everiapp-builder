"""Async, batched, best-effort span writer.

The LLM gateway enqueues plain dicts and returns immediately — a span must
never add latency to, or break, the call it describes. A background task
drains the queue in batches on its own session; the capture-level setting and
payload encryption are applied here (write time), so the hot path does zero
DB reads. When the queue is full, spans are DROPPED and counted — backpressure
must not reach the gateway.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_QUEUE_MAX = 2000
_BATCH_MAX = 100
# Payload size budget per side (prompt / response), pre-encryption. SQLite +
# aiosqlite are single-writer; unbounded payloads would stall every other
# commit in the process.
_PAYLOAD_MAX_CHARS = 200_000

CAPTURE_LEVELS = ("full", "metadata_only", "off")


class SpanWriter:
    def __init__(self) -> None:
        # Created in start(), NOT here: asyncio.Queue binds to the event loop
        # that first awaits it, and this object is a module-global that outlives
        # loops — every fresh lifespan (each TestClient in a pytest process, a
        # uvicorn reload) runs on a new loop and needs a fresh queue. A queue
        # bound to a dead loop makes `await get()` raise synchronously, which
        # would hot-spin the retry loop and freeze the new loop entirely.
        self._queue: asyncio.Queue[dict] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self.dropped = 0

    # ------------------------------------------------------------- hot path
    def enqueue(self, row: dict) -> None:
        """Non-blocking; drops (and counts) when full, unstarted, or mid-rebind."""
        try:
            if self._queue is None:
                raise asyncio.QueueFull  # not started yet — treat as drop
            self._queue.put_nowait(row)
        except Exception:
            # QueueFull, or a queue bound to a previous (dead) loop waking a
            # stale getter — either way: best-effort data, drop and count.
            self.dropped += 1
            if self.dropped % 100 == 1:
                logger.warning("span queue full/unbound — dropped %d spans so far", self.dropped)

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """(Re)bind the writer to the CURRENT running loop.

        Spans still queued from a previous, now-dead loop are deliberately
        dropped — spans are best-effort debug data and the old loop can no
        longer flush them anyway.
        """
        loop = asyncio.get_running_loop()
        if self._task is not None and not self._task.done() and self._loop is loop:
            return  # already running on this loop
        self._loop = loop
        self._queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            # Await the cancellation so an in-flight _flush finishes (or its
            # salvage retry runs) BEFORE drain opens a second session — never
            # two concurrent flushes on the shutting-down loop.
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("span writer task died during stop")
        await self.drain()

    async def _run(self) -> None:
        while True:
            try:
                assert self._queue is not None
                first = await self._queue.get()
                batch = [first]
                while len(batch) < _BATCH_MAX:
                    try:
                        batch.append(self._queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                try:
                    await self._flush(batch)
                except asyncio.CancelledError:
                    # Shutdown landed mid-flush: the batch is already out of
                    # the queue, so give it one salvage attempt before exiting.
                    try:
                        await self._flush(batch)
                    except Exception:
                        logger.warning("span writer lost %d spans at shutdown", len(batch))
                    raise
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("span writer flush failed (batch dropped)")
                # Backstop: if the failure is persistent (e.g. a dead queue),
                # this sleep keeps the retry from hot-spinning the loop.
                await asyncio.sleep(0.5)

    async def drain(self) -> None:
        """Flush everything queued right now — shutdown and tests."""
        if self._queue is None:
            return
        batch = []
        while True:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if batch:
            try:
                await self._flush(batch)
            except Exception:
                logger.exception("span writer drain failed (batch dropped)")

    # -------------------------------------------------------------- storage
    async def _flush(self, batch: list[dict]) -> None:
        from ..database import async_session
        from ..platform_settings.service import get_setting
        from ..secrets.encryption import encryption_service
        from .models import AISpan

        async with async_session() as db:
            level = await get_setting(db, "trace_capture_level")
            if level not in CAPTURE_LEVELS:
                level = "metadata_only"
            if level == "off":
                return

            rows = []
            for item in batch:
                prompt = item.pop("prompt_text", None)
                response = item.pop("response_text", None)
                item["capture_level"] = level
                if level == "metadata_only":
                    # Client-controlled free text must respect the reduced
                    # capture level too: error text is bounded hard, and click
                    # labels (which mirror on-screen user data) are redacted.
                    if item.get("error"):
                        item["error"] = str(item["error"])[:200]
                    if item.get("kind") == "ui.interaction":
                        item["name"] = "(interaction)"
                if level == "full":
                    try:
                        if prompt:
                            item["prompt_ct"] = encryption_service.encrypt(prompt[:_PAYLOAD_MAX_CHARS])
                        if response:
                            item["response_ct"] = encryption_service.encrypt(response[:_PAYLOAD_MAX_CHARS])
                    except Exception:
                        # A broken encryption key must not kill metering —
                        # degrade this row to metadata.
                        item.pop("prompt_ct", None)
                        item.pop("response_ct", None)
                        item["capture_level"] = "metadata_only"
                rows.append(AISpan(**item))

            db.add_all(rows)
            await db.commit()


span_writer = SpanWriter()
