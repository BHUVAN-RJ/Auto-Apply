"""`POST /start-now`: the person's hand where the automatic Apply press was.

The fill no longer presses Apply (`browser/fill.py`, 2026-10-30): getting
from a posting to a form was up to three presses, each able to open a tab,
and 22 runs in a month still ended "no application form on this page". The
person doing that navigation themselves is the reliable half; this endpoint
is the other one - it starts the fill they are already looking at, through
every gate an automatic start goes through, and never queues or submits
anything.
"""

import pytest
from fastapi.testclient import TestClient

from archive import store
from browser import fill as filler
from server import queue, runner
from server.app import app
from server.models import Job, Status

# Two URLs whose sha256 begins with the same four characters, which is all
# `Job.id` is (KNOWN_ISSUES #1). The real pair was two Twitch postings.
COLLIDE_A = "https://boards.greenhouse.io/collide/jobs/310"
COLLIDE_B = "https://boards.greenhouse.io/collide/jobs/333"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    qpath = tmp_path / "queue.json"
    monkeypatch.setattr(queue, "QUEUE_PATH", qpath)
    monkeypatch.setattr(queue, "LOCK_PATH", qpath.with_suffix(".lock"))
    monkeypatch.setattr(store, "APPLICATIONS", tmp_path / "applications")
    monkeypatch.setattr(store, "INDEX_PATH", tmp_path / "applications" / "index.csv")

    import server.review as review_module
    monkeypatch.setattr(review_module, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(review_module.chrome, "close",
                        lambda port_number=None: pytest.fail("a test closed Chrome"))
    monkeypatch.setattr(review_module.chrome, "close_tab",
                        lambda target_id, port_number=None: False)
    from browser import forms
    monkeypatch.setattr(forms, "find_target", lambda cdp, url, target_id="": "")
    from server import corrections
    monkeypatch.setattr(corrections, "capture",
                        lambda job_url, app_dir: {"captured": False, "reason": "test"})

    # No real apply.py, ever; the launches are recorded instead.
    monkeypatch.setattr(runner, "_fills", {})
    monkeypatch.setattr(runner, "_pgrep_fill", lambda job_id: None)
    monkeypatch.setattr(runner, "AUTOFILL_ENABLED", True)


@pytest.fixture
def launches(monkeypatch) -> list:
    started: list = []

    def fake(script, job_id, log_dir=None):
        started.append((script, job_id))
        return 4321

    monkeypatch.setattr(runner, "launch", fake)
    return started


@pytest.fixture
def client():
    return TestClient(app)


def tailored_job(url: str = "https://jobs.example.com/apply/1", status=Status.APPROVED,
                 title: str = "Backend Engineer", company: str = "Example Corp"):
    job, _ = queue.add(Job(url=url, title=title, company=company))
    app_dir = store.create(job)
    store.write(app_dir, "resume.pdf", b"%PDF-1.5 fake")
    store.set_status(app_dir, status)
    queue.update(job.id, status=status, app_dir=str(app_dir))
    return job.id, app_dir


# --------------------------------------------------------------- the button --

def test_the_press_starts_the_fill_for_the_job_on_that_page(client, launches):
    job_id, app_dir = tailored_job()
    body = client.post("/start-now", json={"url": "https://jobs.example.com/apply/1"}).json()
    assert body == {"started": True, "state": "started", "job_id": job_id,
                    "note": body["note"], "candidates": []}
    assert launches == [("apply.py", job_id)]
    assert "start now" in (app_dir / "status.json").read_text()


def test_a_job_still_at_checkpoint_one_is_started_by_the_press(client, launches):
    """The whole case the button exists for: the fill stopped because no form
    was on the screen, and the person has the form in front of them now."""
    job_id, app_dir = tailored_job(status=Status.AWAITING_REVIEW)
    body = client.post("/start-now", json={"url": "https://jobs.example.com/apply/1"}).json()
    assert body["started"] and body["state"] == "started"
    # apply.py only works on an approved job.
    assert queue.get(job_id).status is Status.APPROVED
    assert launches == [("apply.py", job_id)]


def test_a_running_fill_is_synced_with_not_started_again(client, launches, monkeypatch):
    job_id, _ = tailored_job(status=Status.FILLING)
    monkeypatch.setattr(runner, "fill_pid", lambda wanted: 999 if wanted == job_id else None)
    body = client.post("/start-now", json={"url": "https://jobs.example.com/apply/1"}).json()
    assert body["started"] is False and body["state"] == "synced"
    assert body["job_id"] == job_id and "999" in body["note"]
    assert launches == [], "a second fill on one form fights the first"


def test_another_job_filling_means_waiting_not_a_second_tab(client, launches, monkeypatch):
    job_id, _ = tailored_job()
    monkeypatch.setattr(runner, "any_fill_running", lambda: "beef")
    body = client.post("/start-now", json={"url": "https://jobs.example.com/apply/1"}).json()
    assert body["started"] is False and body["state"] == "waiting" and body["job_id"] == job_id
    assert launches == []


def test_a_page_that_is_not_a_job_queues_nothing(client, launches):
    """Adding a posting is the Add button's decision, and it screens first."""
    tailored_job()
    before = len(queue.all_jobs())
    body = client.post("/start-now", json={"url": "https://elsewhere.example/jobs/55"}).json()
    assert body["started"] is False and body["state"] == "no_job" and body["job_id"] == ""
    assert len(queue.all_jobs()) == before
    assert launches == []


def test_no_job_offers_the_jobs_in_flight_to_pick_from(client):
    """A YC listing's Apply goes out to the employer's own ATS, so by the time
    the person reaches the form the tab has nothing in common with the URL on
    file and the identity ladder is right to answer "no job". The page has to
    be able to ask, so the jobs waiting for a fill come back with the answer.
    """
    job_id, _ = tailored_job(title="Physical AI Engineer", company="Yondu")
    queue.add(Job(url="https://example.com/old", title="Old One", status=Status.SUBMITTED))
    body = client.post("/start-now",
                       json={"url": "https://wd1.myworkdayjobs.com/en-US/yondu/job/x"}).json()
    assert body["state"] == "no_job"
    assert [c["id"] for c in body["candidates"]] == [job_id]
    assert body["candidates"][0]["company"] == "Yondu"


def test_the_picked_job_is_filled_in_the_tab_the_person_reached(client, launches):
    """`job_id` binds that tab to that job: the form is on a Workday page the
    job's own URL knows nothing about, and the fill starts there. The job's
    URL is the identity key and is never rewritten."""
    job_id, app_dir = tailored_job(url="https://www.ycombinator.com/companies/yondu/jobs/dLJNfs8")
    form = "https://wd1.myworkdayjobs.com/en-US/yondu/job/Physical-AI/apply"
    body = client.post("/start-now", json={"url": form, "job_id": job_id}).json()
    assert body["started"] and body["state"] == "started"
    assert filler.bound_form_url(app_dir) == form
    assert queue.get(job_id).url == "https://www.ycombinator.com/companies/yondu/jobs/dLJNfs8"
    assert launches == [("apply.py", job_id)]


def test_the_binding_is_appended_so_earlier_landings_are_kept(client, launches):
    job_id, app_dir = tailored_job(url="https://www.ycombinator.com/companies/yondu/jobs/dLJNfs8")
    first = "https://wd1.myworkdayjobs.com/en-US/yondu/job/one/apply"
    second = "https://wd1.myworkdayjobs.com/en-US/yondu/job/two/apply"
    client.post("/start-now", json={"url": first, "job_id": job_id})
    client.post("/start-now", json={"url": second, "job_id": job_id})
    recorded = (app_dir / filler.FORM_URL_FILE).read_text()
    assert first in recorded and second in recorded
    assert filler.bound_form_url(app_dir) == second, "the newest landing is the one used"


def test_a_tab_on_the_job_itself_is_not_recorded_as_somewhere_else(client, launches):
    job_id, app_dir = tailored_job()
    client.post("/start-now", json={"url": "https://jobs.example.com/apply/1?utm_source=x",
                                    "job_id": job_id})
    assert not (app_dir / filler.FORM_URL_FILE).exists()


def test_a_submitted_job_is_refused_with_a_reason(client, launches):
    job_id, _ = tailored_job(status=Status.SUBMITTED)
    body = client.post("/start-now", json={"url": "https://jobs.example.com/apply/1"}).json()
    assert body["started"] is False and body["state"] == "submitted"
    assert "already been submitted" in body["note"]
    assert launches == []


def test_a_rejected_job_is_refused_and_says_how_to_reopen_it(client, launches):
    job_id, _ = tailored_job(status=Status.SKIPPED)
    body = client.post("/start-now", json={"url": "https://jobs.example.com/apply/1"}).json()
    assert body["state"] == "rejected" and "Try again" in body["note"]
    assert launches == []


def test_a_job_with_nothing_tailored_is_not_started(client, launches):
    job, _ = queue.add(Job(url="https://jobs.example.com/apply/9", title="New"))
    app_dir = store.create(job)
    store.set_status(app_dir, Status.AWAITING_REVIEW)
    queue.update(job.id, status=Status.AWAITING_REVIEW, app_dir=str(app_dir))
    body = client.post("/start-now", json={"url": "https://jobs.example.com/apply/9"}).json()
    assert body["state"] == "not_ready" and launches == []


def test_the_press_goes_through_the_one_fill_gate(client, monkeypatch):
    """Not a second scheduler: `runner.start_fill` is what every other start
    uses, so one tab at a time and the autofill switch both still hold."""
    job_id, _ = tailored_job()
    asked: list = []
    monkeypatch.setattr(runner, "start_fill",
                        lambda job, log_dir=None, force=False: asked.append((job, force)) or None)
    body = client.post("/start-now", json={"url": "https://jobs.example.com/apply/1"}).json()
    assert asked == [(job_id, True)]
    assert body["started"] is False and body["state"] == "waiting"


def test_the_press_never_submits_anything(client, launches):
    job_id, _ = tailored_job()
    client.post("/start-now", json={"url": "https://jobs.example.com/apply/1"})
    assert queue.get(job_id).status is Status.APPROVED


# ------------------------------------------- an ambiguous short id, refused --

def collided() -> tuple[str, object, object]:
    """Two jobs whose four-hex ids are the same, as 270 real rows produce."""
    first, _ = queue.add(Job(url=COLLIDE_A, title="Dayforce Engineer", company="Dayforce"))
    second, _ = queue.add(Job(url=COLLIDE_B, title="Robotics Engineer", company="Zoox"))
    assert first.id == second.id
    return first.id, first, second


def test_a_colliding_id_is_refused_rather_than_guessed():
    job_id, first, second = collided()
    with pytest.raises(queue.Ambiguous) as caught:
        queue.get(job_id)
    assert {row["url"] for row in caught.value.candidates()} == {first.url, second.url}
    assert queue.rows_for(job_id) and len(queue.rows_for(job_id)) == 2


def test_a_write_by_a_colliding_id_is_refused_too():
    """The write is the data loss: patching the first of two rows sets a
    status on a job nobody was looking at."""
    job_id, first, _ = collided()
    with pytest.raises(queue.Ambiguous):
        queue.update(job_id, status=Status.SUBMITTED)
    assert all(row.status is Status.QUEUED for row in queue.rows_for(job_id))


def test_a_row_can_still_be_written_by_its_own_url():
    job_id, first, second = collided()
    queue.update_row(second, status=Status.FILLED)
    by_url = {row.url: row.status for row in queue.rows_for(job_id)}
    assert by_url[second.url] is Status.FILLED and by_url[first.url] is Status.QUEUED


def test_the_review_page_is_told_the_id_is_ambiguous(client):
    job_id, first, second = collided()
    response = client.get(f"/review/{job_id}")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "ambiguous_id" and detail["id"] == job_id
    assert {row["company"] for row in detail["candidates"]} == {"Dayforce", "Zoox"}


def test_every_other_route_answers_the_same_way(client):
    """The floor under the whole app: no handler may act on whichever row
    came first in the file."""
    job_id, _, _ = collided()
    response = client.get(f"/jobs/{job_id}")
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "ambiguous_id"


# -------------------------------- "I submitted it" with the tab already gone --

def test_the_submitted_button_reaches_the_job_the_page_means(client):
    """Three of the four submission paths need the tab alive, so a person who
    presses Submit and closes the tab leaves this button as the whole
    mechanism - and it was answering 409 about the other job on the id.
    """
    job_id, first, second = collided()
    for job in (first, second):
        app_dir = store.create(job)
        store.write(app_dir, "resume.pdf", b"%PDF fake")
        store.set_status(app_dir, Status.FILLED if job is second else Status.SKIPPED)
        queue.update_row(job, app_dir=str(app_dir),
                         status=Status.FILLED if job is second else Status.SKIPPED)

    body = client.post(f"/review/{job_id}/submitted", json={"url": second.url})
    assert body.status_code == 200 and body.json()["status"] == "submitted"
    rows = {row.url: row.status for row in queue.rows_for(job_id)}
    assert rows[second.url] is Status.SUBMITTED
    assert rows[first.url] is Status.SKIPPED, "the other job on the id is untouched"


def test_the_submitted_button_without_a_url_still_refuses_an_ambiguous_id(client):
    job_id, first, second = collided()
    for job in (first, second):
        app_dir = store.create(job)
        store.set_status(app_dir, Status.FILLED)
        queue.update_row(job, app_dir=str(app_dir), status=Status.FILLED)
    response = client.post(f"/review/{job_id}/submitted", json={})
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "ambiguous_id"
    assert all(row.status is Status.FILLED for row in queue.rows_for(job_id))


def test_a_job_already_submitted_is_still_refused(client):
    job_id, app_dir = tailored_job(status=Status.SUBMITTED)
    assert client.post(f"/review/{job_id}/submitted", json={}).status_code == 409


def test_a_rejected_job_is_still_refused(client):
    job_id, app_dir = tailored_job(status=Status.SKIPPED)
    assert client.post(f"/review/{job_id}/submitted", json={}).status_code == 409


def test_the_picker_is_short_and_leads_with_what_is_live(client):
    """A list nobody reads is the same as no list.

    Measured on the live install the day this was built: `/start-now` on an
    unmatched page answered with **70** candidates, every one of them
    `failed`, because failures accumulate and live jobs do not. A `failed`
    job still belongs on the list - it is exactly the one a person goes and
    finishes by hand - but behind the live ones, and only a few of them.
    """
    from server import review as review_module

    for n in range(12):
        job_id, _ = tailored_job(f"https://example.com/old/{n}", status=Status.FAILED)
        queue.update(job_id, added_at=f"2026-10-{n + 10:02d}T00:00:00+00:00")
    # Older than every failure above, and must still come first.
    live_id, _ = tailored_job("https://example.com/live", status=Status.FILLING)
    queue.update(live_id, added_at="2026-01-01T00:00:00+00:00")

    out = client.post("/start-now", json={"url": "https://example.com/nothing-like-these"}).json()
    assert out["state"] == "no_job"
    picked = out["candidates"]
    assert len(picked) == review_module.PICK_LIMIT
    assert picked[0]["status"] == "filling", "the job being filled right now leads the list"
    assert all(row["status"] == "failed" for row in picked[1:])
