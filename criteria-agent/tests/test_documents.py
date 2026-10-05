"""Quote matching and citation boxes on real PDF pages."""

import pytest
from helpers import OTHER, PAGES, SAMPLE_PDF, claim

from app.answer import verification
from app.core import documents
from app.core.documents import Doc, Page
from app.core.schemas import Check


@pytest.fixture
def sample() -> Doc:
    """The sample guide, answered from."""
    pdf = SAMPLE_PDF.read_bytes()
    real = Doc(documents.edition(pdf), "guide", pdf)
    documents.GUIDE.set(real)
    return real


@pytest.mark.parametrize(
    ("quote", "found"),
    [
        ("maximum LTV is 75% of the open market value", "guide"),
        ("Maximum LTV is 85%", "other-lender"),
        ("Maximum LTV is 90%", None),
        ("   ", None),
    ],
    ids=["whitespace and case differ", "only in another document", "nowhere", "empty"],
)
def test_a_quote_is_located_on_the_first_page_that_holds_it_verbatim(quote, found):
    """Quotes match despite formatting."""
    page = documents.locate(quote, PAGES + OTHER)
    assert (page.doc if page else None) == found


def test_a_released_claim_links_to_its_page_and_boxes_the_quote(sample):
    """A citation boxes its quote."""
    quote = "Business must have a UK registered address and must operate entirely within the UK."
    cited = verification.cite(Check(claim=claim(quote), verdict="supports", judge="jev", page=5, doc=sample.id))
    assert cited.url == f"/documents/{sample.id}#page=5"
    assert cited.boxes and all(0 <= v <= 1 for box in cited.boxes for v in box)
    assert documents.boxes("Not a sentence in this guide.", 5) == []


def test_a_scanned_page_highlights_the_ocr_block_that_holds_the_quote(sample):
    """Scans highlight OCR blocks."""
    blocks = [
        {
            "type": "text",
            "bbox": [0.1, 0.2, 0.5, 0.3],
            "content": "Scanned text: minimum loan 150,000",
        },
        {"type": "table", "bbox": [0.1, 0.4, 0.9, 0.6], "content": "| Other | row |"},
    ]
    scanned = Doc(sample.id, "scan", sample.pdf, tuple({"text": "", "blocks": blocks} for _ in sample.layer))
    documents.GUIDE.set(scanned)
    assert documents.boxes("Minimum loan 150,000", 1) == [(0.1, 0.2, 0.5, 0.3)]
    assert documents.boxes("Not on the page", 1) == []


def test_a_document_reads_its_ocr_text_and_anchors_to_its_own_text_layer(sample):
    """OCR is read; the PDF anchors quotes."""
    read = Doc(
        sample.id,
        "guide",
        sample.pdf,
        tuple({"text": f"ocr {n}", "blocks": []} for n in range(len(sample.layer))),
    )
    assert [p.text for p in read.pages[:2]] == ["ocr 0", "ocr 1"]
    assert read.pages[0].layer == sample.layer[0] != ""


def test_text_inside_an_image_is_quotable_from_the_ocr_reading():
    """OCR text counts when the text layer lacks it."""
    page = Page("guide", 1, "Screenshot: Maximum loan £2m.", "Rates and terms.")
    assert documents.locate("Maximum loan £2m", (page,)) == page
    assert documents.locate("Rates and terms", (page,)) == page
    assert documents.locate("£2m. Rates", (page,)) is None


def test_a_short_quote_widens_to_its_row_under_its_heading_and_column_header():
    """The judge sees the row, the table header and the section heading."""
    text = "## Loadings\n\nAdded to the rate.\n\n| Feature | Loading (pm) |\n| --- | --- |\n| Expat | 0.10% |\n| Wales | 0.05% |"
    page = Page("guide", 3, text, text)
    assert documents.passage("0.05%", page) == "## Loadings\n| Feature | Loading (pm) |\n| Wales | 0.05% |"
    assert documents.passage("Added to the rate", page) == "## Loadings\nAdded to the rate."
    assert documents.passage("not on the page", page) == "not on the page"


def test_a_row_the_page_prints_twice_gets_no_heading():
    """A row repeated under two sections is shown alone, so the judge reads the page for its section."""
    text = "## AVM criteria\n\n| Size | £1m |\n| --- | --- |\n| **Indemnity A** |\n| Nationalities | UK, EU |\n| **Indemnity B** |\n| Nationalities | UK, EU |"
    page = Page("guide", 2, text, text)
    assert documents.passage("Nationalities | UK, EU", page) == "Nationalities | UK, EU"


def test_a_scanned_quote_is_boxed_by_the_band_of_block_lines_it_runs_through(sample):
    """A table row is boxed as its share of the block; a quote over two blocks gets a band in each."""
    blocks = [
        {"type": "title", "bbox": [0.1, 0.05, 0.9, 0.1], "content": "## Early repayment charges"},
        {
            "type": "table",
            "bbox": [0.1, 0.2, 0.9, 0.6],
            "content": "| Month | Charge | Partial |\n| --- | --- | --- |\n| Month 6 | 2% | Yes |\n| Month 7 | 2% | Yes |",
        },
    ]
    scanned = Doc(sample.id, "scan", sample.pdf, tuple({"text": "", "blocks": blocks} for _ in sample.layer))
    documents.GUIDE.set(scanned)
    assert documents.block_boxes("| Month 7 | 2% | Yes |", 1) == [pytest.approx((0.1, 0.2 + 0.8 / 3, 0.9, 0.6))]
    assert documents.block_boxes("Early repayment charges Month Charge", 1) == [
        (0.1, 0.05, 0.9, 0.1),
        pytest.approx((0.1, 0.2, 0.9, 0.2 + 0.4 / 3)),
    ]
    assert documents.block_boxes("Month 9", 1) == []


@pytest.mark.parametrize(
    ("texts", "unclear"),
    [
        (["SC-3B", "55%", "0.87%"], True),
        (["SC-3A", "65%", "0.67%"], False),
        (["SC-3B"], False),
    ],
    ids=["a figure the second reading disputes", "both readings agree", "the disputed figure not cited"],
)
def test_a_scanned_row_is_unclear_only_where_its_two_readings_differ(texts, unclear):
    """A cited text is disputed when the scan's second reading differs on it, found in reading order."""
    first = "SC-3A 65% 0.67%\nSC-3B 55% 0.87%"
    page = Page("d", 1, first, "", "SC-3A 65% 0.67%\nSC-3B 50% 0.87%")
    assert documents.disputed(texts, page) is unclear
    assert not documents.disputed(texts, Page("d", 1, first, ""))


def turned_pdf(turn: int) -> bytes:
    """A one-page PDF printing one line of text, shown turned clockwise by this angle."""
    stream = b"BT /F1 20 Tf 60 700 Td (Maximum LTV is 75 percent) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Rotate %d /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>" % turn,
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    body, offsets = b"%PDF-1.7\n", []
    for n, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body += b"%d 0 obj\n%s\nendobj\n" % (n, obj)
    xref = b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    xref += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    trailer = b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objects) + 1, len(body))
    return body + xref + trailer


@pytest.mark.parametrize(
    ("turn", "expected"),
    [
        (0, (0.1, 0.1, 0.51, 0.13)),
        (90, (0.87, 0.1, 0.9, 0.51)),
        (180, (0.49, 0.87, 0.9, 0.9)),
        (270, (0.1, 0.49, 0.13, 0.9)),
    ],
    ids=["upright", "turned 90", "turned 180", "turned 270"],
)
def test_a_quote_on_a_turned_page_is_boxed_where_the_page_shows_it(turn, expected):
    """Character boxes are read on the unrotated page and turned with it."""
    documents.GUIDE.set(Doc("t", "t", turned_pdf(turn)))
    box = documents.layer_boxes("Maximum LTV is 75 percent", 1)[0]
    assert box == pytest.approx(expected, abs=0.02)


@pytest.mark.parametrize(
    ("quote", "found"),
    [
        (
            "The records must be retained for a minimum of three years from the date on which the advice was given.",
            True,
        ),
        (
            "The records must be retained for a minimum of three years from the date on which the loan was repaid.",
            False,
        ),
    ],
)
def test_a_quote_running_over_a_page_break_is_found_on_the_page_it_starts_on(quote, found):
    """Running headers and margin labels between the two halves do not hide a real quote; a spliced one stays unfound."""
    first = Page("doc", 1, "Records. The records must be retained for a minimum of three years from the date on", "")
    second = Page(
        "doc", 2, "MCOB 4 : Advising and selling standards\nwhich the advice was given. Other rules follow.", ""
    )
    assert ([p.number for p in documents.locations(quote, (first, second))] == [1]) is found
