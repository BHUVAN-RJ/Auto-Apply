"""The server's own look at a filled form: form state kept while the
form is there, submitted when the page has become a confirmation, and
never on a posting's "thank you for your interest"."""
import json

from server import corrections, watch
from server.models import Job, Status


def test_a_confirmation_is_the_phrase_with_no_form_left():
    assert watch.looks_submitted({"fields": {}, "text": "Acme\nThank you for applying! We will be in touch."}) \
        .startswith("Acme Thank you for applying")
    # The phrase on a page that is still a form is the posting's own text.
    form = {"First Name": "", "Last Name": "", "Email": "", "Phone": ""}
    assert watch.looks_submitted({"fields": form, "text": "Thank you for your interest in Acme. Apply below."}) is None
    assert watch.looks_submitted({"fields": {}, "text": "Careers at Acme"}) is None


def test_look_at_keeps_the_form_state_then_marks_submitted(tmp_path, monkeypatch):
    app = tmp_path / "acme_swe"
    app.mkdir()
    (app / corrections.FILL_REPORT).write_text(json.dumps({"target_id": "TAB1", "after_agent": {"Location": "Delhi"}}))
    job = Job(url="https://boards.greenhouse.io/acme/jobs/1", status=Status.FILLED, app_dir=str(app))
    monkeypatch.setattr(watch.forms, "find_target", lambda url, job_url, saved: "TAB1")
    monkeypatch.setattr(watch.forms, "same_site", lambda url, target, job_url: True)
    looks = [
        {"fields": {"First Name": "J", "Last Name": "D", "Email": "j@d", "Location": "Los Angeles"},
         "meta": {"Location": {"id": "loc"}}, "text": "Apply", "url": job.url},
        {"fields": {}, "meta": {}, "text": "Thank you for applying to Acme.", "url": job.url + "/confirmation"},
    ]

    async def snapshot(url, target, detail=False):
        return looks.pop(0)
    monkeypatch.setattr(watch.forms, "snapshot", snapshot)
    marked = []

    def mark_seen(job_, app_dir, url, quote, by):
        marked.append((job_.id, url, quote, by))
        return {"marked": True}
    import server.review as review
    monkeypatch.setattr(review, "mark_seen", mark_seen)

    first = watch.look_at(job, app)
    assert first == {"looked": True, "captured": 4}
    state = json.loads((app / corrections.FORM_STATE).read_text())
    assert state["fields"]["Location"] == "Los Angeles" and state["meta"]["Location"]["id"] == "loc"

    second = watch.look_at(job, app)
    assert second["submitted"] and "Thank you for applying" in second["quote"]
    assert marked[0][1].endswith("/confirmation") and marked[0][3] == "by the server's watch"


def test_a_closed_tab_is_skipped_never_submitted(tmp_path, monkeypatch):
    app = tmp_path / "a"
    app.mkdir()
    (app / corrections.FILL_REPORT).write_text(json.dumps({"target_id": "GONE"}))
    job = Job(url="https://jobs.lever.co/acme/1", status=Status.FILLED, app_dir=str(app))
    monkeypatch.setattr(watch.forms, "find_target", lambda *a: "")
    assert watch.look_at(job, app) == {"looked": False, "reason": "tab not open"}


def test_the_watch_is_off_by_env(monkeypatch):
    monkeypatch.setenv(watch.ENV, "0")
    assert watch.start() is False
