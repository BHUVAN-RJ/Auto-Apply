"""On-page screening: the parser, the facts loader, and the cached endpoint."""

import json

import pytest
from fastapi.testclient import TestClient

from server import postings, screen as server_screen
from server import settings
from server.app import app
from tailor import profile, screen


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


def reply(verdict="reject", flags=None, summary="visa"):
    return "```json\n" + json.dumps({"verdict": verdict, "flags": flags or [], "summary": summary}) + "\n```"


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


def test_facts_reach_the_prompt():
    message = screen.build_user_message("posting text", "FACT LINE", title="T", url="U")
    assert "FACT LINE" in message
    assert message.index("FACT LINE") < message.index("posting text")
    assert "Title: T" in message


# --- parser ----------------------------------------------------------------

def test_parse_keeps_valid_flags():
    result = screen.parse_reply(reply(flags=[{
        "category": "visa", "severity": "hard",
        "quote": "unable to sponsor", "reason": "needs sponsorship"}]), model="m")
    assert result.verdict == "reject"
    assert result.flags[0].category == "visa"
    assert result.model == "m"


def test_unknown_category_becomes_other():
    result = screen.parse_reply(reply(flags=[{
        "category": "astrology", "severity": "soft", "quote": "q", "reason": "r"}]))
    assert result.flags[0].category == "other"


def test_flag_without_quote_is_dropped():
    result = screen.parse_reply(reply(flags=[{
        "category": "visa", "severity": "hard", "quote": "", "reason": "r"}]))
    assert result.flags == []
    assert result.verdict == "ok"


def test_verdict_follows_flags_not_the_model():
    hard = [{"category": "experience", "severity": "hard", "quote": "5+ years", "reason": "r"}]
    assert screen.parse_reply(reply(verdict="ok", flags=hard)).verdict == "reject"
    soft = [{"category": "degree", "severity": "soft", "quote": "PhD preferred", "reason": "r"}]
    assert screen.parse_reply(reply(verdict="reject", flags=soft)).verdict == "caution"
    assert screen.parse_reply(reply(verdict="reject", flags=[])).verdict == "ok"


def test_us_location_is_green_even_when_model_calls_it_soft():
    posting = "San Jose, California, United States of America"
    result = screen.parse_reply(reply(verdict="caution", flags=[{
        "category": "location",
        "severity": "soft",
        "quote": posting,
        "reason": "Onsite in San Jose; applicant is in Los Angeles and relocation is acceptable.",
    }]), text=posting)

    assert result.flags == []
    assert result.verdict == "ok"


def test_us_location_is_green_even_when_model_calls_it_hard():
    posting = "This position is based in New York, New York."
    result = screen.parse_reply(reply(flags=[{
        "category": "location", "severity": "hard",
        "quote": "New York, New York", "reason": "Applicant is in Los Angeles.",
    }]), text=posting)

    assert result.flags == []
    assert result.verdict == "ok"


def test_a_us_metro_named_without_its_state_is_still_green():
    """A real reject: "Based in the NYC tri-state area" against an applicant
    in Los Angeles. The quote names no state, no state code and no country,
    so the hard flag survived and the job was rejected for a US-to-US
    distance, which the policy says is never a flag."""
    posting = "Based in the NYC tri-state area and in the office three days a week."
    result = screen.parse_reply(reply(flags=[{
        "category": "location", "severity": "hard",
        "quote": "Based in the NYC tri-state area",
        "reason": "Applicant is in Los Angeles and the role requires NYC tri-state presence.",
    }]), text=posting)

    assert result.flags == []
    assert result.verdict == "ok"
    assert screen.quote_names_us_location("Greater Boston area")
    assert screen.quote_names_us_location("hybrid in the Bay Area")
    assert not screen.quote_names_us_location("Based in the Greater Toronto Area")


def test_non_us_location_flag_is_preserved():
    posting = "This position is based in Toronto, Ontario, Canada."
    result = screen.parse_reply(reply(flags=[{
        "category": "location", "severity": "hard",
        "quote": "Toronto, Ontario, Canada", "reason": "Role is restricted to Canada.",
    }]), text=posting)

    assert [(flag.category, flag.severity) for flag in result.flags] == [("location", "hard")]
    assert result.verdict == "reject"


def test_graduating_before_latest_date_is_green(monkeypatch):
    posting = "Bachelor's or Master's degree earned or expected by Summer 2027"
    monkeypatch.setattr(screen.llm, "complete", lambda *a, **k: reply(
        verdict="caution",
        flags=[{
            "category": "timeline",
            "severity": "soft",
            "quote": "earned or expected by Summer 2027",
            "reason": "Applicant expects to graduate in December 2026.",
        }],
    ))

    result = screen.screen(
        posting,
        facts="- Degrees held: MS Computer Science (expected December 2026)",
    )

    assert result.flags == []
    assert result.verdict == "ok"


def test_graduating_after_latest_date_keeps_timeline_flag():
    posting = "Bachelor's or Master's degree earned or expected by Summer 2027"
    result = screen.parse_reply(reply(verdict="caution", flags=[{
        "category": "timeline",
        "severity": "soft",
        "quote": "earned or expected by Summer 2027",
        "reason": "Applicant expects to graduate in December 2027.",
    }]), text=posting, facts="- Graduation: expected December 2027")

    assert [(flag.category, flag.severity) for flag in result.flags] == [("timeline", "soft")]
    assert result.verdict == "caution"


def test_not_a_job_survives():
    assert screen.parse_reply(reply(verdict="not_a_job")).verdict == "not_a_job"


def test_garbage_reply_is_an_error():
    with pytest.raises(screen.ScreenError):
        screen.parse_reply("I cannot help with that")


def test_empty_text_never_calls_the_model(monkeypatch):
    monkeypatch.setattr(screen.llm, "complete", lambda *a, **k: pytest.fail("called"))
    assert screen.screen("   ").verdict == "not_a_job"


def test_screen_uses_the_screen_model(monkeypatch):
    seen = {}

    def fake(system, user, model=None, **kw):
        seen["model"] = model
        seen["system"] = system
        return reply(verdict="ok")

    monkeypatch.setattr(screen.llm, "complete", fake)
    monkeypatch.setenv("OPENROUTER_SCREEN_MODEL", "fast/model")
    screen.screen("a posting")
    assert seen["model"] == "fast/model"
    assert "export_control" in seen["system"]


# --- endpoint and cache ----------------------------------------------------

def test_cache_key_strips_tracking():
    k = server_screen.cache_key
    assert k("https://x.com/j/1?utm_source=a&ref=b#top") == "https://x.com/j/1"
    assert k("https://x.com/j/1/") == k("https://x.com/j/1")
    assert k("https://x.com/j/1?gh_jid=5") == "https://x.com/j/1?gh_jid=5"


def test_endpoint_caches_by_url(monkeypatch):
    calls = []

    def fake(*a, **k):
        calls.append(1)
        return reply(flags=[{"category": "visa", "severity": "hard",
                             "quote": "no sponsorship", "reason": "r"}])

    monkeypatch.setattr(screen.llm, "complete", fake)
    client = TestClient(app)
    body = {"url": "https://x.com/j/1?ref=jobright", "text": "posting, no sponsorship", "title": "T"}
    first = client.post("/screen", json=body).json()
    second = client.post("/screen", json=body | {"url": "https://x.com/j/1"}).json()
    assert first["verdict"] == "reject" and first["cached"] is False
    assert second["cached"] is True and second["flags"] == first["flags"]
    assert len(calls) == 1


def test_cached_us_location_caution_is_normalized_without_model_call(monkeypatch):
    url = "https://jobs.example.com/san-jose-role"
    server_screen.remember(url, screen.Screen(verdict="caution", flags=[
        screen.Flag(
            "location", "soft", "San Jose, California, United States of America",
            "Applicant is in Los Angeles and relocation is acceptable.",
        )
    ]))
    monkeypatch.setattr(screen.llm, "complete", lambda *a, **k: pytest.fail("called"))

    data = TestClient(app).post("/screen", json={
        "url": url,
        "title": "Engineer",
        "text": "San Jose, California, United States of America",
    }).json()

    assert data["cached"] is True
    assert data["flags"] == []
    assert data["verdict"] == "ok"


def test_employer_page_reuses_jobright_verdict_without_a_model_call(monkeypatch):
    source_url = "https://jobright.ai/jobs/info/abc123"
    employer_url = "https://jobs.example.com/apply?jr_id=abc123"
    postings.save_source("abc123", postings.Saved(
        url=source_url, title="Engineer @ Acme | Jobright.ai",
        text="Responsibilities\nBuild distributed systems. " * 30,
    ))
    server_screen.remember(source_url, screen.Screen(verdict="ok", summary="Good fit"))
    monkeypatch.setattr(screen.llm, "complete", lambda *a, **k: pytest.fail("called"))

    data = TestClient(app).post("/screen", json={
        "url": employer_url, "title": "Careers", "text": "Corporate footer and privacy links",
    }).json()

    assert data["verdict"] == "ok" and data["cached"] is True
    assert server_screen.cached(employer_url).summary == "Good fit"


def test_bare_employer_page_is_screened_with_saved_posting_in_one_call(monkeypatch):
    employer_url = "https://jobs.example.com/apply?jr_id=abc123"
    description = "Responsibilities\nBuild distributed systems in Python and Go. " * 30
    postings.save_source("abc123", postings.Saved(
        url="https://jobright.ai/jobs/info/abc123", title="Engineer @ Acme | Jobright.ai",
        text=description,
    ))
    messages = []

    def fake(system, user, **kwargs):
        messages.append(user)
        return reply(verdict="ok")

    monkeypatch.setattr(screen.llm, "complete", fake)
    data = TestClient(app).post("/screen", json={
        "url": employer_url, "title": "Careers", "text": "Corporate footer and privacy links",
    }).json()

    assert data["verdict"] == "ok"
    assert len(messages) == 1
    assert "## Jobright posting copy" in messages[0]
    assert "Build distributed systems in Python and Go." in messages[0]
    assert "Corporate footer" in messages[0]


def test_not_a_job_is_not_cached(monkeypatch):
    replies = iter([reply(verdict="not_a_job"), reply(verdict="ok")])
    monkeypatch.setattr(screen.llm, "complete", lambda *a, **k: next(replies))
    client = TestClient(app)
    body = {"url": "https://x.com/j/2", "text": "loading"}
    assert client.post("/screen", json=body).json()["verdict"] == "not_a_job"
    assert client.post("/screen", json=body).json()["verdict"] == "ok"


def test_force_bypasses_cache(monkeypatch):
    replies = iter([reply(verdict="ok"), reply(verdict="reject", flags=[
        {"category": "visa", "severity": "hard", "quote": "no visas", "reason": "r"}])])
    monkeypatch.setattr(screen.llm, "complete", lambda *a, **k: next(replies))
    client = TestClient(app)
    body = {"url": "https://x.com/j/3", "text": "posting, no visas"}
    client.post("/screen", json=body)
    assert client.post("/screen", json=body | {"force": True}).json()["verdict"] == "reject"


def test_nothing_to_screen_against_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(profile, "APPLICANT", tmp_path / "absent.md")
    client = TestClient(app)
    res = client.post("/screen", json={"url": "https://x.com/j/4", "text": "posting"})
    assert res.status_code == 502
    assert "resume.tex" in res.json()["detail"]


def test_endpoint_reports_the_facts_source(monkeypatch):
    monkeypatch.setattr(screen.llm, "complete", lambda *a, **k: reply(verdict="ok"))
    client = TestClient(app)
    res = client.post("/screen", json={"url": "https://x.com/j/5", "text": "posting"}).json()
    assert res["facts_source"] == "applicant.md"


# --- quote check -----------------------------------------------------------

POSTING = "We are unable to sponsor visas.\nWill you now or in the future require sponsorship?\nMust hold a PhD."


def test_invented_quote_is_dropped():
    result = screen.parse_reply(reply(flags=[{
        "category": "clearance", "severity": "hard",
        "quote": "no statement about clearance", "reason": "r"}]), text=POSTING)
    assert result.flags == [] and result.verdict == "ok"


def test_form_question_is_never_a_flag():
    result = screen.parse_reply(reply(flags=[{
        "category": "visa", "severity": "hard",
        "quote": "Will you now or in the future require sponsorship?", "reason": "r"}]), text=POSTING)
    assert result.flags == []


def test_real_quote_survives_punctuation_and_case():
    result = screen.parse_reply(reply(flags=[{
        "category": "visa", "severity": "hard",
        "quote": "unable to sponsor visas", "reason": "r"}]), text=POSTING)
    assert [f.category for f in result.flags] == ["visa"]


def test_quote_check_needs_the_text():
    # Without the posting there is nothing to check against; keep the flag.
    result = screen.parse_reply(reply(flags=[{
        "category": "visa", "severity": "hard", "quote": "anything", "reason": "r"}]))
    assert len(result.flags) == 1


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


def test_a_confirmation_page_is_answered_locally_and_never_reaches_the_model(monkeypatch):
    """After the person submits, the tab shows a thank-you page and the
    script in it screens it like any page. That is a string check on the
    server, not a model call, and it marks the job when it is ours."""
    calls = []
    monkeypatch.setattr(screen.llm, "complete", lambda *a, **k: calls.append(1) or "")
    from server import seen as server_seen
    monkeypatch.setattr(server_seen, "find", lambda *a, **k: None)
    data = TestClient(app).post("/screen", json={
        "url": "https://jobs.lever.co/acme/1/thanks", "title": "Acme",
        "text": "Acme\nApplication submitted\nThank you for applying. We will be in touch."}).json()
    assert data["verdict"] == "submitted" and "Confirmation page" in data["summary"]
    assert calls == []

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
    monkeypatch.setattr(screen.llm, "complete", lambda *a, **k: pytest.fail("model called"))
    data = TestClient(app).post("/screen", json={
        "url": "https://jobs.lever.co/acme/1/thanks", "title": "", "text": "Thank you for applying to Acme."}).json()
    assert data["verdict"] == "submitted" and "marked in autopilot" in data["summary"]
    assert marked and marked[0][0] == job.id and marked[0][1].endswith("/thanks")


def test_graduating_before_a_cohort_window_is_green():
    posting = (
        "Our new grad programme is open to spring/summer of 2027 college "
        "graduates."
    )
    result = screen.parse_reply(reply(verdict="reject", flags=[{
        "category": "timeline",
        "severity": "hard",
        "quote": "spring/summer of 2027 college graduates",
        "reason": "Applicant graduates December 2026, not spring/summer 2027.",
    }]), text=posting, facts="- Graduation: expected December 2026")

    assert result.flags == []
    assert result.verdict == "ok"


def test_graduating_after_a_cohort_window_keeps_the_flag():
    posting = "Open to spring/summer of 2027 college graduates."
    result = screen.parse_reply(reply(verdict="caution", flags=[{
        "category": "timeline",
        "severity": "soft",
        "quote": "spring/summer of 2027 college graduates",
        "reason": "Applicant graduates December 2028.",
    }]), text=posting, facts="- Graduation: expected December 2028")

    assert [(flag.category, flag.severity) for flag in result.flags] == [("timeline", "soft")]


def test_a_posting_that_needs_the_applicant_still_enrolled_keeps_the_flag():
    posting = "Requires an expected graduation of December 2027 or later."
    result = screen.parse_reply(reply(verdict="reject", flags=[{
        "category": "timeline",
        "severity": "hard",
        "quote": "expected graduation of December 2027 or later",
        "reason": "Applicant graduates December 2026; this is an internship.",
    }]), text=posting, facts="- Graduation: expected December 2026")

    assert [(flag.category, flag.severity) for flag in result.flags] == [("timeline", "hard")]
    assert result.verdict == "reject"
