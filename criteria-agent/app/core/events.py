"""Progress events."""

import asyncio
from contextvars import ContextVar
from typing import Any

EVENTS: ContextVar[asyncio.Queue | None] = ContextVar("events", default=None)


def emit(kind: str, **data: Any) -> None:
    """Send one progress event."""
    queue = EVENTS.get()
    if queue is not None:
        queue.put_nowait({"type": kind, **data})
