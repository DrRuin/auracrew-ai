"""The journey view's code map and Langfuse access."""

from pydantic import SecretStr

from app import journey


def test_the_code_map_lists_every_module_and_every_function_with_its_docstring():
    """Nothing in app/ is left off the map, and every entry says what it does."""
    mapped = {m["name"]: m for m in journey.modules()}
    assert set(mapped) == {p.stem for p in journey.APP.rglob("*.py") if p.stem != "__init__"} | {"policy"}
    assert (mapped["gate"]["folder"], mapped["main"]["folder"]) == ("gate", ".")
    assert {"gate", "tools"} <= set(mapped["agent"]["uses"])
    assert {"name": "screened", "kind": "function"}.items() <= next(
        i for i in mapped["agent"]["items"] if i["name"] == "screened"
    ).items()
    assert all(item["doc"] for m in mapped.values() for item in m["items"])
    assert "permit" in mapped["policy"]["source"]


def test_langfuse_access_is_shown_only_when_the_deployment_allows_it(config, monkeypatch):
    """A deployment can hide the sign-in and keys."""
    monkeypatch.setattr(config, "langfuse_user_password", SecretStr("pw"))
    assert journey.access()["password"] == "pw"
    monkeypatch.setattr(config, "journey_credentials", False)
    assert journey.access() is None


async def test_a_tour_is_written_once_per_document_from_its_own_contents(the_guide, monkeypatch):
    """One model call per document, given the document's contents."""
    prompts = []

    async def ask_model(_, prompt: str, schema: type, *__):
        """Write the tour."""
        prompts.append(prompt)
        return schema(lookup="l", table="t", pressure="p")

    async def opened(_):
        """The test guide."""
        return the_guide

    async def contents_text():
        """The test guide's contents."""
        return "Page 1: Lending limits."

    monkeypatch.setattr(journey, "ask_model", ask_model)
    monkeypatch.setattr(journey, "opened", opened)
    monkeypatch.setattr(journey, "contents_text", contents_text)
    monkeypatch.setattr(journey, "TOURS", {})
    assert (await journey.tour("a" * 16)).pressure == "p"
    assert (await journey.tour("a" * 16)).lookup == "l"
    assert len(prompts) == 1 and "Page 1: Lending limits." in prompts[0]
