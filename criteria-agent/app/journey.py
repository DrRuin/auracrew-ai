"""The code map and Langfuse access the app's journey view shows."""

import ast
from functools import cache
from pathlib import Path

from pydantic import SecretStr

from app.ai.llm import ask_model
from app.answer.tools import contents_text
from app.core.config import settings
from app.core.documents import GUIDE, opened, pages
from app.core.prompts import CONTENTS, TOUR
from app.core.schemas import Tour

APP = Path(__file__).parent
TOURS: dict[str, Tour] = {}


def described(node: ast.AST, prefix: str = "") -> list[dict]:
    """The functions and classes in a body, with the first line of each docstring."""
    found = []
    for child in getattr(node, "body", []):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            kind = "class" if isinstance(child, ast.ClassDef) else "function"
            doc = (ast.get_docstring(child) or "").split("\n")[0]
            found.append({"name": f"{prefix}{child.name}", "kind": kind, "doc": doc, "line": child.lineno})
            if kind == "class":
                found += described(child, f"{child.name}.")
    return found


def imported(tree: ast.Module) -> list[str]:
    """The app modules a module imports, by name."""
    names = set()
    for node in ast.walk(tree):
        parts = (node.module or "").split(".") if isinstance(node, ast.ImportFrom) else []
        if parts[:1] == ["app"]:
            names |= {parts[2]} if len(parts) > 2 else {alias.name for alias in node.names}
    return sorted(names)


def module(path: Path) -> dict:
    """One Python module: what it is for, what it uses and what it defines."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    doc = (ast.get_docstring(tree) or "").split("\n")[0]
    return {
        "name": path.stem,
        "folder": path.parent.relative_to(APP).as_posix(),
        "doc": doc,
        "lines": len(source.splitlines()),
        "uses": imported(tree),
        "items": described(tree),
    }


@cache
def modules() -> list[dict]:
    """Every module of the app, folder by folder, then the gate's Cedar policy."""
    found = [
        module(path)
        for path in sorted(APP.rglob("*.py"), key=lambda p: p.relative_to(APP).parts)
        if path.stem != "__init__"
    ]
    policy = (APP / "gate" / "policy.cedar").read_text(encoding="utf-8")
    cedar = {
        "name": "policy",
        "folder": "gate",
        "doc": "The gate's policy, loaded by Strands' Cedar authorization.",
        "source": policy,
    }
    return [*found, cedar | {"lines": len(policy.splitlines()), "uses": [], "items": []}]


async def tour(document: str) -> Tour:
    """Questions written for this document that each show one control at work, made once per document."""
    if document not in TOURS:
        GUIDE.set(await opened(document))
        prompt = f"{CONTENTS.format(pages=len(pages()))}\n{await contents_text()}"
        TOURS[document] = await ask_model(TOUR, prompt, Tour)
    return TOURS[document]


def revealed(value: SecretStr | None) -> str | None:
    """A secret's value, or None when it is not set."""
    return value.get_secret_value() if value else None


def access() -> dict | None:
    """Where Langfuse is and how to sign in, when this deployment shows it."""
    config = settings()
    if not config.journey_credentials:
        return None
    return {
        "url": "/langfuse",
        "project": config.langfuse_project,
        "email": config.langfuse_user_email,
        "password": revealed(config.langfuse_user_password),
        "public_key": revealed(config.langfuse_public_key),
        "secret_key": revealed(config.langfuse_secret_key),
    }
