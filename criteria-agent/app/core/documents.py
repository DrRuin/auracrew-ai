"""Documents, quotes and highlight boxes."""

import hashlib
import io
import threading
import unicodedata
from collections import OrderedDict
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import cached_property

import pypdfium2
from PIL import Image

from app.core import database
from app.core.config import settings

Box = tuple[float, float, float, float]
TURNS: dict[int, Callable[[float, float], tuple[float, float]]] = {
    0: lambda x, y: (x, y),
    90: lambda x, y: (1 - y, x),
    180: lambda x, y: (1 - x, 1 - y),
    270: lambda x, y: (y, 1 - x),
}


PDFIUM = threading.Lock()


@dataclass(frozen=True)
class Page:
    """One page."""

    doc: str
    number: int
    text: str
    layer: str
    second: str = ""

    @cached_property
    def anchor(self) -> str:
        """The text layer and OCR text, reduced for quote checks."""
        return f"{normalize(self.layer)}|{normalize(self.text)}"

    @cached_property
    def glyphs(self) -> tuple[str, tuple[int, ...]]:
        """The text layer reduced, with each kept character's place in it."""
        kept = folded(self.layer)
        return "".join(c for c, _ in kept), tuple(i for _, i in kept)

    @cached_property
    def doubted(self) -> tuple[bool, ...]:
        """For each reduced character of the OCR text, whether the scan's second reading disagrees."""
        first = normalize(self.text)
        marks = [True] * len(first)
        for start, _, size in SequenceMatcher(
            None, first, normalize(self.second), autojunk=False
        ).get_matching_blocks():
            marks[start : start + size] = [False] * size
        return tuple(marks)


@dataclass(frozen=True, eq=False)
class Doc:
    """A loaded document."""

    id: str
    name: str
    pdf: bytes
    ocr: tuple[dict, ...] = ()
    threshold: float = 1.0

    @cached_property
    def version(self) -> str:
        """The PDF's hash."""
        return hashlib.sha256(self.pdf).hexdigest()

    @cached_property
    def layer(self) -> tuple[str, ...]:
        """The PDF text per page."""
        with PDFIUM, pypdfium2.PdfDocument(self.pdf) as pdf:
            return tuple(p.get_textpage().get_text_range() for p in pdf)

    @cached_property
    def pages(self) -> tuple[Page, ...]:
        """The pages to read."""
        texts = tuple(p["text"] for p in self.ocr) or self.layer
        seconds = tuple(p.get("second", "") for p in self.ocr) or ("",) * len(self.layer)
        return tuple(
            Page(self.id, i, text, raw, second)
            for i, (text, raw, second) in enumerate(zip(texts, self.layer, seconds, strict=True), start=1)
        )


GUIDE: ContextVar[Doc] = ContextVar("guide")


DOCS: OrderedDict[str, Doc] = OrderedDict()


def edition(pdf: bytes) -> str:
    """A document's id."""
    return hashlib.sha256(pdf).hexdigest()[:16]


async def opened(id: str) -> Doc:
    """Load a stored document."""
    if id not in DOCS:
        row, threshold = await database.document(id)
        DOCS[id] = Doc(row.id, row.name, row.pdf, tuple(row.pages or ()), threshold)
        while len(DOCS) > settings().open_documents:
            DOCS.popitem(last=False)
    DOCS.move_to_end(id)
    return DOCS[id]


def forget(id: str | None = None) -> None:
    """Drop one cached document, or all of them."""
    if id is None:
        DOCS.clear()
    DOCS.pop(id, None)


def guide() -> Doc:
    """This request's document."""
    return GUIDE.get()


def primary() -> str:
    """This request's document id."""
    return guide().id


def pages() -> tuple[Page, ...]:
    """This request's pages."""
    return guide().pages


def folded(text: str) -> list[tuple[str, int]]:
    """Text reduced for quote checks."""
    kept = []
    for i, char in enumerate(text):
        point = char == "." and 0 < i < len(text) - 1 and text[i - 1].isdigit() and text[i + 1].isdigit()
        for c in unicodedata.normalize("NFKD", char.casefold()):
            c = str(unicodedata.decimal(c, c))
            if point or c == "%" or unicodedata.category(c).startswith(("L", "N", "Sc")):
                kept.append((c, i))
    return kept


SPLIT = 20


def normalize(text: str) -> str:
    """Text as quotes are compared."""
    return "".join(c for c, _ in folded(text))


def opening(needle: str, here: str) -> int:
    """How much of a reduced quote, from its start, a reduced text holds."""
    low, high = 0, len(needle)
    while low < high:
        middle = (low + high + 1) // 2
        low, high = (middle, high) if needle[:middle] in here else (low, middle - 1)
    return low


def spans(needle: str, here: str, after: str) -> bool:
    """Whether a reduced quote starts in one page's text and ends in the next, whatever prints between them."""
    held = opening(needle, here)
    return min(held, len(needle) - held) >= SPLIT and needle[held:] in after


def locations(quote: str, corpus: tuple[Page, ...]) -> list[Page]:
    """Every page a quote is on or starts on before running over the page break, this request's document first."""
    needle, own = normalize(quote), primary()
    if not needle:
        return []
    found = [p for p in corpus if needle in p.anchor]
    if not found:
        following = {(p.doc, p.number): p for p in corpus}
        found = [
            p
            for p in corpus
            if (p.doc, p.number + 1) in following and spans(needle, p.anchor, following[(p.doc, p.number + 1)].anchor)
        ]
    return sorted(found, key=lambda p: p.doc != own)


def disputed(texts: list[str], page: Page) -> bool:
    """Whether a scanned page's second reading disputes any of these texts, found in order."""
    if not page.second:
        return False
    first, marks, at = normalize(page.text), page.doubted, 0
    for text in filter(None, map(normalize, texts)):
        start = first.find(text, at)
        if start < 0:
            continue
        if any(marks[start : start + len(text)]):
            return True
        at = start + len(text)
    return False


def locate(quote: str, corpus: tuple[Page, ...]) -> Page | None:
    """The page a quote is on."""
    return next(iter(locations(quote, corpus)), None)


def passage(quote: str, page: Page) -> str:
    """A quote widened to its whole lines, under its heading; a quote the page prints twice has no one heading."""
    kept, needle = folded(page.text), normalize(quote)
    joined = "".join(c for c, _ in kept)
    start = joined.find(needle)
    if not needle or start < 0 or joined.find(needle, start + 1) >= 0:
        return quote
    begin = page.text.rfind("\n", 0, kept[start][1]) + 1
    end = page.text.find("\n", kept[start + len(needle) - 1][1])
    lines = page.text[:begin].splitlines()
    heading = next((line for line in reversed(lines) if line.startswith("#")), "")
    rules = [j for j, line in enumerate(lines) if j and line.strip() and set(line) <= set("|-: ")]
    header = lines[rules[-1] - 1] if rules and page.text[begin:].startswith("|") else ""
    return "\n".join(filter(None, [heading, header, page.text[begin : end if end >= 0 else None]]))


def boxes(quote: str, number: int) -> list[Box]:
    """Highlight boxes for a quote, or for the part of it this page prints when it runs over the page break."""
    found = layer_boxes(quote, number) or block_boxes(quote, number)
    if found:
        return found
    needle = normalize(quote)
    held = opening(needle, guide().pages[number - 1].glyphs[0])
    return layer_boxes(needle[:held], number) if held >= SPLIT else []


def block_boxes(quote: str, number: int) -> list[Box]:
    """Highlight boxes from OCR blocks: in each block the quote runs through, the band of its lines."""
    needle, ocr = normalize(quote), guide().ocr
    if not needle or number > len(ocr):
        return []
    blocks = ocr[number - 1]["blocks"]
    kept = [
        (c, (b, i)) for b, block in enumerate(blocks) for i, line in enumerate(rows(block)) for c, _ in folded(line)
    ]
    start = "".join(c for c, _ in kept).find(needle)
    if start < 0:
        return []
    touched = [spot for _, spot in kept[start : start + len(needle)]]
    found = []
    for b in dict.fromkeys(b for b, _ in touched):
        left, top, right, bottom = blocks[b]["bbox"]
        step = (bottom - top) / len(rows(blocks[b]))
        lines = [i for x, i in touched if x == b]
        found.append((left, top + min(lines) * step, right, top + (max(lines) + 1) * step))
    return found


def rows(block: dict) -> list[str]:
    """A block's printed lines, without lines of markup only."""
    return [line for line in (block.get("content") or "").splitlines() if normalize(line)]


def rendered(pdf: bytes, number: int, scale: float) -> Image.Image:
    """A page as an image."""
    with PDFIUM, pypdfium2.PdfDocument(pdf) as document:
        return document[number - 1].render(scale=scale).to_pil().convert("RGB")


def cropped(image: Image.Image, bbox: Box) -> Image.Image:
    """The part of an image inside a page-relative box."""
    left, top, right, bottom = bbox
    width, height = image.size
    return image.crop((left * width, top * height, right * width, bottom * height))


def pictures(doc: Doc, number: int, quotes: tuple[str, ...] | None = None) -> tuple[bytes, ...]:
    """A page's figures as PNG images, or only those printing one of these quotes."""
    blocks = doc.ocr[number - 1]["blocks"] if number <= len(doc.ocr) else []
    figures = [b for b in blocks if b["type"] == "image"]
    if quotes is not None:
        needles = [needle for needle in map(normalize, quotes) if needle]
        figures = [b for b in figures if any(needle in normalize(b["content"] or "") for needle in needles)]
    if not figures:
        return ()
    page = rendered(doc.pdf, number, settings().render_scale)
    return tuple(png(cropped(page, tuple(b["bbox"]))) for b in figures)


def png(image: Image.Image) -> bytes:
    """An image as PNG bytes."""
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def layer_boxes(quote: str, number: int) -> list[Box]:
    """Highlight boxes from the text layer."""
    needle, doc = normalize(quote), guide()
    text, index = doc.pages[number - 1].glyphs
    start = text.find(needle)
    if not needle or start < 0:
        return []
    first, last = index[start], index[start + len(needle) - 1]
    with PDFIUM, pypdfium2.PdfDocument(doc.pdf) as pdf:
        page = pdf[number - 1]
        chars = page.get_textpage()
        (x0, y0, x1, y1), turn = page.get_cropbox(), page.get_rotation()
        rects = [chars.get_charbox(i, loose=True) for i in range(first, last + 1)]
    width, height = x1 - x0, y1 - y0
    flat = [
        ((left - x0) / width, (y1 - top) / height, (right - x0) / width, (y1 - bottom) / height)
        for left, bottom, right, top in rects
        if right > left
    ]
    return [shown(box, TURNS[turn]) for box in lines(flat)]


def shown(box: Box, turn: Callable[[float, float], tuple[float, float]]) -> Box:
    """A box on the unrotated page as it appears on the page shown turned."""
    (a, b), (c, d) = turn(box[0], box[1]), turn(box[2], box[3])
    return (min(a, c), min(b, d), max(a, c), max(b, d))


def lines(rects: list[Box]) -> list[Box]:
    """Merge character boxes into lines."""
    merged: list[Box] = []
    for n, (left, top, right, bottom) in enumerate(rects):
        before = rects[n - 1] if n else None
        if before and left >= before[0] and before[1] <= (top + bottom) / 2 <= before[3]:
            x0, y0, x1, y1 = merged[-1]
            merged[-1] = (min(x0, left), min(y0, top), max(x1, right), max(y1, bottom))
        else:
            merged.append((left, top, right, bottom))
    return merged


def link(number: int) -> str:
    """Link to a page."""
    return f"/documents/{primary()}#page={number}"
