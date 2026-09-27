"""The review page's Submit button: which control it aims at, and what it
says when the form is still on the screen afterwards.

No browser here. `browser.press_submit.choose` and `confirmed` are the two
decisions the press makes, and both are pure; the endpoint is exercised with
the presser stubbed, because the point of the test is the bookkeeping either
way, not the mouse event.
"""

from __future__ import annotations

import pytest

from browser import press_submit


def control(text, **kw):
    base = {"text": text, "tag": "button", "type": "", "disabled": False,
            "shown": True, "in_form": True, "x": 10.0, "y": 20.0, "top": 100.0}
    base.update(kw)
    return base


def test_the_form_s_own_submit_wins_over_a_header_apply():
    page = [control("Apply", in_form=False, top=10.0),
            control("Submit application", type="submit", top=900.0)]
    assert press_submit.choose(page)["text"] == "Submit application"


def test_a_cancel_or_a_save_for_later_is_never_pressed():
    for label in ("Cancel", "Save draft", "Back", "Sign in"):
        assert press_submit.choose([control(label)]) is None


def test_a_hidden_or_disabled_control_is_not_pressed():
    assert press_submit.choose([control("Submit", shown=False)]) is None
    assert press_submit.choose([control("Submit", disabled=True)]) is None


def test_nothing_on_the_page_reads_as_submit():
    assert press_submit.choose([]) is None


def test_the_last_step_wins_on_a_multi_step_form():
    page = [control("Submit", top=100.0), control("Submit", top=800.0)]
    assert press_submit.choose(page)["top"] == 800.0


def test_a_confirmation_needs_the_words_and_the_form_gone():
    thanks = "Thank you for applying. We have received your application."
    assert press_submit.confirmed({"controls": 0, "text": thanks})
    # The same words with the form still there is a posting, not a receipt.
    assert not press_submit.confirmed({"controls": 12, "text": thanks})
    assert not press_submit.confirmed({"controls": 0, "text": "Please fix the errors below."})


# -- the endpoint --------------------------------------------------------

import server.review as review  # noqa: E402
from archive import store  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from server import corrections, queue, runner  # noqa: E402
from server.app import app  # noqa: E402
from server.models import Job, Status  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    qpath = tmp_path / "queue.json"
    monkeypatch.setattr(queue, "QUEUE_PATH", qpath)
    monkeypatch.setattr(queue, "LOCK_PATH", qpath.with_suffix(".lock"))
    monkeypatch.setattr(store, "APPLICATIONS", tmp_path / "applications")
    monkeypatch.setattr(store, "INDEX_PATH", tmp_path / "applications" / "index.csv")
    monkeypatch.setattr(review, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(runner, "AUTOFILL_ENABLED", False)
    monkeypatch.setattr(runner, "_fills", {})
    monkeypatch.setattr(runner, "_pgrep_fill", lambda job_id: None)
    monkeypatch.setattr(corrections, "capture", lambda job_url, app_dir: {"captured": False})
    monkeypatch.setattr(corrections, "changes", lambda app_dir: [])
    monkeypatch.setattr(review.chrome, "close_tab",
                        lambda target_id, port_number=None: pytest.fail("the button closed a tab"))


@pytest.fixture
def client():
    return TestClient(app)


def filled_job(target: str = "TAB1") -> tuple[str, object]:
    """A job whose form is filled and whose tab is still open."""
    job, _ = queue.add(Job(url="https://boards.example.com/jobs/1",
                           title="Backend Engineer", company="Example Corp"))
    app_dir = store.create(job)
    store.write(app_dir, "resume.pdf", b"%PDF-1.5 fake")
    store.set_status(app_dir, Status.FILLED)
    queue.update(job.id, status=Status.FILLED, app_dir=str(app_dir))
    review._form_tab_for_test = target
    return job.id, app_dir


@pytest.fixture(autouse=True)
def tab(monkeypatch):
    """The form's tab, as `_form_tab` would find it."""
    monkeypatch.setattr(review, "_form_tab",
                        lambda job, app_dir: getattr(review, "_form_tab_for_test", "TAB1"))


def test_a_confirmed_press_submits_the_job(client, monkeypatch):
    monkeypatch.setattr(press_submit, "press_now", lambda cdp, target: {
        "pressed": "Submit application", "confirmed": True,
        "text": "Thank you for applying"})
    job_id, app_dir = filled_job()
    out = client.post(f"/review/{job_id}/submit").json()
    assert out["submitted"] is True and out["status"] == "submitted"
    assert queue.get(job_id).status is Status.SUBMITTED
    assert "Submit application" in (app_dir / "status.json").read_text()


def test_a_form_still_on_the_screen_is_not_a_submission(client, monkeypatch):
    monkeypatch.setattr(press_submit, "press_now", lambda cdp, target: {
        "pressed": "Submit", "confirmed": False, "text": ""})
    job_id, _ = filled_job()
    out = client.post(f"/review/{job_id}/submit").json()
    assert out["submitted"] is False
    assert "still on the screen" in out["detail"]
    assert queue.get(job_id).status is Status.FILLED


def test_no_submit_control_leaves_the_job_where_it_was(client, monkeypatch):
    def refuse(cdp, target):
        raise press_submit.NoSubmitControl("no submit button found on the form")
    monkeypatch.setattr(press_submit, "press_now", refuse)
    job_id, _ = filled_job()
    res = client.post(f"/review/{job_id}/submit")
    assert res.status_code == 409
    assert queue.get(job_id).status is Status.FILLED


def test_a_closed_tab_is_refused_before_anything_is_pressed(client, monkeypatch):
    monkeypatch.setattr(review, "_form_tab", lambda job, app_dir: "")
    monkeypatch.setattr(press_submit, "press_now",
                        lambda cdp, target: pytest.fail("pressed with no tab"))
    job_id, _ = filled_job()
    res = client.post(f"/review/{job_id}/submit")
    assert res.status_code == 409 and "tab is not open" in res.json()["detail"]


def test_only_a_filled_job_can_be_submitted(client, monkeypatch):
    monkeypatch.setattr(press_submit, "press_now",
                        lambda cdp, target: pytest.fail("pressed a job that is not filled"))
    job, _ = queue.add(Job(url="https://boards.example.com/jobs/2", title="X", company="Y"))
    app_dir = store.create(job)
    store.set_status(app_dir, Status.AWAITING_REVIEW)
    queue.update(job.id, status=Status.AWAITING_REVIEW, app_dir=str(app_dir))
    assert client.post(f"/review/{job.id}/submit").status_code == 409
