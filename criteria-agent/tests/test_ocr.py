"""OCR ingestion."""

import base64
import json

import httpx
import pypdfium2
import pytest
from helpers import SAMPLE_PDF

from app.core.documents import Doc
from app.ingest import ocr

LAYER = Doc("", "", SAMPLE_PDF.read_bytes()).layer


@pytest.fixture
def mistral(monkeypatch, config):
    """A fake Mistral OCR."""
    monkeypatch.setattr(config, "mistral_api_key", "k")
    sent: list[int] = []

    def handler(request):
        """OCR one part, or refuse it."""
        data = base64.b64decode(json.loads(request.content)["document"]["document_url"].split(",")[1])
        count = len(pypdfium2.PdfDocument(data))
        sent.append(count)
        if count > 4:
            return httpx.Response(422, json={"detail": "too many pages"})
        box = {
            "type": "text",
            "top_left_x": 20,
            "top_left_y": 40,
            "bottom_right_x": 100,
            "bottom_right_y": 200,
            "content": "c",
        }
        raw = {
            "markdown": "text ![img-0.jpeg](img-0.jpeg)",
            "images": [{"id": "img-0.jpeg"}],
            "dimensions": {"width": 200, "height": 400},
            "blocks": [box],
        }
        return httpx.Response(200, json={"pages": [{"index": i, **raw} for i in range(count)]})

    monkeypatch.setattr(ocr, "mistral", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return sent


async def test_ocr_halves_what_mistral_refuses_and_keeps_page_order(mistral):
    """Refused parts are halved."""
    pages = await ocr.ocr(SAMPLE_PDF.read_bytes(), list(range(11)), LAYER)
    assert [p["number"] for p in pages] == list(range(1, 12)) and sorted(mistral) == [1, 2, 2, 3, 3, 5, 5, 11]
    assert pages[0]["text"] == "text" and pages[0]["blocks"][0]["bbox"] == pytest.approx([0.1, 0.1, 0.5, 0.5])
    assert [p["number"] for p in await ocr.ocr(SAMPLE_PDF.read_bytes(), [2, 7], LAYER)] == [3, 8]


def test_text_printed_in_a_figure_replaces_its_placeholder_and_fills_its_block():
    """Mistral's figure annotation becomes quotable page text and the image block's content."""
    raw = {
        "markdown": "# Rates\n\n![img-0.jpeg](img-0.jpeg)\n\nFigure 1.",
        "dimensions": {"width": 100, "height": 200},
        "images": [{"id": "img-0.jpeg", "image_annotation": '{"printed": ["5-year fixed", "4.61%"]}'}],
        "blocks": [
            {
                "type": "image",
                "image_id": "img-0.jpeg",
                "content": "![img-0.jpeg](img-0.jpeg)",
                "top_left_x": 10,
                "top_left_y": 20,
                "bottom_right_x": 90,
                "bottom_right_y": 120,
            },
        ],
    }
    read = ocr.page(raw, 1)
    assert read["text"] == "# Rates\n\n5-year fixed\n4.61%\n\nFigure 1."
    assert read["blocks"] == [{"type": "image", "bbox": [0.1, 0.1, 0.9, 0.6], "content": "5-year fixed\n4.61%"}]
