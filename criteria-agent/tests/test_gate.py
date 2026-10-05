"""The approval gate."""

from types import SimpleNamespace

import pytest
from helpers import State, doc

from app.answer import agent
from app.core import database, documents, errors
from app.gate import gate

pytestmark = pytest.mark.usefixtures("db")


def gate_event(
    reply: object, state: State | None = None, tool: str = "submit_decision", **args: object
) -> SimpleNamespace:
    """A gated call with a fixed reply."""
    owner = SimpleNamespace(state=state if state is not None else State())
    return SimpleNamespace(
        tool_use={"name": tool, "input": args, "toolUseId": "t1"},
        interrupt=lambda *_, **__: reply,
        agent=owner,
        invocation_state={"agent": owner},
        cancel_tool=False,
    )


def action(**args: object) -> str:
    """The hash that approves a call."""
    return gate.fingerprint({"tool": "submit_decision", "input": args})


async def outcomes() -> list[str]:
    """Outcomes written to the audit log."""
    return [row["outcome"] for row in await database.entries(database.Audit)]


@pytest.mark.parametrize(
    "reply",
    [None, "yes", True, {"approved": True}, {"approved": "true"}, {"approved": 1}, {}],
    ids=["no reply", "text", "bare true", "no hash", "string true", "one", "empty"],
)
async def test_the_gate_denies_anything_but_approval_of_this_exact_action(reply):
    """Only the right hash approves."""
    event = gate_event(reply, decision="refer")
    verdict = await agent.Approval().before_tool_call(event)
    assert verdict.type == "deny" and "approved" not in event.agent.state
    assert (await outcomes())[-1] in {"denied", "stale"}


async def test_an_approval_is_bound_to_the_action_and_the_guide_edition():
    """A hash expires with the guide."""
    args = {"decision": "refer", "applicant": "A1"}
    shown = action(**args)
    approved = gate_event({"approved": True, "hash": shown, "by": "u1"}, **args)
    assert (await agent.Approval().before_tool_call(approved)).type == "proceed"
    assert approved.agent.state["approved"] == ["t1"]
    documents.GUIDE.set(doc(pdf=b"july edition"))
    later = gate_event({"approved": True, "hash": shown, "by": "u1"}, **args)
    assert (await agent.Approval().before_tool_call(later)).reason == errors.STALE
    assert await outcomes() == ["approved", "stale"]


@pytest.mark.parametrize(
    ("decision", "checks", "requested", "denied"),
    [
        ("approve", [], "approve", True),
        ("approve", [True, False], "approve", True),
        ("approve", [False], "approve", True),
        ("approve", [True], "approve", False),
        ("approve", [True], "decline", True),
        ("refer", [], "refer", False),
        ("refer", [], "none", True),
    ],
    ids=[
        "approve with no assessment",
        "approve with a broken criterion",
        "approve with nothing met",
        "approve with a criterion met and none broken",
        "request asked for another decision",
        "requested refer needs no assessment",
        "request asked for no decision",
    ],
)
async def test_the_cedar_policy_permits_only_the_requested_decision_and_a_sound_approve(
    decision, checks, requested, denied
):
    """Policy runs before the human."""
    assessed = {"met": True in checks, "broken": False in checks}
    event = gate_event(None, State(assessed=assessed, requested=requested), decision=decision)
    verdict = await agent.Policy().before_tool_call(event)
    assert (verdict.type == "deny") is denied
    assert ((await outcomes())[-1:] == ["policy_denied"]) is denied


async def test_a_policy_denial_states_the_facts_the_policy_saw():
    """The denial names the decision tried and its applicant, the one asked for and what the assessment found."""
    event = gate_event(None, State(requested="approve"), decision="approve", applicant="T2")
    verdict = await agent.Policy().before_tool_call(event)
    assert (
        "tried to record approve for applicant T2, the request asked for approve, and no case was assessed"
        in verdict.reason
    )


@pytest.mark.parametrize(
    ("assessed", "found"),
    [
        ({"met": False, "broken": True}, "the case assessment met no criterion"),
        ({"met": True, "broken": True}, "the case assessment found a criterion broken"),
    ],
    ids=["nothing met", "met and broken"],
)
async def test_a_denial_words_what_the_assessment_found(assessed, found):
    """An assessment that met nothing is not described as a broken criterion."""
    event = gate_event(None, State(assessed=assessed, requested="approve"), decision="approve")
    assert found in (await agent.Policy().before_tool_call(event)).reason


async def test_the_policy_permits_every_other_tool():
    """Only the gated tool is restricted."""
    verdict = await agent.Policy().before_tool_call(gate_event(None, tool="search_guide", query="q"))
    assert verdict.type == "proceed" and await outcomes() == []


async def test_the_tool_refuses_an_unapproved_call_and_records_each_decision_once():
    """No approval, no decision."""
    decide = agent.submit_decision._tool_func
    stray = SimpleNamespace(tool_use={"toolUseId": "t2"}, agent=SimpleNamespace(state=State(approved=["t1"])))
    assert (await decide("A1", "approve", "r", tool_context=stray)).startswith("Refused")
    approved = SimpleNamespace(tool_use={"toolUseId": "t1"}, agent=SimpleNamespace(state=State(approved=["t1"])))
    first = await decide("A1", "approve", "r1", tool_context=approved)
    again = await decide("A1", "approve", "reworded", tool_context=approved)
    assert first.startswith("The approve decision for A1 is recorded") and "already recorded" in again


async def test_a_broken_policy_file_fails_closed(monkeypatch, tmp_path):
    """A policy that cannot be read denies."""
    broken = tmp_path / "policy.cedar"
    broken.write_text("permit(principal, action", encoding="utf-8")
    monkeypatch.setattr(gate, "POLICY", broken)
    with pytest.raises(ValueError):
        gate.Policy()


async def test_a_decision_already_recorded_in_the_conversation_asks_no_one_again(db):
    """A repeat of a recorded decision is turned away before any approval card."""
    state = State(recorded=[gate.decided("A1", "refer")])
    event = gate_event(None, state, applicant="A1", decision="refer", rationale="r")
    event.interrupt = lambda *_, **__: pytest.fail("no second approval for a recorded decision")
    verdict = await agent.Approval().before_tool_call(event)
    assert verdict.type == "deny" and "already recorded in this conversation" in verdict.reason
    assert state["gate"] == ["submit_decision: duplicate"]


async def test_a_decision_a_person_declined_is_not_put_to_them_again_in_the_turn(db):
    """After a person says no, a second attempt at the same decision is refused without a new card."""
    state = State()
    first = gate_event({"approved": False}, state, applicant="A1", decision="approve", rationale="r")
    assert (await agent.Approval().before_tool_call(first)).type == "deny"
    again = gate_event(None, state, applicant="A1", decision="approve", rationale="r")
    again.interrupt = lambda *_, **__: pytest.fail("no second approval card after a person declined")
    verdict = await agent.Approval().before_tool_call(again)
    assert verdict.type == "deny" and state["gate"] == ["submit_decision: denied", "submit_decision: denied"]
