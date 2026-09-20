"""The already-seen check: one posting is never queued twice, a lookalike
stops the countdown but not the person, and the text picker prefers a
description over a form."""
import csv
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from archive import store
from server import postings, queue, seen
from server import screen as server_screen
from server.app import app
from server.models import Job, Status
from tailor import quality, screen

ASHBY = "https://jobs.ashbyhq.com/fieldguide/c7af5e2a-7522-4c8c-91f0-2c3542d42548/application"
JR = "6aab412d4be87a72913a3ece"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    qpath = tmp_path / "queue.json"
    monkeypatch.setattr(queue, "QUEUE_PATH", qpath)
    monkeypatch.setattr(queue, "LOCK_PATH", qpath.with_suffix(".lock"))
    monkeypatch.setattr(store, "APPLICATIONS", tmp_path / "applications")
    monkeypatch.setattr(store, "INDEX_PATH", tmp_path / "applications" / "index.csv")
    monkeypatch.setattr(server_screen, "CACHE_PATH", tmp_path / "screens.json")
    monkeypatch.setenv(postings.DIR_ENV, str(tmp_path / "postings"))


def test_canonical_drops_visitor_noise_and_keeps_job_params():
    a = seen.canonical("https://WWW.Example.com/jobs/1/?utm_source=x&gh_jid=99#top")
    b = seen.canonical("http://example.com/jobs/1?gh_jid=99&ref=jobright")
    assert a == b == "example.com/jobs/1?gh_jid=99"


def test_ats_ids_are_host_independent():
    assert seen.ats_id(ASHBY) == seen.ats_id(ASHBY.replace("/application", "") + "?jr_id=" + JR)
    assert seen.ats_id("https://boards.greenhouse.io/acme/jobs/4001") == ("greenhouse", "4001")
    assert seen.ats_id("https://job-boards.greenhouse.io/embed/job_app?for=acme&token=4001&jr_id=x") == ("greenhouse", "4001")


def test_greenhouse_embeds_are_told_apart_by_their_token():
    # One path for every job on the embedded board: two postings there
    # read as the same page once, and the new one was never queued.
    a = seen.canonical("https://job-boards.greenhouse.io/embed/job_app?for=pallet&token=1&jr_id=x")
    b = seen.canonical("https://job-boards.greenhouse.io/embed/job_app?for=together&token=2&jr_id=y")
    assert a != b
    assert a == seen.canonical("https://job-boards.greenhouse.io/embed/job_app?token=1&for=pallet")
    assert seen.ats_id("https://acme.com/careers") is None


def test_queue_add_refuses_the_same_posting_under_another_url():
    first, created = queue.add(Job(url=ASHBY + "?jr_id=" + JR))
    assert created
    again, created = queue.add(Job(url=ASHBY.replace("/application", "") + "?utm_source=jobright"))
    assert not created and again.id == first.id
    also, created = queue.add(Job(url="https://other.example/x?jr_id=" + JR))
    assert not created and also.id == first.id


def test_high_match_prefers_the_employer_row_over_jobright_row():
    queue.add(Job(url=f"https://jobright.ai/jobs/info/{JR}", company="Fieldguide"))
    employer, _ = queue.add(Job(url=ASHBY + "?jr_id=" + JR, company="Fieldguide"))
    match = seen.find(ASHBY)
    assert match.level == "high" and match.id == employer.id


def test_confident_match_by_title_at_the_same_company():
    queue.add(Job(url="https://boards.greenhouse.io/fieldguide/jobs/1", company="Fieldguide",
                  title="[Remote] Software Engineer, (Internal Audit) [All Levels]"))
    match = seen.find("https://fieldguide.io/careers/swe-audit",
                      title="Software Engineer, Internal Audit @ Fieldguide | Jobright.ai")
    assert match.level == "confident" and "same role" in match.reason
    assert seen.find("https://fieldguide.io/careers/pm", title="Product Manager - Fieldguide Careers") is None
    assert seen.find("https://acme.io/careers/swe", title="Software Engineer, Internal Audit - Acme") is None


def test_confident_match_by_description(tmp_path):
    job, _ = queue.add(Job(url="https://boards.greenhouse.io/acme/jobs/1", company="Acme", title="Wizard"))
    folder = tmp_path / "applications" / "acme"
    folder.mkdir(parents=True)
    body = "\n".join(f"- You will build distributed systems for line {i} of the platform" for i in range(30))
    (folder / "posting.md").write_text("# Wizard\n\n---\n\n" + body + "\n")
    queue.update(job.id, app_dir=str(folder))
    match = seen.find("https://acme.com/jobs/staff-eng", title="Staff Engineer | Acme", text=body)
    assert match and match.level == "confident" and "description" in match.reason


def test_rejection_expires_at_the_confident_level_only():
    old = (datetime.now(timezone.utc) - timedelta(days=seen.REJECT_TTL_DAYS + 1)).isoformat()
    job, _ = queue.add(Job(url="https://boards.greenhouse.io/acme/jobs/1", company="Acme",
                           title="Backend Engineer", status=Status.SKIPPED, added_at=old))
    assert seen.find("https://acme.com/jobs/be", title="Backend Engineer - Acme") is None
    assert seen.find("https://boards.greenhouse.io/acme/jobs/1").id == job.id


def test_archive_index_rows_count(tmp_path):
    store.INDEX_PATH.parent.mkdir(parents=True)
    with store.INDEX_PATH.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=store.INDEX_FIELDS)
        writer.writeheader()
        writer.writerow({"folder": "f", "job_id": "abcd", "company": "Acme", "title": "SRE",
                         "source": "x", "url": "https://boards.greenhouse.io/acme/jobs/7",
                         "created_at": "2026-01-01T00:00:00+00:00", "status": "submitted"})
    match = seen.find("https://boards.greenhouse.io/acme/jobs/7")
    assert match.level == "high" and match.status == "submitted"


def test_capture_refuses_source_pages_and_dedupes(monkeypatch):
    monkeypatch.setattr("server.app.runner.start_pipeline", lambda job_id: None)
    client = TestClient(app)
    body = client.post("/capture", json={"url": f"https://jobright.ai/jobs/info/{JR}"}).json()
    assert body["created"] is False and body["reason"] == "source_page"
    assert queue.all_jobs() == []
    first = client.post("/capture", json={"url": ASHBY + "?jr_id=" + JR}).json()
    second = client.post("/capture", json={"url": ASHBY}).json()
    assert first["created"] and not second["created"] and second["id"] == first["id"]


def test_screen_reply_carries_seen(monkeypatch):
    monkeypatch.setattr(screen.llm, "complete", lambda *a, **k: '{"summary": "s", "flags": []}')
    queue.add(Job(url=ASHBY, company="Fieldguide", title="SWE"))
    client = TestClient(app)
    hit = client.post("/screen", json={"url": ASHBY + "?jr_id=" + JR, "text": "a posting", "title": "T"}).json()
    miss = client.post("/screen", json={"url": "https://x.com/j/2", "text": "a posting", "title": "T"}).json()
    assert hit["seen"]["level"] == "high" and hit["seen"]["status"] == "queued"
    assert miss["seen"] is None


FORM = "\n".join(["Apply for this job", "First name", "Last name", "Email", "Phone number",
                  "Upload your resume", "LinkedIn profile", "Submit application", "Privacy policy",
                  "We use cookies to improve your experience on our site"] * 5)
POSTING = "About the role\n" + "\n".join(
    f"- Build and operate distributed services that handle {i} million requests" for i in range(12)
) + "\nRequirements\n- 3+ years of Python\n- Experience with Kubernetes and Postgres"


def test_quality_prefers_a_description_over_a_form():
    assert quality.score(POSTING) > quality.score(FORM)
    assert quality.score(FORM) < 60
    assert quality.best([("form", FORM), ("posting", POSTING)])[0] == "posting"


def test_quality_ties_go_to_the_first():
    assert quality.best([("a", POSTING), ("b", POSTING)])[0] == "a"
    assert quality.best([("a", ""), ("b", "x")]) is None


def test_simhash_is_close_for_the_same_posting_and_far_for_another():
    near = POSTING.replace("Kubernetes", "K8s")
    other = "\n".join(f"- Design marketing campaigns for region {i} with the brand team" for i in range(14))
    assert quality.distance(quality.simhash(POSTING), quality.simhash(near)) <= seen.TEXT_DISTANCE
    assert quality.distance(quality.simhash(POSTING), quality.simhash(other)) > seen.TEXT_DISTANCE


def test_a_live_employer_row_beats_a_rejected_jobright_row_for_the_same_posting():
    queue.add(Job(url=f"https://jobright.ai/jobs/info/{JR}", company="Fieldguide", status=Status.SKIPPED))
    live, _ = queue.add(Job(url=ASHBY + "?jr_id=" + JR, company="Fieldguide"))
    match = seen.find("https://somewhere.else/apply?jr_id=" + JR)
    assert match.id == live.id and match.status == "queued"
