"""The case assessment tool."""

from types import SimpleNamespace

from helpers import PAGES, State, returns

from app.answer import tools
from app.core.schemas import Assessment, Criterion


def context(question: str = "q") -> SimpleNamespace:
    """A tool context over the test guide."""
    return SimpleNamespace(
        agent=SimpleNamespace(state=State(question=question)), invocation_state={"corpus": PAGES, "work": {}}
    )


def criterion(requirement: str, verdict: str, stated: bool = False, uses_case: bool = True) -> Criterion:
    """One assessed criterion."""
    return Criterion(
        criterion="c",
        case="60%",
        requirement=[requirement],
        verdict=verdict,
        breaks_as_stated=stated,
        uses_case=uses_case,
        reason="model working",
    )


async def run(found: list[Criterion], monkeypatch, question: str = "q") -> tuple[list, SimpleNamespace]:
    """Assess a case with the model returning these criteria."""
    monkeypatch.setattr(tools, "search", returns(([(PAGES[0], 0.9)], 0.9)))
    monkeypatch.setattr(tools, "ask_model", returns(Assessment(criteria=found)))
    ctx = context(question)
    return [e async for e in tools.assess_case._tool_func("a case", ctx)], ctx


async def test_a_breach_blocks_even_when_its_requirement_cannot_be_quoted(monkeypatch):
    """An unquoted breach still blocks; an unquoted meet never counts."""
    found = [
        criterion("Maximum LTV is 75%", "meets"),
        criterion("Invented rule", "breaks"),
        criterion("Made up", "meets"),
    ]
    events, ctx = await run(found, monkeypatch)
    assert "page 1 requires" in events[-1] and "could not be quoted" in events[-1] and "Made up" not in events[-1]
    assert ctx.agent.state["assessed"] == {"met": True, "broken": True} and ctx.agent.state["retrieved"] == [1]
    assert "model working" in events[-1]
    [output] = ctx.agent.state["tool_outputs"]
    assert "model working" in output
    assert ctx.agent.state["grounds"][output] == f"Page 1:\n{PAGES[0].text}"
    assert "a case" not in output


async def test_an_unclear_or_empty_assessment_never_passes(monkeypatch):
    """Unclear criteria do not count; no criteria never passes."""
    _, unclear = await run([criterion("Maximum LTV is 75%", "unclear")], monkeypatch)
    _, empty = await run([], monkeypatch)
    assert unclear.agent.state["assessed"] == empty.agent.state["assessed"] == {"met": False, "broken": True}
    _, mixed = await run(
        [criterion("Maximum LTV is 75%", "meets"), criterion("Maximum term 18 months.", "unclear")], monkeypatch
    )
    assert mixed.agent.state["assessed"] == {"met": True, "broken": False}
    assert next(iter(mixed.agent.state["rows"].values())) == [1, ["Maximum LTV is 75%"]]


async def test_an_unclear_criterion_whose_stated_figures_break_it_blocks(monkeypatch):
    """A hedged verdict cannot hide a stated breach."""
    _, ctx = await run(
        [criterion("Maximum LTV is 75%", "meets"), criterion("Maximum term 18 months.", "unclear", stated=True)],
        monkeypatch,
    )
    assert ctx.agent.state["assessed"] == {"met": True, "broken": True}


async def test_a_criterion_every_case_meets_does_not_open_an_approve(monkeypatch):
    """A rule that holds for any case, such as no maximum age, is not a met criterion of this case."""
    _, ctx = await run(
        [criterion("Maximum LTV is 75%", "meets", uses_case=False), criterion("Maximum term 18 months.", "unclear")],
        monkeypatch,
    )
    assert ctx.agent.state["assessed"] == {"met": False, "broken": True}
