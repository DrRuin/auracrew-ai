"""Hedged calls and the running quantile."""

import asyncio

import pytest

from app.ai import llm


@pytest.fixture
def calls(monkeypatch):
    """Scripted calls with a known typical latency."""
    monkeypatch.setattr(llm, "LATENCY", llm.defaultdict(lambda: llm.Quantile(0.95)))
    for _ in range(6):
        llm.LATENCY["kind"].add(0.01)
    started: list[int] = []

    def make(plan: list[tuple[float, bool]]):
        """One scripted call."""

        async def call() -> int:
            """One request."""
            n = len(started)
            started.append(n)
            seconds, succeeds = plan[n]
            await asyncio.sleep(seconds)
            if not succeeds:
                raise ConnectionError(f"call {n} failed")
            return n

        return call

    return started, make


async def test_a_call_slower_than_typical_is_won_by_its_twin(calls):
    """Slow calls get a twin."""
    started, make = calls
    assert await llm.hedged("kind", make([(5.0, True), (0.001, True)])) == 1 and started == [0, 1]


@pytest.mark.parametrize(
    ("plan", "answer"),
    [([(0.05, False), (0.02, True)], 1), ([(0.05, False), (0.02, False)], None)],
    ids=["one twin fails", "both fail"],
)
async def test_the_error_surfaces_only_when_both_twins_fail(calls, plan, answer):
    """One failed twin is ignored."""
    _, make = calls
    if answer is None:
        with pytest.raises(ConnectionError):
            await llm.hedged("kind", make(plan))
    else:
        assert await llm.hedged("kind", make(plan)) == answer
