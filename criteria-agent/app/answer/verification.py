"""Claim checks, citations and completeness."""

import asyncio
import unicodedata
from itertools import groupby
from typing import Any

from typesafe_sdk import (
    Choice,
    Noul,
)

from app.ai.jev import ask_jev, halving, likely
from app.ai.llm import ask_model
from app.core.documents import (
    Box,
    Page,
    boxes,
    disputed,
    guide,
    link,
    locate,
    locations,
    normalize,
    pages,
    passage,
    pictures,
    primary,
)
from app.core.errors import REFUSAL
from app.core.prompts import ESCALATE, FACTS, HEADINGS, JUDGE, RELATIONS, STATES
from app.core.schemas import Check, Claim, Fact, Facts, rulings
from app.core.tracing import observed, tracer


def cite(check: Check, rows: dict[str, list] | None = None) -> Check:
    """Add a page link and highlights."""
    quote = normalize(check.claim.quote)
    quoted = [
        row for text, row in (rows or {}).items() if quote and (normalize(text) in quote or quote in normalize(text))
    ]
    if check.doc == "tool" and quoted:
        page = quoted[0][0]
        found = [box for p, cells in quoted if p == page for box in spot(cells, page)]
        update = {"page": page, "url": link(page), "boxes": found}
        said = figures(check.claim.text)
        stated = [
            [cell for cell in cells if normalize(cell) in normalize(check.claim.text) or figures(cell) & said] or cells
            for p, cells in quoted
            if p == page
        ]
        return legible(check.model_copy(update=update), stated)
    if check.doc == "tool" and (printed := locate(check.claim.quote, pages())):
        page = printed.number
        update = {"page": page, "url": link(page), "boxes": boxes(check.claim.quote, page)}
        return legible(check.model_copy(update=update), [[check.claim.quote]])
    if check.page is None or check.doc != primary():
        return check
    update = {"url": link(check.page), "boxes": boxes(check.claim.quote, check.page)}
    return legible(check.model_copy(update=update), [[check.claim.quote]])


def legible(check: Check, passages: list[list[str]]) -> Check:
    """The check, unclear when a second reading of its scanned page disputes what it cites."""
    if any(disputed(texts, pages()[check.page - 1]) for texts in passages):
        return check.model_copy(update={"verdict": "unclear"})
    return check


def spot(cells: list[str], page: int) -> list[Box]:
    """Highlight boxes for a table row."""
    whole = boxes(" ".join(cells), page)
    return whole or [box for cell in cells if len(normalize(cell)) > 1 for box in boxes(cell, page)]


async def cited(checks: list[Check], rows: dict[str, list] | None = None) -> list[Check]:
    """Cite released claims."""
    return await asyncio.to_thread(lambda: [cite(c, rows) for c in checks])


async def verify(
    claims: list[Claim],
    tool_outputs: list[str],
    corpus: tuple[Page, ...],
    grounds: dict[str, str] | None = None,
    question: str = "",
    read: set[int] | None = None,
) -> list[Check]:
    """Verify each claim."""
    with tracer.start_as_current_span("grounding.verify") as span:
        results = [(normalize(t), t) for t in tool_outputs]
        slots: list[Check | None] = [None] * len(claims)
        pending: dict[Page, list[tuple[int, Claim]]] = {}
        computed: list[tuple[int, Claim, str]] = []
        for i, claim in enumerate(claims):
            needle = normalize(claim.quote)
            found = (t for folded, t in results if needle and needle in folded)
            printed = locations(claim.quote, corpus)
            output = next(found, None) if claim.source == "tool" or not printed else None
            found_pages = [] if output else printed
            page = found_pages[0] if found_pages else None
            claim = claim.model_copy(update={"source": "tool" if output else "guide"})
            if output in (grounds or {}):
                source = (grounds or {})[output]
                verdict = "says_nothing" if source else "fabricated"
                slots[i] = Check(
                    claim=claim,
                    verdict=verdict,
                    judge="jev" if source else "code",
                    doc="tool",
                    evidence=source,
                    grounded=True,
                )
            elif output and output.startswith(REFUSAL) and normalize(claim.text) in normalize(output):
                slots[i] = Check(claim=claim, verdict="supports", judge="code", doc="tool", evidence=output)
            elif output:
                computed.append((i, claim, output))
            elif page is None:
                slots[i] = Check(claim=claim, verdict="fabricated", judge="code")
            elif page.doc != primary():
                slots[i] = Check(claim=claim, verdict="wrong_source", judge="code", doc=page.doc)
            else:
                for candidate in (p for p in found_pages if p.doc == page.doc):
                    pending.setdefault(candidate, []).append((i, claim))
        groups = list(pending.items())
        judged = await halving(groups, judge_group, f"judge:{primary()}") if groups else []
        tools = await tooled([(claim, output) for _, claim, output in computed])
        for (i, _), check in zip([item for _, items in groups for item in items], judged, strict=True):
            if slots[i] is None or preferred(check, read or set()) < preferred(slots[i], read or set()):
                slots[i] = check
        for (i, _, _), check in zip(computed, tools, strict=True):
            slots[i] = check
        others = sorted({c.claim.quote for c in slots if c and c.verdict not in ("fabricated", "wrong_source")})
        others += [line for line in tool_outputs if line.startswith(HEADINGS)]
        judged = await escalated(slots, guide().threshold, question, others, tool_outputs)
        checks = await confirmed(judged, question, others, tool_outputs)
        checks = await pooled(checks, question, tool_outputs, grounds or {})
        observed(
            span,
            claims,
            [{"verdict": c.verdict, "judge": c.judge, "page": c.page, "claim": c.claim.text} for c in checks],
        )
        span.set_attributes(
            {
                "grounding.claims": len(checks),
                "grounding.escalated": sum(c.judge == "gpt" for c in checks),
                "grounding.unsupported": sum(c.verdict != "supports" for c in checks),
                "grounding.wrong_source": sum(c.verdict == "wrong_source" for c in checks),
                "grounding.jev_threshold": guide().threshold,
            }
        )
        return checks


ORDER = {"supports": 0, "says_nothing": 1, "contradicts": 2}


def preferred(check: Check, read: set[int]) -> tuple[int, bool, float]:
    """How strongly a page backs a claim; pages the agent read first."""
    return ORDER[check.verdict], check.page not in read, -(check.jev_confidence or 0)


async def judge_group(group: list[tuple[Page, list[tuple[int, Claim]]]]) -> list[Check]:
    """Jev judges claims on their pages."""
    state = {f"page_{page.number}": page.text for page, _ in group}
    state |= {f"claim_{i}": claim.text for _, claims in group for i, claim in claims}
    questions = {
        f"claim_{i}_page_{page.number}": Choice(
            instructions=JUDGE.format(source=f"page_{page.number}", claim=f"claim_{i}"), criteria=RELATIONS
        )
        for page, claims in group
        for i, _ in claims
    }
    response = await ask_jev(state, questions)
    return [
        ruled(claim, response.answers[f"claim_{i}_page_{page.number}"], page=page.number, doc=page.doc)
        for page, claims in group
        for i, claim in claims
    ]


async def tooled(claims: list[tuple[Claim, str]]) -> list[Check]:
    """Jev judges each claim on the tool result its quote is in, in as many requests as fit."""

    async def ask(part: list[tuple[Claim, str]]) -> list[Check]:
        """One request over some of the claims."""
        results = list(dict.fromkeys(output for _, output in part))
        state = {f"result_{k}": result for k, result in enumerate(results)}
        state |= {f"claim_{n}": claim.text for n, (claim, _) in enumerate(part)}
        questions = {
            f"claim_{n}": Choice(
                instructions=JUDGE.format(source=f"result_{results.index(output)}", claim=f"claim_{n}"),
                criteria=RELATIONS,
            )
            for n, (_, output) in enumerate(part)
        }
        response = await ask_jev(state, questions)
        return [
            ruled(claim, response.answers[f"claim_{n}"], doc="tool", evidence=output)
            for n, (claim, output) in enumerate(part)
        ]

    return await halving(claims, ask, f"tool:{primary()}") if claims else []


def ruled(claim: Claim, answer: Any, **where: Any) -> Check:
    """A Jev verdict as a check."""
    return Check(
        claim=claim, verdict=answer.choice, judge="jev", jev=answer.choice, jev_confidence=answer.confidence, **where
    )


def figures(text: str) -> set[str]:
    """The runs of digits a text states, in any script."""
    runs = groupby(text, lambda c: unicodedata.decimal(c, None) is not None)
    return {"".join(str(unicodedata.decimal(c)) for c in run) for digit, run in runs if digit}


def trusted(check: Check, threshold: float) -> bool:
    """Whether Jev's verdict stands alone: confident, and every figure in the claim is in its own quote."""
    source = figures(check.claim.quote)
    confident = check.jev_confidence is not None and check.jev_confidence > threshold
    return check.jev == "supports" and confident and figures(check.claim.text) <= source


async def escalated(
    checks: list[Check], threshold: float, question: str, others: list[str], outputs: list[str]
) -> list[Check]:
    """Escalate verdicts Jev may not settle alone."""
    unsure: dict[tuple, list[int]] = {}
    for i, check in enumerate(checks):
        if check.judge == "jev" and not trusted(check, threshold):
            unsure.setdefault((check.doc, check.evidence or check.page), []).append(i)
    ruled = await asyncio.gather(
        *(escalate([checks[i] for i in group], question, others, outputs) for group in unsure.values())
    )
    checks = list(checks)
    for group, verdicts in zip(unsure.values(), ruled, strict=True):
        for i, check in zip(group, verdicts, strict=True):
            checks[i] = check
    return checks


async def confirmed(checks: list[Check], question: str, others: list[str], outputs: list[str]) -> list[Check]:
    """Rejudge at full reasoning each contradiction that would withhold a whole answer."""
    decisive = [i for i, check in enumerate(checks) if check.grounded and check.verdict == "contradicts"]
    again = await asyncio.gather(*(escalate([checks[i]], question, others, outputs, reasoning=True) for i in decisive))
    ruled = dict(zip(decisive, (check for [check] in again), strict=True))
    return [ruled.get(i, check) for i, check in enumerate(checks)]


async def pooled(checks: list[Check], question: str, outputs: list[str], grounds: dict[str, str]) -> list[Check]:
    """Rejudge claims their own quote says nothing about against the evidence set the answer rests on, never the model's working."""
    unsure = [i for i, check in enumerate(checks) if check.verdict == "says_nothing"]
    if not unsure:
        return checks
    cited = sorted({c.page for c in checks if c.page and c.doc in (None, primary())})
    shown = [f"Page {n}:\n{pages()[n - 1].text}" for n in cited]
    shown += [f"A tool's result:\n{o}" for o in outputs if o not in grounds]
    listed = "\n".join(f"claim_{n}: {checks[i].claim.text}" for n, i in enumerate(unsure))
    evidence = "\n\n".join(shown)
    prompt = f"The user's question: {question}\n\nEvidence the answer rests on:\n{evidence}\n\nClaims:\n{listed}"
    with tracer.start_as_current_span("grounding.pooled") as span:
        verdicts = (await ask_model(ESCALATE, prompt, rulings(len(unsure)))).model_dump()
        observed(span, {"claims": [checks[i].claim.text for i in unsure], "pages": cited}, verdicts)
    checks = list(checks)
    for n, i in enumerate(unsure):
        if verdicts[f"claim_{n}"] == "supports":
            checks[i] = checks[i].model_copy(update={"verdict": "supports", "judge": "gpt"})
    return checks


async def escalate(
    group: list[Check], question: str, others: list[str], outputs: list[str], reasoning: bool = False
) -> list[Check]:
    """GPT judges claims on one source, with the answer's other quotes and any tool result holding a figure it lacks."""
    head = group[0]
    source = f"Source:\n{head.evidence}" if head.doc == "tool" else f"Page {head.page}:\n{pages()[head.page - 1].text}"
    quoted = head.doc != "tool"
    listed = "\n\n".join(
        f"claim_{n}: {c.claim.text}" + (f"\nQuote: {passage(c.claim.quote, pages()[c.page - 1])}" if quoted else "")
        for n, c in enumerate(group)
    )
    held = tuple(c.claim.quote for c in group)
    extra = "".join(f"\n- {quote}" for quote in others if quote not in held)
    also = f"\n\nOther quotes this answer rests on:{extra}" if extra else ""
    seen = head.evidence if head.doc == "tool" else pages()[head.page - 1].text
    lacking = {f for c in group for f in figures(c.claim.text) if len(f) > 1} - figures(seen or "")
    near = [o for o in outputs if lacking and o != head.evidence and lacking & figures(o)]
    also += "".join(f"\n\nAnother result of a tool this answer used:\n{o}" for o in near)
    prompt = f"The user's question: {question}\n\n{source}{also}\n\nClaims:\n{listed}"
    shown = await asyncio.to_thread(pictures, guide(), head.page, held) if quoted else ()
    verdicts = (await ask_model(ESCALATE, prompt, rulings(len(group)), reasoning, shown)).model_dump()
    return [c.model_copy(update={"verdict": verdicts[f"claim_{n}"], "judge": "gpt"}) for n, c in enumerate(group)]


async def facts(question: str, context: list[Page]) -> list[Fact]:
    """Facts the pages state."""
    if not context:
        return []
    with tracer.start_as_current_span("grounding.facts") as span:
        pages_text = "\n\n".join(f"Page {p.number}:\n{p.text}" for p in context)
        listed = (await ask_model(FACTS, f"Question: {question}\n\n{pages_text}", Facts)).facts
        anchored = [f for f in listed if locate(f.quote, pages())]
        observed(span, {"question": question, "pages": [p.number for p in context]}, anchored)
        span.set_attributes({"omission.listed": len(listed), "omission.facts": len(anchored)})
        return anchored


async def omissions(released: str, facts: list[Fact]) -> list[Fact]:
    """Facts the answer missed."""
    if not facts:
        return []
    with tracer.start_as_current_span("grounding.omissions") as span:
        gone = await unstated(released, [f.text for f in facts])
        missing = [f for f, left in zip(facts, gone, strict=True) if left]
        observed(span, {"answer": released, "facts": facts}, missing)
        span.set_attribute("omission.missing", len(missing))
        return missing


async def unstated(answer: str, texts: list[str]) -> list[bool]:
    """Which texts an answer leaves out, in as many Jev requests as fit."""

    async def ask(part: list[str]) -> list[bool]:
        """One request over some of the texts."""
        keys = {f"fact_{i}": text for i, text in enumerate(part)}
        questions = {k: Noul(instructions=STATES.format(key=k)) for k in keys}
        response = await ask_jev({"answer": answer} | keys, questions)
        return [not likely(response.answers[k].noul) for k in keys]

    return await halving(texts, ask, f"omit:{primary()}")
