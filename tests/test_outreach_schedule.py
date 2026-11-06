"""Approved follow-ups land at a civil hour, not whenever they were reviewed.

The person's words: "I want to schedule them for 9 am in the morning. When I
click Send, it has to be drafted and scheduled for 9 am, so I don't want to
send them at random times."

The click is still the only thing that can put an email on the queue, so
every test here either asserts that nothing reached SMTP, or stubs SMTP and
asserts what reached it and when.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import mailing
from archive import store as archive
from outreach import schedule, store as outreach_store
from outreach.models import Contact, Draft
from server import queue, sendlater
from server.app import app
from server.models import Job, Status


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    qpath = tmp_path / "queue.json"
    monkeypatch.setattr(queue, "QUEUE_PATH", qpath)
    monkeypatch.setattr(queue, "LOCK_PATH", qpath.with_suffix(".lock"))
    monkeypatch.setattr(archive, "APPLICATIONS", tmp_path / "applications")
    monkeypatch.setattr(archive, "INDEX_PATH", tmp_path / "applications" / "index.csv")
    monkeypatch.setattr(outreach_store, "STATE", tmp_path / "outreach.json")
    monkeypatch.setattr(outreach_store, "CACHE", tmp_path / "outreach_cache.json")
    monkeypatch.setenv(mailing.USER, "me@example.com")
    monkeypatch.setenv(mailing.PASS, "app-password")
    monkeypatch.delenv(schedule.ENV, raising=False)
    monkeypatch.delenv(schedule.HOUR_ENV, raising=False)
    # Nothing in this file may reach a real SMTP server. A test that wants a
    # send to succeed replaces this with a recorder of its own.
    monkeypatch.setattr(mailing, "send_message",
                        lambda message: pytest.fail("this test must not send"))


def application(tmp_path, verified=True):
    job, _ = queue.add(Job(url="https://jobs.example.com/9", title="ML Engineer",
                           company="Acme", status=Status.SUBMITTED))
    folder = tmp_path / "applications" / job.folder_name()
    folder.mkdir(parents=True)
    (folder / "resume.pdf").write_bytes(b"%PDF tailored resume")
    queue.update(job.id, app_dir=str(folder), status=Status.SUBMITTED)

    contact = Contact.create(
        "recruiter", "Rita Vance", company="Acme", email="rita@acme.example",
        source="apollo", verification="verified" if verified else "guessed")
    state = outreach_store.get(job.id)
    state.contacts = [contact]
    state.drafts = [Draft.create(contact.id, "A role at Acme", "Hi Rita,\n\nBest,\nJane",
                                 attach_resume=False)]
    state.active_draft = state.drafts[0].id
    outreach_store.save(state)
    return queue.get(job.id), state.drafts[0], contact


def recorder(monkeypatch):
    sent = []
    monkeypatch.setattr(mailing, "send_message", lambda message: sent.append(message))
    return sent


# --- the click ------------------------------------------------------------

def test_the_click_schedules_and_sends_nothing(tmp_path):
    job, draft, _ = application(tmp_path)
    answer = TestClient(app).post(
        f"/review/{job.id}/outreach/draft/{draft.id}/send",
        json={"confirmed": True}).json()

    row = answer["drafts"][0]
    assert row["status"] == "scheduled"
    assert row["send_at"]
    # The fixture fails the test if anything reached SMTP, so getting here is
    # the assertion that nothing did.
    assert schedule.when(Draft.model_validate(row)).hour == 9


def test_the_hour_is_the_next_nine_in_the_morning():
    morning = datetime.fromisoformat("2026-11-03T08:10:00-08:00")
    assert schedule.next_slot(morning).isoformat() == "2026-11-03T09:00:00-08:00"
    # Reviewed after nine, so the email is tomorrow's: this is the whole
    # point - a follow-up read at one in the morning must not land then.
    night = datetime.fromisoformat("2026-11-03T22:10:00-08:00")
    assert schedule.next_slot(night).isoformat() == "2026-11-04T09:00:00-08:00"


def test_the_hour_is_configurable(monkeypatch):
    monkeypatch.setenv(schedule.HOUR_ENV, "7")
    assert schedule.next_slot(
        datetime.fromisoformat("2026-11-03T06:00:00-08:00")).hour == 7
    # Nonsense falls back rather than scheduling an email at 40 o'clock.
    monkeypatch.setenv(schedule.HOUR_ENV, "40")
    assert schedule.hour() == 9


def test_a_guessed_address_still_needs_the_tick_before_it_can_be_queued(tmp_path):
    job, draft, _ = application(tmp_path, verified=False)
    answer = TestClient(app).post(
        f"/review/{job.id}/outreach/draft/{draft.id}/send",
        json={"confirmed": True})
    assert answer.status_code == 409
    assert "verify this guessed" in answer.json()["detail"]
    assert outreach_store.get(job.id).drafts[0].status == "draft"


def test_send_now_is_still_available(tmp_path, monkeypatch):
    job, draft, contact = application(tmp_path)
    sent = recorder(monkeypatch)
    answer = TestClient(app).post(
        f"/review/{job.id}/outreach/draft/{draft.id}/send",
        json={"confirmed": True, "now": True}).json()

    assert answer["drafts"][0]["status"] == "sent"
    assert len(sent) == 1
    assert sent[0]["To"] == contact.email


def test_a_scheduled_email_can_be_taken_back_off_the_queue(tmp_path):
    job, draft, _ = application(tmp_path)
    client = TestClient(app)
    client.post(f"/review/{job.id}/outreach/draft/{draft.id}/send",
                json={"confirmed": True})
    answer = client.post(
        f"/review/{job.id}/outreach/draft/{draft.id}/unschedule", json={}).json()

    row = answer["drafts"][0]
    assert row["status"] == "draft" and row["send_at"] is None


# --- the clock ------------------------------------------------------------

def test_an_email_goes_at_its_hour_and_only_then(tmp_path, monkeypatch):
    job, draft, contact = application(tmp_path)
    sent = recorder(monkeypatch)
    queued = schedule.schedule(draft, datetime.now().astimezone())
    outreach_store.put_draft(job.id, queued)

    # Its hour has not come, so the clock does nothing at all.
    assert sendlater.tick() == []
    assert sent == []
    assert outreach_store.get(job.id).drafts[0].status == "scheduled"

    # The hour comes round.
    due = queued.model_copy(update={
        "send_at": (datetime.now().astimezone() - timedelta(minutes=1)).isoformat()})
    outreach_store.put_draft(job.id, due)
    assert sendlater.tick() == [draft.id]

    assert len(sent) == 1
    assert sent[0]["To"] == contact.email
    row = outreach_store.get(job.id).drafts[0]
    assert row.status == "sent" and row.sent_at


def test_a_missed_morning_rolls_to_the_next_one_rather_than_firing_late(tmp_path, monkeypatch):
    """A laptop asleep until the afternoon must not fire a nine o'clock email at four."""
    job, draft, _ = application(tmp_path)
    recorder(monkeypatch)                     # a send here would be the defect
    stale = schedule.schedule(draft).model_copy(update={
        "send_at": (datetime.now().astimezone()
                    - timedelta(hours=schedule.LATE_HOURS + 1)).isoformat()})
    outreach_store.put_draft(job.id, stale)

    assert sendlater.tick() == []
    row = outreach_store.get(job.id).drafts[0]
    assert row.status == "scheduled"
    assert schedule.when(row) > datetime.now().astimezone()


def test_the_gates_are_checked_again_at_the_hour_not_trusted_from_the_click(tmp_path, monkeypatch):
    """Hours pass between the click and the send, and state moves."""
    job, draft, _ = application(tmp_path, verified=False)
    recorder(monkeypatch)
    due = schedule.schedule(draft).model_copy(update={
        "verified_by_user": True,
        "send_at": (datetime.now().astimezone() - timedelta(minutes=1)).isoformat()})
    outreach_store.put_draft(job.id, due)
    # The tick is withdrawn after the click.
    state = outreach_store.get(job.id)
    state.drafts[0] = state.drafts[0].model_copy(update={"verified_by_user": False})
    outreach_store.save(state)

    assert sendlater.tick() == []
    row = outreach_store.get(job.id).drafts[0]
    assert row.status == "draft"               # back to the person
    assert "verification tick" in row.error


def test_one_email_a_tick_so_a_morning_is_not_a_blast(tmp_path, monkeypatch):
    job, _, contact = application(tmp_path)
    sent = recorder(monkeypatch)
    earlier = (datetime.now().astimezone() - timedelta(minutes=1)).isoformat()
    state = outreach_store.get(job.id)
    state.drafts = [
        Draft.create(contact.id, f"Role {index} at Acme", "Hi Rita,\n\nBest,\nJane",
                     attach_resume=False).model_copy(update={
                         "status": "scheduled", "send_at": earlier})
        for index in range(3)
    ]
    outreach_store.save(state)

    assert len(sendlater.tick()) == 1
    assert len(sent) == 1
    assert len(sendlater.tick()) == 1
    assert len(sent) == 2


def test_the_clock_does_not_start_when_it_is_switched_off(monkeypatch):
    monkeypatch.setenv(schedule.ENV, "0")
    assert sendlater.start() is False


# --- every submitted application, not ten of them -------------------------

def test_the_backfill_takes_every_submitted_application_in_turn(tmp_path, monkeypatch):
    """No cap and no button: the person asked for all of them, newest first."""
    from server import followqueue

    ids = []
    for index in range(4):
        # Distinct `added_at` on purpose: four rows added in the same second
        # share one, and then "newest first" has nothing to sort on.
        job, _ = queue.add(Job(url=f"https://jobs.example.com/{index}", title="Role",
                               company="Acme", status=Status.SUBMITTED,
                               added_at=f"2026-10-0{index + 1}T12:00:00+00:00"))
        folder = tmp_path / "applications" / f"job-{index}"
        folder.mkdir(parents=True)
        queue.update(job.id, app_dir=str(folder), status=Status.SUBMITTED)
        ids.append(job.id)

    assert followqueue.waiting() == list(reversed(ids))     # newest first

    prepared = []
    monkeypatch.setattr("outreach.follow.start",
                        lambda job_id, force=False: prepared.append(job_id) or True)
    monkeypatch.setattr("outreach.follow.wait", lambda job_id, timeout=0: None)
    # One a tick, and the job drops off the list once it has a status.
    for job_id in list(reversed(ids)):
        assert followqueue.tick() == job_id
        state = outreach_store.get(job_id)
        state.prepare_status = "ready"
        outreach_store.save(state)
    assert prepared == list(reversed(ids))
    assert followqueue.waiting() == []
    assert followqueue.tick() == ""                         # nothing left to do


def test_the_backfill_can_be_paused_because_it_spends(tmp_path, monkeypatch):
    from server import followqueue, settings

    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    settings.save(outreach_backfill=False)
    assert followqueue.enabled() is False
    assert followqueue.tick() == ""
    settings.save(outreach_backfill=True)
    assert followqueue.enabled() is True


def test_a_job_the_person_is_done_with_leaves_the_group(tmp_path):
    """"I click done maybe and they go under submitted like earlier."""
    job, draft, _ = application(tmp_path)
    client = TestClient(app)
    answer = client.post(f"/review/{job.id}/outreach/done", json={"on": True}).json()
    assert answer["done_at"]
    assert outreach_store.summaries()[job.id]["done"] is True

    # And it can be reopened, because done is a decision and not a deletion.
    answer = client.post(f"/review/{job.id}/outreach/done", json={"on": False}).json()
    assert answer["done_at"] is None
    assert outreach_store.summaries()[job.id]["done"] is False


# --- their own Gmail and LinkedIn, wired in -------------------------------

def test_an_approved_email_becomes_a_draft_in_their_own_gmail(tmp_path, monkeypatch):
    """The address a follow-up leaves from is the whole point of this path.

    Gmail stamps the account that logged in onto every message, and no app
    password exists for the university account, so SMTP can never send as it
    (measured live on 2026-11-03). Their own Gmail tool is already signed in
    as that address, so an approved email is written there instead.
    """
    from outreach import gmailthing

    job, draft, contact = application(tmp_path)
    monkeypatch.setenv(gmailthing.ENV, "1")
    asked = {}

    def fake(to, subject, body, attachment=None):
        asked.update(to=to, subject=subject, attachment=attachment)
        return {"account": "them@usc.edu", "draft_id": "r99", "attached": True}

    monkeypatch.setattr(gmailthing, "enabled", lambda: True)
    monkeypatch.setattr(gmailthing, "draft", fake)

    answer = TestClient(app).post(
        f"/review/{job.id}/outreach/draft/{draft.id}/send",
        json={"confirmed": True}).json()

    row = answer["drafts"][0]
    assert row["status"] == "in_gmail"
    assert row["gmail_account"] == "them@usc.edu" and row["gmail_draft_id"] == "r99"
    assert asked["to"] == contact.email
    # The fixture fails the test if anything reached SMTP, so nothing did:
    # this path creates a draft and sends nothing by itself.


def test_the_one_press_sends_the_emails_and_the_invites(tmp_path, monkeypatch):
    """"once i approve then you can start and then send completely."

    One press per job, and it is the only way either tool is reached: no
    timer, no verdict, no submission path leads here.
    """
    from outreach import gmailthing, linkedinthing
    from outreach.models import Invite
    from server import outreach as route

    job, draft, contact = application(tmp_path)
    state = outreach_store.get(job.id)
    state.contacts[0] = state.contacts[0].model_copy(update={
        "linkedin_url": "https://www.linkedin.com/in/rita/"})
    state.invites = [Invite.create(contact.id, "https://www.linkedin.com/in/rita/",
                                   "Hi Rita, USC MS CS student graduating in December.")]
    outreach_store.save(state)

    sent, invited = [], []
    monkeypatch.setattr(gmailthing, "enabled", lambda: True)
    monkeypatch.setattr(gmailthing, "draft", lambda to, s, b, attachment=None: {
        "account": "them@usc.edu", "draft_id": "r1", "attached": True})
    monkeypatch.setattr(gmailthing, "send", lambda rows: sent.append(rows) or {
        "sent": len(rows), "recipients": [row["to"] for row in rows], "heldBack": 0})
    monkeypatch.setattr(linkedinthing, "enabled", lambda: True)
    monkeypatch.setattr(linkedinthing, "invite", lambda rows: invited.append(rows) or {
        "items": [{"profileUrl": rows[0]["profileUrl"], "status": "sent"}], "skipped": []})

    client = TestClient(app)
    assert client.post(f"/review/{job.id}/outreach/go", json={}).status_code == 409
    client.post(f"/review/{job.id}/outreach/go", json={"confirmed": True})
    # The work is on a thread, because their tools pace themselves.
    for _ in range(100):
        if not route.going(job.id):
            break
        import time as _t
        _t.sleep(0.05)

    after = outreach_store.get(job.id)
    assert after.drafts[0].status == "sent"
    assert after.invites[0].status == "sent"
    assert sent and sent[0][0]["to"] == contact.email
    assert invited and invited[0][0]["note"].startswith("Hi Rita")


def test_a_guessed_address_holds_the_whole_press_back(tmp_path, monkeypatch):
    """The tick is not waived by approving the job as a whole."""
    from outreach import gmailthing, linkedinthing
    from server import outreach as route

    job, _, _ = application(tmp_path, verified=False)
    monkeypatch.setattr(gmailthing, "enabled", lambda: True)
    monkeypatch.setattr(gmailthing, "draft", lambda *a, **k: pytest.fail("must not draft"))
    monkeypatch.setattr(gmailthing, "send", lambda rows: pytest.fail("must not send"))
    monkeypatch.setattr(linkedinthing, "enabled", lambda: False)

    TestClient(app).post(f"/review/{job.id}/outreach/go", json={"confirmed": True})
    for _ in range(100):
        if not route.going(job.id):
            break
        import time as _t
        _t.sleep(0.05)
    after = outreach_store.get(job.id)
    assert after.drafts[0].status == "draft"
    assert "tick" in after.error
