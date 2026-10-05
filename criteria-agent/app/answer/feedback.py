"""Thumbs up or down on an answer."""

import contextlib

import httpx

from app.ai.jev import triage
from app.core import database
from app.core.config import settings
from app.core.errors import STANDS
from app.core.events import emit
from app.core.prompts import RETRY
from app.core.schemas import Feedback
from app.core.tracing import langfuse, trace_id


async def scored(feedback: Feedback) -> None:
    """Score the trace in Langfuse."""
    config = settings()
    if config.langfuse_public_key is None or config.langfuse_secret_key is None:
        return
    score = {
        "traceId": feedback.trace_id,
        "name": "user-feedback",
        "value": float(feedback.helpful),
        "dataType": "BOOLEAN",
        "comment": feedback.comment or None,
    }
    with contextlib.suppress(httpx.HTTPError):
        async with langfuse() as client:
            (await client.post(f"{config.langfuse_host}/api/public/scores", json=score)).raise_for_status()


async def reviewed(feedback: Feedback, session: str) -> tuple[str | None, dict]:
    """Handle feedback."""
    earlier = await database.answered(session, feedback.trace_id)
    if earlier is None:
        raise LookupError("no answer in this session has that trace id")
    emit("stage", stage="feedback")
    question = earlier.get("question") or ""
    verdict = "helpful" if feedback.helpful else await triage(question, said(earlier), feedback.comment)
    emit("stage", stage="feedback", status="done", details={"verdict": verdict})
    await database.append(database.Feedback, {"session": session, **feedback.model_dump(), "verdict": verdict})
    await scored(feedback)
    reply = {"verdict": verdict, "rated": feedback.trace_id}
    if verdict in ("helpful", "irrelevant"):
        note = STANDS if verdict == "irrelevant" else ""
        return None, {"status": "feedback", "feedback": reply, "note": note, "trace_id": trace_id()}
    return RETRY.format(question=question, comment=feedback.comment), reply


def said(response: dict) -> str:
    """What the user was told."""
    claims = " ".join(c["claim"]["text"] for c in response.get("released", []))
    return claims or (response.get("answer") or {}).get("note") or response.get("status", "")
