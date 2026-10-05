"""Test data and stand-ins."""

from pathlib import Path
from types import SimpleNamespace

from app.core.documents import Doc, Page
from app.core.schemas import Claim

SAMPLE_PDF = Path(__file__).with_name("sample.pdf")
PAGES = (
    Page(
        "guide",
        1,
        "Maximum LTV | is 75% of the open market value.",
        "Maximum LTV is 75%\nof the  open market value.",
    ),
    Page("guide", 2, "Maximum term 18 months.", "Maximum term 18 months."),
)
OTHER = (Page("other-lender", 1, "Maximum LTV is 85% gross.", "Maximum LTV is 85% gross."),)


class State(dict):
    """Minimal stand-in for Strands agent state."""

    def set(self, key: str, value: object) -> None:
        """Store a value."""
        self[key] = value

    def delete(self, key: str) -> None:
        """Drop a value."""
        self.pop(key, None)


def doc(threshold: float = 1.0, pdf: bytes = b"february edition") -> Doc:
    """The two-page test guide."""
    made = Doc("guide", "guide", pdf, threshold=threshold)
    made.__dict__["pages"] = PAGES
    return made


def claim(quote: str, source: str = "guide") -> Claim:
    """A claim whose text is its quote."""
    return Claim(text=quote, quote=quote, source=source)


def returns(value: object):
    """An async stand-in returning a value."""

    async def stand_in(*_, **__):
        """Return the fixed value."""
        return value

    return stand_in


def fake_jev(
    choice: str = "supports", confidence: float = 1.0, noul: float = 1.0, sent: list | None = None
) -> SimpleNamespace:
    """A fake Jev client."""

    async def system_one(state, questions):
        """Answer all questions uniformly."""
        if sent is not None:
            sent.append(state)
        answer = SimpleNamespace(choice=choice, confidence=confidence, noul=noul)
        spent = SimpleNamespace(input_tokens=len(questions), output_tokens=0)
        return SimpleNamespace(answers=dict.fromkeys(questions, answer), model="jev-1", usage=spent)

    return SimpleNamespace(system_one=system_one)
