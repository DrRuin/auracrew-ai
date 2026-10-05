"""Shared fixtures."""

import asyncio

import _settings
import pytest
from helpers import doc, fake_jev

from app import main
from app.ai import jev as jev_module
from app.core import database, documents
from app.core import events as progress


@pytest.fixture
def config():
    """The test settings."""
    return _settings.TEST


@pytest.fixture(autouse=True)
def the_guide():
    """Use the two-page test guide."""
    documents.forget()
    documents.GUIDE.set(doc())
    return documents.guide()


@pytest.fixture
def trusted(the_guide):
    """The test guide with Jev trusted above 0.5."""
    documents.GUIDE.set(doc(threshold=0.5))


@pytest.fixture
async def db():
    """An empty test database."""
    async with (await database.sessions()).begin() as session:
        for table in database.Base.metadata.sorted_tables:
            await session.execute(table.delete())


@pytest.fixture
def jev(monkeypatch):
    """Install a fake Jev."""
    sent: list[dict] = []

    def install(choice: str = "supports", confidence: float = 1.0, noul: float = 1.0) -> list[dict]:
        """Answer with this choice, confidence and P(yes)."""
        monkeypatch.setattr(jev_module, "jev_client", lambda: fake_jev(choice, confidence, noul, sent))
        return sent

    return install


@pytest.fixture
def events():
    """The progress events the code under test streams."""
    queue: asyncio.Queue = asyncio.Queue()
    progress.EVENTS.set(queue)
    yield lambda: [queue.get_nowait() for _ in range(queue.qsize())]
    progress.EVENTS.set(None)


@pytest.fixture
def invoke():
    """All events for one request."""

    async def call(payload: dict, session: str = "s") -> list[dict]:
        """Drain the stream."""
        return [e async for e in main.invoke(payload, session)]

    return call
