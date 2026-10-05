"""The reading tools."""

from types import SimpleNamespace

import pytest
from helpers import PAGES, State, returns

from app.answer import tools, verification
from app.core import database
from app.core.schemas import Check, Claim

pytestmark = pytest.mark.usefixtures("db")

COLUMNS = [{"name": "Code", "kind": "text"}, {"name": "Max LTV", "kind": "percent"}]
ROWS = [["AF-4B", "75%"], ["HM-1 A", "70%"], ["CB-2", "65%"], ["RG-1", "—"]]


@pytest.fixture
async def table(db):
    """A stored four-row table."""
    await database.save("guide", "guide", b"pdf", None)
    sheet = database.Sheet(document="guide", number=1, title="Products", columns=COLUMNS, pages=[1, 2], rows=len(ROWS))
    lines = [
        database.Line(
            document="guide",
            sheet=1,
            row=n,
            page=1 if n < 3 else 2,
            cells=cells,
            values=[None, reading_value(cells[1])],
        )
        for n, cells in enumerate(ROWS, start=1)
    ]
    await database.structure("guide", [], [sheet], lines)


def reading_value(cell: str) -> float | None:
    """The stored number of a percent cell."""
    return float(cell.rstrip("%")) if cell.endswith("%") else None


def context(**state: object) -> SimpleNamespace:
    """A tool context over the test guide."""
    return SimpleNamespace(agent=SimpleNamespace(state=State(state)), invocation_state={"corpus": PAGES, "work": {}})


@pytest.mark.usefixtures("table")
async def test_a_table_is_queried_in_code_by_row_number_condition_order_and_count():
    """Rows by number, condition, order, count and extreme."""
    query = tools.query_table._tool_func
    assert "Table 1, row 2, page 1:" in await query(1, context(), numbers=[2])
    over = await query(1, context(), where=[{"column": "Max LTV", "op": ">=", "value": "70"}])
    assert [line.split(":")[2].split(";")[0].strip() for line in over.splitlines()] == ["AF-4B", "HM-1 A"]
    assert "HM-1 A" in await query(1, context(), where=[{"column": "Code", "op": "=", "value": "HM-1 A"}])
    with pytest.raises(ValueError, match="its values are"):
        await query(1, context(), where=[{"column": "Code", "op": "=", "value": "hm-1a"}])
    top = await query(1, context(), sort_by="Max LTV", descending=True, limit=1)
    assert top.startswith("Table 1, row 1,") and "75%" in top
    counted = await query(1, context(), where=[{"column": "Max LTV", "op": "<", "value": "70"}], summary="count")
    assert counted.splitlines()[0] == '1 rows of table 1 "Products" meet Max LTV < 70.' and "CB-2" in counted
    lowest = await query(1, context(), sort_by="Max LTV", summary="lowest")
    assert (
        lowest.splitlines()[0]
        == 'The lowest Max LTV in table 1 "Products" under no conditions is 65%, in row 3 (CB-2).'
    )
    values = await query(1, context(), sort_by="Code", summary="values")
    assert values == 'The Code values in table 1 "Products" under no conditions are: AF-4B, CB-2, HM-1 A, RG-1.'
    with pytest.raises(ValueError, match="holds text"):
        await query(1, context(), sort_by="Code", summary="highest")
    with pytest.raises(ValueError, match="the columns are"):
        await query(1, context(), where=[{"column": "Rate", "op": "=", "value": "1"}])


@pytest.mark.usefixtures("table")
async def test_a_row_the_answer_quotes_is_cited_to_its_page_and_highlighted(monkeypatch):
    """A quoted row is cited to its page."""
    ctx = context()
    said = await tools.query_table._tool_func(1, ctx, numbers=[3])
    assert ctx.agent.state["tool_outputs"] == [said] and ctx.agent.state["retrieved"] == [2]
    monkeypatch.setattr(
        verification, "boxes", lambda quote, page: [(0.1, 0.2, 0.3, 0.4)] if quote == "CB-2 65%" else []
    )
    check = Check(
        claim=Claim(text="CB-2 allows 65%.", quote=said, source="tool"),
        verdict="supports",
        judge="jev",
        doc="tool",
        evidence=said,
    )
    cited = verification.cite(check, ctx.agent.state["rows"])
    assert (cited.page, cited.url, cited.boxes) == (2, "/documents/guide#page=2", [(0.1, 0.2, 0.3, 0.4)])


async def test_pages_are_read_whole_and_a_missing_page_is_named():
    """Pages are read whole."""
    ctx = context(question="q")
    assert (await tools.read_pages._tool_func([2], ctx))[0]["text"] == PAGES[1].text
    assert ctx.agent.state["retrieved"] == [2]
    with pytest.raises(ValueError, match="the guide has 2 pages"):
        await tools.read_pages._tool_func([3], ctx)


async def test_a_scan_designs_its_record_reads_the_pages_jev_keeps_and_keeps_only_quoted_unrepeated_records(
    monkeypatch,
):
    """A scan keeps quoted, new records."""
    monkeypatch.setattr(tools, "contents", returns(([], [])))

    async def ask_model(system, prompt, schema):
        """A fake model for the scan."""
        if system == tools.DESIGN:
            return tools.Design(fields=["limit", "applies to"])
        found = {
            "Page 1:": [("75%", "open market value", "Maximum LTV is 75%"), ("80%", "all", "Maximum LTV is 80%")],
            "Page 2:": [("75%", "open market value", "Maximum term 18 months.")],
        }
        page = next(key for key in found if key in prompt)
        return schema(records=[{"f0": a, "f1": b, "quote": q} for a, b, q in found[page]])

    monkeypatch.setattr(tools, "ask_model", ask_model)
    monkeypatch.setattr(tools, "search", returns(([(PAGES[1], 0.7), (PAGES[0], 0.9)], 0.9)))
    ctx = context(question="q")
    *progress, records = [event async for event in tools.scan._tool_func("every LTV limit", ctx)]
    assert progress[1] == {"progress": "designed", "fields": ["limit", "applies to"], "pages": 2}
    assert records == [{"page": 1, "limit": "75%", "applies to": "open market value", "quote": "Maximum LTV is 75%"}]
    assert ctx.agent.state["retrieved"] == [1]


@pytest.mark.usefixtures("table")
async def test_a_quote_spanning_rows_is_verified_and_boxes_each_row(monkeypatch, trusted, jev):
    """A multi-row quote is found and cited."""
    ctx = context()
    said = await tools.query_table._tool_func(1, ctx, numbers=[1, 2])
    jev("supports", 1.0)
    [check] = await verification.verify(
        [Claim(text="Two codes.", quote=said, source="tool")], ctx.agent.state["tool_outputs"], PAGES
    )
    monkeypatch.setattr(
        verification,
        "boxes",
        lambda quote, page: [(0.0, 0.0, 1.0, 1.0)] if quote in ("AF-4B 75%", "HM-1 A 70%") else [],
    )
    cited = verification.cite(check, ctx.agent.state["rows"])
    assert (check.verdict, cited.page, len(cited.boxes)) == ("supports", 1, 2)


@pytest.mark.usefixtures("table")
async def test_an_extreme_over_rows_without_numbers_finds_nothing_rather_than_every_row():
    """The lowest of rows that print no number is no row, not all of them."""
    said = await tools.query_table._tool_func(
        1, context(), where=[{"column": "Code", "op": "=", "value": "RG-1"}], sort_by="Max LTV", summary="lowest"
    )
    assert said == 'No rows of table 1 "Products" match.'


async def test_a_question_whose_search_kept_no_page_lists_no_facts_from_every_page(the_guide, monkeypatch):
    """An empty search result is not read as every page of the document."""
    seen = []

    async def facts(question: str, context: list) -> list:
        """Record the pages the fact lister is given."""
        seen.append(context)
        return []

    monkeypatch.setattr(tools, "facts", facts)
    work: dict = {}
    tools.listing(work, State({"question": "Submit a refer decision for A7.", "retrieved": []}))
    await work["facts"]
    assert seen == [[]]


async def test_a_decision_request_lists_no_facts_to_add(the_guide, monkeypatch):
    """Asking to submit a decision is not a question about the rules, so no criteria are appended to the answer."""
    seen = []

    async def facts(question: str, context: list) -> list:
        """Record the pages the fact lister is given."""
        seen.append(context)
        return []

    monkeypatch.setattr(tools, "facts", facts)
    for requested, kept in (("approve", 0), ("none", 1)):
        work: dict = {}
        tools.listing(work, State({"question": "q", "retrieved": [1], "requested": requested}))
        await work["facts"]
        assert len(seen.pop()) == kept


async def test_an_overview_lists_no_facts_to_add(the_guide, monkeypatch):
    """A summary keeps the shape the reader asked for: no key facts are appended to it."""
    seen = []

    async def facts(question: str, context: list) -> list:
        """Record the pages the fact lister is given."""
        seen.append(context)
        return []

    monkeypatch.setattr(tools, "facts", facts)
    for route, kept in (("overview", 0), ("lookup", 1)):
        work: dict = {}
        tools.listing(work, State({"question": "q", "retrieved": [1], "route": route}))
        await work["facts"]
        assert len(seen.pop()) == kept
