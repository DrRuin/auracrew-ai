"""The Jev client: screening, routing, page search and triage."""

import asyncio
from collections.abc import Awaitable, Callable
from functools import cache

from typesafe_sdk import (
    AsyncTypeSafeClient,
    Choice,
    Noul,
    NoulAnswer,
    SystemOneResponse,
    TypeSafeBadRequestError,
)

from app.core.config import settings
from app.core.documents import Page, primary
from app.core.errors import JEV_TOO_LONG
from app.core.prompts import ANSWERED, ASKED, KIND, RELEVANT, REQUESTED, ROUTES, SCREEN, TOPIC, TRIAGE, UNFOUNDED
from app.core.schemas import Screen
from app.core.tracing import observed, tracer


def jev_client() -> AsyncTypeSafeClient:
    """The Jev client."""
    return client(asyncio.get_running_loop())


@cache
def client(loop: asyncio.AbstractEventLoop) -> AsyncTypeSafeClient:
    """One Jev client per event loop."""
    return AsyncTypeSafeClient(api_key=settings().typesafe_api_key.get_secret_value(), model=settings().typesafe_model)


async def ask_jev(state: dict, questions: dict) -> SystemOneResponse:
    """Ask Jev."""
    with tracer.start_as_current_span(
        "jev.system_one",
        attributes={
            "gen_ai.operation.name": "system_one",
            "gen_ai.system": "typesafe",
            "gen_ai.request.model": settings().typesafe_model,
        },
    ) as span:
        response = await jev_client().system_one(state=state, questions=questions)
        observed(span, {"state": state, "questions": questions}, response.answers)
        span.set_attributes(
            {
                "gen_ai.response.model": response.model,
                "gen_ai.usage.input_tokens": response.usage.input_tokens or 0,
                "gen_ai.usage.output_tokens": response.usage.output_tokens or 0,
            }
        )
        return response


def likely(yes: float) -> bool:
    """Whether yes is more likely than no."""
    return yes > 1 - yes


async def screen(request: str, previous: str, name: str, cards: list[str], tables: list[str]) -> Screen:
    """Screen and route a request against a document's contents."""
    views = None
    if f"all:{primary()}" not in PARTS:
        try:
            views = await halving(
                cards, lambda part: viewed(request, previous, "\n".join([name, *part, *tables])), f"screen:{primary()}"
            )
        except TypeSafeBadRequestError as error:
            if not too_long(error):
                raise
    if views is None:
        views = await halving(
            [*cards, *tables], lambda part: viewed(request, previous, "\n".join([name, *part])), f"all:{primary()}"
        )
    asked = {v.requested for v in views} - {"none"}
    return Screen(
        off_topic=all(v.off_topic for v in views),
        pressure=any(v.pressure for v in views),
        route=next((v.route for v in views if not v.off_topic), views[0].route),
        requested=asked.pop() if len(asked) == 1 else "none",
    )


async def viewed(request: str, previous: str, document: str) -> list[Screen]:
    """One screen of a request against part of a document."""
    questions = {k: Noul(instructions=v) for k, v in SCREEN.items()}
    questions["route"] = Choice(instructions=KIND, criteria=ROUTES)
    questions["requested"] = Choice(instructions=ASKED, criteria=REQUESTED)
    response = await ask_jev({"request": request, "previous": previous, "document": document}, questions)
    answers = response.answers
    return [
        Screen(
            **{k: likely(answers[k].noul) for k in SCREEN},
            route=answers["route"].choice,
            requested=answers["requested"].choice,
        )
    ]


async def unfounded(note: str, question: str, answer: str) -> bool:
    """Whether a note calls missing what the question or answer states."""
    response = await ask_jev(
        {"note": note, "question": question, "answer": answer}, {"unfounded": Noul(instructions=UNFOUNDED)}
    )
    return likely(response.answers["unfounded"].noul)


async def triage(question: str, answer: str, feedback: str) -> str:
    """Classify feedback on an answer."""
    kind = Choice(instructions="What does `feedback` say about `answer` to `question`?", criteria=TRIAGE)
    response = await ask_jev({"question": question, "answer": answer, "feedback": feedback}, {"kind": kind})
    return response.answers["kind"].choice


async def banking(excerpt: str) -> NoulAnswer:
    """Is this document about lending?"""
    response = await ask_jev({"excerpt": excerpt}, {"banking": Noul(instructions=TOPIC)})
    return response.answers["banking"]


PARTS: dict[str, int] = {}


def too_long(error: TypeSafeBadRequestError) -> bool:
    """Whether Jev refused a request as too long."""
    detail = error.body.get("detail") if isinstance(error.body, dict) else None
    return isinstance(detail, dict) and detail.get("error_type") == JEV_TOO_LONG


async def halving[T, R](items: list[T], ask: Callable[[list[T]], Awaitable[list[R]]], key: str) -> list[R]:
    """Ask Jev in as many parts as last fitted, halving any too long."""
    size = -(-len(items) // PARTS.get(key, 1)) or 1
    parts = await asyncio.gather(*(split(items[i : i + size], ask) for i in range(0, len(items), size) or [0]))
    PARTS[key] = max(PARTS.get(key, 1), sum(count for _, count in parts))
    return [answer for answers, _ in parts for answer in answers]


async def split[T, R](items: list[T], ask: Callable[[list[T]], Awaitable[list[R]]]) -> tuple[list[R], int]:
    """Ask Jev, halving if too long, and count the parts sent."""
    try:
        return await ask(items), 1
    except TypeSafeBadRequestError as error:
        if len(items) < 2 or not too_long(error):
            raise
        half = len(items) // 2
        (first, m), (second, n) = await asyncio.gather(split(items[:half], ask), split(items[half:], ask))
        return first + second, m + n


async def search(query: str, doc: tuple[Page, ...]) -> tuple[list[tuple[Page, float]], float]:
    """The relevant pages, and the chance that any page answers the query."""
    with tracer.start_as_current_span("search") as span:
        key = f"rank:{doc[0].doc}" if doc else "rank"
        parts = await halving(list(doc), lambda group: rank(query, group), key)
        scored = [hit for hits, _ in parts for hit in hits]
        hits = sorted((hit for hit in scored if likely(hit[1])), key=lambda x: -x[1])
        answered = max(answered for _, answered in parts)
        kept = [{"page": p.number, "relevance": s} for p, s in hits]
        observed(span, {"query": query, "pages": len(doc)}, {"kept": kept, "answered": answered})
        return hits, answered


async def rank(query: str, doc: list[Page]) -> list[tuple[list[tuple[Page, float]], float]]:
    """Score pages for a query, and whether any of them answers it."""
    keys = {f"page_{p.number}": p for p in doc}
    questions = {k: Noul(instructions=RELEVANT.format(key=k)) for k in keys}
    questions["answered"] = Noul(instructions=ANSWERED)
    response = await ask_jev({"query": query} | {k: p.text for k, p in keys.items()}, questions)
    return [([(p, response.answers[k].noul) for k, p in keys.items()], response.answers["answered"].noul)]
