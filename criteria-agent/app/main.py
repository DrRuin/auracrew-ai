"""The HTTP entrypoint."""

import asyncio
import contextvars
import json
import logging
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import uuid4

import httpx
from fastapi import FastAPI, Header
from fastapi.requests import Request as HttpRequest
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.sse import EventSourceResponse
from opentelemetry import propagate
from strands import Agent
from strands.types.exceptions import StructuredOutputException

from app.answer.agent import build, prepare, settled, step, withheld
from app.answer.feedback import reviewed, said
from app.answer.tools import settle
from app.core import database
from app.core.config import settings
from app.core.documents import GUIDE, Doc, Page, forget, opened, pages
from app.core.errors import Unusable
from app.core.events import EVENTS
from app.core.schemas import Request, Result, hashed
from app.core.tracing import failure, langfuse, telemetry, trace_id, tracer
from app.ingest.uploads import receive
from app.journey import access, modules, tour


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Set up tracing and the request log once at startup."""
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    LOG.setLevel(logging.INFO)
    telemetry()
    yield


app = FastAPI(lifespan=lifespan, telemetry={"tracing": False})
SESSIONS: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
LOG = logging.getLogger("criteria")


async def respond(request: Request, agent: Agent, corpus: tuple[Page, ...]) -> dict:
    """Answer, re-answer or resume."""
    work: dict = {}
    reply = None
    if request.feedback is not None:
        retry, reply = await reviewed(request.feedback, agent.state.get("session"))
        if retry is None:
            return reply
        request = request.model_copy(update={"prompt": retry, "feedback": None})
    try:
        if request.approvals is None:
            refused, flags, message = await prepare(agent, request.prompt, corpus, work)
            if refused:
                return refused.model_dump(mode="json")
            agent.state.set("flags", flags)
        else:
            by = request.approver
            message = [
                {
                    "interruptResponse": {
                        "interruptId": i,
                        "response": {"approved": isinstance(action, str), "hash": action, "by": by},
                    }
                }
                for i, action in request.approvals.items()
            ]
        result = await settled(agent, await step(agent, message, corpus, work), corpus, work)
        if not isinstance(result, Result):
            return {
                "question": agent.state.get("question"),
                "status": "awaiting_approval",
                "approvals": [{"id": i.id, **i.reason} for i in result.interrupts],
                "trace_id": trace_id(),
            }
        return result.model_dump(mode="json") | ({"feedback": reply} if reply else {})
    except StructuredOutputException:
        return withheld(agent).model_dump(mode="json")
    finally:
        settle(work)


async def choose(request: Request, session: str) -> Doc:
    """The session's document."""
    chosen = request.document_id if request.prompt is not None else await database.recall(session)
    if chosen is None:
        raise Unusable("Name the uploaded document to ask about.")
    doc = await opened(chosen)
    if request.prompt is not None and await database.bind(session, chosen) != chosen:
        raise Unusable("This session is about another document; start a new session.")
    return doc


async def serve(request: Request, agent: Agent, corpus: tuple[Page, ...], session: str) -> dict:
    """Run a request in its trace."""
    asking = request.approvals is None
    kind = "question" if request.prompt is not None else "feedback" if asking else "approval"
    parent = None if asking else propagate.extract(agent.state.get("trace") or {})
    attributes = {
        "session.id": session,
        "langfuse.trace.tags": ["chat", kind],
        "langfuse.observation.input": request.prompt or request.model_dump_json(exclude_none=True),
    }
    with tracer.start_as_current_span(kind, context=parent, attributes=attributes) as span:
        if asking:
            carrier: dict[str, str] = {}
            propagate.inject(carrier)
            agent.state.set("trace", carrier)
        try:
            response = await respond(request, agent, corpus)
        except Exception as error:
            response = failure(span, error)
        span.set_attribute("langfuse.observation.output", said(response))
        return response


async def handle(payload: dict, session: str) -> dict:
    """Serve one request, one at a time per session."""
    async with SESSIONS[session]:
        start = time.perf_counter()
        response = await handled(payload, session)
        LOG.info(logged(payload, response, time.perf_counter() - start))
        return response


def logged(payload: dict, response: dict, seconds: float) -> str:
    """One log line per request: what was asked, how it ended, and the path of its Langfuse trace."""
    kind = next((k for k in ("prompt", "upload", "approvals", "feedback") if payload.get(k) is not None), "invalid")
    upload = payload.get("upload")
    asked = payload.get("prompt") or (upload.get("name") if isinstance(upload, dict) else "")
    trace = f"/langfuse/project/{settings().langfuse_project}/traces/{response.get('trace_id')}"
    return f"{kind} session={response['session_id']} status={response.get('status')} {seconds:.1f}s trace={trace} | {asked}"


async def handled(payload: dict, session: str) -> dict:
    """Serve one request."""
    try:
        request = Request.model_validate(payload)
        if request.upload is None:
            GUIDE.set(await choose(request, session))
            corpus = await asyncio.to_thread(pages)
            agent = build(corpus, "retrieve", session)
            agent.state.set("session", session)
    except Exception as error:
        attributes = {"session.id": session, "langfuse.trace.tags": ["chat", "rejected"]}
        with tracer.start_as_current_span("rejected", attributes=attributes) as span:
            response = failure(span, error)
    else:
        if request.upload is None:
            response = await serve(request, agent, corpus, session)
        else:
            response = await receive(request.upload, session)
    response = {"session_id": session, **response}
    try:
        await database.append(database.Run, response)
    except Exception:
        response["run_record"] = "failed"
    return response


async def invoke(payload: dict, session: str) -> AsyncIterator[dict]:
    """Stream a request as events."""
    queue: asyncio.Queue = asyncio.Queue()
    scope = contextvars.copy_context()
    scope.run(EVENTS.set, queue)
    work = asyncio.create_task(handle(payload, session), context=scope)
    work.add_done_callback(lambda _: queue.put_nowait(None))
    try:
        while (event := await queue.get()) is not None:
            yield event
        yield {"type": "done", **await work}
    finally:
        work.cancel()


@app.post("/invocations", response_class=EventSourceResponse)
async def invocations(payload: dict, x_session_id: Annotated[str | None, Header()] = None) -> AsyncIterator[dict]:
    """Stream one request's events, in the session the header names or a new one."""
    async for event in invoke(payload, x_session_id or str(uuid4())):
        yield event


@app.get("/ping")
async def ping() -> dict:
    """Health check."""
    return {"status": "Healthy"}


@app.get("/documents/{name}")
async def document(name: str) -> Response:
    """Serve a stored PDF."""
    try:
        content = await database.pdf(hashed(name))
    except (ValueError, LookupError):
        return JSONResponse({"error": "no such document"}, status_code=404)
    headers = {"Cache-Control": "private, max-age=31536000, immutable"}
    return Response(content, media_type="application/pdf", headers=headers)


@app.get("/documents")
async def documents() -> Response:
    """List uploaded documents."""
    return JSONResponse(await database.uploads())


async def forgetting() -> AsyncIterator[int]:
    """Delete every Langfuse trace a page at a time, yielding how many are gone so far."""
    config = settings()
    if config.langfuse_public_key is None or config.langfuse_secret_key is None:
        return
    base, deleted = config.langfuse_host, set()
    params = {"fields": "core", "fromStartTime": "1970-01-01T00:00:00Z"}
    async with langfuse() as client:
        while True:
            response = await client.get(f"{base}/api/public/v2/observations", params=params)
            page = response.raise_for_status().json()
            if fresh := {o["traceId"] for o in page["data"] if o.get("traceId")} - deleted:
                response = await client.request("DELETE", f"{base}/api/public/traces", json={"traceIds": sorted(fresh)})
                response.raise_for_status()
                deleted |= fresh
                yield len(deleted)
            if not (cursor := page["meta"].get("cursor")):
                return
            params["cursor"] = cursor


@app.post("/reset")
async def reset(request: HttpRequest) -> Response:
    """Clear all data."""
    if not settings().reset_enabled:
        return JSONResponse({"error": "reset is disabled on this deployment"}, status_code=403)
    if not request.headers.get("content-type", "").startswith("application/json"):
        return JSONResponse({"error": "send the reset as application/json"}, status_code=415)
    return StreamingResponse(resetting(), media_type="application/x-ndjson")


async def resetting() -> AsyncIterator[str]:
    """Clear the data, then the traces, streaming each stage as a line of JSON."""
    yield json.dumps({"stage": "data"}) + "\n"
    counts = await database.reset(uploads=True)
    forget()
    yield json.dumps({"stage": "data", "status": "done", **counts}) + "\n"
    deleted = 0
    yield json.dumps({"stage": "traces", "deleted": deleted}) + "\n"
    try:
        async for deleted in forgetting():
            yield json.dumps({"stage": "traces", "deleted": deleted}) + "\n"
        yield json.dumps({"stage": "traces", "status": "done", "deleted": deleted}) + "\n"
    except httpx.HTTPError as error:
        failed = {"status": "failed", "deleted": deleted, "reason": type(error).__name__}
        yield json.dumps({"stage": "traces", **failed}) + "\n"
    yield json.dumps({"stage": "done"}) + "\n"


@app.get("/journey")
async def journey() -> Response:
    """The code map and, when this deployment shows it, how to reach Langfuse."""
    return JSONResponse({"modules": modules(), "project": settings().langfuse_project, "langfuse": access()})


@app.get("/journey/tour/{name}")
async def touring(name: str) -> Response:
    """Questions written for one document that each show a control at work."""
    try:
        document = hashed(name)
        await database.document(document)
    except (ValueError, LookupError):
        return JSONResponse({"error": "no such document"}, status_code=404)
    return JSONResponse((await tour(document)).model_dump())
