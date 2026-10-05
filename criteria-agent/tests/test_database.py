"""Postgres storage: upserts, logs, sessions and reset."""

import asyncio

import pytest
from strands.types.session import Session, SessionAgent, SessionMessage, SessionType

from app.core import database

pytestmark = pytest.mark.usefixtures("db")


async def test_two_services_racing_on_one_decision_record_it_once():
    """A decision is recorded once."""
    outcomes = await asyncio.gather(*(database.record("k1", {"decision": "approve"}) for _ in range(8)))
    assert sorted(outcomes) == [False] * 7 + [True]


async def test_an_upload_keeps_its_ocr_pages_and_a_reupload_becomes_the_newest():
    """A re-upload keeps its OCR."""
    await database.save("a" * 16, "a.pdf", b"%PDF-a", [{"text": "ocr"}])
    await database.save("b" * 16, "b.pdf", b"%PDF-b", None)
    await database.save("a" * 16, "a-again.pdf", b"%PDF-a", None)
    row, threshold = await database.document("a" * 16)
    assert (row.name, row.pages, threshold) == ("a-again.pdf", [{"text": "ocr"}], 1.0)
    assert [d["id"] for d in await database.uploads()] == ["a" * 16, "b" * 16]


async def test_reset_clears_uploads_and_run_state_but_keeps_reference_documents_and_calibration():
    """Reset keeps shipped documents."""
    await database.save("r" * 16, "guide", b"%PDF-r", [{"text": "ocr"}], reference=True)
    await database.save("r" * 16, "guide.pdf", b"%PDF-r", None)
    await database.save("u" * 16, "upload.pdf", b"%PDF-u", None)
    async with (await database.sessions()).begin() as db:
        db.add(
            database.Calibration(
                edition=database.trust("r" * 16), threshold=0.2, claims=90, disagreements=0, upper_bound=0.03, pairs=[]
            )
        )
    await database.bind("s", "u" * 16)
    assert (await database.reset(uploads=True))["documents"] == 2
    assert await database.uploads() == [] and await database.recall("s") is None
    row, threshold = await database.document("r" * 16)
    assert (row.pages, threshold) == ([{"text": "ocr"}], 0.2)
    with pytest.raises(LookupError):
        await database.document("u" * 16)


async def test_a_conversation_stays_bound_to_its_first_document():
    """A session keeps its document."""
    assert await database.bind("s", "a" * 16) == "a" * 16
    assert await database.bind("s", "b" * 16) == "a" * 16
    assert await database.recall("s") == "a" * 16


async def test_strands_sessions_round_trip_through_postgres():
    """Sessions survive restarts."""
    memory = database.Memory()
    memory.create_session(Session(session_id="s", session_type=SessionType.AGENT))
    memory.create_agent("s", SessionAgent(agent_id="a", state={"checks": [True]}, conversation_manager_state={}))
    for n in range(3):
        memory.create_message(
            "s",
            "a",
            SessionMessage(message={"role": "user", "content": [{"text": f"m{n}"}]}, message_id=n),
        )
    memory.update_message(
        "s",
        "a",
        SessionMessage(message={"role": "user", "content": [{"text": "edited"}]}, message_id=1),
    )
    assert memory.read_session("s").session_id == "s" and memory.read_session("other") is None
    assert memory.read_agent("s", "a").state == {"checks": [True]}
    texts = [m.message["content"][0]["text"] for m in memory.list_messages("s", "a", limit=2, offset=1)]
    assert texts == ["edited", "m2"]
