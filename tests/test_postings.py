"""Posting text kept from the browser, and the pipeline falling back to it."""

import pytest
from fastapi.testclient import TestClient

import pipeline
from archive import store
from server import postings, queue
from server.app import app
from server.models import Job
from tailor import fetch

EMPLOYER = "https://jobs.example.com/apply/123?jr_id=abc123"
DESCRIPTION = "Build distributed systems in Go and Kubernetes. " * 20


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(postings.DIR_ENV, str(tmp_path / "postings"))
    qpath = tmp_path / "queue.json"
    monkeypatch.setattr(queue, "QUEUE_PATH", qpath)
    monkeypatch.setattr(queue, "LOCK_PATH", qpath.with_suffix(".lock"))
    monkeypatch.setattr(store, "APPLICATIONS", tmp_path / "applications")
    monkeypatch.setattr(store, "INDEX_PATH", tmp_path / "applications" / "index.csv")


def test_source_id_comes_from_jobrights_tag():
    assert postings.source_id(EMPLOYER) == "abc123"
    assert postings.source_id("https://jobs.example.com/apply/123") is None


def test_source_title_yields_company_and_role():
    postings.save_source("abc123", postings.Saved(
        url="https://jobright.ai/jobs/info/abc123",
        title="EDA Software Developer @ Nokia | Jobright.ai", text=DESCRIPTION))
    [saved] = postings.fallbacks("nojob", EMPLOYER)
    assert saved.company == "Nokia"
    assert saved.title == "EDA Software Developer"


def test_generic_jobright_title_uses_the_visible_posting_header():
    postings.save_source("abc123", postings.Saved(
        url="https://jobright.ai/jobs/info/abc123",
        title="Job Recommendations | Jobright AI",
        text="""Jobs
Original Job Post
H&R Block
·
40 minutes ago
Machine Learning Engineer
United States
Full-time
Responsibilities
Build and deploy machine learning models.
""",
    ))

    assert postings.source_meta(EMPLOYER) == ("Machine Learning Engineer", "H&R Block")
    [saved] = postings.fallbacks("nojob", EMPLOYER)
    assert saved.title == "Machine Learning Engineer"
    assert saved.company == "H&R Block"


def test_employer_page_text_beats_jobrights_and_borrows_its_company():
    postings.save_source("abc123", postings.Saved(
        url="https://jobright.ai/jobs/info/abc123", title="Role @ Acme | Jobright.ai",
        text="jobright copy " * 40))
    postings.save_captured("job1", postings.Saved(url=EMPLOYER, title="Apply", text=DESCRIPTION))
    first, second = postings.fallbacks("job1", EMPLOYER)
    assert first.text == DESCRIPTION
    assert first.company == "Acme"
    assert second.text.startswith("jobright copy")


def test_capture_with_text_is_kept_for_the_pipeline(monkeypatch):
    from server import runner
    monkeypatch.setattr(runner, "launch", lambda *a, **k: 1)
    client = TestClient(app)
    body = client.post("/capture", json={"url": EMPLOYER, "title": "Apply", "text": DESCRIPTION}).json()
    [saved] = postings.fallbacks(body["id"], EMPLOYER)
    assert saved.text == DESCRIPTION


def test_posting_endpoint_saves_jobrights_copy():
    client = TestClient(app)
    assert client.post("/posting", json={
        "id": "abc123", "url": "https://jobright.ai/jobs/info/abc123",
        "title": "Role @ Acme | Jobright.ai", "text": DESCRIPTION}).json() == {"ok": True}
    [saved] = postings.fallbacks("nojob", EMPLOYER)
    assert saved.company == "Acme"


def failing_fetch(url):
    raise fetch.JavaScriptRequired(f"{url} returned 0 chars of text")


def test_pipeline_uses_the_browsers_text_when_the_fetch_has_none(monkeypatch):
    monkeypatch.setattr(pipeline.fetch, "fetch", failing_fetch)
    job = Job(url=EMPLOYER, title="Apply")
    postings.save_captured(job.id, postings.Saved(url=EMPLOYER, title="Apply", text=DESCRIPTION))
    postings.save_source("abc123", postings.Saved(
        url="https://jobright.ai/jobs/info/abc123", title="Role @ Acme | Jobright.ai", text="x" * 500))

    posting = pipeline.fetch_posting(job)

    assert posting.text == DESCRIPTION
    assert posting.company == "Acme"
    assert posting.url == EMPLOYER


def test_pipeline_falls_back_to_jobright_when_the_form_page_was_bare(monkeypatch):
    monkeypatch.setattr(pipeline.fetch, "fetch", failing_fetch)
    job = Job(url=EMPLOYER)
    postings.save_captured(job.id, postings.Saved(url=EMPLOYER, text="Apply now. First name. Last name."))
    postings.save_source("abc123", postings.Saved(
        url="https://jobright.ai/jobs/info/abc123", title="Role @ Acme | Jobright.ai", text=DESCRIPTION))

    posting = pipeline.fetch_posting(job)

    assert posting.text == DESCRIPTION


def test_pipeline_still_fails_with_nothing_to_fall_back_on(monkeypatch):
    monkeypatch.setattr(pipeline.fetch, "fetch", failing_fetch)
    with pytest.raises(fetch.JavaScriptRequired):
        pipeline.fetch_posting(Job(url=EMPLOYER))


def test_a_fetched_posting_is_preferred(monkeypatch):
    fetched = fetch.Posting(url=EMPLOYER, text="from the page " * 40, company="Real")
    monkeypatch.setattr(pipeline.fetch, "fetch", lambda url: fetched)
    postings.save_captured(Job(url=EMPLOYER).id, postings.Saved(url=EMPLOYER, text=DESCRIPTION))
    assert pipeline.fetch_posting(Job(url=EMPLOYER)) is fetched


FORM_PAGE = "\n".join(["Apply for this job", "First name", "Last name", "Email address here please",
                       "Upload your resume", "Submit application", "Privacy policy and cookies"] * 12)
REAL_POSTING = "About the role\n" + "\n".join(
    f"- Build and operate distributed services handling {i} million requests a day" for i in range(12)
) + "\nRequirements\n- 3+ years of Python\n- Kubernetes and Postgres in production"


def test_a_fetched_form_loses_to_jobrights_description(monkeypatch):
    fetched = fetch.Posting(url=EMPLOYER, text=FORM_PAGE, title="Apply")
    monkeypatch.setattr(pipeline.fetch, "fetch", lambda url: fetched)
    postings.save_source("abc123", postings.Saved(
        url="https://jobright.ai/jobs/info/abc123", title="Role @ Acme | Jobright.ai", text=REAL_POSTING))

    posting = pipeline.fetch_posting(Job(url=EMPLOYER))

    assert posting.text == REAL_POSTING
    assert posting.text_source == "Jobright's copy"
    assert posting.company == "Acme"
    assert "**Text from:** Jobright's copy" in posting.to_markdown()


def test_a_shorter_real_posting_still_wins_the_fetch(monkeypatch):
    fetched = fetch.Posting(url=EMPLOYER, text=REAL_POSTING, title="Role")
    monkeypatch.setattr(pipeline.fetch, "fetch", lambda url: fetched)
    postings.save_source("abc123", postings.Saved(
        url="https://jobright.ai/jobs/info/abc123", text=REAL_POSTING + "\n- One more bullet about the team"))
    assert pipeline.fetch_posting(Job(url=EMPLOYER)) is fetched
