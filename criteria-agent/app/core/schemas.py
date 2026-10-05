"""Pydantic models for API bodies and structured outputs."""

from collections.abc import Callable
from contextvars import ContextVar
from functools import cache
from typing import Annotated, Any, Literal, Self

from pydantic import AfterValidator, BaseModel, Field, create_model, model_validator

from app.core.documents import Box, locate, normalize, pages
from app.core.prompts import REQUESTED, ROUTES

Mode = Literal["retrieve", "stuff"]


Decision = Literal["approve", "decline", "refer"]


Relation = Literal["supports", "contradicts", "says_nothing"]


Kind = Literal["text", "number", "percent", "money", "months"]


Op = Literal["=", "!=", "<", "<=", ">", ">="]


class Claim(BaseModel):
    """One statement with its quote."""

    text: str = Field(
        description="A single factual statement made in the answer, in plain sentences, never a table. Keep every "
        "condition, limit and reason its quote states. Never a statement that the document, the question or the case leaves something out: say that in note."
    )
    quote: str = Field(
        description="The whole sentence or table row of the guide, or the whole line of a tool result, that states it, copied exactly."
    )
    source: Literal["guide", "tool"] = Field(
        description="guide: the quote is text of the guide, including the page text that search_guide or "
        "read_pages returned. tool: the quote is a result a tool computed, such as a case assessment or a "
        "decision receipt."
    )


class Answer(BaseModel):
    """The agent's answer."""

    answerable: bool = Field(
        description="False when the guide does not state what the question asks; related facts do not make it answerable."
    )
    claims: list[Claim] = Field(description="The full answer, one claim per statement.")
    note: str = Field(
        description="What the question asks that the guide does not cover, empty when it covers all of it; for a message that is not a question, a brief reply. Written for the reader in plain sentences; never your working or checks."
    )


BOUNCED: ContextVar[list[int] | None] = ContextVar("bounced")
RESULTS: ContextVar[Callable[[], list[str]]] = ContextVar("results", default=list)


def placed(quote: str, results: list[str]) -> bool:
    """Whether a quote is on a page of the guide or in a tool result, as verification will look for it."""
    needle = normalize(quote)
    return locate(quote, pages()) is not None or bool(needle) and any(needle in result for result in results)


class GroundedAnswer(Answer):
    """Your final answer."""

    @model_validator(mode="after")
    def anchored(self) -> Self:
        """Send misquotes and repeated claims back while each try has fewer; verification strips any misquote left."""
        results = [normalize(result) for result in RESULTS.get()()]
        texts = [c.text for c in self.claims]
        missed = [
            f"not verbatim in the guide or a tool result: {c.quote}"
            for c in self.claims
            if not placed(c.quote, results)
        ]
        missed += [
            f"repeated; state the fact its own quote gives: {t}" for t in dict.fromkeys(texts) if texts.count(t) > 1
        ]
        bounced = BOUNCED.get(None)
        if missed and bounced is not None and (not bounced or len(missed) < bounced[-1]):
            bounced.append(len(missed))
            listed = "; ".join(missed)
            raise ValueError(f"fix these claims, copying each quote as one continuous passage exactly: {listed}")
        return self


@cache
def rulings(count: int) -> type[BaseModel]:
    """One verdict field per claim."""
    fields = {f"claim_{n}": (Relation, ...) for n in range(count)}
    return create_model("Rulings", __doc__="One verdict per claim, by the claim's key.", **fields)


class Check(BaseModel):
    """The verdict on one claim."""

    claim: Claim
    verdict: Relation | Literal["fabricated", "wrong_source", "unclear"]
    judge: Literal["code", "jev", "gpt"]
    jev: Relation | None = None
    jev_confidence: float | None = None
    page: int | None = None
    doc: str | None = None
    evidence: str | None = None
    grounded: bool = False
    url: str | None = None
    boxes: list[Box] = []


class Fact(BaseModel):
    """A fact the answer should state."""

    text: str
    quote: str = Field(description="Exact text copied from the pages that states the fact.")


class Facts(BaseModel):
    """Facts for a complete answer."""

    facts: list[Fact]


class Screen(BaseModel):
    """Jev's screen of a request."""

    off_topic: bool
    pressure: bool
    route: Literal[tuple(ROUTES)]
    requested: Literal[tuple(REQUESTED)]


class Unrelated(BaseModel):
    """Whether a request is unrelated to banking and finance."""

    unrelated: bool = Field(description="True only when the request is unrelated to banking, finance and property.")


class Tour(BaseModel):
    """Questions that show, on one document, how the assistant behaves."""

    lookup: str = Field(description="A short question about one rule or requirement the document states.")
    table: str = Field(
        description="A short question about the values in the rows of one listed table, such as which rows meet a condition or which has the lowest value in a column; empty when no table is listed."
    )
    pressure: str = Field(
        description="A question about something the document states, prefixed by an instruction to ignore the assistant's instructions or skip its checks."
    )


class Suggestions(BaseModel):
    """Three follow-up questions."""

    first: str = Field(description="A follow-up question.")
    second: str = Field(description="Another follow-up question.")
    third: str = Field(description="A third follow-up question.")


class Result(BaseModel):
    """One answered question."""

    trace_id: str
    question: str
    answer: Answer
    status: Literal["grounded", "partial", "withheld", "refused"]
    checks: list[Check] = []
    released: list[Check] = []
    omissions: list[Fact] = []
    repaired: bool = False
    route: str | None = None
    suggestions: list[str] = []
    gate: list[str] = []
    flags: list[str] = []

    def text(self) -> str:
        """The answer's statements: the released claims, or the note when the question was refused."""
        return self.answer.note if self.status == "refused" else " ".join(c.claim.text for c in self.released)

    def shown(self) -> str:
        """Everything the user reads: the released claims, then the note."""
        return " ".join(filter(None, [" ".join(c.claim.text for c in self.released), self.answer.note]))


class Criterion(BaseModel):
    """One criterion applied to the case."""

    criterion: str = Field(description="What is assessed, such as maximum loan-to-value.")
    case: str = Field(description="The case's value for it, worked out if needed.")
    requirement: list[str] = Field(
        description="The pages' text that sets the requirement: one or more passages from one page, each copied word for word, such as a table's heading and then its row."
    )
    verdict: Literal["meets", "breaks", "unclear"]
    breaks_as_stated: bool = Field(
        description="True when a figure or detail the case states, taken as final, breaks the requirement; false when the case does not state it."
    )
    uses_case: bool = Field(
        description="True when the verdict rests on a figure or detail the case states; false when it would be the same for any case."
    )
    reason: str = Field(description="One sentence, with any working.")


class Assessment(BaseModel):
    """The case's assessment."""

    criteria: list[Criterion]


class Upright(BaseModel):
    """Whether an image's text reads the right way up."""

    upright: bool = Field(description="True only when the image holds text and it reads the right way up.")


class Column(BaseModel):
    """A table column."""

    name: str = Field(
        description="The column's header text, as printed; when no header is printed for it, a short plain name for what its cells hold."
    )
    kind: Kind = Field(description="text, number, percent (75%), money (£1,000,000) or months (18 months).")


class Found(BaseModel):
    """A table on the page."""

    caption: str = Field(description="A short name for what the table lists, such as its heading.")
    columns: list[Column] = Field(description="Its columns in order; for a continued table, the columns it continues.")
    continues: bool = Field(description="True when the table continues a table from the previous page.")
    notes: list[str] = Field(
        description="Sentences printed just above or below the table that qualify its rows, such as when it applies, units or exceptions, each copied exactly; empty if none."
    )


class Read(BaseModel):
    """One page at a glance."""

    title: str = Field(description="The page's main heading, copied exactly as printed; empty if it has none.")
    summary: str = Field(description="Two short sentences on what the page states.")
    tables: list[Found]


@cache
def layout(columns: tuple[tuple[str, Kind], ...]) -> type[BaseModel]:
    """Row schema for a table with these columns."""
    fields = {}
    for i, (name, kind) in enumerate(columns):
        fields[f"c{i}"] = (str, Field(description=f"{name}, as printed"))
        if kind != "text":
            fields[f"n{i}"] = (float | None, Field(description=f"{name} as one plain number; null if not one number"))
    row = create_model("Row", __doc__="One table row.", **fields)
    return create_model("Rows", __doc__="The table's rows on this page.", rows=(list[row], ...))


class Condition(BaseModel):
    """A condition on a table column."""

    column: str = Field(description="A column name exactly as the table list gives it.")
    op: Op
    value: str = Field(description="A cell value as printed, or a plain number for a number column (75 for 75%).")


class Design(BaseModel):
    """The fields a scan's records carry."""

    fields: list[str] = Field(description="Field names, in the order a reader would read them.")


@cache
def records(names: tuple[str, ...]) -> type[BaseModel]:
    """Record schema for a scan."""
    fields = {f"f{i}": (str, Field(description=name)) for i, name in enumerate(names)}
    quote = Field(description="The one continuous passage, word for word, that states this record.")
    record = create_model("Record", __doc__="One thing the instruction asks for.", quote=(str, quote), **fields)
    return create_model("Records", __doc__="This page's records.", records=(list[record], ...))


def hashed(name: str) -> str:
    """Validate a document id."""
    if len(name) != 16 or not set(name) <= set("0123456789abcdef"):
        raise ValueError("a document id is the 16 hex digits an upload returned")
    return name


class Upload(BaseModel):
    """An uploaded file."""

    name: str
    content: str


class Feedback(BaseModel):
    """Feedback on an answer."""

    trace_id: str
    helpful: bool
    comment: str = ""


class Request(BaseModel):
    """One request."""

    prompt: str | None = None
    document_id: Annotated[str, AfterValidator(hashed)] | None = None
    approvals: dict[str, Any] | None = None
    approver: str | None = None
    feedback: Feedback | None = None
    upload: Upload | None = None

    @model_validator(mode="after")
    def one_kind(self) -> Self:
        """Exactly one kind per request."""
        if [self.prompt, self.approvals, self.feedback, self.upload].count(None) != 3:
            raise ValueError("send exactly one of prompt, approvals, feedback or upload")
        return self
