"""Tailoring: block extraction, the frozen-section and length gates, retries."""

import pytest

from tailor import layout, tailor
from tailor.fetch import Posting


@pytest.fixture(autouse=True)
def fixed_line_width(monkeypatch):
    """The line rule is measured off the compiled master; the miniature
    resume here needs a width of its own, or every bullet is one line."""
    monkeypatch.setattr(layout, "chars_per_line", lambda pdf_path=None: 40)

# A miniature of the real template: same section heading form, same
# \resumeItem{\normalsize{...}} bullet shape, so the parsers are exercised
# against the structure they actually meet.
ORIGINAL = r"""\documentclass{article}
\newcommand{\resumeItem}[1]{\item{#1}}
\begin{document}
\section{\texorpdfstring{\color{airforceblue}SUMMARY}{}}
{\normalsize Software engineer with backend and ML experience.}
\section{\texorpdfstring{\color{airforceblue}EDUCATION}{}}
MS Computer Science, 2026.
\section{\texorpdfstring{\color{airforceblue}EXPERIENCE}{}}
\resumeItem{\normalsize{Built a scalable Python service handling 50K requests per day}}
\resumeItem{\normalsize{Cut p95 latency 52 percent with a Redis caching layer}}
\section{\texorpdfstring{\color{airforceblue}ACHIEVEMENTS}{}}
Early Career Innovator Award, 2025.
\end{document}
"""

POSTING = Posting(
    url="https://example.com/jobs/1",
    text="Backend role. Distributed systems, caching, Python. " * 12,
    title="Backend Engineer",
    company="Example Corp",
)


def reply(tex: str, verdict: str = "MATCH: backend and caching experience lines up.",
          rationale: str = "- reworded a bullet") -> str:
    return (
        f"```verdict\n{verdict}\n```\n\n"
        f"```tex\n{tex}```\n\n"
        f"```markdown\n{rationale}\n```\n"
    )


def stub_model(monkeypatch, *replies):
    """Serve `replies` in order, and record every prompt sent."""
    sent = []

    def complete(system, user, **kwargs):
        sent.append(user)
        return replies[min(len(sent) - 1, len(replies) - 1)]

    monkeypatch.setattr(tailor.llm, "complete", complete)
    monkeypatch.setattr(tailor.llm, "tailor_model", lambda: "test/model")
    return sent


# --- parsing -----------------------------------------------------------------

def test_split_sections_finds_every_heading():
    sections = tailor.split_sections(ORIGINAL)
    assert set(sections) == {"PREAMBLE", "SUMMARY", "EDUCATION", "EXPERIENCE", "ACHIEVEMENTS"}
    assert "Redis" in sections["EXPERIENCE"]


def test_bullet_lengths_reads_every_resume_item():
    assert len(tailor.bullet_lengths(ORIGINAL)) == 2


def test_extract_block_reads_each_fence():
    text = reply(ORIGINAL)
    assert tailor._extract_block(text, ("tex", "latex")).startswith(r"\documentclass")
    assert tailor._extract_block(text, ("verdict",)).startswith("MATCH")
    assert tailor._extract_block(text, ("markdown", "md")) == "- reworded a bullet"


def test_make_diff_is_a_unified_diff():
    after = ORIGINAL.replace("scalable Python service", "scalable distributed service")
    diff = tailor.make_diff(ORIGINAL, after)
    assert "+" in diff and "distributed" in diff
    assert diff.startswith("--- a/resume.tex")


# --- the gates ---------------------------------------------------------------

def test_validate_accepts_an_edit_inside_an_editable_section():
    after = ORIGINAL.replace("scalable Python service", "scalable Python backend")
    assert tailor._validate(ORIGINAL, after) == []


def test_validate_rejects_an_edit_to_a_frozen_section():
    after = ORIGINAL.replace("Early Career Innovator Award, 2025.", "Some Other Award, 2025.")
    with pytest.raises(tailor.TailorError, match="modified ACHIEVEMENTS"):
        tailor._validate(ORIGINAL, after)


def test_validate_rejects_an_edit_to_education():
    after = ORIGINAL.replace("MS Computer Science, 2026.", "MS Computer Science, 2025.")
    with pytest.raises(tailor.TailorError, match="modified EDUCATION"):
        tailor._validate(ORIGINAL, after)


def test_validate_rejects_a_dropped_section():
    after = ORIGINAL.replace(
        r"\section{\texorpdfstring{\color{airforceblue}ACHIEVEMENTS}{}}" "\n"
        "Early Career Innovator Award, 2025.\n", "")
    with pytest.raises(tailor.TailorError, match="dropped section"):
        tailor._validate(ORIGINAL, after)


def test_validate_rejects_a_changed_bullet_count():
    after = ORIGINAL.replace(
        r"\resumeItem{\normalsize{Cut p95 latency 52 percent with a Redis caching layer}}", "")
    with pytest.raises(tailor.TailorError, match="bullet count changed"):
        tailor._validate(ORIGINAL, after)


def test_validate_rejects_a_bullet_that_grew_a_line():
    """Words may change; printed lines may not. A bullet that runs onto
    another line is what turns the tuned one-page resume into two."""
    after = ORIGINAL.replace(
        "Cut p95 latency 52 percent with a Redis caching layer",
        "Cut p95 latency 52 percent with a Redis caching layer " + "and more words " * 6)
    with pytest.raises(tailor.TailorError, match="line budget"):
        tailor._validate(ORIGINAL, after)


def test_validate_accepts_more_words_inside_the_same_lines():
    """The point of the change: a short bullet may be filled out to the
    width it already occupies without tripping anything."""
    short = ORIGINAL.replace("Cut p95 latency 52 percent with a Redis caching layer",
                             "Cut p95 latency 52 percent")
    filled = short.replace("Cut p95 latency 52 percent",
                           "Cut p95 latency 52 percent by adding Redis")
    assert tailor._validate(short, filled) == []


def test_validate_warns_when_a_bullet_lost_a_line():
    after = ORIGINAL.replace(
        "Built a scalable Python service handling 50K requests per day",
        "Built a Python service")
    warnings = tailor._validate(ORIGINAL, after)
    assert len(warnings) == 1 and "lost a line" in warnings[0]


def test_validate_ignores_pure_whitespace_reflow():
    after = ORIGINAL.replace("MS Computer Science, 2026.", "MS Computer Science,\n2026.")
    assert tailor._validate(ORIGINAL, after) == []


def test_validate_rejects_unbalanced_braces():
    with pytest.raises(tailor.TailorError, match="unbalanced braces"):
        tailor._validate(ORIGINAL, ORIGINAL + "\\textbf{oops")


def test_validate_rejects_empty_output():
    with pytest.raises(tailor.TailorError, match="empty"):
        tailor._validate(ORIGINAL, "   \n  ")


# --- the loop ----------------------------------------------------------------

def test_happy_path(monkeypatch):
    after = ORIGINAL.replace("scalable Python service", "scalable Python backend")
    stub_model(monkeypatch, reply(after))
    result = tailor.tailor(POSTING, resume_tex=ORIGINAL)
    assert "Python backend" in result.tex
    assert result.attempts == 1
    assert result.verdict.startswith("MATCH")
    assert result.model == "test/model"


def test_mismatch_raises_and_tailors_nothing(monkeypatch):
    stub_model(monkeypatch, "```verdict\nMISMATCH: role requires 10 years and a clearance.\n```")
    with pytest.raises(tailor.Mismatch, match="clearance"):
        tailor.tailor(POSTING, resume_tex=ORIGINAL)


def test_retries_when_a_frozen_section_was_touched(monkeypatch):
    bad = ORIGINAL.replace("Early Career Innovator Award, 2025.", "Fake Award, 2025.")
    good = ORIGINAL.replace("scalable Python service", "scalable Python backend")
    sent = stub_model(monkeypatch, reply(bad), reply(good))

    result = tailor.tailor(POSTING, resume_tex=ORIGINAL)

    assert result.attempts == 2
    assert "Fake Award" not in result.tex
    assert "modified ACHIEVEMENTS" in sent[1], "the model must be told what it broke"


def test_retries_until_the_page_count_fits(monkeypatch):
    long = ORIGINAL.replace("Cut p95", "Cut, at some considerable length, p95")
    short = ORIGINAL.replace("scalable Python service", "scalable Python backend")
    sent = stub_model(monkeypatch, reply(long), reply(short))
    pages = {"n": 0}

    def page_check(tex):
        pages["n"] += 1
        return 2 if pages["n"] == 1 else 1

    result = tailor.tailor(POSTING, resume_tex=ORIGINAL, page_check=page_check, target_pages=1)

    assert result.attempts == 2
    assert "compiled to 2 pages" in sent[1]
    assert "Do not delete a bullet" in sent[1]


def test_gives_up_after_max_attempts(monkeypatch):
    bad = ORIGINAL.replace("Early Career Innovator Award, 2025.", "Fake Award, 2025.")
    stub_model(monkeypatch, reply(bad))
    with pytest.raises(tailor.TailorError, match="gave up after 4 attempts"):
        tailor.tailor(POSTING, resume_tex=ORIGINAL)


def test_uncompilable_candidate_does_not_block_the_result(monkeypatch):
    """An unknown page count is accepted; the real compile reports the error."""
    after = ORIGINAL.replace("scalable Python service", "scalable Python backend")
    stub_model(monkeypatch, reply(after))
    result = tailor.tailor(POSTING, resume_tex=ORIGINAL, page_check=lambda tex: None)
    assert result.attempts == 1


def test_prompt_carries_the_bullet_length_budget(monkeypatch):
    after = ORIGINAL.replace("scalable Python service", "scalable Python backend")
    sent = stub_model(monkeypatch, reply(after))
    tailor.tailor(POSTING, resume_tex=ORIGINAL)
    assert "bullet 1:" in sent[0] and "printed line" in sent[0] and "characters" in sent[0]


def test_no_tex_block_is_retried_then_fails(monkeypatch):
    stub_model(monkeypatch, "```verdict\nMATCH: fine\n```\n\nI could not do it.")
    with pytest.raises(tailor.TailorError, match="no ```tex block"):
        tailor.tailor(POSTING, resume_tex=ORIGINAL)


def test_overrun_report_names_the_worst_offenders():
    """A retry must be told which bullets to cut, not just that it is too long."""
    grown = ORIGINAL.replace(
        "Cut p95 latency 52 percent with a Redis caching layer",
        "Cut p95 latency 52 percent with a Redis caching layer and async database access "
        "and connection pooling")
    report = tailor.overrun_report(ORIGINAL, grown)
    assert "bullet 2" in report
    assert "Cut " in report
    assert "bullet 1" not in report, "an unchanged bullet must not be named"


def test_reordered_bullets_are_matched_by_content_not_position():
    """Reordering entries is allowed; it must not read as one bullet growing
    by the other's length. A real run failed four times on exactly that."""
    items = tailor.bullets(ORIGINAL)
    assert len(items) >= 2
    swapped = ORIGINAL.replace(items[0], "\x00").replace(items[1], items[0]).replace("\x00", items[1])
    assert swapped != ORIGINAL
    pairs = tailor.pair_bullets(ORIGINAL, swapped)
    assert all(was == now for _, was, now in pairs)
    assert "No bullet grew" in tailor.overrun_report(ORIGINAL, swapped)
    assert tailor._check_lengths(ORIGINAL, swapped) == []


def test_overrun_report_points_elsewhere_when_no_bullet_grew():
    shrunk = ORIGINAL.replace("Built a scalable Python service handling 50K requests per day",
                              "Built a Python service")
    assert "summary" in tailor.overrun_report(ORIGINAL, shrunk)


def test_page_retry_feedback_carries_the_numbers(monkeypatch):
    grown = ORIGINAL.replace(
        "Cut p95 latency 52 percent with a Redis caching layer",
        "Cut p95 latency 52 percent with a Redis caching layer, an async database "
        "driver and connection pooling in front of it")
    good = ORIGINAL.replace("scalable Python service", "scalable Python backend")
    sent = stub_model(monkeypatch, reply(grown), reply(good))
    pages = {"n": 0}

    def page_check(tex):
        pages["n"] += 1
        return 2 if pages["n"] == 1 else 1

    tailor.tailor(POSTING, resume_tex=ORIGINAL, page_check=page_check, target_pages=1)
    # The line check is cheaper than a compile, so it is what rejects this
    # one, and it names the bullet and the budget it blew.
    assert "bullet 2" in sent[1] and "line budget" in sent[1]


def test_the_summary_and_the_skills_lines_hold_their_lines_too(monkeypatch):
    """Bullets are not the only thing that fills the page. A summary that
    runs onto another line is rejected the same way, before the compile."""
    grown = ORIGINAL.replace(
        "Software engineer with backend and ML experience.",
        "Software engineer with backend, ML, distributed systems and caching "
        "experience across production services and internal platforms.")
    with pytest.raises(tailor.TailorError, match="the summary"):
        tailor._validate(ORIGINAL, grown)


def test_an_unchanged_resume_is_retried(monkeypatch):
    """Under length pressure the safest reply is no edit, which is a non-answer."""
    changed = ORIGINAL.replace("scalable Python service", "scalable Python backend")
    sent = stub_model(monkeypatch, reply(ORIGINAL), reply(changed))

    result = tailor.tailor(POSTING, resume_tex=ORIGINAL)

    assert result.attempts == 2
    assert "unchanged" in sent[1]
    assert "Python backend" in result.tex


def test_an_unchanged_resume_is_accepted_on_the_final_attempt(monkeypatch):
    """If it still says nothing needs changing after being pushed, take it."""
    stub_model(monkeypatch, reply(ORIGINAL))
    result = tailor.tailor(POSTING, resume_tex=ORIGINAL, max_attempts=2)
    assert result.attempts == 2
    assert result.tex.strip() == ORIGINAL.strip()


def test_the_rationale_is_asked_for_in_caveman_style():
    from tailor import tailor as t

    system = t.system_prompt()
    assert "twelve words" in system and "caveman" in system
    assert "rationale block only" in system, "the terse style must never reach the resume"
    assert "Gaps:" in t.REPLY_FORMAT


PROJECT_RESUME = r"""\documentclass{article}
\newcommand{\resumeItem}[1]{\item{#1}}
\begin{document}
\section{\texorpdfstring{\color{airforceblue}SUMMARY}{}}
{\normalsize Software engineer with backend and ML experience.}
\section{\texorpdfstring{\color{airforceblue}EXPERIENCE}{}}
\resumeItem{\normalsize{Built a scalable Python service handling 50K requests per day}}
\section{\texorpdfstring{\color{airforceblue}PROJECTS}{}}
\resumeItem{\normalsize{Hydra: a load-test harness that drives Docker topologies and fits a model to them}}
\resumeItem{\normalsize{AudiTex: a text-to-speech extension that runs a model in the browser}}
\end{document}
"""


def test_projects_may_move_lines_between_entries():
    """A story swapped in may deserve a line the entry it replaced did not.
    What holds is the height of the section, not of one entry."""
    grown = PROJECT_RESUME.replace(
        "Hydra: a load-test harness that drives Docker topologies and fits a model to them",
        "Hydra: a load-test harness driving Docker topologies with k6 and Prometheus, "
        "fitting a scalability model")
    paid_for = grown.replace(
        "AudiTex: a text-to-speech extension that runs a model in the browser",
        "AudiTex: in-browser text-to-speech")
    assert tailor._section_lines(PROJECT_RESUME, "PROJECTS", 40) == 4
    assert tailor._section_lines(paid_for, "PROJECTS", 40) == 4
    assert tailor._validate(PROJECT_RESUME, paid_for) == []


def test_the_projects_section_may_not_grow_as_a_whole():
    grown = PROJECT_RESUME.replace(
        "Hydra: a load-test harness that drives Docker topologies and fits a model to them",
        "Hydra: a load-test harness driving Docker topologies with k6 and Prometheus, "
        "fitting a Universal Scalability Law model to the measurements and projecting "
        "saturation points with confidence intervals")
    with pytest.raises(tailor.TailorError, match="PROJECTS as a whole"):
        tailor._validate(PROJECT_RESUME, grown)


def test_an_experience_bullet_is_still_measured_on_its_own():
    """Only PROJECTS is a block; a role's bullets each hold their lines,
    since one that grows pushes everything under it down the page."""
    grown = PROJECT_RESUME.replace(
        "Built a scalable Python service handling 50K requests per day",
        "Built a scalable Python service handling 50K requests per day, peaking at "
        "10K an hour, with a Redis cache in front of it")
    with pytest.raises(tailor.TailorError, match="bullet 1"):
        tailor._validate(PROJECT_RESUME, grown)


def test_a_touched_preamble_is_restored_rather_than_rejected(monkeypatch):
    """A reflowed comment above \\begin{document} used to throw the whole
    reply away, a quarter of the attempt budget for something no reader
    sees. The master's preamble is put back and the sections are judged."""
    meddled = ORIGINAL.replace(r"\newcommand{\resumeItem}[1]{\item{#1}}",
                               r"\newcommand{\resumeItem}[1]{\item {#1}} % tidy")
    meddled = meddled.replace("scalable Python service", "scalable Python backend")
    sent = stub_model(monkeypatch, reply(meddled))

    result = tailor.tailor(POSTING, resume_tex=ORIGINAL)

    assert len(sent) == 1, "the attempt must not have been spent on the preamble"
    assert result.tex.startswith(ORIGINAL[:ORIGINAL.index(r"\section")])
    assert "scalable Python backend" in result.tex
    assert any("preamble was restored" in w for w in result.warnings)


def test_an_untouched_preamble_is_left_exactly_as_it_was():
    after = ORIGINAL.replace("scalable Python service", "scalable Python backend")
    spliced, restored = tailor.restore_preamble(ORIGINAL, after)
    assert spliced == after and restored is False
