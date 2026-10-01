"""The Simplify list tracker: rows from the list's own GitHub page matched
to our queue, screened, and queued one day at a time.

What matters here is what cannot be seen on the page: that the certain nos
cost nothing, that "Applied" is the queue's own answer rather than a second
record, that a day's run queues only what the screen allows, and that the
list's own URLs can never be queued as jobs.
"""
import pytest
from fastapi.testclient import TestClient

from server import queue, runner, seen, settings, simplify
from server.app import app
from server.models import Job, Status


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(simplify.STORE_ENV, str(tmp_path / "simplify.json"))
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    qpath = tmp_path / "queue.json"
    monkeypatch.setattr(queue, "QUEUE_PATH", qpath)
    monkeypatch.setattr(queue, "LOCK_PATH", qpath.with_suffix(".lock"))
    monkeypatch.setattr(runner, "start_pipeline", lambda job_id, log_dir=None: 4242)
    # No model and no network: the screen is stubbed per test.
    monkeypatch.setattr(simplify, "_screen", lambda row: ("ok", "fits"))


@pytest.fixture
def client():
    return TestClient(app)


def row(**kw) -> simplify.Row:
    base = dict(uuid="11111111-2222-3333-4444-555555555555", company="Acme",
                title="Software Engineer, New Grad", url="https://jobs.ashbyhq.com/acme/abc",
                location="Austin, TX", category="💻 Software Engineering New Grad Roles",
                age="0d", day="2026-10-01")
    return simplify.Row(**(base | kw))


# ------------------------------------------------------- the certain nos --


def test_a_row_the_list_itself_says_no_to_is_never_fetched(monkeypatch):
    """The legend's own marks are a hard no, and reading them costs nothing.
    A fetch here would be a request per row for an answer already on screen."""
    monkeypatch.setattr(simplify, "_screen", lambda row: pytest.fail("screened a hard no"))
    assert "sponsor" in simplify.why_not(row(marks="🛂"))
    assert "citizen" in simplify.why_not(row(title="Software Engineer 🇺🇸"))
    assert simplify.work(row(marks="🛂")).outcome == "skipped"


def test_level_and_place_are_read_off_the_row():
    assert "level" in simplify.why_not(row(title="Senior Software Engineer"))
    assert "level" in simplify.why_not(row(title="Mechanical Design Engineer"))
    assert "outside the US" in simplify.why_not(row(location="Tokyo, Japan"))
    # One US location among foreign ones keeps the row.
    assert simplify.why_not(row(location="Toronto, ON, Canada; Austin, TX")) == ""
    assert simplify.why_not(row()) == ""


def test_the_lists_own_urls_can_never_become_jobs():
    for url in ("https://github.com/SimplifyJobs/New-Grad-Positions",
                "https://simplify.jobs/p/11111111-2222-3333-4444-555555555555",
                "https://jobright.ai/jobs/info/abc"):
        assert simplify.banned(url), url
    assert "not to an employer's form" in simplify.why_not(row(url="https://simplify.jobs/p/x"))


def test_the_marks_come_off_the_title():
    assert simplify.clean_title("Full-Stack Engineer 🛂") == "Full-Stack Engineer"
    assert simplify.clean_title("🔥 Software Engineer 🎓") == "Software Engineer"


# ------------------------------------------------------------- one row ----


def test_a_clean_row_is_queued_and_the_pipeline_starts(monkeypatch):
    started = []
    monkeypatch.setattr(runner, "start_pipeline", lambda job_id, log_dir=None: started.append(job_id) or 1)
    done = simplify.work(row())
    assert done.outcome == "queued" and done.verdict == "ok"
    job = queue.all_jobs()[0]
    assert (job.source, job.title, job.company) == ("simplify", "Software Engineer, New Grad", "Acme")
    assert started == [job.id]
    assert done.job_id == job.id


def test_a_rejected_row_is_recorded_and_not_queued(monkeypatch):
    monkeypatch.setattr(simplify, "_screen", lambda row: ("reject", "asks for 7+ years of experience"))
    done = simplify.work(row())
    assert (done.outcome, done.job_id) == ("reject", None)
    assert "7+ years" in done.reason
    assert queue.all_jobs() == []


def test_a_posting_that_cannot_be_read_is_not_a_rejection(monkeypatch):
    monkeypatch.setattr(simplify, "_screen", lambda row: ("", "could not read the posting: HTTP 404"))
    done = simplify.work(row())
    assert done.outcome == "failed" and done.job_id is None


def test_a_row_already_in_autopilot_is_left_alone():
    queue.add(Job(url="https://jobs.ashbyhq.com/acme/abc", title="Software Engineer", company="Acme"))
    done = simplify.work(row())
    assert done.outcome == "known"
    assert len(queue.all_jobs()) == 1


def test_add_by_hand_overrules_the_filter(monkeypatch):
    """Pressing Add is the person's decision; the pipeline still screens it.
    A title the level filter passed over is exactly when this is wanted."""
    monkeypatch.setattr(simplify, "_screen", lambda row: pytest.fail("screened a forced row"))
    done = simplify.work(row(title="Member of Technical Staff, Robotics 🛂"), force=True)
    assert done.outcome == "queued"
    assert queue.all_jobs()[0].title == "Member of Technical Staff, Robotics"


def test_add_by_hand_still_refuses_the_lists_own_url():
    done = simplify.work(row(url="https://github.com/SimplifyJobs/New-Grad-Positions"), force=True)
    assert done.outcome == "skipped" and queue.all_jobs() == []


# ------------------------------------------------------------- matching --


def test_a_row_is_matched_to_the_job_that_got_furthest():
    """Several of our rows can be one posting - the page captured today and
    the application sent last week. The answer is the one that was sent."""
    old = Job(url="https://jobs.ashbyhq.com/acme/abc?utm_source=Simplify", title="Software Engineer",
              company="Acme", status=Status.SUBMITTED)
    new = Job(url="https://jobright.ai/jobs/info/zz", title="Software Engineer, New Grad",
              company="Acme", status=Status.TAILORING)
    queue.add(old)
    queue.add(new)
    match = simplify.Index().find(row())
    assert (match.level, match.status) == ("high", "submitted")


def test_a_match_by_company_and_role_is_confident_not_certain():
    queue.add(Job(url="https://boards.greenhouse.io/acme/jobs/1", title="Software Engineer, New Grad",
                  company="Acme", status=Status.FILLED))
    match = simplify.Index().find(row(url="https://jobs.lever.co/acme/" + "a" * 36))
    assert match.level == "confident" and match.status == "filled"


# ---------------------------------------------------------------- the API --


def test_status_says_what_the_queue_says_and_writes_nothing(client):
    queue.add(Job(url="https://jobs.ashbyhq.com/acme/abc", title="Software Engineer",
                  company="Acme", status=Status.SUBMITTED))
    res = client.post("/simplify/status", json={"rows": [row().model_dump()]})
    assert res.status_code == 200
    body = res.json()
    assert body["rows"][0]["seen"]["status"] == "submitted"
    assert body["rows"][0]["tracked"] is None      # nothing was recorded by looking
    assert body["hold"] is True                    # a day's worth of jobs opens no tab
    assert body["worked"] == [row().uuid]
    assert not simplify.path().exists()


def test_a_day_queues_what_the_screen_allows(client, monkeypatch):
    verdicts = {"a": ("ok", "fits"), "b": ("reject", "clearance"), "c": ("caution", "far")}
    monkeypatch.setattr(simplify, "_screen", lambda r: verdicts[r.company])
    rows = [row(uuid=f"{i}1111111-2222-3333-4444-555555555555", company=c,
                url=f"https://jobs.ashbyhq.com/{c}/{'x' * 36}")
            for i, c in enumerate("abc")]
    res = client.post("/simplify/day", json={"day": "2026-10-01",
                                             "rows": [r.model_dump() for r in rows]})
    assert res.json()["started"] is True
    _wait()
    run = simplify.days()["2026-10-01"]
    assert (len(run.queued), run.rejected, run.done, run.running) == (2, 1, 3, False)
    assert {job.company for job in queue.all_jobs()} == {"a", "c"}


def test_a_day_runs_a_few_pipelines_at_a_time_not_one_per_row(client, monkeypatch):
    """`hold_fills` holds the fills, not the tailoring. A day of 90 postings
    started a `pipeline.py` each: 90 fetches, model calls and LaTeX compiles
    at once on one laptop."""
    live = set()
    pid = iter(range(100, 200))

    def start(job_id, log_dir=None):
        new = next(pid)
        live.add(new)
        return new

    monkeypatch.setattr(runner, "start_pipeline", start)
    monkeypatch.setattr(simplify, "_alive", lambda p: p in live)
    monkeypatch.setattr(simplify, "PIPELINE_POLL", 0.01)
    seen_at_once = []
    real_room = simplify._room

    def room(pids, **kw):
        seen_at_once.append(len([p for p in pids if p in live]))
        # Nothing ever finishes on its own here; the oldest is let go once
        # the run has waited, which is what a real pipeline ending looks like.
        if len([p for p in pids if p in live]) >= simplify.MAX_PIPELINES:
            live.discard(sorted(p for p in pids if p in live)[0])
        return real_room(pids, **kw)

    monkeypatch.setattr(simplify, "_room", room)
    rows = [row(uuid=f"{i}1111111-2222-3333-4444-555555555555", company=f"c{i}",
                url=f"https://jobs.ashbyhq.com/c{i}/{'x' * 36}") for i in range(6)]
    client.post("/simplify/day", json={"day": "2026-10-01", "rows": [r.model_dump() for r in rows]})
    _wait()
    assert len(queue.all_jobs()) == 6
    assert max(seen_at_once) < simplify.MAX_PIPELINES + 1


def test_a_day_leaves_the_sections_it_does_not_cover(client):
    hardware = row(category="🔧 Hardware Engineering New Grad Roles")
    res = client.post("/simplify/day", json={"day": "2026-10-01", "rows": [hardware.model_dump()]})
    assert res.json() == {"started": False, "day": "2026-10-01",
                          "reason": "no rows in the sections a day's run covers"}
    assert queue.all_jobs() == []


def test_one_day_at_a_time(client, monkeypatch):
    """A day is 20 to 90 page fetches. Two runs at once would be two of them
    on one queue, and the page cannot say whose answer a row is."""
    monkeypatch.setattr(simplify, "running_day", lambda: "2026-09-30")
    res = client.post("/simplify/day", json={"day": "2026-10-01", "rows": [row().model_dump()]})
    assert res.json()["started"] is False and "2026-09-30" in res.json()["reason"]


def test_the_row_endpoint_adds_one_posting(client):
    res = client.post("/simplify/row", json=row().model_dump() | {"force": True})
    assert res.json()["queued"] is True
    assert len(queue.all_jobs()) == 1


def test_the_summary_counts_applications_off_the_queue(client):
    simplify.work(row())
    job = queue.all_jobs()[0]
    queue.update(job.id, status=Status.SUBMITTED)
    body = client.get("/simplify").json()
    assert (body["rows"], body["queued"], body["applied"]) == (1, 1, 1)


def _wait(seconds: float = 5.0) -> None:
    import time
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if simplify.running_day() is None:
            return
        time.sleep(0.05)
    raise AssertionError("the day's run never finished")
