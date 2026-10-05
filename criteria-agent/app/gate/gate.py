"""Policy, human approval and the gated tool."""

import hashlib
import json
from pathlib import Path
from typing import Annotated, Any

from opentelemetry import trace
from strands import ToolContext, tool
from strands.hooks import BeforeToolCallEvent
from strands.interventions import Deny, InterventionHandler, Proceed
from strands.vended_interventions.cedar import CedarAuthorization

from app.core import database
from app.core.documents import guide
from app.core.errors import DENIED, FOUND, POLICY_DENIED, RECORDED, REFUSAL, REFUSED, STALE
from app.core.events import emit
from app.core.schemas import Decision
from app.core.state import remember
from app.core.tracing import observed, tracer

GATED = {"submit_decision"}
POLICY = Path(__file__).with_name("policy.cedar")


def fingerprint(fields: dict) -> str:
    """Hash of an action and the guide edition."""
    payload = {**fields, "guide": guide().version}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def decided(applicant: str, decision: str) -> str:
    """The key a recorded decision is kept under: applicant, decision and guide edition."""
    return fingerprint({"tool": "submit_decision", "applicant": applicant, "decision": decision})


def approved(tool_context: ToolContext) -> bool:
    """Whether a human approved this call."""
    return tool_context.tool_use["toolUseId"] in (tool_context.agent.state.get("approved") or [])


@tool(context=True)
async def submit_decision(
    applicant: Annotated[str, "Name or reference of the applicant."],
    decision: Annotated[Decision, "The decision to record."],
    rationale: Annotated[str, "Why, citing the guide."],
    tool_context: ToolContext,
) -> str:
    """Record a lending decision once."""
    if not approved(tool_context):
        return REFUSED
    key = decided(applicant, decision)
    entry = {
        "guide": guide().version,
        "applicant": applicant,
        "decision": decision,
        "rationale": rationale,
    }
    if await database.record(key, entry):
        result = f"The {decision} decision for {applicant} is recorded and underwriting notified."
        remember(tool_context.agent.state, "gate", ["submit_decision: executed"])
        remember(tool_context.agent.state, "recorded", [key])
    else:
        result = f"The {decision} decision for {applicant} is already recorded; not recorded again."
        remember(tool_context.agent.state, "gate", ["submit_decision: duplicate"])
    remember(tool_context.agent.state, "tool_outputs", [result])
    remember(tool_context.agent.state, "outcomes", [result])
    return result


async def refuse(event: BeforeToolCallEvent, outcome: str, message: str, by: Any) -> Deny:
    """Deny and log a gated call."""
    state, name = event.agent.state, event.tool_use["name"]
    remember(state, "gate", [f"{name}: {outcome}"])
    remember(state, "tool_outputs", [f"{REFUSAL}{message}"])
    remember(state, "outcomes", [f"{REFUSAL}{message}"])
    await record(event, outcome, by, message)
    trace.get_current_span().set_attributes(
        {
            "gate.outcome": outcome,
            "langfuse.observation.level": "WARNING",
            "langfuse.observation.status_message": message,
        }
    )
    return Deny(reason=message)


async def record(event: BeforeToolCallEvent, outcome: str, by: Any, reason: str = "") -> None:
    """Log and stream a gate outcome."""
    use, state = event.tool_use, event.agent.state
    with tracer.start_as_current_span("gate") as span:
        observed(span, {"tool": use["name"], "input": use["input"], "by": by}, {"outcome": outcome, "reason": reason})
    emit("gate", tool=use["name"], outcome=outcome, reason=reason, input=use["input"])
    await database.append(
        database.Audit,
        {
            "session": state.get("session"),
            "tool": use["name"],
            "input": use["input"],
            "tool_use_id": use["toolUseId"],
            "action": fingerprint({"tool": use["name"], "input": use["input"]}),
            "guide": guide().version,
            "outcome": outcome,
            "by": by,
            "flags": state.get("flags") or [],
        },
    )


def situation(request: dict) -> dict:
    """What the policy knows about this request."""
    state = request["invocation_state"]["agent"].state
    assessed = state.get("assessed") or {"met": False, "broken": False}
    return {"requested": state.get("requested") or "none", **assessed}


class Policy(CedarAuthorization):
    """The Cedar decision policy; a denial is logged."""

    name = "policy"

    def __init__(self) -> None:
        """Load the policy file."""
        super().__init__(policies=str(POLICY), context_enricher=situation, on_error="deny")

    async def before_tool_call(self, event: BeforeToolCallEvent, **kwargs: Any) -> Proceed | Deny:
        """Check the call against the policy."""
        verdict = super().before_tool_call(event, **kwargs)
        if isinstance(verdict, Proceed):
            return verdict
        known = situation({"invocation_state": event.invocation_state})
        assessed = event.invocation_state["agent"].state.get("assessed")
        found = FOUND[None if assessed is None else (known["met"], known["broken"])]
        given = event.tool_use["input"]
        message = POLICY_DENIED.format(
            submitted=given.get("decision", "none"),
            applicant=given.get("applicant"),
            requested=known["requested"],
            found=found,
        )
        return await refuse(event, "policy_denied", message, None)


class Approval(InterventionHandler):
    """Ask a human to approve gated calls."""

    name = "approval"
    on_error = "deny"

    async def before_tool_call(self, event: BeforeToolCallEvent) -> Proceed | Deny:
        """Pause for approval and check the reply."""
        use, state = event.tool_use, event.agent.state
        if use["name"] not in GATED:
            return Proceed()
        applicant, decision = use["input"].get("applicant"), use["input"].get("decision")
        if decided(applicant, decision) in (state.get("recorded") or []):
            return await refuse(event, "duplicate", RECORDED.format(decision=decision, applicant=applicant), None)
        if decided(applicant, decision) in (state.get("declined") or []):
            return await refuse(event, "denied", DENIED, None)
        action = fingerprint({"tool": use["name"], "input": use["input"]})
        reason = {"tool": use["name"], "input": use["input"], "hash": action}
        reply = event.interrupt("criteria-approval", reason={**reason, "flags": state.get("flags") or []})
        by = reply.get("by") if isinstance(reply, dict) else None
        if not (isinstance(reply, dict) and reply.get("approved") is True):
            remember(state, "declined", [decided(applicant, decision)])
            return await refuse(event, "denied", DENIED, by)
        if reply.get("hash") != action:
            return await refuse(event, "stale", STALE, by)
        remember(state, "approved", [use["toolUseId"]])
        remember(state, "gate", [f"{use['name']}: approved"])
        await record(event, "approved", by)
        return Proceed()
