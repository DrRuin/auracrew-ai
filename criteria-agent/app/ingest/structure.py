"""Page cards and tables, built at upload."""

import asyncio
import re
from collections import Counter

from opentelemetry.trace import Span
from pydantic import BaseModel

from app.ai.llm import ask_model, bounded
from app.core import database
from app.core.documents import Doc, normalize, pictures
from app.core.events import emit
from app.core.prompts import PAGE, ROWS
from app.core.schemas import Found, Read, layout
from app.core.tracing import observed, tracer

NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?!\w|\.\d)")


def printed(texts: list[str], page: int, doc: Doc) -> bool:
    """Whether each text appears on the page."""
    return all(normalize(text) in doc.pages[page - 1].anchor for text in texts if normalize(text))


async def read(page: int, doc: Doc, reasoning: bool = False) -> Read:
    """Read one page with the model."""
    before = f"Previous page:\n{doc.pages[page - 2].text}\n\n" if page > 1 else ""
    prompt = f"{before}Page {page}:\n{doc.pages[page - 1].text}"
    return await ask_model(PAGE, prompt, Read, reasoning, await asyncio.to_thread(pictures, doc, page))


def stitched(reads: list[Read]) -> list[list[tuple[int, Found]]]:
    """Group tables that continue across pages."""
    groups: list[list[tuple[int, Found]]] = []
    for page, found in enumerate(reads, start=1):
        for i, table in enumerate(found.tables):
            last = groups[-1] if groups else None
            if table.continues and i == 0 and last and last[-1][0] == page - 1:
                groups[-1].append((page, table))
            else:
                groups.append([(page, table)])
    return groups


def columns(group: list[tuple[int, Found]]) -> tuple[tuple[str, str], ...]:
    """A stitched table's columns."""
    return tuple((c.name, c.kind) for c in group[0][1].columns)


def single(cell: str) -> float | None:
    """The one number a cell prints, if it prints exactly one."""
    found = NUMBER.findall(cell.replace(",", ""))
    return float(found[0]) if len(found) == 1 else None


async def extract(group: list[tuple[int, Found]], page: int, doc: Doc) -> BaseModel:
    """One page's rows of a stitched table."""
    ask = f'The table "{group[0][1].caption}", columns {[name for name, _ in columns(group)]}.'
    before = f"Previous page, for context only:\n{doc.pages[page - 2].text}\n\n" if page != group[0][0] else ""
    current = doc.pages[page - 1]
    layer = (
        f"\n\nThe same page's exact text, one table row per line: keep every row it shows and copy values from it:\n{current.layer}"
        if current.layer.strip() and current.layer != current.text
        else ""
    )
    prompt = f"{ask}\n\n{before}Page {page}, the page to copy rows from:\n{current.text}{layer}"
    shown = await asyncio.to_thread(pictures, doc, page)
    return await ask_model(ROWS, prompt, layout(columns(group)), bool(before), shown)


def unmerged(cells: list[str], rows: list, kinds: tuple[tuple[str, str], ...]) -> list[str]:
    """A row whose text cell was read into the cell to its left, split back using the values that column holds elsewhere."""
    cells = list(cells)
    for n in range(1, len(cells)):
        if cells[n] or kinds[n][1] != "text":
            continue
        held = {other[0][n] for other in rows if other[0][n]}
        found = next((v for v in sorted(held, key=len, reverse=True) if cells[n - 1].endswith(f" {v}")), None)
        if found:
            cells[n - 1], cells[n] = cells[n - 1][: -len(found)].rstrip(), found
    return cells


def fragment(text: str, names: list[str]) -> list[str] | None:
    """The cells of the row a page starts by finishing, as OCR printed it, when its markdown row has the table's width."""
    first = next((line.strip() for line in text.splitlines() if line.strip().startswith("|")), "")
    cells = [cell.strip() for cell in first.strip("|").split("|")]
    if len(cells) != len(names) or cells[0] or not any(cells) or set(cells) & set(names):
        return None
    return cells


def tail(cell: str) -> str:
    """What a cell prints after its last digit, such as a unit."""
    found = re.search(r"\d([^\d]*)$", cell)
    return found.group(1).strip() if found else ""


def shifted(cells: list[str], rows: list) -> list[str]:
    """A row whose unit was read onto the cell before it, moved back where each column's other rows print it."""
    cells = list(cells)
    usual = [Counter(tail(other[0][n]) for other in rows if other[0][n]).most_common(1) for n in range(len(cells))]
    usual = [found[0][0] if found else "" for found in usual]
    for n in range(len(cells) - 1):
        here, after = cells[n], cells[n + 1]
        if tail(here) == usual[n] or " " not in here or not usual[n + 1] or tail(after) == usual[n + 1]:
            continue
        head, unit = here.rsplit(" ", 1)
        if tail(head) == usual[n] and tail(f"{after} {unit}") == usual[n + 1]:
            cells[n], cells[n + 1] = head, f"{after} {unit}"
    return cells


def table(
    number: int, group: list[tuple[int, Found]], pages: list[BaseModel], doc: Doc
) -> tuple[database.Sheet, list[database.Line]]:
    """One stitched table's rows, kept only if printed."""
    head, width = group[0][1], len(columns(group))
    rows: list[tuple[list[str], list[float | None], list[int], bool]] = []
    names = [name for name, _ in columns(group)]
    for (page, _), extracted in zip(group, pages, strict=True):
        read = [
            ([getattr(r, f"c{n}").strip() for n in range(width)], [getattr(r, f"n{n}", None) for n in range(width)])
            for r in extracted.rows
        ]
        piece = fragment(doc.pages[page - 1].text, names) if page != group[0][0] else None
        if piece and read and read[0][0][0]:
            read = [(piece, [None] * width), *read]
        for i, (cells, values) in enumerate(read):
            values = [
                single(cells[n]) if v is None and kind != "text" else v
                for n, (v, (_, kind)) in enumerate(zip(values, columns(group), strict=True))
            ]
            real = printed(cells, page, doc)
            clash = rows and any(a is not None and b is not None for a, b in zip(rows[-1][1], values, strict=True))
            if i == 0 and rows and not cells[0] and not clash:
                before, known, spans, ok = rows[-1]
                joined = [
                    a if b and a.endswith(b) else " ".join(filter(None, (a, b)))
                    for a, b in zip(before, cells, strict=True)
                ]
                merged = [a if a is not None else b for a, b in zip(known, values, strict=True)]
                rows[-1] = (joined, merged, [*spans, page], ok and real)
            else:
                rows.append((cells, values, [page], real))
    kept = [
        (shifted(unmerged(cells, rows, columns(group)), rows), values, spans, ok)
        for cells, values, spans, ok in rows
        if ok
    ]
    title = head.caption or f"Table {number}"
    emit("ingest", stage="table", number=number, title=title, rows=len(kept), dropped=len(rows) - len(kept))
    sheet = database.Sheet(
        document=doc.id,
        number=number,
        title=title,
        columns=[c.model_dump() for c in head.columns],
        pages=sorted({p for p, _ in group}),
        rows=len(kept),
        notes=list(dict.fromkeys(note for page, found in group for note in found.notes if printed([note], page, doc))),
    )
    lines = [
        database.Line(document=doc.id, sheet=number, row=n, page=spans[0], cells=cells, values=values)
        for n, (cells, values, spans, _) in enumerate(kept, start=1)
    ]
    return sheet, lines


async def structure(doc: Doc) -> None:
    """Build and store a document's cards and tables."""
    with tracer.start_as_current_span("structure") as span:
        await structured(doc, span)


async def structured(doc: Doc, span: Span) -> None:
    """Read every page, stitch its tables and store the result, showing it on the span."""
    emit("ingest", stage="structure", pages=len(doc.pages))
    reads = await bounded([lambda n=n: read(n, doc) for n in range(1, len(doc.pages) + 1)])
    after = [n for n in range(2, len(reads) + 1) if reads[n - 2].tables]
    again = await bounded([lambda n=n: read(n, doc, reasoning=True) for n in after])
    reads = [dict(zip(after, again, strict=True)).get(n, r) for n, r in enumerate(reads, start=1)]
    cards = [
        database.Card(document=doc.id, page=n, title=r.title if printed([r.title], n, doc) else "", summary=r.summary)
        for n, r in enumerate(reads, start=1)
    ]
    groups = stitched(reads)
    wanted = [(g, p) for g in groups for p, _ in g]
    extracted = iter(await bounded([lambda g=g, p=p: extract(g, p, doc) for g, p in wanted]))
    tables = [table(n, g, [next(extracted) for _ in g], doc) for n, g in enumerate(groups, start=1)]
    await database.structure(doc.id, cards, [s for s, _ in tables], [line for _, lines in tables for line in lines])
    rows = sum(len(lines) for _, lines in tables)
    observed(
        span,
        {"document": doc.name, "pages": len(doc.pages)},
        {
            "cards": [{"page": c.page, "title": c.title, "summary": c.summary} for c in cards],
            "tables": [{"title": s.title, "pages": s.pages, "rows": len(lines)} for s, lines in tables],
        },
    )
    emit("ingest", stage="structure", status="done", pages=len(cards), tables=len(tables), rows=rows)


async def ensure(doc: Doc) -> None:
    """Build a document's structure if missing."""
    if not (await database.outline(doc.id))[0]:
        await structure(doc)
