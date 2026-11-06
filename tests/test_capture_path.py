"""How a job gets into autopilot at all, checked end to end without a model.

`/capture` is the whole front door: the banner screens a posting, the
countdown presses Apply, the employer tab that opens queues itself here, and
everything downstream - the tailoring, the folder, the review page, the fill
- hangs off the row this endpoint writes. `tests/test_capture.py` covers
what the banner does and when the pipeline starts; these are the three
things about the row itself that nothing else asserts.

No network and no model: the pipeline launch is recorded rather than run,
which is the pattern every server test here uses.
"""

import pytest
from fastapi.testclient import TestClient

from archive import store
from server import postings, queue, runner
from server.app import app
from server.models import Status

JOBRIGHT = "https://jobright.ai/jobs/info/abc123"
EMPLOYER = "https://jobs.example.com/apply/1?jr_id=abc123"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """The person's own queue, archive and saved postings are never read or
    written by a test; a capture writes to all three."""
    qpath = tmp_path / "queue.json"
    monkeypatch.setattr(queue, "QUEUE_PATH", qpath)
    monkeypatch.setattr(queue, "LOCK_PATH", qpath.with_suffix(".lock"))
    monkeypatch.setattr(store, "APPLICATIONS", tmp_path / "applications")
    monkeypatch.setattr(store, "INDEX_PATH", tmp_path / "applications" / "index.csv")
    monkeypatch.setenv(postings.DIR_ENV, str(tmp_path / "postings"))


@pytest.fixture
def launched(monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "launch",
                        lambda script, job_id, log_dir=None: calls.append((script, job_id)) or 4242)
    return calls


def test_a_capture_puts_the_posting_in_the_queue_before_anything_models_it(launched):
    """The row is the job. It is written synchronously, at QUEUED, carrying
    the employer URL the browser was actually on - not Jobright's link, not
    a canonicalised guess - because that URL is what the fill later opens,
    what the duplicate check keys on, and what names the folder. The id in
    the reply has to be the id of that row, or the review page the banner
    then points at is a 404.
    """
    client = TestClient(app)
    body = client.post("/capture", json={
        "url": EMPLOYER, "title": "Software Engineer, New Grad", "company": "Example",
        "source": "jobright",
    }).json()

    assert body["created"] is True
    job = queue.get(body["id"])
    assert job is not None, "the capture answered with an id that is not in the queue"
    assert job.url == EMPLOYER
    assert job.title == "Software Engineer, New Grad" and job.company == "Example"
    assert job.status is Status.QUEUED, "a captured job starts at the first step, nowhere else"
    assert job.app_dir is None and job.error is None
    # Exactly one row, and the only thing started is the tailoring.
    assert [j.id for j in queue.all_jobs()] == [body["id"]]
    assert launched == [("pipeline.py", body["id"])]


def test_a_listing_sites_page_queues_nothing_and_starts_nothing(launched):
    """Jobright's own posting page is not a job - its Apply leads to one -
    and queueing it would mean one posting applied for twice, under two
    URLs. The refusal is server-side on purpose: the same guard lived in the
    extension's content.js once and Chrome served the old copy for forty
    minutes, so the Jobright URL was queued anyway. `test_seen.py` checks
    that no row appears; the other half is that no pipeline is spent on it
    either - a tailoring run against a listing page costs a model call and
    leaves a folder behind.
    """
    client = TestClient(app)
    body = client.post("/capture", json={"url": JOBRIGHT, "title": "SWE @ Example | Jobright.ai"}).json()

    assert body["created"] is False and body["reason"] == "source_page"
    assert body["id"] is None and body["processing"] is False
    assert queue.all_jobs() == []
    assert launched == [], "a listing page started a pipeline"


def test_the_text_the_browser_saw_is_kept_for_the_fetch_that_may_get_a_shell(launched):
    """Many Apply buttons land straight on the application form and many ATS
    pages render client-side, so the fetch that runs seconds later can see an
    empty shell. The browser had the text; the capture carries it, and it is
    filed under the job's own id so the pipeline can fall back to it. An
    empty `text` must write nothing at all, because an empty saved posting
    would be offered as a fallback and would win over no fallback.
    """
    client = TestClient(app)
    body = client.post("/capture", json={
        "url": EMPLOYER, "title": "SWE", "company": "Example",
        "text": "About the role\nYou will build things.",
    }).json()
    kept = postings.fallbacks(body["id"], EMPLOYER)
    assert [saved.text for saved in kept] == ["About the role\nYou will build things."]

    other = client.post("/capture", json={"url": "https://jobs.example.com/apply/2", "text": "   "}).json()
    assert postings.fallbacks(other["id"], "https://jobs.example.com/apply/2") == []
