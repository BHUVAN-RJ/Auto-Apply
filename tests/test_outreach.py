"""Contact lookup, drafting, carousel state, attachment and send gate."""

from __future__ import annotations

import json
from email.message import EmailMessage
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import mailing
from archive import store as archive
from outreach import apollo, jobright
from outreach import store as outreach_store
from outreach.models import Contact, Draft
from server import queue
from server.app import app
from server.models import Job, Status
from tailor import outreach as writer
from tailor.answers import Context


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    qpath = tmp_path / "queue.json"
    monkeypatch.setattr(queue, "QUEUE_PATH", qpath)
    monkeypatch.setattr(queue, "LOCK_PATH", qpath.with_suffix(".lock"))
    monkeypatch.setattr(archive, "APPLICATIONS", tmp_path / "applications")
    monkeypatch.setattr(archive, "INDEX_PATH", tmp_path / "applications" / "index.csv")
    monkeypatch.setattr(outreach_store, "STATE", tmp_path / "outreach.json")
    monkeypatch.setattr(outreach_store, "CACHE", tmp_path / "outreach_cache.json")
    monkeypatch.delenv(apollo.API_KEY, raising=False)
    monkeypatch.delenv(mailing.USER, raising=False)
    monkeypatch.delenv(mailing.PASS, raising=False)


def application(tmp_path: Path):
    job, _ = queue.add(Job(
        url="https://jobs.example.com/42", title="Machine Learning Engineer",
        company="Acme", status=Status.APPROVED,
    ))
    folder = tmp_path / "applications" / job.folder_name()
    folder.mkdir(parents=True)
    (folder / "posting.md").write_text("# Machine Learning Engineer\nBuild production ML systems.")
    (folder / "resume.tex").write_text("\\documentclass{article}\\begin{document}Jane Doe\\end{document}")
    (folder / "resume.pdf").write_bytes(b"%PDF outreach resume")
    (folder / "cover_letter.md").write_text("Dear Hiring Manager,\n\nProof.\n\nSincerely,\nJane Doe\n")
    queue.update(job.id, app_dir=str(folder), status=Status.APPROVED)
    return job, folder


class Response:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status
        self.text = json.dumps(data)

    def json(self):
        return self.data


class ApolloHttp:
    def __init__(self):
        self.calls = []
        self.obfuscated = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url == apollo.SEARCH_URL:
            if self.obfuscated:
                return Response({"people": [{
                    "id": "abc123", "first_name": "Rita",
                    "last_name_obfuscated": "R***a",
                    "title": "Technical Recruiter",
                    "organization": {"name": "Acme"},
                    "has_email": True,
                }]})
            return Response({"people": [{
                "name": "Rita Recruiter", "title": "Technical Recruiter",
                "linkedin_url": "https://linkedin.com/in/rita",
                "organization": {"name": "Acme"},
                "has_email": True,
            }]})
        return Response({"person": {
            "email": "rita@acme.com", "email_status": "verified",
        }})

    def close(self):
        pass


def test_apollo_search_keeps_obfuscated_api_results(monkeypatch):
    monkeypatch.setenv(apollo.API_KEY, "apollo-key-" + "x" * 30)
    http = ApolloHttp()
    http.obfuscated = True
    contacts = apollo.search("Acme", "recruiter", client=http)
    assert len(contacts) == 1
    assert contacts[0].name.startswith("Rita")
    assert contacts[0].apollo_id == "abc123"
    assert not contacts[0].linkedin_url


def test_apollo_search_then_enrich_spends_only_on_the_selected_contact(monkeypatch):
    monkeypatch.setenv(apollo.API_KEY, "apollo-key-" + "x" * 30)
    http = ApolloHttp()
    contacts = apollo.search("Acme", "recruiter", client=http)
    assert len(contacts) == 1 and not contacts[0].email
    assert http.calls[0][0] == apollo.SEARCH_URL
    assert "reveal_personal_emails" not in http.calls[0][1].get("json", {})

    enriched = apollo.enrich(contacts[0], client=http)
    assert enriched.email == "rita@acme.com"
    assert enriched.verification == "verified"
    assert http.calls[1][0] == apollo.ENRICH_URL
    assert http.calls[1][1]["params"]["reveal_personal_emails"] == "false"


def test_jobright_requires_linkedin_and_an_open_finder(monkeypatch):
    contact = Contact.create("recruiter", "Rita")
    with pytest.raises(jobright.JobrightError, match="LinkedIn"):
        import asyncio
        asyncio.run(jobright.lookup(contact))
    contact = Contact.create(
        "recruiter", "Rita", linkedin_url="https://linkedin.com/in/rita",
    )
    monkeypatch.setattr(jobright, "_jobright_targets", lambda: [])
    with pytest.raises(jobright.JobrightError, match="Open Jobright"):
        import asyncio
        asyncio.run(jobright.lookup(contact))


def test_outreach_writer_retries_bad_shape(monkeypatch):
    good = """```subject
ML platform role at Acme
```
```email
Hi Rita,

I applied for Acme's Machine Learning Engineer role. At Koantek, I led a forecasting system from data selection through deployment, reaching 9.1 percent MAPE on more than 300,000 records. That production ownership maps closely to the platform work in this role.

Would you be open to pointing me toward the right person on the hiring team? I attached my tailored resume for context.

Best,
Jane Doe
```"""
    replies = iter(["not the requested shape", good])
    monkeypatch.setattr(writer.llm, "complete", lambda *args, **kwargs: next(replies))
    monkeypatch.setattr(writer.llm, "tailor_model", lambda: "test/model")
    context = Context(
        posting="Machine Learning Engineer at Acme",
        resume_tex="Koantek forecasting, 300,000 records, 9.1 percent MAPE",
    )
    result = writer.write(
        context,
        Contact.create("recruiter", "Rita", email="rita@acme.com"),
        "Machine Learning Engineer", "Acme", "Jane Doe",
    )
    assert result.attempts == 2 and result.subject == "ML platform role at Acme"
    assert result.body.endswith("Jane Doe")


def test_mail_message_has_external_recipient_and_tailored_pdf(tmp_path, monkeypatch):
    monkeypatch.setenv(mailing.USER, "me@gmail.com")
    monkeypatch.setenv(mailing.PASS, "app-password")
    resume = tmp_path / "Jane_Doe_Resume.pdf"
    resume.write_bytes(b"%PDF")
    msg = mailing.message(
        "rita@acme.com", "Acme ML role", "Hello", attachments=[resume],
    )
    assert msg["To"] == "rita@acme.com"
    attachments = list(msg.iter_attachments())
    assert len(attachments) == 1 and attachments[0].get_filename() == resume.name
    assert attachments[0].get_content_type() == "application/pdf"


def test_review_flow_requires_verified_guess_and_sends_once(tmp_path, monkeypatch):
    job, folder = application(tmp_path)
    client = TestClient(app)
    added = client.post(f"/review/{job.id}/outreach/contact", json={
        "role": "recruiter", "name": "Rita", "email": "rita@acme.com",
        "linkedin_url": "https://linkedin.com/in/rita",
    })
    assert added.status_code == 200
    contact = added.json()["contacts"][0]
    draft = Draft.create(
        contact["id"], "Acme ML platform role",
        "Hi Rita,\n\nA grounded note with enough context for this test.\n\nBest,\nJane Doe",
        resume_folder=folder.name,
    )
    monkeypatch.setattr(writer, "draft", lambda *args, **kwargs: draft)
    made = client.post(f"/review/{job.id}/outreach/draft", json={
        "contact_id": contact["id"], "attach_resume": True,
    })
    assert made.status_code == 200
    draft_id = made.json()["drafts"][0]["id"]

    monkeypatch.setenv(mailing.USER, "me@gmail.com")
    monkeypatch.setenv(mailing.PASS, "app-password")
    sent = []

    def send_message(message: EmailMessage):
        sent.append(message)
        return str(message["Message-ID"])

    monkeypatch.setattr(mailing, "send_message", send_message)
    endpoint = f"/review/{job.id}/outreach/draft/{draft_id}/send"
    assert client.post(endpoint, json={"confirmed": True}).status_code == 409
    result = client.post(endpoint, json={
        "confirmed": True, "verified_by_user": True,
    })
    assert result.status_code == 200
    assert result.json()["drafts"][0]["status"] == "sent"
    assert len(sent) == 1 and list(sent[0].iter_attachments())
    assert len(list(folder.glob("outreach_*_sent.json"))) == 1
    assert client.post(endpoint, json={
        "confirmed": True, "verified_by_user": True,
    }).status_code == 409


def test_review_page_places_an_email_carousel_after_the_cover_letter():
    html = (Path(__file__).resolve().parent.parent / "review" / "index.html").read_text()
    assert "const outreachDoc = outreachHtml(id, outreach);" in html
    assert "${outreachDoc}` : \"\"}" in html
    assert html.index("<div class=\"docs\">${resumeDoc}${letterDoc}</div>") < html.index("${outreachDoc}")
    assert "outreachPrev" in html and "outreachNext" in html
    assert "data-outreach-slide" in html and "outreachKeyHandler" in html
    assert "outreachPickAll" in html and "outreachWriteSelected" in html
    assert "/outreach/drafts" in html


def test_selected_contacts_write_one_carousel_and_never_send(tmp_path, monkeypatch):
    job, folder = application(tmp_path)
    client = TestClient(app)
    rita = client.post(f"/review/{job.id}/outreach/contact", json={
        "role": "recruiter", "name": "Rita", "email": "rita@acme.com",
    }).json()["contacts"][0]
    morgan = client.post(f"/review/{job.id}/outreach/contact", json={
        "role": "hiring_manager", "name": "Morgan", "email": "morgan@acme.com",
    }).json()["contacts"][1]
    drafts = iter([
        Draft.create(rita["id"], "Acme recruiter note",
                     "Hi Rita,\n\nA grounded recruiter note for this test.\n\nBest,\nJane Doe",
                     resume_folder=folder.name),
        Draft.create(morgan["id"], "Acme manager note",
                     "Hi Morgan,\n\nA grounded manager note for this test.\n\nBest,\nJane Doe",
                     resume_folder=folder.name),
    ])
    monkeypatch.setattr(writer, "draft", lambda *args, **kwargs: next(drafts))
    sent = []
    monkeypatch.setattr(mailing, "send_message", lambda message: sent.append(message))
    made = client.post(f"/review/{job.id}/outreach/drafts", json={
        "contact_ids": [rita["id"], morgan["id"]], "attach_resume": True,
    })
    assert made.status_code == 200
    bodies = made.json()["drafts"]
    assert len(bodies) == 2
    assert {row["contact_id"] for row in bodies} == {rita["id"], morgan["id"]}
    assert all(row["status"] == "draft" for row in bodies)
    assert sent == []
    assert client.post(f"/review/{job.id}/outreach/drafts", json={
        "contact_ids": [],
    }).status_code == 400
