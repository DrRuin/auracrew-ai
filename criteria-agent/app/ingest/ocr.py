"""Mistral OCR."""

import asyncio
import base64
import io
import json

import httpx
import pypdfium2

from app.ai.llm import bounded
from app.core.config import client, settings
from app.core.documents import PDFIUM, png, rendered
from app.core.prompts import FIGURE_TEXT
from app.core.tracing import observed, tracer

FIGURE = {
    "type": "json_schema",
    "json_schema": {
        "name": "figure",
        "strict": True,
        "schema": {
            "type": "object",
            "title": "Figure",
            "additionalProperties": False,
            "required": ["printed"],
            "properties": {"printed": {"type": "array", "items": {"type": "string"}, "description": FIGURE_TEXT}},
        },
    },
}


TOO_LARGE = {
    httpx.codes.BAD_REQUEST,
    httpx.codes.REQUEST_ENTITY_TOO_LARGE,
    httpx.codes.UNPROCESSABLE_ENTITY,
}


def extract(pdf: bytes, part: list[int]) -> bytes:
    """Cut pages out of a PDF."""
    buffer = io.BytesIO()
    with PDFIUM, pypdfium2.PdfDocument(pdf) as whole, pypdfium2.PdfDocument.new() as piece:
        piece.import_pages(whole, part)
        piece.save(buffer)
    return buffer.getvalue()


def mistral() -> httpx.AsyncClient:
    """The Mistral client."""
    config = settings()
    headers = {"Authorization": f"Bearer {config.mistral_api_key.get_secret_value()}"}
    return client(headers=headers)


async def transcribe(client: httpx.AsyncClient, pdf: bytes, pages: list[int], probed: bool = False) -> list[dict]:
    """OCR pages, halving if too large."""
    part = await asyncio.to_thread(extract, pdf, pages)
    document = {
        "type": "document_url",
        "document_url": "data:application/pdf;base64," + base64.b64encode(part).decode(),
    }
    with tracer.start_as_current_span("mistral.ocr") as span:
        response = await client.post(settings().ocr_url, json=request(document))
        observed(span, {"pages": [n + 1 for n in pages], "status": response.status_code}, response.text)
    if response.status_code in TOO_LARGE and len(pages) > 1:
        head = [] if probed else await transcribe(client, pdf, pages[:1], probed=True)
        rest = pages[len(head) :]
        parts = [part for part in (rest[: len(rest) // 2], rest[len(rest) // 2 :]) if part]
        done = await asyncio.gather(*(transcribe(client, pdf, part, probed=True) for part in parts))
        return head + [p for part in done for p in part]
    response.raise_for_status()
    return [page(raw, pages[raw["index"]] + 1) for raw in response.json()["pages"]]


def request(document: dict) -> dict:
    """An OCR request for one document or image."""
    return {
        "model": settings().ocr_model,
        "document": document,
        "include_blocks": True,
        "extract_header": True,
        "extract_footer": True,
        "bbox_annotation_format": FIGURE,
    }


async def photographed(client: httpx.AsyncClient, pdf: bytes, number: int) -> dict:
    """OCR one scanned page from its image, which reads better than the PDF it sits in."""
    image = await asyncio.to_thread(
        lambda: base64.b64encode(png(rendered(pdf, number + 1, settings().render_scale))).decode()
    )
    document = {"type": "image_url", "image_url": "data:image/png;base64," + image}
    with tracer.start_as_current_span("mistral.ocr") as span:
        response = await client.post(settings().ocr_url, json=request(document))
        observed(span, {"page image": number + 1, "status": response.status_code}, response.text)
    response.raise_for_status()
    return page(response.json()["pages"][0], number + 1)


def page(raw: dict, number: int) -> dict:
    """One OCR page, cleaned."""
    width, height = raw["dimensions"]["width"], raw["dimensions"]["height"]
    text, blocks = raw["markdown"], raw.get("blocks") or []
    printed = {
        image["id"]: "\n".join(json.loads(image.get("image_annotation") or "{}").get("printed", []))
        for image in raw.get("images") or []
    }
    for image, words in printed.items():
        text = text.replace(f"![{image}]({image})", words)
    return {
        "number": number,
        "text": text.strip(),
        "blocks": [
            {
                "type": block["type"],
                "bbox": [
                    block["top_left_x"] / width,
                    block["top_left_y"] / height,
                    block["bottom_right_x"] / width,
                    block["bottom_right_y"] / height,
                ],
                "content": printed.get(block.get("image_id"), block["content"]),
            }
            for block in blocks
        ],
    }


async def ocr(pdf: bytes, pages: list[int], layer: tuple[str, ...]) -> list[dict]:
    """OCR these pages: scans from their images, kept with a second reading from the PDF, the rest as PDF."""
    scans, rest = [n for n in pages if not layer[n].strip()], [n for n in pages if layer[n].strip()]
    async with mistral() as client:
        read, seen, again = await asyncio.gather(
            transcribe(client, pdf, rest) if rest else asyncio.sleep(0, []),
            bounded([lambda n=n: photographed(client, pdf, n) for n in scans]),
            transcribe(client, pdf, scans) if scans else asyncio.sleep(0, []),
        )
    second = {page["number"]: page["text"] for page in again}
    for page in seen:
        page["second"] = second[page["number"]]
    return sorted([*read, *seen], key=lambda page: page["number"])
