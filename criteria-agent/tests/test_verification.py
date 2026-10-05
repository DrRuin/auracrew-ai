"""Claim verification."""

import pytest
from helpers import OTHER, PAGES, claim

from app.ai import jev as jev_module
from app.answer import agent, verification
from app.core import schemas
from app.core.schemas import Answer, Check, Claim


@pytest.fixture
def judge(monkeypatch):
    """A fake GPT judge."""
    prompts: list[str] = []

    def install(relation: str) -> list[str]:
        """Rule every claim with this relation."""

        async def ask_model(_, prompt: str, schema: type, reasoning: bool = False, images: tuple = ()):
            """Answer the escalation."""
            prompts.append(prompt)
            return schema(**dict.fromkeys(schema.model_fields, relation))

        monkeypatch.setattr(verification, "ask_model", ask_model)
        return prompts

    return install


async def test_code_settles_invented_and_other_document_quotes_and_jev_judges_the_rest(trusted, jev, monkeypatch):
    """Claims are traced to their source."""
    jev("supports", 0.9)
    monkeypatch.setattr(verification, "ask_model", lambda *_: pytest.fail("confident verdicts do not escalate"))
    claims = [
        claim("Maximum LTV is 75%"),
        claim("Maximum LTV is 90%"),
        claim("Maximum LTV is 85%"),
        claim('"ltv_percent": 60.0', "tool"),
        claim("ltv_percent", "tool"),
        claim("loan to value", "tool"),
    ]
    checks = await verification.verify(claims, ['{"ltv_percent": 60.0}'], PAGES + OTHER)
    assert {c.claim.quote: (c.verdict, c.judge) for c in checks} == {
        "Maximum LTV is 75%": ("supports", "jev"),
        "Maximum LTV is 90%": ("fabricated", "code"),
        "Maximum LTV is 85%": ("wrong_source", "code"),
        '"ltv_percent": 60.0': ("supports", "jev"),
        "ltv_percent": ("supports", "jev"),
        "loan to value": ("fabricated", "code"),
    }
    assert next(c for c in checks if c.claim.quote == "ltv_percent").evidence == '{"ltv_percent": 60.0}'


async def test_a_guide_quote_labelled_as_a_tool_result_is_judged_on_its_page(trusted, jev):
    """Mislabelled guide quotes still count."""
    jev("supports", 0.9)
    [check] = await verification.verify([claim("Maximum term 18 months.", "tool")], [], PAGES)
    assert (check.verdict, check.page, check.claim.source) == ("supports", 2, "guide")


async def test_a_tool_claim_is_escalated_against_the_real_tool_result(jev, judge):
    """The judge reads the real tool result."""
    jev("supports", 0.5)
    prompts = judge("supports")
    await verification.verify(
        [claim("ltv is 60% within the 75% maximum on guide page 1", "tool")],
        ["LTV is 60%, within the 75% maximum on guide page 1."],
        PAGES,
    )
    assert "\n\nSource:\nLTV is 60%, within the 75% maximum on guide page 1." in prompts[0]


def test_the_answer_schema_bounces_a_guide_quote_the_guide_lacks():
    """Misquotes go back to the model while each try has fewer; with no run to bound it, nothing bounces."""
    schemas.BOUNCED.set(None)
    schemas.GroundedAnswer(answerable=True, claims=[claim("Maximum term 36 months.").model_dump()], note="")
    schemas.BOUNCED.set([])
    schemas.GroundedAnswer(answerable=True, claims=[claim("Maximum term 18 months.").model_dump()], note="")
    with pytest.raises(ValueError, match="not verbatim in the guide"):
        schemas.GroundedAnswer(answerable=True, claims=[claim("Maximum term 36 months.").model_dump()], note="")
    schemas.BOUNCED.set([])
    misquoted = [claim("Maximum term 36 months.").model_dump(), claim("Maximum LTV is 95%").model_dump()]
    with pytest.raises(ValueError, match="36 months.*95%"):
        schemas.GroundedAnswer(answerable=True, claims=misquoted, note="")
    with pytest.raises(ValueError, match="36 months"):
        schemas.GroundedAnswer(answerable=True, claims=misquoted[:1], note="")
    schemas.GroundedAnswer(answerable=True, claims=misquoted[:1], note="")


def test_a_tool_quote_bounces_unless_a_tool_result_or_page_holds_it():
    """A quote labelled tool is checked as verification will check it: in the tool results, then on the pages."""
    result = "LTV is 60%, within the 75% maximum on guide page 1."
    token = schemas.RESULTS.set(lambda: [result])
    schemas.BOUNCED.set([])
    schemas.GroundedAnswer(answerable=True, claims=[claim(result, "tool").model_dump()], note="")
    schemas.GroundedAnswer(answerable=True, claims=[claim("Maximum term 18 months.", "tool").model_dump()], note="")
    with pytest.raises(ValueError, match="repeated; state the fact its own quote gives: Maximum term 18 months."):
        schemas.GroundedAnswer(answerable=True, claims=[claim("Maximum term 18 months.").model_dump()] * 2, note="")
    schemas.BOUNCED.set([])
    with pytest.raises(ValueError, match="not verbatim in the guide or a tool result"):
        schemas.GroundedAnswer(
            answerable=True, claims=[claim('{"page": 2, "text": "# Contents"}', "tool").model_dump()], note=""
        )
    schemas.RESULTS.reset(token)


@pytest.mark.parametrize(
    ("verdicts", "answerable", "status", "kept", "doc"),
    [
        (["supports", "supports"], True, "grounded", 2, None),
        (["supports", "says_nothing"], True, "partial", 1, None),
        (["supports", "wrong_source"], True, "partial", 1, None),
        (["supports", "contradicts"], True, "partial", 1, None),
        (["supports", "contradicts"], True, "withheld", 0, "tool"),
        (["fabricated"], True, "withheld", 0, None),
        (["supports"], False, "refused", 0, None),
        (["supports", "blurred"], True, "partial", 1, None),
        (["blurred"], True, "withheld", 0, None),
    ],
    ids=[
        "all supported",
        "one unsupported",
        "one wrong source",
        "one page claim contradicted",
        "an assessment claim contradicted",
        "nothing supported",
        "guide silent",
        "one on a disputed scan",
        "all on a disputed scan",
    ],
)
async def test_release_policy(monkeypatch, verdicts, answerable, status, kept, doc):
    """What gets released; a contradicted tool claim withholds all, and a claim the scan disputes is held back."""

    async def cited(checks, located):
        """Mark claims on a disputed scan unclear."""
        return [c.model_copy(update={"verdict": "unclear"}) if c.claim.text == "blurred" else c for c in checks]

    monkeypatch.setattr(agent, "cited", cited)
    checks = [
        Check(claim=claim(v), verdict="supports" if v == "blurred" else v, judge="code", grounded=doc == "tool")
        for v in verdicts
    ]
    answer = Answer(answerable=answerable, claims=[c.claim for c in checks], note="")
    _, got, released = await agent.release(answer, checks, {})
    assert (got, len(released)) == (status, kept)


@pytest.mark.parametrize(
    ("jev_verdict", "confidence", "text", "quote", "trusted"),
    [
        ("supports", 0.95, "Maximum LTV is 75%.", "Maximum LTV is 75%", True),
        ("supports", 0.95, "The £300,000 loan is 60% LTV.", "Maximum LTV is 75%", False),
        ("supports", 0.5, "Maximum LTV is 75%.", "Maximum LTV is 75%", False),
        ("says_nothing", 0.99, "Maximum LTV is 75%.", "Maximum LTV is 75%", False),
    ],
    ids=["confident, figures quoted", "a figure not in the quote", "unsure", "not supports"],
)
def test_jev_stands_alone_only_on_a_confident_supports_with_its_figures_quoted(
    jev_verdict, confidence, text, quote, trusted
):
    """Numbers and non-supports go to GPT."""
    check = Check(
        claim=claim(quote).model_copy(update={"text": text}),
        verdict=jev_verdict,
        judge="jev",
        jev=jev_verdict,
        jev_confidence=confidence,
    )
    assert verification.trusted(check, 0.9) is trusted


async def test_a_tool_claim_quoting_the_working_is_judged_only_on_its_grounds(jev, judge):
    """The working is quotable, never evidence; GPT always rules."""
    jev("supports", 1.0)
    prompts = judge("supports")
    output = 'The case: loan 60, value 100\nLTV: page 1 requires "Maximum LTV is 75%". Worked out: 60%; meets.'
    [check] = await verification.verify(
        [Claim(text="The LTV is 60%, within the 75% maximum.", quote="Worked out: 60%; meets.", source="tool")],
        [output],
        PAGES,
        {output: 'The case: loan 60, value 100\nLTV: page 1 requires "Maximum LTV is 75%"'},
    )
    assert (check.verdict, check.judge, check.jev) == ("supports", "gpt", None)
    assert "Worked out" not in prompts[0] and "Worked out" not in check.evidence


@pytest.mark.parametrize(("yes", "dropped"), [(0.9, True), (0.1, False)])
async def test_a_note_that_calls_missing_what_the_question_states_is_flagged(jev, yes, dropped):
    """Jev decides whether the note is unfounded."""
    sent = jev(noul=yes)
    assert (
        await jev_module.unfounded("The purchase price is not stated.", "Bought at 500,000", "LTV is 60%.") is dropped
    )
    assert set(sent[-1]) == {"note", "question", "answer"}


async def test_only_a_contradiction_that_would_withhold_everything_is_rejudged_at_full_reasoning(monkeypatch):
    """A grounded contradiction gets a second, reasoning judge; other verdicts stand."""
    calls: list[bool] = []

    async def escalate(group, question, others, outputs, reasoning=False):
        """Record the effort and overturn the verdict."""
        calls.append(reasoning)
        return [c.model_copy(update={"verdict": "supports"}) for c in group]

    monkeypatch.setattr(verification, "escalate", escalate)
    checks = [
        Check(claim=claim("a"), verdict="contradicts", judge="gpt", doc="tool", grounded=True),
        Check(claim=claim("b"), verdict="contradicts", judge="gpt", page=1),
        Check(claim=claim("c"), verdict="says_nothing", judge="gpt", doc="tool", grounded=True),
    ]
    got = await verification.confirmed(checks, "q", [], [])
    assert [c.verdict for c in got] == ["supports", "contradicts", "says_nothing"] and calls == [True]


def test_a_tool_claim_whose_figure_is_only_in_another_tool_output_goes_to_gpt():
    """The figures must be in the claim's own quoted line, not anywhere among the tool outputs."""
    check = Check(
        claim=claim("Table 1, row 3: Rate (pm): 0.87.", "tool").model_copy(
            update={"text": "SC-3B costs 0.97% a month."}
        ),
        verdict="supports",
        judge="jev",
        jev="supports",
        jev_confidence=0.99,
        doc="tool",
        evidence="Table 1, row 3: Rate (pm): 0.87.\nTable 1, row 9: Rate (pm): 0.97.",
    )
    assert not verification.trusted(check, 0.9)


async def test_a_tool_result_quoted_under_a_guide_label_is_judged_as_the_tool_result(trusted, jev):
    """The model's source label does not decide where a quote is found."""
    jev("supports", 1.0)
    row = "Table 1, row 3, page 2: Code: SC-3B; Rate (pm): 0.87."
    [check] = await verification.verify([claim(row)], [row], PAGES)
    assert check.verdict != "fabricated" and check.claim.source == "tool"


async def test_a_tool_claim_is_judged_on_the_one_result_its_quote_is_in(jev, judge):
    """Other tool results do not bury the one a claim rests on."""
    jev("supports", 0.5)
    prompts = judge("supports")
    outputs = ["Contents: page 1 holds the products table.", "The refer decision for A7 is already recorded."]
    await verification.verify([claim(outputs[1], "tool")], outputs, PAGES)
    assert f"Source:\n{outputs[1]}\n\nClaims" in prompts[0] and outputs[0] not in prompts[0]


async def test_the_judge_sees_the_other_quotes_the_answer_rests_on(judge):
    """A total built from two sources is judged with both in view."""
    prompts = judge("supports")
    own = Check(
        claim=claim("Loan above 2,500,000: 0.10%", "tool"),
        verdict="says_nothing",
        judge="jev",
        doc="tool",
        evidence="Loan above 2,500,000: 0.10%",
    )
    await verification.escalate([own], "q", ["Loan above 2,500,000: 0.10%", "RG-4B rate 1.01%"], [])
    assert "Other quotes this answer rests on:\n- RG-4B rate 1.01%" in prompts[0] and prompts[0].count("0.10%") == 2


async def test_a_tool_claim_comparing_two_results_is_judged_with_the_result_holding_its_other_figure(judge):
    """A claim's figure missing from its own result is looked up in the answer's other tool results."""
    prompts = judge("supports")
    own_result = "Table 1, row 46: Code: HM-1B; Arrangement fee: 1.5%."
    other_result = "Table 1, row 45: Code: HM-1A; Arrangement fee: 1.75%."
    unrelated = "Table 3, row 2: Expat borrower; Loading: 0.10%."
    own = Check(
        claim=claim("HM-1B's fee of 1.5% is lower than HM-1A's 1.75%.", "tool"),
        verdict="says_nothing",
        judge="jev",
        doc="tool",
        evidence=own_result,
    )
    await verification.escalate([own], "q", [], [own_result, other_result, unrelated])
    assert other_result in prompts[0] and unrelated not in prompts[0]


async def test_a_claim_restating_the_gates_refusal_is_supported_by_code(judge):
    """The gate writes its refusal itself, so a claim within it needs no model to verify it."""
    prompts = judge("says_nothing")
    refusal = "DENIED: Not submitted: the agent tried to record approve for applicant H9."
    stated = claim("Not submitted: the agent tried to record approve for applicant H9.", "tool")
    stated = stated.model_copy(update={"quote": refusal})
    [check] = await verification.verify([stated], [refusal], PAGES)
    assert (check.verdict, check.judge, prompts) == ("supports", "code", [])


async def test_a_claim_its_quote_says_nothing_about_is_rejudged_on_the_whole_evidence_set(judge):
    """A summary across rows stands when the pages and code-made results together state it, never the model's working."""
    prompts = judge("supports")
    working = "Worked out: the minimum is £100,000."
    rows = "Table 1, row 1: Min loan: £100,000. Table 1, row 2: Min loan: £500,001."
    summary = Check(
        claim=claim("The smallest loan in the table is £100,000."), verdict="says_nothing", judge="gpt", page=1
    )
    other = Check(claim=claim("A row sets £500,001."), verdict="supports", judge="jev", page=1)
    got = await verification.pooled([summary, other], "q", [rows, working], {working: "page text"})
    assert [c.verdict for c in got] == ["supports", "supports"]
    assert rows in prompts[0] and working not in prompts[0]
