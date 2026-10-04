"""The cover letter step: slot fills, LaTeX safety, and the retry."""

import datetime as dt

import pytest

from tailor import cover
from tailor.fetch import Posting

POSTING = Posting(url="https://example.com/j/1", text="Build APIs for payments.",
                  title="Backend Engineer", company="Example Corp")
PROFILE = (
    "Koantek FastAPI SQL forecasting. Built a production data copilot. "
    "American Eagle. Python."
)
RESUME = r"\documentclass{article}\begin{document}Koantek FastAPI SQL\end{document}"

TEMPLATE = """Dear Hiring Team,

<OPENING Two sentences about the company.>

What draws me to the <JOB_ROLE> role is building systems people can rely on.
I have spent the last few years taking a model out of a notebook and putting
it behind an API with monitoring, so the work is useful after the experiment
ends. That is the habit I want to keep, and it is the habit this role asks for.

<ROLE_SPECIFIC_EVIDENCE One example.>

<COMPANY_SPECIFIC_PARAGRAPH Why this company.>

I am looking for a role at the intersection of machine learning and software
engineering. From what I understand about <COMPANY_NAME>, this role offers
exactly that kind of challenge.

Thank you for your consideration. I would welcome the opportunity to discuss
how I could contribute to <SPECIFIC_GOAL_OR_TEAM>.

Best,
Jane Doe
"""

GOOD_FILLS = """OPENING:
Example Corp is building a payments API that has to stay correct under load. That is the reliability problem I have been chasing in production systems.

ROLE_SPECIFIC_EVIDENCE:
At Koantek I shipped a FastAPI service that turned questions into SQL. Failures dropped after we validated each query before it ran.

COMPANY_SPECIFIC_PARAGRAPH:
I want to do that work on Example Corp's payments API, where a mistake is a failed payment rather than a bad dashboard. That is a higher bar than the systems I have shipped so far.

SPECIFIC_GOAL_OR_TEAM:
the payments API team
"""

DASHED_FILLS = GOOD_FILLS.replace(
    "Example Corp is building a payments API that has to stay correct under load.",
    "Example Corp is building a payments API — it has to stay correct under load.",
    1,
)


def test_clean_strips_a_code_fence_and_extra_blank_lines():
    assert cover.clean("```text\nDear X,\n\n\n\nBody\n```") == "Dear X,\n\nBody"
    assert cover.clean("  plain  ") == "plain"


def test_escape_handles_every_latex_special():
    out = cover.escape(r"50% & $1M # a_b {x} ~ ^ \ ")
    assert r"\%" in out and r"\&" in out and r"\$" in out and r"\#" in out
    assert r"\_" in out and r"\{" in out and r"\}" in out
    assert r"\textasciitilde{}" in out and r"\textasciicircum{}" in out
    assert r"\textbackslash{}" in out
    assert out.count("\\textbackslash") == 1, "escaping must not re-escape its own output"


def test_to_tex_keeps_the_sign_off_on_adjacent_lines():
    tex = cover.to_tex("Dear X,\n\nBody 100%.\n\nBest,\nJane Doe", today=dt.date(2026, 9, 11))
    assert "September 11, 2026" in tex
    assert "Body 100\\%." in tex
    assert "Best, \\\\\nJane Doe" in tex
    assert tex.count("\\documentclass") == 1
    assert "fontspec" not in tex, "pdflatex only; BasicTeX has no fonts to spec"


def test_parse_slots_reads_names_and_instructions():
    slots = cover.parse_slots(TEMPLATE)
    assert [slot.name for slot in slots] == [
        "OPENING", "JOB_ROLE", "ROLE_SPECIFIC_EVIDENCE",
        "COMPANY_SPECIFIC_PARAGRAPH", "COMPANY_NAME", "SPECIFIC_GOAL_OR_TEAM",
    ]
    assert "company" in cover.parse_slots(TEMPLATE)[0].instruction
    assert [slot.name for slot in cover.model_slots(TEMPLATE)] == [
        "OPENING", "ROLE_SPECIFIC_EVIDENCE",
        "COMPANY_SPECIFIC_PARAGRAPH", "SPECIFIC_GOAL_OR_TEAM",
    ]


def test_assemble_fills_the_role_and_company_from_the_posting():
    fills = cover.parse_fills(GOOD_FILLS)
    letter = cover.assemble(TEMPLATE, fills, POSTING)
    assert "Backend Engineer" in letter
    assert letter.count("Example Corp") >= 2
    assert "<OPENING" not in letter
    assert "Best,\nJane Doe" in letter
    assert "payments API team" in letter


def test_load_template_prefers_the_persons_letter(tmp_path, monkeypatch):
    monkeypatch.setattr(cover, "template_path", lambda: tmp_path / "cover.md")
    (tmp_path / "cover.md").write_text("mine\n")
    assert cover.load_template() == "mine\n"


def test_load_template_falls_back_to_the_example(tmp_path, monkeypatch):
    monkeypatch.setattr(cover, "template_path", lambda: tmp_path / "missing.md")
    text = cover.load_template()
    assert "<OPENING" in text and "<JOB_ROLE>" in text and "Your Name" in text


def test_write_accepts_well_shaped_slots(monkeypatch):
    calls = []

    def fake_complete(system, user, **kwargs):
        calls.append((system, user))
        return GOOD_FILLS

    monkeypatch.setattr(cover.llm, "complete", fake_complete)
    monkeypatch.setattr(cover.llm, "tailor_model", lambda: "test/model")
    letter = cover.write(POSTING, RESUME, profile=PROFILE, extra_instruction="mention Go",
                         template=TEMPLATE)

    assert letter.attempts == 1 and letter.model == "test/model"
    assert letter.text.startswith("Dear Hiring Team,")
    assert "Backend Engineer" in letter.text and "Best,\nJane Doe" in letter.text
    system, user = calls[0]
    assert "Never invent" in system or "never invent" in system.lower()
    assert "Example Corp" in user and "Likes Go" not in user and "mention Go" in user
    assert "### OPENING" in user and "JOB_ROLE" not in user.split("## Slots to fill")[1]
    assert "visa" in system.lower(), "the rules must forbid visa talk in the letter"


def test_dashes_are_recognised_but_hyphenated_words_are_not():
    assert cover.has_dash("the role — which")
    assert cover.has_dash("the role – which")
    assert cover.has_dash("the role - which")
    assert cover.has_dash("the role -- which")
    assert not cover.has_dash("end-to-end, high-throughput work")


def test_strip_dashes_leaves_clean_punctuation():
    out = cover.strip_dashes("the role — which I like — is good.\nAlso this - and that -- end —\nDone —")
    assert out == "the role, which I like, is good.\nAlso this, and that, end\nDone"
    assert ", ," not in out and " ," not in out


def test_a_dashed_slot_is_sent_back(monkeypatch):
    replies = iter([DASHED_FILLS, GOOD_FILLS])
    seen = []

    def fake_complete(system, user, **kwargs):
        seen.append(user)
        return next(replies)

    monkeypatch.setattr(cover.llm, "complete", fake_complete)
    monkeypatch.setattr(cover.llm, "tailor_model", lambda: "test/model")
    letter = cover.write(POSTING, RESUME, profile=PROFILE, template=TEMPLATE)
    assert letter.attempts == 2 and "—" not in letter.text
    assert "used a dash" in seen[1]


def test_dashes_that_survive_every_retry_are_rewritten_not_shipped(monkeypatch):
    monkeypatch.setattr(cover.llm, "complete", lambda *a, **k: DASHED_FILLS)
    monkeypatch.setattr(cover.llm, "tailor_model", lambda: "test/model")
    letter = cover.write(POSTING, RESUME, profile=PROFILE, template=TEMPLATE)
    assert "—" not in letter.text and "API, it has to stay" in letter.text
    assert letter.attempts == cover.ATTEMPTS


def test_the_rules_forbid_dashes_and_cliches():
    rules = cover.RULES.read_text()
    assert "em dash" in rules and "passionate about" in rules
    assert "I am applying for" in rules


def test_write_retries_once_when_a_slot_is_missing(monkeypatch):
    replies = iter(["Too short.", GOOD_FILLS])
    seen = []

    def fake_complete(system, user, **kwargs):
        seen.append(user)
        return next(replies)

    monkeypatch.setattr(cover.llm, "complete", fake_complete)
    monkeypatch.setattr(cover.llm, "tailor_model", lambda: "test/model")
    letter = cover.write(POSTING, RESUME, profile=PROFILE, template=TEMPLATE)
    assert letter.attempts == 2
    assert "previous reply was rejected" in seen[1]
    assert "OPENING was missing" in seen[1]


def test_write_gives_up_after_the_retry(monkeypatch):
    monkeypatch.setattr(cover.llm, "complete", lambda *a, **k: "Nope.")
    monkeypatch.setattr(cover.llm, "tailor_model", lambda: "test/model")
    with pytest.raises(cover.CoverLetterError, match=f"{cover.ATTEMPTS} attempts"):
        cover.write(POSTING, RESUME, profile=PROFILE, template=TEMPLATE)


def test_a_banned_opener_is_rejected(monkeypatch):
    bad = GOOD_FILLS.replace(
        "Example Corp is building a payments API that has to stay correct under load.",
        "I am excited to apply to Example Corp for this API role.",
        1,
    )
    replies = iter([bad, GOOD_FILLS])
    monkeypatch.setattr(cover.llm, "complete", lambda *a, **k: next(replies))
    monkeypatch.setattr(cover.llm, "tailor_model", lambda: "test/model")
    letter = cover.write(POSTING, RESUME, profile=PROFILE, template=TEMPLATE)
    assert letter.attempts == 2
    assert "I am excited to apply" not in letter.text


def test_an_invented_figure_is_rejected(monkeypatch):
    bad = GOOD_FILLS.replace(
        "Failures dropped after we validated each query before it ran.",
        "The service cut latency by 48 percent after we validated each query.",
        1,
    )
    replies = iter([bad, GOOD_FILLS])
    seen = []

    def fake_complete(system, user, **kwargs):
        seen.append(user)
        return next(replies)

    monkeypatch.setattr(cover.llm, "complete", fake_complete)
    monkeypatch.setattr(cover.llm, "tailor_model", lambda: "test/model")
    letter = cover.write(POSTING, RESUME, profile=PROFILE, template=TEMPLATE)
    assert letter.attempts == 2
    assert "48" in seen[1] and "48" not in letter.text


def test_a_generic_company_paragraph_is_rejected(monkeypatch):
    bad = GOOD_FILLS.replace(
        "I want to do that work on Example Corp's payments API, where a mistake is a failed payment rather than a bad dashboard. That is a higher bar than the systems I have shipped so far.",
        "I want to grow as an engineer and keep shipping useful systems. That is the kind of challenge I am looking for next.",
        1,
    )
    replies = iter([bad, GOOD_FILLS])
    monkeypatch.setattr(cover.llm, "complete", lambda *a, **k: next(replies))
    monkeypatch.setattr(cover.llm, "tailor_model", lambda: "test/model")
    letter = cover.write(POSTING, RESUME, profile=PROFILE, template=TEMPLATE)
    assert letter.attempts == 2
    assert "Example Corp's payments API" in letter.text


def test_sentence_count_ignores_decimals():
    assert cover.sentence_count("MAPE was 9.1 percent. Costs fell.") == 2
    assert cover.sentence_count("the payments API team") == 0
