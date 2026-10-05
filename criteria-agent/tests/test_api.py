"""The HTTP entrypoint."""

import base64
import io
import json
from types import SimpleNamespace

import httpx
import pypdfium2
import pytest
from helpers import PAGES, SAMPLE_PDF, State, claim, returns
from pydantic import SecretStr
from starlette.testclient import TestClient
from typesafe_sdk import Noul, TypeSafeAPIError

from app import main
from app.answer import agent
from app.core import database, errors
from app.core.documents import edition as guide_edition
from app.core.schemas import Answer, Result
from app.core.tracing import observed
from app.ingest import uploads

pytestmark = pytest.mark.usefixtures("db")


def upload(name: str, content: bytes) -> dict:
    """An upload request body."""
    return {"upload": {"name": name, "content": base64.b64encode(content).decode()}}


@pytest.fixture(autouse=True)
def structured(monkeypatch):
    """Record structured documents."""
    done: list[str] = []

    async def ensure(doc):
        """Record the document."""
        done.append(doc.id)

    monkeypatch.setattr(uploads, "ensure", ensure)
    monkeypatch.setattr(uploads, "structure", ensure)
    return done


@pytest.fixture
def agentless(monkeypatch, the_guide):
    """A scripted agent."""
    scripted = SimpleNamespace(state=State())
    monkeypatch.setattr(main, "choose", returns(the_guide))
    monkeypatch.setattr(main, "pages", lambda: PAGES)
    monkeypatch.setattr(main, "build", lambda *_: scripted)
    return scripted


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"wrong": 1},
        {"prompt": "q", "approvals": {"a": "h"}},
        {"prompt": "q", "document_id": "../distractors/apex"},
        {"prompt": "q", "document_id": "B53431D53E4AB886"},
    ],
    ids=["empty", "unknown field", "two kinds", "a path", "upper-case hash"],
)
async def test_a_malformed_request_is_a_named_failure_before_any_work(invoke, monkeypatch, payload):
    """Bad requests are rejected."""
    monkeypatch.setattr(main, "build", lambda *_: pytest.fail("no agent for a bad request"))
    response = (await invoke(payload))[-1]
    assert (response["status"], response["error"]) == ("failed", "ValidationError")


def test_invocations_stream_server_sent_events_in_the_named_session(monkeypatch):
    """The HTTP route streams each event as an SSE data line, in the session X-Session-Id names."""
    monkeypatch.setattr(main, "build", lambda *_: pytest.fail("no agent for a bad request"))
    response = TestClient(main.app).post("/invocations", json={}, headers={"X-Session-Id": "s1"})
    events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
    assert response.headers["content-type"].startswith("text/event-stream")
    assert (events[-1]["type"], events[-1]["status"], events[-1]["session_id"]) == ("done", "failed", "s1")


def answered(status: str) -> Result:
    """A finished answer."""
    return Result(trace_id="t", question="q", answer=Answer(answerable=True, claims=[], note=""), status=status)


async def test_an_approval_carries_the_action_hash_or_denies(invoke, agentless, monkeypatch):
    """Only a hash approves."""
    sent = {}

    async def step(_, message, *__):
        """Capture the resume message."""
        sent["message"] = message
        return SimpleNamespace(stop_reason="end_turn")

    monkeypatch.setattr(main, "step", step)
    monkeypatch.setattr(agent, "finish", returns(answered("grounded")))
    await invoke({"approvals": {"a": "abc123", "b": True, "c": 1}})
    replies = {m["interruptResponse"]["interruptId"]: m["interruptResponse"]["response"] for m in sent["message"]}
    assert {k: (r["approved"], r["hash"]) for k, r in replies.items()} == {
        "a": (True, "abc123"),
        "b": (False, True),
        "c": (False, 1),
    }


async def test_a_crashing_request_is_a_named_failure_and_is_recorded(invoke, agentless, monkeypatch):
    """Failures come back as failed."""

    async def crash(*_):
        """Stand in for a dependency that is down."""
        raise ConnectionError("jev unreachable")

    monkeypatch.setattr(main, "respond", crash)
    response = (await invoke({"prompt": "q"}))[-1]
    assert (response["status"], response["error"]) == (
        "failed",
        "ConnectionError",
    ) and "released" not in response
    [row] = await database.entries(database.Run)
    assert (row["session_id"], row["status"], row["error"]) == ("s", "failed", "ConnectionError")


async def test_a_model_service_refusal_names_the_service(invoke, agentless, monkeypatch):
    """A refused model call says which service refused, not only an error class."""

    async def refuse(*_):
        """Stand in for Jev refusing, as when its credits run out."""
        raise TypeSafeAPIError(402, None, {}, "no available credits")

    monkeypatch.setattr(main, "respond", refuse)
    response = (await invoke({"prompt": "q"}))[-1]
    assert response["status"] == "failed" and response["reason"].startswith("Jev, a model service")


async def test_a_banking_upload_is_stored_served_and_answered_from(invoke, jev, structured):
    """An upload is checked, stored and served."""
    jev(noul=0.9)
    pdf = SAMPLE_PDF.read_bytes()
    *progress, uploaded = await invoke(upload("guide.pdf", pdf))
    classified = next(e for e in progress if e.get("stage") == "classify" and e.get("status") == "done")
    assert classified["banking"] and 0 < len(classified["excerpt"].split()) <= uploads.settings().excerpt_words
    assert [e["page"] for e in progress if e["stage"] == "read"] == list(range(1, 12)) and progress[-1][
        "stage"
    ] == "stored"
    assert (uploaded["status"], uploaded["pages"], uploaded["parsed"]) == ("uploaded", 11, False)
    assert structured == [uploaded["document_id"]]
    chosen = await main.choose(main.Request(prompt="q", document_id=uploaded["document_id"]), "s")
    assert chosen.pdf == pdf and await database.recall("s") == uploaded["document_id"]
    with pytest.raises(LookupError):
        await main.choose(main.Request(prompt="q", document_id="0" * 16), "s2")
    assert await database.recall("s2") is None
    await database.save("1" * 16, "other.pdf", b"%PDF-other", None)
    with pytest.raises(ValueError, match="another document"):
        await main.choose(main.Request(prompt="q", document_id="1" * 16), "s")
    served = await main.document(uploaded["document_id"])
    assert served.body == pdf and served.headers["cache-control"].startswith("private")


async def test_an_upload_about_something_else_is_rejected_before_it_is_read(invoke, jev):
    """A non-banking document is never read or stored."""
    jev(noul=0.1)
    *progress, response = await invoke(upload("recipes.pdf", SAMPLE_PDF.read_bytes()))
    assert (response["status"], response["reason"]) == ("rejected", errors.NOT_BANKING)
    assert not [e for e in progress if e["stage"] in ("read", "stored")] and await database.uploads() == []


@pytest.mark.parametrize("ocr_up", [True, False], ids=["ocr reads every page", "ocr down falls back to the text layer"])
async def test_an_upload_is_read_by_ocr_or_falls_back_to_its_text_layer(invoke, jev, config, monkeypatch, ocr_up):
    """OCR, or the text layer if OCR is down."""
    jev(noul=0.9)
    monkeypatch.setattr(config, "mistral_api_key", SecretStr("k"))

    async def ocr(_, pages, layer):
        """Stand in for Mistral OCR, up or down."""
        if not ocr_up:
            raise ConnectionError("ocr down")
        return [{"number": n + 1, "text": f"parsed {n + 1}", "blocks": []} for n in pages]

    monkeypatch.setattr(uploads, "ocr", ocr)
    *progress, uploaded = await invoke(upload("g.pdf", SAMPLE_PDF.read_bytes()))
    assert (uploaded["status"], uploaded["parsed"]) == ("uploaded", ocr_up)
    parse = [e.get("status") for e in progress if e["stage"] == "parse"]
    assert parse[-1] == ("done" if ocr_up else "failed")
    row, _ = await database.document(uploaded["document_id"])
    assert (row.pages[0]["text"] if row.pages else None) == ("parsed 1" if ocr_up else None)


async def test_a_word_file_is_converted_and_an_unconvertible_one_is_refused(monkeypatch):
    """Other formats are converted."""

    def handler(request):
        """A fake Gotenberg."""
        if b'filename="brief.docx"' in request.content:
            return httpx.Response(200, content=b"%PDF-1.7 converted")
        return httpx.Response(400, text="unsupported")

    monkeypatch.setattr(
        uploads,
        "converter",
        lambda: httpx.AsyncClient(base_url="http://converter", transport=httpx.MockTransport(handler)),
    )
    assert await uploads.portable(b"PK docx bytes", "brief.docx") == b"%PDF-1.7 converted"
    assert await uploads.portable(b"%PDF-1.4 as is", "a.pdf") == b"%PDF-1.4 as is"
    with pytest.raises(ValueError, match="could not be converted"):
        await uploads.portable(b"\x00\x01", "tool.exe")


async def test_an_upload_without_a_text_layer_is_refused(invoke):
    """A scan without OCR is refused."""
    blank = pypdfium2.PdfDocument.new()
    blank.new_page(595, 842)
    buffer = io.BytesIO()
    blank.save(buffer)
    response = (await invoke(upload("scan.pdf", buffer.getvalue())))[-1]
    assert (response["status"], response["error"]) == ("failed", "Unusable") and "OCR" in response["reason"]


async def test_a_damaged_upload_is_named_as_damaged(invoke):
    """A file that is not really a PDF is refused as damaged, not as locked."""
    response = (await invoke(upload("broken.pdf", b"%PDF-1.7\n" + bytes(range(256)) * 40)))[-1]
    assert response["status"] == "failed" and "damaged" in response["reason"]


def test_reset_accepts_only_json_and_can_be_switched_off(config, monkeypatch):
    """Reset needs JSON and can be off."""
    client = TestClient(main.app)
    assert client.post("/reset", content="x").status_code == 415
    stages = [json.loads(line) for line in client.post("/reset", json={}).text.splitlines()]
    assert stages[1] == {"stage": "data", "status": "done", "conversations": 0, "decisions": 0, "documents": 0}
    assert stages[-2:] == [{"stage": "traces", "status": "done", "deleted": 0}, {"stage": "done"}]
    monkeypatch.setattr(config, "reset_enabled", False)
    assert client.post("/reset", json={}).status_code == 403


async def test_reset_deletes_every_langfuse_trace_a_page_at_a_time(config, monkeypatch):
    """Reset deletes every trace once."""
    monkeypatch.setattr(config, "langfuse_public_key", SecretStr("pk"))
    monkeypatch.setattr(config, "langfuse_secret_key", SecretStr("sk"))
    monkeypatch.setattr(config, "langfuse_host", "http://langfuse/langfuse")
    pages = {None: (["t1", "t2", "t1"], "next"), "next": (["t3"], None)}
    deleted = []

    def handler(request):
        """A fake Langfuse."""
        if request.method == "DELETE":
            deleted.append(json.loads(request.content)["traceIds"])
            return httpx.Response(200, json={"message": "Traces deleted successfully"})
        traces, cursor = pages[request.url.params.get("cursor")]
        return httpx.Response(
            200,
            json={
                "data": [{"traceId": t} for t in traces],
                "meta": {"cursor": cursor} if cursor else {},
            },
        )

    monkeypatch.setattr(main, "langfuse", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert [n async for n in main.forgetting()] == [2, 3] and deleted == [["t1", "t2"], ["t3"]]


async def test_a_reupload_of_a_stored_document_skips_classification_and_ocr(invoke, jev, config, monkeypatch):
    """A re-upload skips the checks."""
    pdf = SAMPLE_PDF.read_bytes()
    await database.save(guide_edition(pdf), "first.pdf", pdf, [{"text": "ocr", "blocks": []}])
    monkeypatch.setattr(config, "mistral_api_key", SecretStr("k"))
    monkeypatch.setattr(uploads, "ocr", lambda *_: pytest.fail("no OCR for a stored document"))
    sent = jev(noul=0.9)
    uploaded = (await invoke(upload("again.pdf", pdf)))[-1]
    assert (uploaded["status"], uploaded["parsed"], sent) == ("uploaded", True, [])
    assert [d["name"] for d in await database.uploads()] == ["again.pdf"]


@pytest.mark.parametrize(
    ("helpful", "verdict", "reruns"),
    [(True, "helpful", False), (False, "irrelevant", False), (False, "wrong", True)],
    ids=["thumbs up", "an irrelevant complaint", "a relevant complaint"],
)
async def test_feedback_is_kept_scored_and_a_relevant_complaint_is_answered_again(
    invoke, jev, agentless, monkeypatch, helpful, verdict, reruns
):
    """Relevant complaints re-ask the question."""
    await database.append(
        database.Run, {"session_id": "s", "trace_id": "t1", "question": "What is the term?", "released": []}
    )
    jev(choice=verdict)
    asked = []

    async def prepare(agent, prompt, *_):
        """Record the re-asked question."""
        asked.append(prompt)
        return main.empty(prompt, "refused", "stop", []), [], prompt

    monkeypatch.setattr(main, "prepare", prepare)
    *progress, response = await invoke(
        {"feedback": {"trace_id": "t1", "helpful": helpful, "comment": "the term is wrong"}}
    )
    [kept] = await database.entries(database.Feedback)
    assert (kept["verdict"], kept["session"]) == (verdict, "s")
    assert any(e.get("stage") == "feedback" and e.get("details") == {"verdict": verdict} for e in progress)
    assert bool(asked) == reruns and (not reruns or "the term is wrong" in asked[0] and "What is the term?" in asked[0])
    if not reruns:
        assert (response["status"], response["note"] == errors.STANDS) == ("feedback", verdict == "irrelevant")


async def test_feedback_on_another_sessions_answer_is_refused(invoke, jev, agentless):
    """Feedback stays in its session."""
    await database.append(database.Run, {"session_id": "s", "trace_id": "t1", "question": "q", "released": []})
    response = (await invoke({"feedback": {"trace_id": "t1", "helpful": False}}, session="other"))[-1]
    assert (response["status"], response["error"]) == ("failed", "LookupError") and await database.entries(
        database.Feedback
    ) == []


def test_a_spreadsheet_prints_one_page_wide_with_every_cell_in_full():
    """Columns fit their longest text; CSV becomes a workbook first."""
    book = uploads.openpyxl.load_workbook(
        io.BytesIO(uploads.fitted(uploads.workbook(b"Code,Product\nRB-1A,Residential Bridging\n", "p.csv")))
    )
    sheet = book.active
    assert sheet.column_dimensions["B"].width == len("Residential Bridging") + 1
    assert sheet.sheet_properties.pageSetUpPr.fitToPage and (
        sheet.page_setup.fitToWidth,
        sheet.page_setup.fitToHeight,
    ) == (1, 0)
    assert [cell.value for cell in sheet[2]] == ["RB-1A", "Residential Bridging"]


@pytest.mark.parametrize(("upright", "rotation"), [(True, 90), (False, 0)], ids=["confirmed", "a photo"])
async def test_a_scan_turns_only_when_a_vision_check_confirms_the_turned_page_reads_upright(
    monkeypatch, upright, rotation
):
    """The classifier proposes a turn; it is applied only when the page so turned reads the right way up."""
    with uploads.pypdfium2.PdfDocument.new() as document:
        document.new_page(200, 300)
        buffer = io.BytesIO()
        document.save(buffer)
    shown = []

    async def ask_model(system, prompt, schema, reasoning=False, images=()):
        """Record the image shown and answer."""
        shown.extend(images)
        return schema(upright=upright)

    monkeypatch.setattr(uploads, "orientation", lambda: lambda image: ("270", 0.0))
    monkeypatch.setattr(uploads, "ask_model", ask_model)
    with uploads.pypdfium2.PdfDocument(await uploads.upright(buffer.getvalue(), ("",))) as pdf:
        assert pdf[0].get_rotation() == rotation
    assert len(shown) == 1


def test_tracked_changes_are_accepted_before_a_word_file_is_converted():
    """Deleted text and deleted rows go, inserted text stays, as Word shows the file without markup."""
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = (
        f"<w:document {w}><w:body>"
        "<w:p><w:r><w:t>Maximum LTV is </w:t></w:r><w:del><w:r><w:delText>75%</w:delText></w:r></w:del>"
        "<w:ins><w:r><w:t>70%</w:t></w:r></w:ins></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>kept row</w:t></w:r></w:p></w:tc></w:tr>"
        "<w:tr><w:trPr><w:del/></w:trPr><w:tc><w:p><w:r><w:t>deleted row</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        "</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with uploads.zipfile.ZipFile(buffer, "w") as docx:
        docx.writestr("word/document.xml", body)
    with uploads.zipfile.ZipFile(io.BytesIO(uploads.accepted(buffer.getvalue()))) as docx:
        xml = docx.read("word/document.xml").decode()
    assert "70%" in xml and "kept row" in xml
    assert not any(gone in xml for gone in ("75%", "deleted row", "w:ins", "w:del"))


def test_a_step_shows_its_input_and_output_in_langfuse():
    """Inputs and outputs, models included, reach Langfuse as JSON on the span."""
    seen: dict[str, str] = {}
    span = SimpleNamespace(set_attribute=seen.__setitem__)
    observed(span, {"questions": {"q": Noul(instructions="Is it on topic?")}}, [claim("Term is 12 months.")])
    given, returned = json.loads(seen["langfuse.observation.input"]), json.loads(seen["langfuse.observation.output"])
    assert given["questions"]["q"]["instructions"] == "Is it on topic?" and returned[0]["text"] == "Term is 12 months."


def test_every_request_logs_its_outcome_and_the_path_of_its_trace(config):
    """One log line names the request kind, its status and where its trace is in Langfuse."""
    line = main.logged(
        {"prompt": "What is the term?"}, {"session_id": "s", "status": "grounded", "trace_id": "t1"}, 1.25
    )
    assert line == (
        f"prompt session=s status=grounded 1.2s trace=/langfuse/project/{config.langfuse_project}/traces/t1"
        " | What is the term?"
    )
    assert main.logged({"upload": "not an object"}, {"session_id": "s"}, 0).startswith("upload session=s status=None")
