"""The OpenAI model, hedged calls and structured calls."""

import asyncio
from bisect import bisect_right, insort
from collections import defaultdict
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from functools import cache
from typing import Any

from openai import DefaultAsyncHttpxClient
from pydantic import BaseModel
from strands import Agent
from strands.models.openai_responses import OpenAIResponsesModel

from app.core.config import settings


@cache
def model(counting: bool = False, effort: str | None = None) -> OpenAIResponsesModel:
    """The OpenAI model."""
    return Hedged(
        use_native_token_count=counting,
        client_args={"api_key": settings().openai_api_key.get_secret_value()},
        model_id=settings().model_id,
        params={"reasoning": {"effort": effort}} if effort else {},
    )


async def ask_model[T: BaseModel](
    system: str, prompt: str, schema: type[T], reasoning: bool = False, images: tuple[bytes, ...] = ()
) -> T:
    """One structured model call with any images, reasoning at full effort when asked."""
    judge = model(effort=settings().reasoning_effort if reasoning else settings().judge_effort)
    agent = Agent(model=judge, system_prompt=system, callback_handler=None, name=schema.__name__)
    shown = [{"image": {"format": "png", "source": {"bytes": image}}} for image in images]
    content = [{"text": prompt}, *shown] if shown else prompt
    return (await agent.invoke_async(content, structured_output_model=schema)).structured_output


async def bounded[T](calls: list[Callable[[], Awaitable[T]]]) -> list[T]:
    """Run calls in parallel."""
    gate = asyncio.Semaphore(settings().parallel_calls)

    async def one(call: Callable[[], Awaitable[T]]) -> T:
        """Run one call."""
        async with gate:
            return await call()

    return await asyncio.gather(*(one(call) for call in calls))


class Quantile:
    """A running quantile."""

    def __init__(self, p: float) -> None:
        """Start with no samples."""
        self.heights: list[float] = []
        self.positions = [0, 1, 2, 3, 4]
        self.steps = (0.0, p / 2, p, (1 + p) / 2, 1.0)
        self.wanted = [0.0, 2 * p, 4 * p, 2 + 2 * p, 4.0]

    @property
    def value(self) -> float | None:
        """The quantile so far, or None before five samples."""
        return self.heights[2] if len(self.heights) == 5 else None

    def add(self, sample: float) -> None:
        """Add one sample."""
        q, n = self.heights, self.positions
        if len(q) < 5:
            insort(q, sample)
            return
        q[0], q[4] = min(q[0], sample), max(q[4], sample)
        cell = bisect_right(q, sample, 1, 4) - 1
        for i in range(cell + 1, 5):
            n[i] += 1
        self.wanted = [w + step for w, step in zip(self.wanted, self.steps, strict=True)]
        for i in (1, 2, 3):
            gap = self.wanted[i] - n[i]
            if (gap >= 1 and n[i + 1] - n[i] > 1) or (gap <= -1 and n[i - 1] - n[i] < -1):
                s = 1 if gap > 0 else -1
                right = (n[i] - n[i - 1] + s) * (q[i + 1] - q[i]) / (n[i + 1] - n[i])
                left = (n[i + 1] - n[i] - s) * (q[i] - q[i - 1]) / (n[i] - n[i - 1])
                guess = q[i] + s * (right + left) / (n[i + 1] - n[i - 1])
                linear = q[i] + s * (q[i + s] - q[i]) / (n[i + s] - n[i])
                q[i] = guess if q[i - 1] < guess < q[i + 1] else linear
                n[i] += s


LATENCY: defaultdict[str | None, Quantile] = defaultdict(lambda: Quantile(settings().hedge_quantile))


async def hedged[T](kind: str, make: Callable[[], Awaitable[T]]) -> T:
    """Race a slow call with a twin."""
    loop, typical, overall = asyncio.get_running_loop(), LATENCY[kind], LATENCY[None]
    start, pending = loop.time(), {asyncio.ensure_future(make())}
    try:
        done, pending = await asyncio.wait(pending, timeout=typical.value or overall.value)
        if not done:
            pending.add(asyncio.ensure_future(make()))
        while True:
            if won := next((task for task in done if not task.exception()), None):
                typical.add(loop.time() - start)
                overall.add(loop.time() - start)
                return won.result()
            if not pending:
                return done.pop().result()
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in pending:
            task.cancel()


async def gathered(stream: AsyncIterator) -> list:
    """Every event of a model stream."""
    return [event async for event in stream]


class Pooled(DefaultAsyncHttpxClient):
    """An HTTP client that stays open."""

    async def aclose(self) -> None:
        """Stay open."""


@cache
def pooled(loop: asyncio.AbstractEventLoop) -> Pooled:
    """One HTTP client per event loop."""
    return Pooled()


class Hedged(OpenAIResponsesModel):
    """The OpenAI model with hedged calls."""

    async def stream(
        self, messages: Any, tool_specs: Any = None, system_prompt: Any = None, **kwargs: Any
    ) -> AsyncGenerator[Any]:
        """Stream a hedged request."""

        def make() -> Awaitable[list]:
            """One full request."""
            return gathered(OpenAIResponsesModel.stream(self, messages, tool_specs, system_prompt, **kwargs))

        for event in await hedged(system_prompt or "", make):
            yield event

    def _resolve_client_args(self) -> dict[str, Any]:
        """Use the pooled HTTP client."""
        return {**super()._resolve_client_args(), "http_client": pooled(asyncio.get_running_loop())}
