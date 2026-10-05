"""The question loop."""

import asyncio
from types import SimpleNamespace

import pytest
from helpers import PAGES, State, claim, returns

from app.ai.jev import Screen
from app.answer import agent
from app.core.schemas import Answer, Check, Fact, Unrelated

TABLE = 'Table 1, "Terms", runs over pages 1, 2 with 2 rows. Columns: Term.'


class Scripted:
    """An agent that stops at once."""

    def __init__(self, stop: str = "end_turn", tools: tuple[str, ...] = ("search_guide",), **state: object) -> None:
        """Start with this state."""
        self.state, self.messages, self.tool_names, self.stop = State(state), [], list(tools), stop

    async def stream_async(self, *_, **__):
        """End the run."""
        yield {"result": SimpleNamespace(stop_reason=self.stop) if self.stop else None}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No database, no follow-ups."""
    monkeypatch.setattr(agent, "contents", returns((["Page 1: Terms. The loan terms."], [TABLE])))
    monkeypatch.setattr(agent, "suggest", returns([]))


@pytest.fixture
def screened(monkeypatch):
    """Make the Jev screen return a fixed view."""

    def install(
        off_topic: bool = False, pressure: bool = False, route: str = "lookup", requested: str = "none"
    ) -> None:
        """Screen every question this way."""
        monkeypatch.setattr(
            agent, "screen", returns(Screen(off_topic=off_topic, pressure=pressure, route=route, requested=requested))
        )

    return install


async def test_an_off_topic_question_is_refused_before_the_agent_runs(screened, monkeypatch):
    """Off-topic questions are refused."""
    screened(off_topic=True, pressure=True)
    monkeypatch.setattr(agent, "ask_model", returns(Unrelated(unrelated=True)))
    monkeypatch.setattr(agent, "search", returns(([], 0.1)))
    monkeypatch.setattr(agent, "step", lambda *_: pytest.fail("the agent should not run"))
    result = await agent.ask("Write a poem.", lambda _: None, PAGES)
    assert (result.status, result.flags) == ("refused", ["pressure", "out_of_scope"])
    assert result.answer.note == agent.OUT_OF_SCOPE and len(result.trace_id) == 32


async def test_a_lending_question_jev_calls_off_topic_goes_ahead_when_gpt_disagrees(screened, monkeypatch):
    """Jev's off-topic call refuses only when GPT agrees the request is unrelated to lending."""
    screened(off_topic=True)
    monkeypatch.setattr(agent, "ask_model", returns(Unrelated(unrelated=False)))
    refused, _, view = await agent.screened("japanese knotweed?", "", [], [])
    assert refused is None and not view.off_topic


@pytest.mark.parametrize(("requested", "told"), [("none", True), ("approve", False)])
async def test_only_a_question_no_page_answers_is_told_to_set_answerable_false(screened, monkeypatch, requested, told):
    """A decision request finds no page, yet its gate result must still be explained."""
    screened(requested=requested)
    monkeypatch.setattr(agent, "search", returns(([], 0.1)))
    scripted = Scripted()
    await agent.prepare(scripted, "Please submit an approve decision for applicant T2.", PAGES, {})
    assert (agent.UNANSWERED in scripted.state.get("primer")) is told


DENIAL = "Not submitted: the agent tried to record approve for applicant T2, and the case assessment met no criterion."


async def test_a_policy_denial_the_answer_leaves_out_is_stated_as_a_verified_claim(monkeypatch, events):
    """An answer that never says the decision was refused still tells the reader."""
    monkeypatch.setattr(
        agent,
        "verify",
        lambda claims, *_: returns([Check(claim=c, verdict="supports", judge="code") for c in claims])(),
    )
    monkeypatch.setattr(agent, "cited", lambda checks, *_: returns(checks)())
    monkeypatch.setattr(agent, "omissions", returns([]))
    state = State(question="q", tool_outputs=[f"DENIED: {DENIAL}"], outcomes=[f"DENIED: {DENIAL}"])
    work = {"facts": asyncio.ensure_future(returns([])())}
    run = SimpleNamespace(stop_reason="end_turn", structured_output=Answer(answerable=False, claims=[], note=""))
    result = await agent.finish(SimpleNamespace(state=state), run, PAGES, work)
    assert result.status == "grounded" and [c.claim.text for c in result.released] == [DENIAL]


def test_a_denial_the_answer_already_quotes_is_not_added_again():
    """No duplicate claim when the model stated the refusal itself."""
    own = claim("The approve for T2 was not submitted.")
    own = own.model_copy(update={"quote": f"DENIED: {DENIAL}", "source": "tool"})
    answer = Answer(answerable=True, claims=[own], note="")
    assert agent.stated(answer, [f"DENIED: {DENIAL}"]).claims == [own]


async def test_missing_facts_are_added_as_their_own_verified_claims(monkeypatch, events):
    """Missed facts are added and checked."""
    original = claim("Maximum term 18 months.")
    missing = [
        Fact(text="Maximum LTV is 75%", quote="Maximum LTV is 75%"),
        Fact(text="LTV can reach 85%", quote="Maximum LTV is 85%"),
    ]

    async def verify(claims, *_):
        """Contradict only the 85% claim."""
        return [Check(claim=c, verdict="contradicts" if "85" in c.quote else "supports", judge="code") for c in claims]

    monkeypatch.setattr(agent, "verify", verify)
    monkeypatch.setattr(agent, "cited", lambda checks, *_: returns(checks)())
    monkeypatch.setattr(agent, "omissions", returns(missing))
    work = {"facts": asyncio.ensure_future(returns(missing)())}
    run = SimpleNamespace(stop_reason="end_turn", structured_output=Answer(answerable=True, claims=[original], note=""))
    result = await agent.finish(SimpleNamespace(state=State(question="q", tool_outputs=[])), run, PAGES, work)
    assert [c.claim.text for c in result.released] == [original.text, "Maximum LTV is 75%"]
    assert (result.status, result.repaired, [f.text for f in result.omissions]) == (
        "partial",
        True,
        ["LTV can reach 85%"],
    )
    repaired = next(e for e in events() if e.get("stage") == "repaired")
    assert (
        repaired["details"]["added"] == ["Maximum LTV is 75%"]
        and repaired["details"]["dropped"][0]["verdict"] == "contradicts"
    )


async def test_a_failed_fact_list_leaves_the_verified_answer_released(monkeypatch, events):
    """A failed fact list keeps the answer."""
    original = claim("Maximum term 18 months.")

    async def broken(*_):
        """Stand in for a fact lister whose model call fails."""
        raise agent.StructuredOutputException("no valid facts")

    monkeypatch.setattr(
        agent,
        "verify",
        lambda claims, *_: returns([Check(claim=c, verdict="supports", judge="code") for c in claims])(),
    )
    monkeypatch.setattr(agent, "cited", lambda checks, *_: returns(checks)())
    work = {"facts": asyncio.ensure_future(broken())}
    run = SimpleNamespace(stop_reason="end_turn", structured_output=Answer(answerable=True, claims=[original], note=""))
    result = await agent.finish(SimpleNamespace(state=State(question="q", tool_outputs=[])), run, PAGES, work)
    assert (result.status, [c.claim.text for c in result.released]) == ("grounded", [original.text])
    assert any(e.get("stage") == "completeness" and e.get("status") == "failed" for e in events())


async def test_a_run_that_spends_its_step_budget_is_answered_from_what_it_gathered(screened, monkeypatch, events):
    """At the step budget, answer from what was gathered."""
    screened()
    monkeypatch.setattr(agent, "search", returns(([], 0.1)))
    scripted = Scripted(stop="limit_turns")
    scripted.system_prompt = "system"
    monkeypatch.setattr(agent, "build", lambda *_, **__: scripted)
    prompts = []

    class Closing:
        """The tool-less model that concludes."""

        def __init__(self, **options):
            """Refuse tools."""
            assert "tools" not in options

        async def invoke_async(self, prompt, structured_output_model):
            """Answer from the gathered text."""
            prompts.append(prompt)
            return SimpleNamespace(
                structured_output=Answer(answerable=True, claims=[claim("Maximum term 18 months.")], note="")
            )

    async def step(agent_, message, *_):
        """Gather one tool result, then stop at the limit."""
        agent_.messages += [
            {"role": "user", "content": [{"toolResult": {"content": [{"text": "Maximum term 18 months."}]}}]}
        ]
        return SimpleNamespace(stop_reason="limit_turns")

    monkeypatch.setattr(agent, "step", step)
    monkeypatch.setattr(agent, "Agent", Closing)
    monkeypatch.setattr(
        agent,
        "verify",
        lambda claims, *_: returns([Check(claim=c, verdict="supports", judge="code") for c in claims])(),
    )
    monkeypatch.setattr(agent, "cited", lambda checks, *_: returns(checks)())
    result = await agent.ask("What is the maximum term?", lambda _: None, PAGES)
    assert result.status == "grounded" and "Maximum term 18 months." in prompts[0]
    assert [e.get("status") for e in events() if e.get("stage") == "budget"] == [None, "done"]


@pytest.mark.parametrize(
    ("gate", "retried"), [([], True), (["submit_decision: denied"], False)], ids=["no decision", "a decision tried"]
)
async def test_an_answer_withheld_by_a_contradicted_assessment_is_retried_once_unless_a_decision_was_tried(
    monkeypatch, events, gate, retried
):
    """A contradicted assessment earns one more turn, never after a decision was submitted or refused."""
    finding = claim("The loan-size test is unclear because no purchase price is given.", source="tool")
    monkeypatch.setattr(
        agent,
        "verify",
        lambda claims, *_: returns(
            [Check(claim=c, verdict="contradicts", judge="gpt", grounded=True) for c in claims]
        )(),
    )

    async def nothing():
        """No facts."""
        return []

    work = {"facts": asyncio.ensure_future(nothing())}
    run = SimpleNamespace(stop_reason="end_turn", structured_output=Answer(answerable=True, claims=[finding], note=""))
    state = State(question="q", tool_outputs=[], gate=gate)
    result = await agent.finish(SimpleNamespace(state=state), run, PAGES, work, retry=True)
    assert (result is None) is retried
    assert work.get("again", (None, []))[1] == ([finding.text] if retried else [])


@pytest.mark.parametrize(
    ("note", "status", "verdicts", "expected"),
    [
        ("", "withheld", ["fabricated"], agent.UNVERIFIED),
        ("Term not stated.", "withheld", ["fabricated"], "Term not stated."),
        ("", "partial", ["supports", "contradicts"], agent.CONTRADICTED),
        ("", "withheld", ["unclear"], agent.UNCLEAR),
        ("Ask the lender.", "grounded", ["supports"], "Ask the lender."),
    ],
    ids=["withheld, no note", "withheld, own note", "partial with a contradiction", "scan unclear", "grounded"],
)
def test_the_note_tells_the_reader_what_the_checks_found(note, status, verdicts, expected):
    """Withheld answers explain themselves; contradictions and unclear scans are always told."""
    checks = [Check(claim=claim("x"), verdict=v, judge="code") for v in verdicts]
    result = agent.Result(
        trace_id="t", question="q", answer=Answer(answerable=True, claims=[], note=note), status=status, checks=checks
    )
    assert agent.noted(note, result) == expected


@pytest.mark.parametrize(
    ("answered", "retried"), [(True, True), (False, False)], ids=["pages were found", "no page was"]
)
async def test_a_question_declined_although_pages_answer_it_gets_one_more_turn(monkeypatch, events, answered, retried):
    """A declined question gets one more look only when page search found relevant pages."""
    monkeypatch.setattr(agent, "verify", lambda claims, *_: returns([])())
    run = SimpleNamespace(
        stop_reason="end_turn", structured_output=Answer(answerable=False, claims=[], note="Need details.")
    )
    state = State(question="How much could I get?", tool_outputs=[], retrieved=[1] if answered else [])
    work: dict = {}
    result = await agent.finish(SimpleNamespace(state=state), run, PAGES, work, retry=True)
    assert (result is None) is retried
    assert (work["again"][2] == agent.ANSWERABLE) if retried else "again" not in work


async def test_an_answer_whose_every_claim_says_more_than_its_quote_gets_one_more_turn(monkeypatch, events):
    """When no claim is supported, the agent is asked once to split its claims to fit their quotes."""
    monkeypatch.setattr(
        agent,
        "verify",
        lambda claims, *_: returns([Check(claim=c, verdict="says_nothing", judge="gpt", page=1) for c in claims])(),
    )
    bundled = claim("It covers fees, loadings and transfers.")
    run = SimpleNamespace(stop_reason="end_turn", structured_output=Answer(answerable=True, claims=[bundled], note=""))
    work = {"facts": asyncio.ensure_future(returns([])())}
    result = await agent.finish(
        SimpleNamespace(state=State(question="q", tool_outputs=[])), run, PAGES, work, retry=True
    )
    assert result is None and work["again"][:2] == ("unsupported", [bundled.text])


async def test_a_recorded_decision_the_answer_leaves_in_its_note_is_stated_as_a_claim(monkeypatch, events):
    """After an approval, the recorded decision is told as a claim even when the model only acknowledged it."""
    monkeypatch.setattr(
        agent,
        "verify",
        lambda claims, *_: returns([Check(claim=c, verdict="supports", judge="gpt") for c in claims])(),
    )
    monkeypatch.setattr(agent, "cited", lambda checks, *_: returns(checks)())
    monkeypatch.setattr(agent, "omissions", returns([]))
    recorded = "The approve decision for H8 is recorded and underwriting notified."
    state = State(question="q", tool_outputs=[recorded], outcomes=[recorded], gate=["submit_decision: executed"])
    run = SimpleNamespace(stop_reason="end_turn", structured_output=Answer(answerable=False, claims=[], note=""))
    result = await agent.finish(
        SimpleNamespace(state=state), run, PAGES, {"facts": asyncio.ensure_future(returns([])())}
    )
    assert result.status == "grounded" and [c.claim.text for c in result.released] == [recorded]
