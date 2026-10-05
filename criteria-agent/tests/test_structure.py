"""Upload-time structure."""

import pytest

from app.core import database
from app.core.documents import Doc, Page
from app.core.schemas import Column
from app.ingest import structure
from app.ingest.structure import Found, Read

TEXTS = ("Products\nA-1 75%\nB-2 1st", "charge\nC-3 60%\nEnd of table.")
LAYERS = ("Products Code Max LTV A-1 75% B-2 1st", "charge C-3 60% End of table.")
COLUMNS = [Column(name="Code", kind="text"), Column(name="Max LTV", kind="percent")]
ROWS = {
    1: [("A-1", "75%", 75.0), ("B-2", "1st", None)],
    2: [("", "charge", None), ("C-3", "60%", 60.0), ("D-4", "99%", 99.0)],
}


def rate_sheet() -> Doc:
    """A two-page document whose table crosses the page break."""
    made = Doc("sheet", "sheet", b"pdf")
    pages = zip(TEXTS, LAYERS, strict=True)
    made.__dict__["pages"] = tuple(Page("sheet", n, text, layer) for n, (text, layer) in enumerate(pages, start=1))
    return made


async def test_a_table_across_a_page_break_is_stitched_joined_and_checked(monkeypatch):
    """Split rows join, unprinted rows and titles drop."""

    async def ask_model(system, prompt, schema, reasoning=False, images=()):
        """Answer as the model would."""
        page = 2 if "Page 2" in prompt else 1
        if system == structure.PAGE:
            found = Found(
                caption="Products", columns=COLUMNS, continues=page == 2, notes=["End of table.", "Invented note."]
            )
            return Read(title="Products" if page == 1 else "Invented heading", summary="s", tables=[found])
        return schema(rows=[{"c0": a, "c1": b, "n1": n} for a, b, n in ROWS[page]])

    stored = {}

    async def keep(document, cards, sheets, lines):
        """Capture what would be stored."""
        stored.update(cards=cards, sheets=sheets, lines=lines)

    monkeypatch.setattr(structure, "ask_model", ask_model)
    monkeypatch.setattr(database, "structure", keep)
    await structure.structure(rate_sheet())
    [sheet] = stored["sheets"]
    assert (sheet.pages, sheet.rows, sheet.notes) == ([1, 2], 3, ["End of table."])
    assert [(line.row, line.page, line.cells, line.values) for line in stored["lines"]] == [
        (1, 1, ["A-1", "75%"], [None, 75.0]),
        (2, 1, ["B-2", "1st charge"], [None, None]),
        (3, 2, ["C-3", "60%"], [None, 60.0]),
    ]
    assert [c.title for c in stored["cards"]] == ["Products", ""]


@pytest.mark.parametrize(
    ("cell", "value"),
    [("0.64 %", 0.64), ("£2,500,001", 2500001.0), ("18 months", 18.0), ("1st", None), ("12-24", None), ("", None)],
)
def test_a_number_cell_the_model_left_untyped_is_read_from_its_single_printed_number(cell, value):
    """Exactly one printed number, or nothing."""
    assert structure.single(cell) == value


async def test_a_full_row_left_without_its_key_is_not_merged_into_the_row_above(monkeypatch):
    """A first row with its own numbers is a row, not a fragment."""

    async def ask_model(system, prompt, schema, reasoning=False, images=()):
        """Page 2 starts with a complete row whose key the model left empty."""
        page = 2 if "Page 2" in prompt else 1
        if system == structure.PAGE:
            found = Found(caption="Products", columns=COLUMNS, continues=page == 2, notes=[])
            return Read(title="Products" if page == 1 else "", summary="s", tables=[found])
        rows = {1: [("A-1", "75%", 75.0)], 2: [("", "60%", 60.0), ("C-3", "60%", 60.0)]}[page]
        return schema(rows=[{"c0": a, "c1": b, "n1": n} for a, b, n in rows])

    stored = {}

    async def keep(document, cards, sheets, lines):
        """Capture what would be stored."""
        stored.update(lines=lines)

    monkeypatch.setattr(structure, "ask_model", ask_model)
    monkeypatch.setattr(database, "structure", keep)
    await structure.structure(rate_sheet())
    assert stored["lines"][0].values == [None, 75.0]


async def test_a_fragment_the_row_above_already_holds_is_not_added_twice(monkeypatch):
    """The page before may already show the whole cell that the next page finishes."""

    async def ask_model(system, prompt, schema, reasoning=False, images=()):
        """Page 1 reads the split row whole; page 2 still starts with its fragment."""
        page = 2 if "Page 2" in prompt else 1
        if system == structure.PAGE:
            found = Found(caption="Products", columns=COLUMNS, continues=page == 2, notes=[])
            return Read(title="Products" if page == 1 else "", summary="s", tables=[found])
        rows = {1: [("A-1", "75%", 75.0), ("B-2", "1st charge", None)], 2: [("", "charge", None), ("C-3", "60%", 60.0)]}
        return schema(rows=[{"c0": a, "c1": b, "n1": n} for a, b, n in rows[page]])

    stored = {}

    async def keep(document, cards, sheets, lines):
        """Capture what would be stored."""
        stored.update(lines=lines)

    monkeypatch.setattr(structure, "ask_model", ask_model)
    monkeypatch.setattr(database, "structure", keep)
    monkeypatch.setattr(structure, "printed", lambda *_: True)
    await structure.structure(rate_sheet())
    assert [line.cells for line in stored["lines"]][1] == ["B-2", "1st charge"]


KINDS = (("Code", "text"), ("Product", "text"), ("Security", "text"), ("Max LTV", "percent"))
TABLE = [
    (["RB-1A", "Residential Bridging", "Residential", "75%"], [], [1], True),
    (["CB-1A", "Commercial Bridging", "Commercial", "65%"], [], [1], True),
]


@pytest.mark.parametrize(
    ("cells", "split"),
    [
        (["DE-1A", "Development Exit Residential", "", "70%"], ["DE-1A", "Development Exit", "Residential", "70%"]),
        (["PB-1A", "Portfolio Bridging Residential", "Residential", "70%"], None),
        (["XX-1", "Something Else", "", "70%"], None),
    ],
)
def test_a_text_cell_read_into_its_left_neighbour_is_split_back_by_the_values_its_column_holds(cells, split):
    """Only an empty cell whose left neighbour ends with a value of that column is filled."""
    assert structure.unmerged(cells, TABLE, KINDS) == (split or cells)


NAMES = ["Code", "Product", "Term", "Rate"]


@pytest.mark.parametrize(
    ("text", "piece"),
    [
        (
            "|   | Bridging | months | % |\n| --- | --- | --- | --- |\n| RG-3A | Regulated | 18 | 0.77 |",
            ["", "Bridging", "months", "%"],
        ),
        ("| Code | Product | Term | Rate |\n| --- | --- | --- | --- |", None),
        ("| RG-3A | Regulated | 18 | 0.77 |", None),
        ("| Planning | | months % |", None),
        ("No table here.", None),
    ],
)
def test_a_page_that_starts_by_finishing_a_row_gives_that_fragment_as_ocr_printed_it(text, piece):
    """Only a first markdown row of the table's width, with an empty key and no column names."""
    assert structure.fragment(text, NAMES) == piece


TERMS = [(["RB-1A", "18 months", "0.59 %"], [], [1], True), (["RB-2A", "18 months", "0.73 %"], [], [1], True)]


@pytest.mark.parametrize(
    ("cells", "fixed"),
    [
        (["AF-4B", "12 months %", "0.91"], ["AF-4B", "12 months", "0.91 %"]),
        (["AF-4A", "12 months", "0.81 %"], None),
        (["AF-4C", "12 weeks", "0.81 %"], None),
    ],
)
def test_a_unit_read_onto_the_cell_before_it_moves_back_to_the_column_that_prints_it(cells, fixed):
    """Only a unit its own column never prints, and the next column always prints but lacks."""
    assert structure.shifted(cells, TERMS) == (fixed or cells)
