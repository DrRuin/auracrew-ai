"""Postgres models and queries."""

import asyncio
import hashlib
import json
from datetime import datetime
from functools import cache
from typing import Any, ClassVar

from sqlalchemy import DateTime, Engine, ForeignKey, create_engine, delete, func, select, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.schema import CreateSchema
from strands.session.session_repository import SessionRepository
from strands.types.session import Session, SessionAgent, SessionMessage

from app.core.config import settings
from app.core.prompts import JUDGE, RELATIONS

SHARED, RUN = "shared", "run"


class Base(DeclarativeBase):
    """ORM base."""

    type_annotation_map: ClassVar[dict] = {
        dict[str, Any]: JSONB(none_as_null=True),
        list[Any]: JSONB(none_as_null=True),
        datetime: DateTime(timezone=True),
    }


class Document(Base):
    """A stored document."""

    __tablename__ = "documents"
    __table_args__ = ({"schema": SHARED},)
    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    pdf: Mapped[bytes]
    pages: Mapped[list[Any] | None]
    reference: Mapped[bool]
    uploaded: Mapped[datetime | None]


class Card(Base):
    """A page card."""

    __tablename__ = "cards"
    __table_args__ = ({"schema": SHARED},)
    document: Mapped[str] = mapped_column(ForeignKey(Document.id, ondelete="CASCADE"), primary_key=True)
    page: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str]
    summary: Mapped[str]


class Sheet(Base):
    """A stitched table."""

    __tablename__ = "sheets"
    __table_args__ = ({"schema": SHARED},)
    document: Mapped[str] = mapped_column(ForeignKey(Document.id, ondelete="CASCADE"), primary_key=True)
    number: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str]
    columns: Mapped[list[Any]]
    pages: Mapped[list[Any]]
    rows: Mapped[int]
    notes: Mapped[list[Any]] = mapped_column(default=list)


class Line(Base):
    """A table row."""

    __tablename__ = "lines"
    __table_args__ = ({"schema": SHARED},)
    document: Mapped[str] = mapped_column(ForeignKey(Document.id, ondelete="CASCADE"), primary_key=True)
    sheet: Mapped[int] = mapped_column(primary_key=True)
    row: Mapped[int] = mapped_column(primary_key=True)
    page: Mapped[int]
    cells: Mapped[list[Any]]
    values: Mapped[list[Any]]


class Calibration(Base):
    """Jev's calibration on a document."""

    __tablename__ = "calibrations"
    __table_args__ = ({"schema": SHARED},)
    edition: Mapped[str] = mapped_column(primary_key=True)
    threshold: Mapped[float]
    claims: Mapped[int]
    disagreements: Mapped[int]
    upper_bound: Mapped[float]
    pairs: Mapped[list[Any]]


class Decision(Base):
    """A recorded decision."""

    __tablename__ = "decisions"
    __table_args__ = ({"schema": RUN},)
    key: Mapped[str] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(server_default=func.now())
    record: Mapped[dict[str, Any]]


class Audit(Base):
    """A gate outcome."""

    __tablename__ = "audit"
    __table_args__ = ({"schema": RUN},)
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(server_default=func.now())
    entry: Mapped[dict[str, Any]]


class Run(Base):
    """A request's response."""

    __tablename__ = "runs"
    __table_args__ = ({"schema": RUN},)
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(server_default=func.now())
    entry: Mapped[dict[str, Any]]


class Feedback(Base):
    """Feedback on an answer."""

    __tablename__ = "feedback"
    __table_args__ = ({"schema": RUN},)
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(server_default=func.now())
    entry: Mapped[dict[str, Any]]


class Binding(Base):
    """A session's document."""

    __tablename__ = "bindings"
    __table_args__ = ({"schema": RUN},)
    session: Mapped[str] = mapped_column(primary_key=True)
    document: Mapped[str]


class Record(Base):
    """Stored agent session data."""

    __tablename__ = "records"
    __table_args__ = ({"schema": RUN},)
    session_id: Mapped[str] = mapped_column(primary_key=True)
    agent_id: Mapped[str] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(primary_key=True)
    data: Mapped[dict[str, Any]]


OPENED: dict[tuple, asyncio.Future] = {}


def schemas() -> dict[str, str]:
    """Schema names."""
    config = settings()
    return {SHARED: config.database_schema, RUN: config.run_schema or config.database_schema}


def address(driver: str) -> str:
    """The database URL."""
    url = make_url(settings().database_url.get_secret_value())
    return url.set(drivername=f"postgresql+{driver}").render_as_string(hide_password=False)


async def sessions() -> async_sessionmaker[AsyncSession]:
    """The database session factory."""
    names = schemas()
    key = (asyncio.get_running_loop(), address("asyncpg"), *names.values())
    future = OPENED.get(key)
    if future is None or future.cancelled() or (future.done() and future.exception()):
        future = OPENED[key] = asyncio.ensure_future(opened(key[1], names))
    return await asyncio.shield(future)


async def opened(url: str, names: dict[str, str]) -> async_sessionmaker[AsyncSession]:
    """Open the database."""
    engine = create_async_engine(url, execution_options={"schema_translate_map": names})
    async with engine.begin() as conn:
        for name in set(names.values()):
            await conn.execute(CreateSchema(name, if_not_exists=True))
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def record(key: str, entry: dict) -> bool:
    """Record a decision once."""
    statement = insert(Decision).values(key=key, record=entry).on_conflict_do_nothing()
    async with (await sessions()).begin() as db:
        return await db.scalar(statement.returning(Decision.key)) is not None


async def append(log: type[Audit] | type[Run] | type[Feedback], entry: dict) -> None:
    """Append a log row."""
    async with (await sessions()).begin() as db:
        db.add(log(entry=entry))


async def entries(log: type[Audit] | type[Run] | type[Feedback]) -> list[dict]:
    """Read a log."""
    async with (await sessions())() as db:
        return list(await db.scalars(select(log.entry).order_by(log.id)))


async def answered(session: str, trace: str) -> dict | None:
    """A session's response by trace."""
    query = select(Run.entry).where(Run.entry["session_id"].astext == session, Run.entry["trace_id"].astext == trace)
    async with (await sessions())() as db:
        return await db.scalar(query.order_by(Run.id.desc()).limit(1))


async def save(id: str, name: str, pdf: bytes, pages: list[dict] | None, reference: bool = False) -> None:
    """Store a document."""
    statement = insert(Document).values(
        id=id,
        name=name,
        pdf=pdf,
        pages=pages,
        reference=reference,
        uploaded=None if reference else func.now(),
    )
    new = statement.excluded
    statement = statement.on_conflict_do_update(
        index_elements=[Document.id],
        set_={
            "name": new.name,
            "pages": func.coalesce(new.pages, Document.pages),
            "reference": Document.reference | new.reference,
            "uploaded": func.coalesce(new.uploaded, Document.uploaded),
        },
    )
    async with (await sessions()).begin() as db:
        await db.execute(statement)


async def document(id: str) -> tuple[Document, float]:
    """A document and its Jev threshold."""
    async with (await sessions())() as db:
        found = await db.get(Document, id)
        earned = await db.scalar(select(Calibration.threshold).where(Calibration.edition == trust(id)))
    if found is None:
        raise LookupError(f"no document {id}")
    return found, 1.0 if earned is None else earned


async def structure(document: str, cards: list[Card], sheets: list[Sheet], lines: list[Line]) -> None:
    """Store a document's structure."""
    async with (await sessions()).begin() as db:
        for table in (Line, Sheet, Card):
            await db.execute(delete(table).where(table.document == document))
        db.add_all([*cards, *sheets, *lines])


async def outline(document: str) -> tuple[list[Card], list[Sheet]]:
    """A document's cards and tables."""
    async with (await sessions())() as db:
        cards = await db.scalars(select(Card).where(Card.document == document).order_by(Card.page))
        sheets = await db.scalars(select(Sheet).where(Sheet.document == document).order_by(Sheet.number))
        return list(cards), list(sheets)


async def lines(document: str, sheet: int, *where: Any, order: Any = None, limit: int | None = None) -> list[Line]:
    """Query a table's rows."""
    query = select(Line).where(Line.document == document, Line.sheet == sheet, *where)
    query = query.order_by(*([order] if order is not None else []), Line.row).limit(limit)
    async with (await sessions())() as db:
        return list(await db.scalars(query))


async def pdf(id: str) -> bytes:
    """A document's PDF."""
    async with (await sessions())() as db:
        found = await db.scalar(select(Document.pdf).where(Document.id == id))
    if found is None:
        raise LookupError(f"no document {id}")
    return found


async def known(id: str) -> bool | None:
    """Whether a document is stored."""
    async with (await sessions())() as db:
        return await db.scalar(select(Document.pages.is_not(None)).where(Document.id == id))


async def uploads() -> list[dict]:
    """Uploaded documents."""
    query = select(Document.id, Document.name, Document.uploaded).where(Document.uploaded.is_not(None))
    async with (await sessions())() as db:
        rows = await db.execute(query.order_by(Document.uploaded.desc()))
    return [{"id": id_, "name": name, "at": at.isoformat()} for id_, name, at in rows]


async def bind(session: str, document: str | None) -> str | None:
    """Bind a session to a document."""
    statement = insert(Binding).values(session=session, document=document)
    async with (await sessions()).begin() as db:
        await db.execute(statement.on_conflict_do_nothing())
        return await db.scalar(select(Binding.document).where(Binding.session == session))


async def recall(session: str) -> str | None:
    """A session's document."""
    async with (await sessions())() as db:
        return await db.scalar(select(Binding.document).where(Binding.session == session))


def trust(edition: str) -> str:
    """The calibration key: one document edition, one Jev model, one judging question."""
    question = hashlib.sha256((JUDGE + json.dumps(RELATIONS, sort_keys=True)).encode()).hexdigest()[:8]
    return f"{edition}:{settings().typesafe_model}:{question}"


async def reset(uploads: bool = False) -> dict[str, int]:
    """Delete run data, and the uploads when asked; how many conversations, decisions and uploads there were."""
    async with (await sessions()).begin() as db:
        counts = {
            "conversations": await db.scalar(select(func.count(func.distinct(Record.session_id)))),
            "decisions": await db.scalar(select(func.count()).select_from(Decision)),
            "documents": await db.scalar(select(func.count()).where(Document.uploaded.is_not(None))) if uploads else 0,
        }
        for table in (Decision, Audit, Run, Feedback, Binding, Record):
            await db.execute(delete(table))
        if uploads:
            await db.execute(delete(Document).where(Document.reference.is_(False)))
            await db.execute(update(Document).values(uploaded=None))
    return counts


@cache
def engine(url: str, shared: str, run: str) -> Engine:
    """The sync engine for sessions."""
    names = {SHARED: shared, RUN: run}
    return create_engine(url, execution_options={"schema_translate_map": names})


class Memory(SessionRepository):
    """Agent sessions in Postgres."""

    def __init__(self) -> None:
        """Connect."""
        self.maker = sessionmaker(engine(address("psycopg"), *schemas().values()))

    def put(self, session_id: str, agent_id: str, message_id: int, data: dict) -> None:
        """Save a record."""
        key = {"session_id": session_id, "agent_id": agent_id, "message_id": message_id}
        statement = insert(Record).values(**key, data=data)
        with self.maker.begin() as db:
            db.execute(
                statement.on_conflict_do_update(index_elements=list(key), set_={"data": statement.excluded.data})
            )

    def get(self, session_id: str, agent_id: str, message_id: int) -> dict | None:
        """Read a record."""
        with self.maker() as db:
            found = db.get(Record, (session_id, agent_id, message_id))
        return found.data if found else None

    def create_session(self, session: Session, **kwargs: Any) -> Session:
        """Store a new session."""
        self.put(session.session_id, "", -1, session.to_dict())
        return session

    def read_session(self, session_id: str, **kwargs: Any) -> Session | None:
        """A stored session."""
        data = self.get(session_id, "", -1)
        return Session.from_dict(data) if data else None

    def create_agent(self, session_id: str, session_agent: SessionAgent, **kwargs: Any) -> None:
        """Store an agent's state."""
        self.put(session_id, session_agent.agent_id, -1, session_agent.to_dict())

    def update_agent(self, session_id: str, session_agent: SessionAgent, **kwargs: Any) -> None:
        """Replace an agent's state."""
        self.create_agent(session_id, session_agent)

    def read_agent(self, session_id: str, agent_id: str, **kwargs: Any) -> SessionAgent | None:
        """An agent's stored state."""
        data = self.get(session_id, agent_id, -1)
        return SessionAgent.from_dict(data) if data else None

    def create_message(self, session_id: str, agent_id: str, session_message: SessionMessage, **kwargs: Any) -> None:
        """Store one message."""
        self.put(session_id, agent_id, session_message.message_id, session_message.to_dict())

    def update_message(self, session_id: str, agent_id: str, session_message: SessionMessage, **kwargs: Any) -> None:
        """Replace a message."""
        self.create_message(session_id, agent_id, session_message)

    def read_message(self, session_id: str, agent_id: str, message_id: int, **kwargs: Any) -> SessionMessage | None:
        """One stored message."""
        data = self.get(session_id, agent_id, message_id)
        return SessionMessage.from_dict(data) if data else None

    def list_messages(
        self,
        session_id: str,
        agent_id: str,
        limit: int | None = None,
        offset: int = 0,
        **kwargs: Any,
    ) -> list[SessionMessage]:
        """List messages."""
        mine = (
            Record.session_id == session_id,
            Record.agent_id == agent_id,
            Record.message_id >= 0,
        )
        query = select(Record.data).where(*mine).order_by(Record.message_id).offset(offset).limit(limit)
        with self.maker() as db:
            return [SessionMessage.from_dict(data) for data in db.scalars(query)]
