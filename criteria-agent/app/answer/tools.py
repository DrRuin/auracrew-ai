"""The agent's tools."""

import asyncio
import operator
from collections.abc import AsyncIterator
from typing import Annotated, Literal

from pydantic import BaseModel
from sqlalchemy import ColumnElement
from strands import ToolContext, tool
from strands.agent.state import AgentState

from app.ai.jev import likely, search
from app.ai.llm import ask_model
from app.answer.verification import facts
from app.core import database
from app.core.config import settings
from app.core.documents import Page, locate, locations, pages, primary
from app.core.prompts import ASSESS, DESIGN, EXTRACT, HEADINGS
from app.core.schemas import Assessment, Condition, Criterion, Design, records
from app.core.state import remember


def holding(pieces: list[str]) -> Page | None:
    """The first page that holds every piece of a quote."""
    common = set.intersection(*(set(locations(piece, pages())) for piece in pieces)) if pieces else set()
    return min(common, key=lambda p: (p.doc != primary(), p.number), default=None)


def breached(criterion: Criterion) -> bool:
    """Whether a criterion blocks an approve."""
    return criterion.verdict == "breaks" or criterion.breaks_as_stated


@tool(context=True)
async def assess_case(
    case: Annotated[str, "The applicant's case in the user's words: loan, property, borrower and any other detail."],
    tool_context: ToolContext,
) -> AsyncIterator:
    """Assess a case against the document's criteria, criterion by criterion."""
    scoped = own(tool_context.invocation_state["corpus"])
    yield {"progress": "ranking", "count": len(scoped)}
    hits = [page for page, _ in (await search(case, scoped))[0]]
    yield {"progress": "kept", "pages": [p.number for p in hits]}
    text = "\n\n".join(f"Page {p.number}:\n{p.text}" for p in hits)
    found = (await ask_model(ASSESS, f"Case: {case}\n\n{text}", Assessment, reasoning=True)).criteria
    placed = [(c, holding(c.requirement)) for c in found]
    kept = [(c, page) for c, page in placed if page]
    widen(tool_context, sorted({page.number for _, page in kept}))
    evidence = [
        f"page {page.number} requires " + " and ".join(f'"{piece}"' for piece in c.requirement) for c, page in kept
    ]
    said = [
        f"{c.criterion}: {line}. Worked out: {c.case}; {c.verdict}. {c.reason}"
        for line, (c, _) in zip(evidence, kept, strict=True)
    ]
    unquoted = [
        f"{c.criterion}: breaks, though its requirement could not be quoted. Worked out: {c.case}. {c.reason}"
        for c, page in placed
        if not page and breached(c)
    ]
    met = any(c.verdict == "meets" and c.uses_case and not c.breaks_as_stated for c, _ in kept)
    broken = any(breached(c) for c, _ in placed) or not met
    known = tool_context.agent.state.get("assessed") or {"met": False, "broken": False}
    tool_context.agent.state.set("assessed", {"met": known["met"] or met, "broken": known["broken"] or broken})
    sourced(tool_context, {line: [page.number, c.requirement] for line, (c, page) in zip(evidence, kept, strict=True)})
    if not said + unquoted:
        yield "No criterion in the document applies."
        return
    output = "\n".join([*said, *unquoted])
    remember(tool_context.agent.state, "tool_outputs", [output])
    grounds = tool_context.agent.state.get("grounds") or {}
    tool_context.agent.state.set("grounds", {**grounds, output: text})
    yield output


OPS = {"=": operator.eq, "!=": operator.ne, "<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}


def own(corpus: tuple[Page, ...]) -> tuple[Page, ...]:
    """The guide's pages in a corpus."""
    return tuple(p for p in corpus if p.doc == primary())


def rows(hits: list[tuple[Page, float]]) -> list[dict]:
    """Search hits as the agent sees them."""
    return [{"page": p.number, "relevance": s, "text": p.text} for p, s in hits]


def settle(work: dict) -> None:
    """Cancel a question's unfinished side tasks."""
    for task in work.values():
        if isinstance(task, asyncio.Future):
            if task.done() and not task.cancelled():
                task.exception()
            task.cancel()


def listing(work: dict, state: AgentState) -> None:
    """Start listing the facts the pages read so far state, unless the question is about table rows or asks for a decision."""
    settle(work)
    read = state.get("retrieved")
    read = [p.number for p in pages()] if read is None else read
    asks = state.get("route") in ("table", "overview") or state.get("requested") not in (None, "none")
    context = [] if asks else [p for p in pages() if p.number in read]
    work["facts"] = asyncio.ensure_future(facts(state.get("query") or state.get("question"), context))


def sourced(tool_context: ToolContext, located: dict[str, list]) -> None:
    """Record where tool lines are printed."""
    tool_context.agent.state.set("rows", {**(tool_context.agent.state.get("rows") or {}), **located})


def widen(tool_context: ToolContext, numbers: list[int]) -> None:
    """Record newly read pages."""
    state = tool_context.agent.state
    known = set(state.get("retrieved") or [])
    if set(numbers) - known:
        state.set("retrieved", sorted(known | set(numbers)))
        listing(tool_context.invocation_state["work"], state)


async def contents() -> tuple[list[str], list[str]]:
    """The guide's page cards, and the code-made lines a claim may quote: its printed headings and its tables."""
    cards, sheets = await database.outline(primary())
    tables = [
        f'Table {s.number}, "{s.title}", runs over pages {", ".join(map(str, s.pages))} with {s.rows} rows. '
        f"Columns: {', '.join(c['name'] for c in s.columns)}."
        for s in sheets
    ]
    listed = [f"Page {c.page}: {c.title + '. ' if c.title else ''}{c.summary}" for c in cards]
    headings = "; ".join(f"Page {c.page}: {c.title}" for c in cards if c.title)
    return listed, [f"{HEADINGS}{headings}."] * bool(headings) + tables


async def contents_text() -> str:
    """The guide's contents as text."""
    cards, tables = await contents()
    return "\n".join([*cards, *tables])


@tool(context=True)
async def search_guide(
    query: Annotated[str, "What to look for in the guide."], tool_context: ToolContext
) -> AsyncIterator:
    """Find the guide pages relevant to a query."""
    scoped = own(tool_context.invocation_state["corpus"])
    yield {"progress": "ranking", "count": len(scoped)}
    hits, _ = await search(query, scoped)
    widen(tool_context, [p.number for p, _ in hits])
    yield {"progress": "kept", "pages": [p.number for p, _ in hits]}
    yield rows(hits)


@tool(context=True)
async def read_pages(
    numbers: Annotated[list[int], "Page numbers to read in full."], tool_context: ToolContext
) -> list[dict]:
    """Read whole guide pages by number."""
    doc = pages()
    if missing := [n for n in numbers if not 1 <= n <= len(doc)]:
        raise ValueError(f"pages {missing} do not exist; the guide has {len(doc)} pages")
    widen(tool_context, numbers)
    return [{"page": n, "text": doc[n - 1].text} for n in numbers]


def condition(wanted: Condition, columns: list[dict], known: list[set[str]]) -> ColumnElement[bool]:
    """A condition as SQL."""
    names = [c["name"] for c in columns]
    if wanted.column not in names:
        raise ValueError(f"no column {wanted.column!r}; the columns are {names}")
    i = names.index(wanted.column)
    if columns[i]["kind"] != "text":
        try:
            number = float(wanted.value)
        except ValueError:
            raise ValueError(f"{wanted.column} holds numbers; give a plain number, not {wanted.value!r}") from None
        return OPS[wanted.op](database.Line.values[i].as_float(), number)
    if wanted.value not in known[i]:
        raise ValueError(f"{wanted.column} has no value {wanted.value!r}; its values are {sorted(known[i])}")
    return OPS[wanted.op](database.Line.cells[i].as_string(), wanted.value)


def sentence(sheet: database.Sheet, line: database.Line) -> str:
    """A row as one quotable sentence."""
    named = zip(sheet.columns, line.cells, strict=True)
    cells = "; ".join(f"{c['name']}: {cell}" if c["name"] else cell for c, cell in named if cell)
    return f"Table {sheet.number}, row {line.row}, page {line.page}: {cells}."


@tool(context=True)
async def query_table(
    table: Annotated[int, "The table's number in the table list."],
    tool_context: ToolContext,
    numbers: Annotated[
        list[int] | None, "Row numbers to return, counting from 1 below the header across pages."
    ] = None,
    where: Annotated[list[Condition] | None, "Conditions every returned row meets."] = None,
    sort_by: Annotated[str | None, "A column to order the rows by, or to compare with extreme."] = None,
    descending: Annotated[bool, "Order from the largest value first."] = False,
    limit: Annotated[int | None, "Return at most this many rows."] = None,
    summary: Annotated[
        Literal["none", "count", "lowest", "highest", "values"],
        "none; count: how many rows match; lowest or highest: only the rows with that sort_by value; "
        "values: the distinct values of sort_by in the matching rows.",
    ] = "none",
) -> str:
    """Query a guide table: rows, counts, lowest or highest."""
    sheets = {s.number: s for s in (await database.outline(primary()))[1]}
    if table not in sheets:
        raise ValueError(f"no table {table}; the tables are {sorted(sheets)}")
    sheet, names = sheets[table], [c["name"] for c in sheets[table].columns]
    every = await database.lines(primary(), table)
    known = [{line.cells[i] for line in every} for i in range(len(names))]
    conditions = [Condition.model_validate(c) for c in where or []]
    filters = [condition(c, sheet.columns, known) for c in conditions]
    if numbers:
        filters.append(database.Line.row.in_(numbers))
    count, extreme = summary in ("count", "values"), summary if summary in ("lowest", "highest") else None
    if (sort_by or extreme or summary == "values") and sort_by not in names:
        raise ValueError(f"sort_by must name a column: {names}")
    i = names.index(sort_by) if sort_by else 0
    numeric = sheet.columns[i]["kind"] != "text"
    if extreme and not numeric:
        raise ValueError(f"{sort_by} holds text; extreme compares a number column")
    order = None
    if sort_by:
        order = database.Line.values[i].as_float() if numeric else database.Line.cells[i].as_string()
        order = order.desc().nulls_last() if descending else order.asc().nulls_last()
    found = await database.lines(primary(), table, *filters, order=order, limit=None if count or extreme else limit)
    stated = " and ".join(f"{c.column} {c.op} {c.value}" for c in conditions) or "no conditions"
    said = [f"Printed with table {table}: {' '.join(sheet.notes)}"] if sheet.notes else []
    named: dict[str, list] = {}
    if summary == "count":
        said.append(f'{len(found)} rows of table {table} "{sheet.title}" meet {stated}.')
    if summary == "values":
        distinct = ", ".join(dict.fromkeys(line.cells[i] for line in found if line.cells[i]))
        said.append(f'The {sort_by} values in table {table} "{sheet.title}" under {stated} are: {distinct}.')
        found = []
    if extreme:
        valued = [line for line in found if line.values[i] is not None]
        best = (min if extreme == "lowest" else max)((line.values[i] for line in valued), default=None)
        found = [line for line in valued if line.values[i] == best]
    if extreme and found:
        listed = ", ".join(f"row {line.row} ({line.cells[0]})" for line in found)
        said.append(
            f'The {extreme} {sort_by} in table {table} "{sheet.title}" under {stated} is {found[0].cells[i]}, in {listed}.'
        )
        named = {said[-1]: [found[0].page, found[0].cells]}
    said += [sentence(sheet, line) for line in found] or (
        [] if said else [f'No rows of table {table} "{sheet.title}" match.']
    )
    remember(tool_context.agent.state, "tool_outputs", ["\n".join(said)])
    sourced(tool_context, named | {sentence(sheet, line): [line.page, line.cells] for line in found})
    widen(tool_context, sorted({line.page for line in found}))
    return "\n".join(said)


@tool(context=True)
async def scan(
    instruction: Annotated[str, "What to find, such as every bedroom limit and the case it applies to."],
    tool_context: ToolContext,
) -> AsyncIterator:
    """Collect every instance of something from the pages that hold it."""
    scoped = own(tool_context.invocation_state["corpus"])
    yield {"progress": "ranking", "count": len(scoped)}
    prompt = f"Instruction: {instruction}\n\nThe document:\n{await contents_text()}"
    (hits, answered), design = await asyncio.gather(search(instruction, scoped), ask_model(DESIGN, prompt, Design))
    doc = sorted((page for page, _ in hits), key=lambda page: page.number) or (list(scoped) if likely(answered) else [])
    names = tuple(design.fields)
    yield {"progress": "designed", "fields": list(names), "pages": len(doc)}
    schema, gate = records(names), asyncio.Semaphore(settings().parallel_calls)

    async def read(page: Page) -> tuple[Page, BaseModel]:
        """One page's records."""
        async with gate:
            prompt = f"Instruction: {instruction}\nFields: {list(names)}\n\nPage {page.number}:\n{page.text}"
            return page, await ask_model(EXTRACT, prompt, schema)

    kept, seen = [], set()
    tasks = [asyncio.ensure_future(read(p)) for p in doc]
    try:
        for done, next_page in enumerate(asyncio.as_completed(tasks), start=1):
            page, extracted = await next_page
            for record in extracted.records:
                values = tuple(getattr(record, f"f{i}").strip() for i in range(len(names)))
                if values not in seen and locate(record.quote, (page,)):
                    seen.add(values)
                    kept.append((page.number, values, record.quote))
            yield {"progress": "read", "page": page.number, "done": done, "pages": len(doc), "records": len(kept)}
    finally:
        for task in tasks:
            task.cancel()
    kept.sort(key=lambda r: r[0])
    widen(tool_context, sorted({number for number, _, _ in kept}))
    yield [
        {"page": number, **dict(zip(names, values, strict=True)), "quote": quote} for number, values, quote in kept
    ] or "No page states what the instruction asks for."
