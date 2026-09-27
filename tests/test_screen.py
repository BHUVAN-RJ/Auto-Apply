"""On-page screening: the rules, the facts loader, and the cached endpoint.

No model is asked here, and none is stubbed: a screen is `tailor.screening`
read over the posting's own words. `tests/test_screening.py` covers the
rules themselves; this file covers the verdict, the policies that correct a
flag after it is raised, and the endpoint's cache.
"""

import pytest
from fastapi.testclient import TestClient

from server import postings, screen as server_screen
from server import settings
from server.app import app
from tailor import profile, screen, screening


FACTS = """# Applicant

## Facts

- Years of professional experience: 2
- Work authorisation: needs sponsorship in the future

## Form details

- Phone: 000
"""


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(server_screen, "CACHE_PATH", tmp_path / "screens.json")
    monkeypatch.setenv(postings.DIR_ENV, str(tmp_path / "postings"))
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    applicant = tmp_path / "applicant.md"
    applicant.write_text(FACTS)
    monkeypatch.setattr(profile, "APPLICANT", applicant)
    monkeypatch.setattr(profile, "BASE_RESUME", tmp_path / "resume.tex")
    monkeypatch.setattr(profile, "DERIVED_FACTS", tmp_path / "derived_facts.md")


# Postings that trip exactly one stock rule each, used wherever a test
# needs a verdict rather than a particular rule.
NO_SPONSORSHIP = "About the role. We are unable to sponsor or take over sponsorship of an employment visa."
CLEAN = "About the role. You will build services in Python and Go with a small team in Chicago."


def flag(category, severity, quote, reason="r"):
    return screen.Flag(category, severity, quote, reason)


# --- facts -----------------------------------------------------------------

def test_load_facts_returns_only_the_facts_section():
    facts, source = screen.load_facts()
    assert "Years of professional experience" in facts
    assert "Phone" not in facts
    assert source == "applicant.md"


def test_missing_applicant_falls_back_to_the_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(profile, "APPLICANT", tmp_path / "nope.md")
    (tmp_path / "resume.tex").write_text("\\section{Education} MS CS 2025")
    monkeypatch.setattr(profile.llm, "complete",
                        lambda *a, **k: "- Years of professional experience: 2\n- Degrees held: MS CS (2025)\n")
    facts, source = screen.load_facts()
    assert source == "resume" and "MS CS" in facts


def test_profile_switch_off_ignores_applicant(tmp_path, monkeypatch):
    settings.save(use_profile=False)
    (tmp_path / "resume.tex").write_text("resume")
    monkeypatch.setattr(profile.llm, "complete", lambda *a, **k: "- Years of professional experience: 3\n")
    facts, source = screen.load_facts()
    assert source == "resume" and "3" in facts


def test_derived_facts_are_cached_until_the_resume_changes(tmp_path, monkeypatch):
    resume = tmp_path / "resume.tex"
    resume.write_text("v1")
    calls = []

    def fake(*a, **k):
        calls.append(1)
        return "- Years of professional experience: 1\n"

    monkeypatch.setattr(profile.llm, "complete", fake)
    profile.derived_facts()
    profile.derived_facts()
    assert len(calls) == 1
    resume.write_text("v2")
    import os
    os.utime(resume, (resume.stat().st_atime, resume.stat().st_mtime + 10))
    profile.derived_facts()
    assert len(calls) == 2


def test_no_resume_and_no_applicant_is_loud(tmp_path, monkeypatch):
    monkeypatch.setattr(profile, "APPLICANT", tmp_path / "nope.md")
    with pytest.raises(FileNotFoundError):
        screen.load_facts()


# --- verdict and policies --------------------------------------------------

def test_a_posting_that_refuses_sponsorship_is_a_reject():
    result = screen.screen(NO_SPONSORSHIP, facts=FACTS)
    assert result.verdict == "reject"
    assert [f.category for f in result.flags] == ["visa"]
    assert "unable to sponsor" in result.flags[0].quote
    assert result.model == "rules"


def test_a_clean_posting_is_ok():
    assert screen.screen(CLEAN, facts=FACTS).verdict == "ok"


def test_the_verdict_follows_the_flags():
    assert screen.verdict_for([flag("experience", "hard", "5+ years")]) == "reject"
    assert screen.verdict_for([flag("degree", "soft", "PhD preferred")]) == "caution"
    assert screen.verdict_for([]) == "ok"


def test_us_location_is_green_however_a_rule_reads_it():
    """Under the policy a US location is never a flag: relocation is open,
    so onsite in San Jose is the same as onsite in Los Angeles. The policy
    runs after the rules, so a rule someone writes cannot undo it."""
    for quote in ("San Jose, California, United States of America",
                  "This position is based in New York, New York."):
        assert screen.enforce_location_policy([flag("location", "hard", quote)]) == []
    assert screen.enforce_location_policy([flag("location", "soft", "anywhere")]) == []


def test_a_us_metro_named_without_its_state_is_still_green():
    """A real reject: "Based in the NYC tri-state area" against an applicant
    in Los Angeles. The quote names no state, no state code and no country,
    so the hard flag survived and the job was rejected for a US-to-US
    distance, which the policy says is never a flag."""
    assert screen.enforce_location_policy(
        [flag("location", "hard", "Based in the NYC tri-state area")]) == []
    assert screen.quote_names_us_location("Greater Boston area")
    assert screen.quote_names_us_location("hybrid in the Bay Area")
    assert not screen.quote_names_us_location("Based in the Greater Toronto Area")


def test_non_us_location_flag_is_preserved():
    kept = screen.enforce_location_policy(
        [flag("location", "hard", "Toronto, Ontario, Canada")])
    assert [(f.category, f.severity) for f in kept] == [("location", "hard")]


def test_graduating_before_latest_date_is_green():
    posting = "Requirements. Bachelor's or Master's degree earned or expected by Summer 2027."
    result = screen.screen(posting, facts="- Degrees held: MS Computer Science (expected December 2026)")
    assert result.flags == []
    assert result.verdict == "ok"


def test_graduating_after_latest_date_keeps_timeline_flag():
    posting = "Requirements. Bachelor's or Master's degree earned or expected by Summer 2027."
    result = screen.screen(posting, facts="- Graduation: expected December 2027")
    assert [(f.category, f.severity) for f in result.flags] == [("timeline", "hard")]
    assert result.verdict == "reject"


def test_empty_text_is_not_a_job():
    assert screen.screen("   ").verdict == "not_a_job"


def test_screening_asks_no_model(monkeypatch):
    """The whole point of the rules. Anything that reaches a model here is a
    regression, whatever it is asking."""
    from tailor import llm
    monkeypatch.setattr(llm, "complete", lambda *a, **k: pytest.fail("the screen called a model"))
    assert screen.screen(NO_SPONSORSHIP, facts=FACTS).verdict == "reject"


def test_a_form_question_is_never_a_flag():
    """Every second application form asks it, and it says nothing about the
    employer's policy. The model made this mistake four times."""
    posting = ("Application questions. Will you now or in the future require sponsorship "
               "for employment visa status (e.g. H-1B)? Please answer honestly.")
    assert screen.screen(posting, facts=FACTS).verdict == "ok"


def test_a_sentence_that_offers_sponsorship_never_reads_as_refusing_it():
    posting = "Visas. We will sponsor candidates who need it; we cannot guarantee sponsorship for every role."
    assert screen.screen(posting, facts=FACTS).verdict == "ok"


# --- endpoint and cache ----------------------------------------------------

def test_cache_key_strips_tracking():
    k = server_screen.cache_key
    assert k("https://x.com/j/1?utm_source=a&ref=b#top") == "https://x.com/j/1"
    assert k("https://x.com/j/1/") == k("https://x.com/j/1")
    assert k("https://x.com/j/1?gh_jid=5") == "https://x.com/j/1?gh_jid=5"


def test_endpoint_caches_by_url():
    client = TestClient(app)
    body = {"url": "https://x.com/j/1?ref=jobright", "text": NO_SPONSORSHIP, "title": "T"}
    first = client.post("/screen", json=body).json()
    second = client.post("/screen", json=body | {"url": "https://x.com/j/1"}).json()
    assert first["verdict"] == "reject" and first["cached"] is False
    assert second["cached"] is True and second["flags"] == first["flags"]


def test_a_cached_us_location_caution_is_normalized_on_read():
    url = "https://jobs.example.com/san-jose-role"
    server_screen.remember(url, screen.Screen(verdict="caution", flags=[
        screen.Flag(
            "location", "soft", "San Jose, California, United States of America",
            "Applicant is in Los Angeles and relocation is acceptable.",
        )
    ]))
    data = TestClient(app).post("/screen", json={
        "url": url,
        "title": "Engineer",
        "text": "San Jose, California, United States of America",
    }).json()

    assert data["cached"] is True
    assert data["flags"] == []
    assert data["verdict"] == "ok"


def test_employer_page_reuses_the_jobright_verdict():
    source_url = "https://jobright.ai/jobs/info/abc123"
    employer_url = "https://jobs.example.com/apply?jr_id=abc123"
    postings.save_source("abc123", postings.Saved(
        url=source_url, title="Engineer @ Acme | Jobright.ai",
        text="Responsibilities\nBuild distributed systems. " * 30,
    ))
    server_screen.remember(source_url, screen.Screen(verdict="ok", summary="Good fit"))

    data = TestClient(app).post("/screen", json={
        "url": employer_url, "title": "Careers", "text": "Corporate footer and privacy links",
    }).json()

    assert data["verdict"] == "ok" and data["cached"] is True
    assert server_screen.cached(employer_url).summary == "Good fit"


def test_a_bare_employer_page_is_read_against_the_saved_posting():
    """The form the Apply lands on carries a footer, not a description. The
    rules read Jobright's copy of the posting instead, so a refusal written
    only there is still caught."""
    employer_url = "https://jobs.example.com/apply?jr_id=abc123"
    postings.save_source("abc123", postings.Saved(
        url="https://jobright.ai/jobs/info/abc123", title="Engineer @ Acme | Jobright.ai",
        text="Responsibilities. Build distributed systems in Python and Go. " * 20 + NO_SPONSORSHIP,
    ))

    data = TestClient(app).post("/screen", json={
        "url": employer_url, "title": "Careers", "text": "Corporate footer and privacy links",
    }).json()

    assert data["verdict"] == "reject"
    assert [f["category"] for f in data["flags"]] == ["visa"]


def test_not_a_job_is_not_cached():
    """A page that had not rendered yet must be screened again when it has."""
    client = TestClient(app)
    url = "https://x.com/j/2"
    assert client.post("/screen", json={"url": url, "text": "  "}).json()["verdict"] == "not_a_job"
    assert client.post("/screen", json={"url": url, "text": CLEAN}).json()["verdict"] == "ok"


def test_force_bypasses_cache():
    client = TestClient(app)
    url = "https://x.com/j/3"
    assert client.post("/screen", json={"url": url, "text": CLEAN}).json()["verdict"] == "ok"
    again = client.post("/screen", json={"url": url, "text": NO_SPONSORSHIP, "force": True}).json()
    assert again["verdict"] == "reject"


def test_nothing_to_screen_against_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(profile, "APPLICANT", tmp_path / "absent.md")
    client = TestClient(app)
    res = client.post("/screen", json={"url": "https://x.com/j/4", "text": "posting"})
    assert res.status_code == 502
    assert "resume.tex" in res.json()["detail"]


def test_endpoint_reports_the_facts_source():
    client = TestClient(app)
    res = client.post("/screen", json={"url": "https://x.com/j/5", "text": CLEAN}).json()
    assert res["facts_source"] == "applicant.md"


# --- facts the resume cannot supply ----------------------------------------

def test_resume_derived_facts_soften_visa_flags():
    result = screen.Screen(verdict="reject", facts_source="resume", flags=[
        screen.Flag("visa", "hard", "unable to sponsor", "r"),
        screen.Flag("clearance", "hard", "US citizen", "r")])
    result = screen.soften_unknowns(result)
    assert {f.severity for f in result.flags} == {"soft"}
    assert result.verdict == "caution"


def test_resume_derived_facts_keep_experience_hard():
    result = screen.Screen(verdict="reject", facts_source="resume", flags=[
        screen.Flag("experience", "hard", "7+ years", "r")])
    assert screen.soften_unknowns(result).verdict == "reject"


def test_applicant_facts_are_not_softened():
    result = screen.Screen(verdict="reject", facts_source="applicant.md", flags=[
        screen.Flag("visa", "hard", "unable to sponsor", "r")])
    assert screen.soften_unknowns(result).verdict == "reject"


def test_a_confirmation_page_is_answered_before_any_rule_runs(monkeypatch):
    """After the person submits, the tab shows a thank-you page and the
    script in it screens it like any page. That is a string check on the
    server, not a model call, and it marks the job when it is ours."""
    from server import seen as server_seen
    monkeypatch.setattr(server_seen, "find", lambda *a, **k: None)
    data = TestClient(app).post("/screen", json={
        "url": "https://jobs.lever.co/acme/1/thanks", "title": "Acme",
        "text": "Acme\nApplication submitted\nThank you for applying. We will be in touch."}).json()
    assert data["verdict"] == "submitted" and "Confirmation page" in data["summary"]

    # A posting that thanks the reader deep in a long description is a posting.
    quote = server_screen.confirmation_quote("Software Engineer\n" + "Requirements. " * 400
                                             + "Thank you for your interest in Acme.")
    assert quote is None
    assert server_screen.confirmation_quote("Thanks for applying!") == "Thanks for applying!"


def test_a_confirmation_on_a_filled_job_marks_it_submitted(monkeypatch, tmp_path):
    from server import queue, review, seen as server_seen
    from server.models import Job, Status
    app_dir = tmp_path / "acme"
    app_dir.mkdir()
    job = Job(url="https://jobs.lever.co/acme/1", status=Status.FILLED, app_dir=str(app_dir))
    match = server_seen.Match(level="high", id=job.id, status="filled", at="", title="", company="", reason="url")
    monkeypatch.setattr(server_seen, "find", lambda *a, **k: match)
    monkeypatch.setattr(queue, "get", lambda job_id: job if job_id == job.id else None)
    marked = []
    monkeypatch.setattr(review, "mark_seen", lambda j, d, url, quote, by: marked.append((j.id, url, quote)) or {"marked": True})
    data = TestClient(app).post("/screen", json={
        "url": "https://jobs.lever.co/acme/1/thanks", "title": "", "text": "Thank you for applying to Acme."}).json()
    assert data["verdict"] == "submitted" and "marked in autopilot" in data["summary"]
    assert marked and marked[0][0] == job.id and marked[0][1].endswith("/thanks")


def test_graduating_before_a_cohort_window_is_green():
    """SeatGeek's "spring/summer of 2027 college graduates" rejected a
    December 2026 graduate. Graduating early only widens what fits."""
    posting = "Our new grad programme is open to spring/summer of 2027 college graduates."
    result = screen.screen(posting, facts="- Graduation: expected December 2026")
    assert result.flags == []
    assert result.verdict == "ok"


def test_graduating_after_a_cohort_window_keeps_the_flag():
    posting = "Open to spring/summer of 2027 college graduates."
    result = screen.screen(posting, facts="- Graduation: expected December 2028")
    assert [f.category for f in result.flags] == ["timeline"]


def test_a_posting_that_needs_the_applicant_still_enrolled_keeps_the_flag():
    """The one timeline case an early graduate genuinely fails."""
    posting = "Requires an expected graduation of December 2027 or later."
    result = screen.screen(posting, facts="- Graduation: expected December 2026")
    assert [(f.category, f.severity) for f in result.flags] == [("timeline", "hard")]
    assert result.verdict == "reject"
