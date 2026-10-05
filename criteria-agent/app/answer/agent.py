"""The agent and the question loop."""

import asyncio
import json
from collections.abc import Callable
from typing import Any

from opentelemetry import trace
from strands import Agent
from strands.agent.agent_result import AgentResult
from strands.agent.conversation_manager import SummarizingConversationManager
from strands.agent.state import AgentState
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent
from strands.plugins import Plugin, hook
from strands.session.repository_session_manager import RepositorySessionManager
from strands.types.exceptions import StructuredOutputException
from strands.vended_plugins.context_injector import ContextInjector

from app.ai.jev import likely, screen, search, unfounded
from app.ai.llm import ask_model, model
from app.answer.tools import (
    assess_case,
    contents,
    contents_text,
    listing,
    own,
    query_table,
    read_pages,
    rows,
    scan,
    search_guide,
    settle,
)
from app.answer.verification import cited, omissions, verify
from app.core import database
from app.core.config import settings
from app.core.documents import Page, guide, pages, primary
from app.core.errors import CHECKED, CONTRADICTED, OUT_OF_SCOPE, REFUSAL, UNCLEAR, UNSTRUCTURED, UNVERIFIED
from app.core.events import emit
from app.core.prompts import (
    ANSWERABLE,
    ANSWERING,
    CONCLUDE,
    CONTENTS,
    PRESSURED,
    PRIMED,
    PROMPT,
    REASSESS,
    RETRIEVE,
    ROUTED,
    STUFF,
    SUGGEST,
    UNANSWERED,
    UNRELATED,
    UNSUPPORTED,
)
from app.core.schemas import (
    BOUNCED,
    RESULTS,
    Answer,
    Check,
    Claim,
    Fact,
    GroundedAnswer,
    Mode,
    Result,
    Screen,
    Suggestions,
    Unrelated,
)
from app.core.tracing import observed, telemetry, trace_id, tracer
from app.gate.gate import Approval, Policy, submit_decision


class Steps(Plugin):
    """Stream each tool call."""

    name = "steps"

    @hook
    def started(self, event: BeforeToolCallEvent) -> None:
        """Stream a tool's start."""
        use = event.tool_use
        emit("tool", id=use["toolUseId"], name=use["name"], status="start")

    @hook
    def finished(self, event: AfterToolCallEvent) -> None:
        """Stream a tool's result."""
        use, result = event.tool_use, event.result
        output = "\n".join(map(shown, result["content"]))
        emit("tool", id=use["toolUseId"], name=use["name"], status=result["status"], input=use["input"], output=output)


def primed(context: Any) -> str | None:
    """This question's route hint and the guide's contents."""
    return context.state.get("primer")


def stuffed(corpus: tuple[Page, ...]) -> str:
    """Every page of the corpus, for an agent that reads it all at once."""
    return STUFF.format(documents="\n\n".join(f'[document "{p.doc}", page {p.number}]\n{p.text}' for p in corpus))


MODES: dict[Mode, tuple[Callable[[tuple[Page, ...]], str], list]] = {
    "retrieve": (lambda _: RETRIEVE, [search_guide, read_pages, query_table, scan]),
    "stuff": (stuffed, []),
}


def build(
    corpus: tuple[Page, ...],
    mode: Mode,
    session_id: str | None = None,
    plugins: list[Plugin] | None = None,
) -> Agent:
    """Build the agent."""
    telemetry()
    sources, reading = MODES[mode]
    tools = [assess_case, submit_decision, *reading]
    return Agent(
        model=model(),
        system_prompt=PROMPT.format(guide=primary(), sources=sources(corpus)),
        tools=tools,
        interventions=[Policy(), Approval()],
        plugins=[Steps(), ContextInjector(primed, name="primer", trigger="everyTurn"), *(plugins or [])],
        conversation_manager=SummarizingConversationManager(proactive_compression=True),
        session_manager=RepositorySessionManager(session_id, database.Memory()) if session_id else None,
        callback_handler=None,
        structured_output_model=GroundedAnswer,
        name="criteria-agent",
        trace_attributes={"session.id": session_id} if session_id else None,
    )


async def release(
    answer: Answer, checks: list[Check], located: dict[str, list]
) -> tuple[list[Check], str, list[Check]]:
    """Decide which claims to release, citing them and holding back any the scan leaves unclear."""
    supported = [c for c in checks if c.verdict == "supports"]
    if not answer.answerable:
        return checks, "refused", []
    contradicted = [c for c in checks if c.verdict == "contradicts"]
    if not supported or any(c.grounded for c in contradicted):
        return checks, "withheld", []
    read = dict(zip(map(id, supported), await cited(supported, located), strict=True))
    checks = [read.get(id(c), c) for c in checks]
    released = [c for c in checks if c.verdict == "supports"]
    status = "grounded" if len(released) == len(checks) else "partial" if released else "withheld"
    return checks, status, released


async def judge(
    answer: Answer, state: Any, corpus: tuple[Page, ...], stage: str = "verify"
) -> tuple[list[Check], str, list[Check]]:
    """Verify and release an answer."""
    emit("stage", stage=stage, count=len(answer.claims))
    outputs, grounds = state.get("tool_outputs") or [], state.get("grounds") or {}
    read = set(state.get("retrieved") or [])
    asked = "\n".join(state.get("asked") or [state.get("question") or ""])
    checks = await verify(answer.claims, outputs, corpus, grounds, asked, read)
    emit(
        "stage",
        stage=stage,
        count=len(checks),
        status="done",
        details=[
            {
                "claim": c.claim.text,
                "quote": c.claim.quote,
                "verdict": c.verdict,
                "judge": c.judge,
                "page": c.page,
            }
            for c in checks
        ],
    )
    return await release(answer, checks, state.get("rows") or {})


def stated(answer: Answer, outcomes: list[str]) -> Answer:
    """The answer with each gate outcome of this turn, recorded or refused, as its own claim unless one quotes it."""
    missing = [o for o in outcomes if not any(len(c.quote) > 20 and c.quote in o for c in answer.claims)]
    if not missing:
        return answer
    told = [Claim(text=o.removeprefix(REFUSAL), quote=o, source="tool") for o in missing]
    return answer.model_copy(update={"answerable": True, "claims": [*told, *answer.claims]})


async def finish(
    agent: Agent, run: AgentResult, corpus: tuple[Page, ...], work: dict, retry: bool = False
) -> Result | None:
    """Verify, complete and stream the answer; None when a contradicted assessment is worth one more try."""
    state = agent.state
    question = state.get("question")
    answer: Answer = await conclude(agent) if run.stop_reason == "limit_turns" else run.structured_output
    answer = stated(answer, state.get("outcomes") or [])
    if answer.answerable and "facts" not in work:
        listing(work, state)
    known = asyncio.gather(work["facts"], return_exceptions=True) if answer.answerable else asyncio.sleep(0, [[]])
    (checks, status, released), [listed] = await asyncio.gather(judge(answer, state, corpus), known)
    faulted = [c.claim.text for c in checks if c.grounded and c.verdict == "contradicts"]
    if retry and status == "withheld" and faulted and not state.get("gate"):
        work["again"] = ("reassess", faulted, REASSESS.format(findings="\n".join(f"- {text}" for text in faulted)))
        return None
    unsupported = [c.claim.text for c in checks if c.verdict != "supports"]
    if retry and status == "withheld" and checks and not released and not state.get("gate"):
        listed_text = "\n".join(f"- {text}" for text in unsupported)
        work["again"] = ("unsupported", unsupported, UNSUPPORTED.format(findings=listed_text))
        return None
    if retry and not answer.answerable and state.get("retrieved") and not state.get("gate"):
        work["again"] = ("answerable", [answer.note], ANSWERABLE)
        return None
    following = work["suggestions"] = asyncio.ensure_future(suggest(question, released))
    failed = isinstance(listed, BaseException)
    emit(
        "stage",
        stage="completeness",
        **({"status": "failed", "reason": type(listed).__name__} if failed else {"count": len(listed)}),
    )
    listed = [] if failed else listed
    missed = await omissions(" ".join(c.claim.text for c in released), listed) if released else listed
    if not failed:
        covered(listed, missed)
    result = Result(
        question=question,
        answer=answer,
        checks=checks,
        status=status,
        released=released,
        omissions=missed,
        gate=state.get("gate") or [],
        flags=state.get("flags") or [],
        route=state.get("route"),
        trace_id=trace_id(),
    )
    if missed and released:
        result = await repair(result, listed, state, corpus)
    dropped = result.answer.note and result.released and await unfounded(result.answer.note, question, result.text())
    result.answer = result.answer.model_copy(update={"note": noted("" if dropped else result.answer.note, result)})
    speak(result)
    result.suggestions = await following
    return result


async def repair(result: Result, listed: list[Fact], state: AgentState, corpus: tuple[Page, ...]) -> Result:
    """Add the facts the answer missed as their own claims, keeping those the guide supports."""
    missed = result.omissions
    emit("stage", stage="repair", count=len(missed), details={"missing": [f.text for f in missed]})
    extra = Answer(answerable=True, claims=[Claim(text=f.text, quote=f.quote, source="guide") for f in missed], note="")
    new, _, added = await judge(extra, state, corpus, stage="recheck")
    dropped = [c for c in new if c.verdict != "supports"]
    emit(
        "stage",
        stage="repaired",
        count=len(added),
        status="done",
        details={
            "added": [c.claim.text for c in added],
            "dropped": [{"claim": c.claim.text, "verdict": c.verdict, "page": c.page} for c in dropped],
        },
    )
    if not added:
        return result
    left = [f for f, c in zip(missed, new, strict=True) if c.verdict != "supports"]
    covered(listed, left)
    claims = [*result.answer.claims, *(c.claim for c in added)]
    return result.model_copy(
        update={
            "answer": result.answer.model_copy(update={"claims": claims}),
            "checks": [*result.checks, *new],
            "released": [*result.released, *added],
            "status": "grounded" if result.status == "grounded" and not dropped else "partial",
            "omissions": left,
            "repaired": True,
        }
    )


def noted(note: str, result: Result) -> str:
    """The answer's note with what the checks must tell the reader."""
    unclear = [UNCLEAR] * any(c.verdict == "unclear" for c in result.checks)
    contradicted = result.status == "partial" and any(c.verdict == "contradicts" for c in result.checks)
    told = " ".join([note, *unclear, *[CONTRADICTED] * contradicted]).strip()
    return told or (UNVERIFIED if result.status == "withheld" else "")


def gathered(message: dict) -> list[str]:
    """A message as plain text."""
    said = []
    for block in message["content"]:
        if "text" in block:
            said.append(block["text"])
        elif "toolUse" in block:
            said.append(f"Called {block['toolUse']['name']} with {json.dumps(block['toolUse']['input'])}")
        elif "toolResult" in block:
            said.append("\n".join(map(shown, block["toolResult"]["content"])))
    return said


def asked(agent: Agent) -> int:
    """Where this question's message sits in the history, wherever trimming has moved it."""
    opening = [{"text": agent.state.get("question")}]
    starts = (i for i, m in enumerate(agent.messages) if m["role"] == "user" and m["content"][:1] == opening)
    return max(starts, default=0)


async def conclude(agent: Agent) -> Answer:
    """Answer from what was gathered."""
    emit("stage", stage="budget", count=settings().max_turns)
    history = [text for m in agent.messages[asked(agent) :] for text in gathered(m)]
    BOUNCED.set([])
    RESULTS.set(lambda: agent.state.get("tool_outputs") or [])
    closing = Agent(model=model(), system_prompt=agent.system_prompt, callback_handler=None, name="conclude")
    prompt = f"{CONCLUDE}\n\n{agent.state.get('primer') or ''}\n\n" + "\n\n".join(history)
    answer = (await closing.invoke_async(prompt, structured_output_model=GroundedAnswer)).structured_output
    emit("stage", stage="budget", count=len(answer.claims), status="done")
    return answer


async def suggest(question: str, released: list[Check]) -> list[str]:
    """Suggest three follow-up questions."""
    try:
        answer = " ".join(c.claim.text for c in released)
        prompt = f"Question: {question}\n\nAnswer: {answer}\n\n{CONTENTS.format(pages=len(pages()))}\n{await contents_text()}"
        offered = await ask_model(SUGGEST, prompt, Suggestions)
    except Exception:
        return []
    questions = [offered.first, offered.second, offered.third]
    emit("suggestions", questions=questions)
    return questions


def covered(listed: list[Fact], missed: list[Fact]) -> None:
    """Stream fact coverage."""
    emit(
        "stage",
        stage="completeness",
        count=len(listed),
        status="done",
        details={"facts": [f.text for f in listed], "missing": [f.text for f in missed]},
    )


def speak(result: Result) -> None:
    """Stream the released claims."""
    for number, check in enumerate(result.released, start=1):
        emit(
            "claim", number=number, text=check.claim.text, citation=check.model_dump(mode="json", exclude={"evidence"})
        )
    if result.answer.note or not result.released:
        emit("note", text=result.answer.note or result.status)


def empty(question: str, status: str, note: str, flags: list[str], gate: list[str] | None = None) -> Result:
    """A result with no answer."""
    result = Result(
        question=question,
        answer=Answer(answerable=False, claims=[], note=note),
        status=status,
        gate=gate or [],
        flags=flags,
        trace_id=trace_id(),
    )
    speak(result)
    return result


def withheld(agent: Agent) -> Result:
    """The result when no structured answer came back."""
    state = agent.state
    return empty(state.get("question"), "withheld", UNSTRUCTURED, state.get("flags") or [], state.get("gate"))


async def screened(
    question: str, previous: str, cards: list[str], tables: list[str]
) -> tuple[Result | None, list[str], Screen]:
    """Screen and route a question."""
    emit("stage", stage="screen")
    with tracer.start_as_current_span("screen") as span:
        view = await screen(question, previous, guide().name, cards, tables)
        if view.off_topic:
            asked = f"Request: {question}" + (f"\nPrevious request: {previous}" if previous else "")
            view = view.model_copy(update={"off_topic": (await ask_model(UNRELATED, asked, Unrelated)).unrelated})
        observed(span, {"question": question, "previous": previous}, view)
    emit("stage", stage="screen", status="done", details=view.model_dump())
    trace.get_current_span().set_attributes(
        {"screen.off_topic": view.off_topic, "screen.pressure": view.pressure, "screen.route": view.route}
    )
    flags = ["pressure"] if view.pressure else []
    if not view.off_topic:
        return None, flags, view
    return empty(question, "refused", OUT_OF_SCOPE, [*flags, "out_of_scope"]), flags, view


def shown(block: dict) -> str:
    """A tool result as text."""
    value = block.get("text", block.get("json"))
    return value if isinstance(value, str) else json.dumps(value)


async def prefetch(question: str, corpus: tuple[Page, ...]) -> tuple[list[tuple[Page, float]], float]:
    """Search the guide for the question."""
    scoped = own(corpus)
    emit(
        "tool",
        id="prefetch",
        name="search_guide",
        status="progress",
        progress="ranking",
        count=len(scoped),
    )
    hits, answered = await search(question, scoped)
    found = rows(hits)
    emit(
        "tool",
        id="prefetch",
        name="search_guide",
        status="success",
        pages=[row["page"] for row in found],
        input={"query": question},
        output=json.dumps(found),
    )
    return hits, answered


async def prepare(
    agent: Agent, question: str, corpus: tuple[Page, ...], work: dict
) -> tuple[Result | None, list[str], str | list[dict]]:
    """Screen a question and prime the agent."""
    state, previous = agent.state, agent.state.get("question") or ""
    state.set("question", question)
    state.set("asked", [*(state.get("asked") or []), question])
    for key in (
        "gate",
        "outcomes",
        "declined",
        "assessed",
        "approved",
        "requested",
        "rows",
        "route",
        "primer",
        "query",
    ):
        state.delete(key)
    cards, tables = await contents()
    if "search_guide" not in agent.tool_names:
        refused, flags, view = await screened(question, previous, cards, tables)
        state.set("requested", view.requested)
        return refused, flags, question
    query = f"{question}\n(asked after: {previous})" if previous else question
    state.set("query", query)
    searching = asyncio.ensure_future(prefetch(query, corpus))
    try:
        refused, flags, view = await screened(question, previous, cards, tables)
        state.set("requested", view.requested)
        if refused:
            return refused, flags, question
        hits, answered = await searching
    finally:
        searching.cancel()
    route = view.route
    state.set("route", route)
    earlier = state.get("tool_outputs") or []
    state.set("tool_outputs", list(dict.fromkeys([*tables, *([CHECKED] if flags else []), *earlier])))
    state.set("retrieved", sorted(p.number for p, _ in hits))
    listing(work, state)
    unanswered = not likely(answered) and view.requested == "none"
    hint = f"{ANSWERING}{question}\n{ROUTED[route]}" + (f"\n{UNANSWERED}" if unanswered else "")
    hint += f"\n{PRESSURED}" if flags else ""
    listed = [CONTENTS.format(pages=len(pages())), *cards, *tables]
    state.set("primer", "\n".join([hint, "", *listed]) if cards or tables else hint)
    found = f"{PRIMED}\n{json.dumps(rows(hits), ensure_ascii=False)}"
    return None, flags, [{"text": question}, {"text": found}]


async def step(agent: Agent, message: str | list, corpus: tuple[Page, ...], work: dict) -> AgentResult:
    """Run the agent and stream its steps, each answer bouncing misquotes afresh."""
    BOUNCED.set([])
    RESULTS.set(lambda: agent.state.get("tool_outputs") or [])
    run = None
    state = {"corpus": corpus, "work": work}
    stream = agent.stream_async(message, invocation_state=state, limits={"turns": settings().max_turns})
    async for event in stream:
        if progress := event.get("tool_stream_event"):
            data, tool = progress.get("data"), progress["tool_use"]
            if isinstance(data, dict) and "progress" in data:
                emit("tool", id=tool["toolUseId"], name=tool["name"], status="progress", **data)
        run = event.get("result", run)
    if run is None:
        raise StructuredOutputException("the agent stopped without a result")
    return run


async def settled(
    agent: Agent,
    run: AgentResult,
    corpus: tuple[Page, ...],
    work: dict,
    approve: Callable[[dict], Any] | None = None,
    retry: bool = True,
) -> Result | AgentResult:
    """Finish a run, giving a contradicted assessment or a declined answerable question one more turn; a pause without an approver comes back as it is."""
    while run.stop_reason == "interrupt":
        if approve is None:
            return run
        run = await step(agent, replies(run, approve), corpus, work)
    if result := await finish(agent, run, corpus, work, retry):
        return result
    stage, findings, message = work.pop("again")
    emit("stage", stage=stage, details={"findings": findings})
    again = await step(agent, message, corpus, work)
    return await settled(agent, again, corpus, work, approve, retry=False)


def replies(run: AgentResult, decide: Callable[[dict], Any]) -> list[dict]:
    """Answer pending approvals."""
    return [{"interruptResponse": {"interruptId": i.id, "response": decide(i.reason)}} for i in run.interrupts]


async def ask(
    question: str,
    approve: Callable[[dict], Any],
    corpus: tuple[Page, ...] | None = None,
    mode: Mode = "retrieve",
    plugins: list[Plugin] | None = None,
) -> Result:
    """Answer one question end to end."""
    corpus, work = corpus or pages(), {}
    telemetry()
    attributes = {"criteria.mode": mode, "langfuse.observation.input": question}
    with tracer.start_as_current_span("question", attributes=attributes) as span:
        agent = build(corpus, mode, plugins=plugins)
        try:
            refused, flags, message = await prepare(agent, question, corpus, work)
            if refused:
                result = refused
            else:
                agent.state.set("flags", flags)
                result = await settled(agent, await step(agent, message, corpus, work), corpus, work, approve)
        except StructuredOutputException:
            result = withheld(agent)
        finally:
            settle(work)
        span.set_attributes(
            {
                "criteria.status": result.status,
                "criteria.repaired": result.repaired,
                "langfuse.observation.output": result.text() or result.status,
            }
        )
        return result
