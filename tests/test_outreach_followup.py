"""The follow-up that prepares itself once a job has been submitted.

Everything up to the click is automation and is tested here; the click is
not, so every one of these asserts that nothing was sent.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

# Imported at module scope on purpose: `apply` loads the clone's .env when it
# is first imported, which would hand a real Apollo key back to a test that
# has just deleted it. Importing here means that happens at collection, well
# before any fixture.
import apply as apply_script  # noqa: F401
import mailing
from archive import store as archive
from outreach import apollo, follow, jobright
from outreach import people as webpeople
from outreach import store as outreach_store
from outreach.models import Contact, Draft
from server import queue
from server.models import Job, Status
from tailor import outreach as writer
from tailor.answers import Context

KEY = "apollo-key-" + "x" * 30


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
    monkeypatch.delenv(follow.ENV, raising=False)
    # The profile the drafts are grounded in is not what these tests measure,
    # and reading it for real would reach the person's own stories.
    monkeypatch.setattr(
        follow, "build_context",
        lambda app_dir: Context(posting="A posting", resume_tex="\\documentclass{article}"),
    )
    # Delivery is not automation: if any of this reaches SMTP, fail loudly.
    monkeypatch.setattr(
        mailing, "send_message",
        lambda message: pytest.fail("the automatic follow-up must never send"),
    )
    follow._busy.clear()
    follow._threads.clear()


def application(tmp_path: Path):
    job, _ = queue.add(Job(
        url="https://jobs.example.com/42", title="Machine Learning Engineer",
        company="Acme", status=Status.SUBMITTED,
    ))
    folder = tmp_path / "applications" / job.folder_name()
    folder.mkdir(parents=True)
    (folder / "posting.md").write_text("# Machine Learning Engineer\nBuild ML systems.")
    (folder / "resume.tex").write_text("\\documentclass{article}\\begin{document}x\\end{document}")
    (folder / "resume.pdf").write_bytes(b"%PDF tailored resume")
    (folder / "cover_letter.md").write_text("Dear Hiring Manager,\n\nProof.\n\nBest,\nJane Doe\n")
    queue.update(job.id, app_dir=str(folder), status=Status.SUBMITTED)
    return queue.get(job.id), folder


class Drafter:
    """`tailor.outreach.draft` with the model taken out, counting its calls."""

    def __init__(self):
        self.calls = []

    def __call__(self, context, contact, job_title, company, candidate_name,
                 attach_resume=True, resume_folder="", model=None):
        self.calls.append(contact.id)
        return Draft.create(
            contact.id, f"{company} role for {contact.name}",
            f"Hi {contact.name},\n\nA grounded note.\n\nBest,\n{candidate_name}",
            attach_resume=attach_resume, resume_folder=resume_folder,
        )


class Apollo:
    """Apollo with the network taken out, counting search and enrich calls."""

    def __init__(self, people=None):
        self.searched, self.enriched = 0, []
        self.people = people if people is not None else [
            Contact.create("recruiter", "Rita", title="Technical Recruiter",
                           company="Acme", linkedin_url="https://example.com/in/rita",
                           source="apollo"),
            Contact.create("hiring_manager", "Morgan", title="Engineering Manager",
                           company="Acme", linkedin_url="https://example.com/in/morgan",
                           source="apollo"),
        ]

    def discover(self, company, per_role=3, client=None):
        self.searched += 1
        return list(self.people)

    def enrich(self, contact, client=None):
        self.enriched.append(contact.id)
        local = contact.name.lower()
        return contact.model_copy(update={
            "email": f"{local}@acme.test", "verification": "verified",
            "email_status": "verified", "source": "apollo",
        })


def wire(monkeypatch, apollo_stub=None, drafter=None):
    drafter = drafter or Drafter()
    if apollo_stub is not None:
        monkeypatch.setenv(apollo.API_KEY, KEY)
        monkeypatch.setattr(apollo, "discover", apollo_stub.discover)
        monkeypatch.setattr(apollo, "enrich", apollo_stub.enrich)
    monkeypatch.setattr(writer, "draft", drafter)
    return drafter


def test_a_submitted_job_prepares_its_own_drafts_and_sends_nothing(tmp_path, monkeypatch):
    job, _ = application(tmp_path)
    people = Apollo()
    drafter = wire(monkeypatch, people)

    assert follow.start(job.id) is True
    follow.wait(job.id)

    state = outreach_store.get(job.id)
    assert state.prepare_status == "ready"
    assert len(state.drafts) == 2
    assert {row.status for row in state.drafts} == {"draft"}
    assert len(drafter.calls) == 2
    assert state.active_draft
    # The job itself is untouched: the follow-up is not a stage of the application.
    assert queue.get(job.id).status == Status.SUBMITTED


def test_the_tailored_resume_is_attached_to_every_draft(tmp_path, monkeypatch):
    job, folder = application(tmp_path)
    wire(monkeypatch, Apollo())

    follow.start(job.id)
    follow.wait(job.id)

    state = outreach_store.get(job.id)
    assert state.drafts and all(row.attach_resume for row in state.drafts)
    for row in state.drafts:
        assert row.resume_folder == folder.name
        assert row.attachment_name.endswith(".pdf")
        assert (folder / row.attachment_name).is_file()
    assert "resume attached" in state.lookup_note


def test_preparing_the_same_job_twice_writes_nothing_twice(tmp_path, monkeypatch):
    job, _ = application(tmp_path)
    people = Apollo()
    drafter = wire(monkeypatch, people)

    follow.start(job.id)
    follow.wait(job.id)
    first = [row.id for row in outreach_store.get(job.id).drafts]

    # The ordinary second trigger does nothing at all.
    assert follow.start(job.id) is False
    # Even the person's own "Prepare again" writes no second email and spends
    # no second credit on somebody who already has one waiting.
    assert follow.start(job.id, force=True) is True
    follow.wait(job.id)

    state = outreach_store.get(job.id)
    assert [row.id for row in state.drafts] == first
    assert len(drafter.calls) == 2
    assert len(people.enriched) == 2


def test_without_an_apollo_key_the_address_is_a_labelled_guess(tmp_path, monkeypatch):
    job, _ = application(tmp_path)
    drafter = wire(monkeypatch)                      # no Apollo key at all
    known = Contact.create("recruiter", "Rita", company="Acme",
                           linkedin_url="https://example.com/in/rita")
    outreach_store.add_contacts(job.id, [known])

    def guess(contact):
        return contact.model_copy(update={
            "email": "rita@acme.test", "source": "jobright",
            "verification": "guessed", "email_status": "Jobright guess",
        })

    monkeypatch.setattr(jobright, "lookup", lambda contact: _resolved(guess(contact)))

    follow.start(job.id)
    follow.wait(job.id)

    state = outreach_store.get(job.id)
    assert state.prepare_status == "ready"
    assert len(drafter.calls) == 1
    assert state.contact(known.id).verification == "guessed"
    # The draft exists and is unsent, and the card says why the address is a guess.
    assert state.drafts[0].status == "draft"
    assert state.drafts[0].verified_by_user is False
    assert "Apollo is not connected" in state.lookup_note
    assert "guess" in state.lookup_note


def test_an_apollo_failure_lands_on_the_card_and_moves_no_job(tmp_path, monkeypatch):
    job, _ = application(tmp_path)
    wire(monkeypatch)
    monkeypatch.setenv(apollo.API_KEY, KEY)

    def angry(company, per_role=3, client=None):
        raise apollo.ApolloError("Apollo search answered 429: slow down")

    monkeypatch.setattr(apollo, "discover", angry)

    assert follow.start(job.id) is True
    follow.wait(job.id)

    state = outreach_store.get(job.id)
    assert state.prepare_status == "failed"
    assert "429" in state.error and "429" in state.lookup_note
    assert state.drafts == []
    assert queue.get(job.id).status == Status.SUBMITTED


def test_the_switch_turns_the_whole_thing_off(tmp_path, monkeypatch):
    job, _ = application(tmp_path)
    wire(monkeypatch, Apollo())
    monkeypatch.setenv(follow.ENV, "0")

    assert follow.enabled() is False
    assert follow.start(job.id) is False
    assert outreach_store.get(job.id).drafts == []


def test_the_trigger_never_raises_into_the_submission(tmp_path, monkeypatch):
    wire(monkeypatch, Apollo())
    # No such job, so the work cannot be done; the trigger still answers and
    # the reason lands on the state rather than on the caller.
    follow.start("0000")
    follow.wait("0000")
    assert follow.prepare("0000").prepare_status == "failed"

    job, folder = application(tmp_path)
    queue.update(job.id, app_dir=str(folder / "gone"))
    state = follow.prepare(job.id)
    assert state.prepare_status == "failed"
    assert "folder is missing" in state.error


def test_fewer_people_than_asked_for_is_said_not_padded(tmp_path, monkeypatch):
    job, _ = application(tmp_path)
    only_one = Apollo(people=[Contact.create(
        "recruiter", "Rita", company="Acme",
        linkedin_url="https://example.com/in/rita", source="apollo",
    )])
    wire(monkeypatch, only_one)

    follow.start(job.id)
    follow.wait(job.id)

    state = outreach_store.get(job.id)
    assert len(state.drafts) == 1
    assert f"Only 1 of {follow.MAX_CONTACTS}" in state.lookup_note


def test_at_most_three_people_are_written_to(tmp_path, monkeypatch):
    job, _ = application(tmp_path)
    crowd = Apollo(people=[
        Contact.create("recruiter", f"Rita {index}", company="Acme",
                       linkedin_url=f"https://example.com/in/rita{index}", source="apollo")
        for index in range(4)
    ] + [Contact.create("hiring_manager", "Morgan", company="Acme",
                        linkedin_url="https://example.com/in/morgan", source="apollo")])
    drafter = wire(monkeypatch, crowd)

    follow.start(job.id)
    follow.wait(job.id)

    state = outreach_store.get(job.id)
    # One recruiter per account, so the three slots cannot be filled by four
    # recruiters at one employer: this pick is Rita 0 and Morgan, and the cap
    # as an equality is asserted where it can be reached, below.
    assert len(state.drafts) == 2
    assert len(drafter.calls) == len(state.drafts)
    assert len(crowd.enriched) == len(state.drafts)
    # A manager is worth more than a second recruiter, so one holds the slot.
    roles = [state.contact(row.contact_id).role for row in state.drafts]
    assert "hiring_manager" in roles
    assert roles.count("recruiter") == follow.RECRUITERS


def test_nothing_in_the_automatic_path_can_deliver_mail():
    source = (Path(__file__).resolve().parent.parent / "outreach" / "follow.py").read_text()
    assert "import mailing" not in source and "from mailing" not in source
    assert "smtplib" not in source and "send_message" not in source


async def _resolved(value):
    return value


def test_a_follow_up_email_is_written_by_the_subscription_when_it_is_on(monkeypatch):
    """Three emails a job, out over the person's own name to a named human:
    the one place the cheap tailoring model is the wrong default while the
    subscription is switched on."""
    from server import settings
    from tailor import claudecode, llm

    monkeypatch.delenv("OPENROUTER_OUTREACH_MODEL", raising=False)
    monkeypatch.setattr(settings, "claude_code", lambda: True)
    monkeypatch.setattr(claudecode, "available", lambda: True)
    assert llm.outreach_model() == claudecode.CLAUDE_CODE

    monkeypatch.setattr(claudecode, "available", lambda: False)
    assert llm.outreach_model() == llm.tailor_model()

    monkeypatch.setattr(settings, "claude_code", lambda: False)
    monkeypatch.setattr(claudecode, "available", lambda: True)
    assert llm.outreach_model() == llm.tailor_model()

    monkeypatch.setenv("OPENROUTER_OUTREACH_MODEL", "z-ai/glm-5.3")
    assert llm.outreach_model() == "z-ai/glm-5.3"


def test_the_writer_asks_for_the_outreach_model(monkeypatch):
    """The routing has to be at the writer's own default, not at one caller:
    the review page's Prepare and the submission's own thread both land here."""
    from tailor import outreach as writer

    seen = {}

    def fake(system, user, model=None, **rest):
        seen["model"] = model
        raise writer.OutreachError("stop here")

    monkeypatch.setattr(writer.llm, "complete", fake)
    monkeypatch.setattr(writer.llm, "outreach_model", lambda: "sentinel/for-the-test")
    context = type("C", (), {"posting": "p", "resume_tex": "r",
                             "as_message": lambda self: "m"})()
    contact = type("K", (), {"name": "Dana", "title": "Recruiter",
                             "role": "recruiter", "company": "Acme"})()
    with pytest.raises(writer.OutreachError):
        writer.write(context, contact, "Engineer", "Acme", "Someone")
    assert seen["model"] == "sentinel/for-the-test"


def _person(role: str, name: str, company: str = "Acme") -> Contact:
    return Contact.create(role, name, company=company,
                          linkedin_url=f"https://example.com/in/{name.lower()}",
                          source="apollo")


def test_one_recruiter_per_account_however_many_were_found():
    """The maintainer's own rule: "always do 1 recruiter per account".

    Two recruiters at one employer is the same mailbox twice, which is how a
    first email becomes a thing to apologise for. The slots behind the first
    one belong to a different reader or to nobody.
    """
    picked = follow.choose([_person("recruiter", f"Rita{index}") for index in range(4)])
    assert [row.name for row in picked] == ["Rita0"]

    picked = follow.choose(
        [_person("recruiter", f"Rita{index}") for index in range(4)]
        + [_person("hiring_manager", f"Morgan{index}") for index in range(3)]
    )
    assert len(picked) == follow.MAX_CONTACTS
    assert [row.role for row in picked].count("recruiter") == follow.RECRUITERS
    assert [row.name for row in picked] == ["Rita0", "Morgan0", "Morgan1"]


def test_the_cap_is_still_the_cap_when_the_people_are_there_to_fill_it(tmp_path, monkeypatch):
    """The equality the cap deserves, on a pick the rule above allows: one
    recruiter and the managers behind them, never a fourth person."""
    job, _ = application(tmp_path)
    crowd = Apollo(people=[_person("recruiter", "Rita")]
                   + [_person("hiring_manager", f"Morgan{index}") for index in range(4)])
    drafter = wire(monkeypatch, crowd)

    follow.start(job.id)
    follow.wait(job.id)

    state = outreach_store.get(job.id)
    assert len(state.drafts) == follow.MAX_CONTACTS
    assert len(drafter.calls) == follow.MAX_CONTACTS
    roles = [state.contact(row.contact_id).role for row in state.drafts]
    assert roles.count("recruiter") == follow.RECRUITERS
    assert roles.count("hiring_manager") == follow.MAX_CONTACTS - follow.RECRUITERS


# --- the phase a submitted application is in, as the list reads it ---------

def test_the_store_counts_what_is_waiting_for_a_click(tmp_path):
    """One pass over the file, because the list asks for every row at once."""
    job, folder = application(tmp_path)
    outreach = outreach_store.get(job.id)
    outreach.contacts = [Contact.create("recruiter", "Rita", email="rita@acme.com")]
    outreach.drafts = [
        Draft.create(outreach.contacts[0].id, "One", "Body"),
        Draft.create(outreach.contacts[0].id, "Two", "Body", status="sent"),
        Draft.create(outreach.contacts[0].id, "Three", "Body", status="failed"),
    ]
    outreach.prepare_status = "ready"
    outreach_store.save(outreach)

    row = outreach_store.summaries()[job.id]
    # A failed send is not something to click on, so it is not waiting.
    assert (row["total"], row["sent"], row["unsent"]) == (2, 1, 1)
    assert row["prepare_status"] == "ready"
    assert row["contacts"] == 1


def test_a_submitted_job_carries_its_follow_up_state_on_the_list(tmp_path):
    """Submitting is not the end of the job, so the list has to be able to say so.

    The key is on every submitted row even when nothing has been prepared:
    "prepared nothing" and "never asked" look the same on a missing key, and
    the page needs to tell them apart to offer the press.
    """
    from fastapi.testclient import TestClient

    from server.app import app

    job, folder = application(tmp_path)
    # Deliberately no `with`: the lifespan starts the watch and the scout.
    rows = TestClient(app).get("/jobs").json()
    row = next(item for item in rows if item["id"] == job.id)
    assert row["outreach"] == {"prepare_status": "", "total": 0, "sent": 0,
                               "unsent": 0, "contacts": 0, "note": "", "error": ""}

    outreach = outreach_store.get(job.id)
    outreach.contacts = [Contact.create("recruiter", "Rita", email="rita@acme.com")]
    outreach.drafts = [Draft.create(outreach.contacts[0].id, "One", "Body")]
    outreach.prepare_status = "ready"
    outreach_store.save(outreach)

    rows = TestClient(app).get("/jobs").json()
    row = next(item for item in rows if item["id"] == job.id)
    assert row["outreach"]["unsent"] == 1
    assert row["outreach"]["prepare_status"] == "ready"


def test_the_sweep_prepares_the_jobs_it_is_given_and_nothing_else(tmp_path, monkeypatch):
    """The page names the job ids, because the credits are the person's money."""
    from fastapi.testclient import TestClient

    from server import outreach as route
    from server.app import app

    job, folder = application(tmp_path)
    started: list[str] = []
    monkeypatch.setattr(follow, "start", lambda job_id, force=False: started.append(job_id) or True)
    monkeypatch.setattr(follow, "wait", lambda job_id, timeout=0: None)
    route._sweep.update(running=False, done=0, total=0, job="")

    client = TestClient(app)
    answer = client.post("/review/followups/sweep",
                         json={"job_ids": [job.id, "nosuchjob"]}).json()
    assert answer["started"] is True
    assert answer["total"] == 1
    for _ in range(50):
        if not route._sweep_state()["running"]:
            break
        time.sleep(0.05)
    assert started == [job.id]

    # Nothing with a folder means nothing to do, said rather than pretended.
    assert client.post("/review/followups/sweep", json={"job_ids": ["nope"]}).status_code == 409


# --- free first, paid last ------------------------------------------------
#
# The maintainer's instruction, in their own words: "1st it links to my
# claude and then it tries to find people and then after that fails it then
# asks appolo - just to save credits". So the subscription reads the web
# first and Apollo's credit-spending enrichment is the last thing tried.


def free_web(monkeypatch, found=None, address=None):
    """The web research with the subprocess taken out."""
    monkeypatch.setenv(webpeople.ENV, "1")
    monkeypatch.setattr(webpeople, "available", lambda: True)
    monkeypatch.setattr(webpeople, "find",
                        lambda company, title="", per_role=2: list(found or []))
    monkeypatch.setattr(webpeople, "address", lambda contact: address(contact)
                        if callable(address) else None)


def test_the_web_is_searched_for_people_before_apollo(tmp_path, monkeypatch):
    job, _ = application(tmp_path)
    crowd = Apollo()
    drafter = wire(monkeypatch, crowd)
    rita = Contact.create("recruiter", "Rita Vance", title="Technical Recruiter",
                          company="Acme", linkedin_url="https://www.linkedin.com/in/rita",
                          email="rita.vance@acme.com", source="claude",
                          verification="guessed")
    free_web(monkeypatch, found=[rita])

    follow.start(job.id)
    follow.wait(job.id)

    state = outreach_store.get(job.id)
    assert [row.source for row in state.contacts] == ["claude"]
    assert crowd.searched == 0                  # Apollo was never asked
    assert crowd.enriched == []                 # and no credit was spent
    assert len(drafter.calls) == 1
    assert state.drafts[0].status == "draft"


def test_apollo_searches_only_when_the_web_found_nobody(tmp_path, monkeypatch):
    job, _ = application(tmp_path)
    crowd = Apollo()
    wire(monkeypatch, crowd)
    free_web(monkeypatch, found=[])

    follow.start(job.id)
    follow.wait(job.id)

    state = outreach_store.get(job.id)
    assert crowd.searched == 1
    assert "nobody on the web" in state.lookup_note
    assert state.prepare_status == "ready"


def test_a_free_address_spends_no_apollo_credit(tmp_path, monkeypatch):
    """The whole point of the order: the credit goes on the address, not the search."""
    job, _ = application(tmp_path)
    crowd = Apollo()
    wire(monkeypatch, crowd)
    nameless = Contact.create("recruiter", "Rita Vance", title="Recruiter",
                              company="Acme", source="claude")
    free_web(
        monkeypatch, found=[nameless],
        address=lambda contact: contact.model_copy(update={
            "email": "rita.vance@acme.com", "verification": "guessed",
            "email_status": "found by Claude on https://example.com/team",
        }),
    )

    follow.start(job.id)
    follow.wait(job.id)

    state = outreach_store.get(job.id)
    assert crowd.enriched == []
    assert state.contact(nameless.id).email == "rita.vance@acme.com"
    # Free is never verified, so the send route still wants the person's tick.
    assert state.contact(nameless.id).verification == "guessed"
    assert state.drafts[0].verified_by_user is False


def test_apollo_is_the_last_resort_for_an_address(tmp_path, monkeypatch):
    job, _ = application(tmp_path)
    crowd = Apollo()
    wire(monkeypatch, crowd)
    nameless = Contact.create("recruiter", "Rita", title="Recruiter", company="Acme",
                              linkedin_url="https://example.com/in/rita", source="claude")
    free_web(monkeypatch, found=[nameless], address=lambda contact: None)
    monkeypatch.setattr(
        jobright, "lookup",
        lambda contact: _resolved((_ for _ in ()).throw(jobright.JobrightError("no guess"))),
    )

    follow.start(job.id)
    follow.wait(job.id)

    state = outreach_store.get(job.id)
    assert crowd.enriched == [nameless.id]
    assert state.contact(nameless.id).verification == "verified"
    assert "one credit" in state.lookup_note


# --- what the web answer is allowed to become -----------------------------

def test_a_generic_inbox_is_never_a_person():
    assert webpeople.usable_email("careers@acme.com") == ""
    assert webpeople.usable_email("Recruiting@Acme.com") == ""
    assert webpeople.usable_email(" Rita.Vance@Acme.com ") == "rita.vance@acme.com"
    assert webpeople.usable_email("not an address") == ""


def test_a_person_the_web_could_not_evidence_is_dropped():
    rows = webpeople.parse("""```json
    [{"role": "recruiter", "name": "Rita Vance", "title": "Recruiter",
      "linkedin_url": "https://www.linkedin.com/in/rita"},
     {"role": "hiring_manager", "name": "Nobody"},
     {"role": "ceo", "name": "Wrong Role", "title": "CEO"}]
    ```""")
    assert len(rows) == 3
    kept = [webpeople._contact(row, "Acme") for row in rows]
    # A bare name is not a person found, and only the two outreach roles count.
    assert [row.name for row in kept if row] == ["Rita Vance"]


def test_an_address_the_web_offered_is_still_only_a_guess():
    row = webpeople._contact({
        "role": "recruiter", "name": "Rita Vance", "title": "Recruiter",
        "linkedin_url": "https://www.linkedin.com/in/rita",
        "email": "rita.vance@acme.com", "evidence": "https://acme.com/team",
    }, "Acme")
    assert row.source == "claude"
    assert row.verification == "guessed"
    assert "acme.com/team" in row.email_status


def test_a_person_at_another_company_is_never_written_to(monkeypatch):
    """Apollo's answer is checked against the employer, not just believed.

    Found live on a Qualtrics job (2026-11-03): the web research returned an
    "Engineering Manager" by name and LinkedIn URL, enriching by URL asks
    Apollo nothing about where they work, and the address came back at
    apollo.io. A draft was written to them about somebody else's opening.
    """
    import httpx

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"person": {"name": "Bashir Souid", "title": "Engineering Manager",
                               "email": "bashir.souid@apollo.io", "email_status": "verified",
                               "organization": {"name": "Apollo.io"}}}

    class Client:
        def post(self, *args, **kwargs):
            return Response()

        def close(self):
            pass

    monkeypatch.setenv(apollo.API_KEY, KEY)
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: Client())
    contact = Contact.create("hiring_manager", "Bashir Souid", company="Qualtrics",
                             linkedin_url="https://www.linkedin.com/in/bashir",
                             source="claude")
    with pytest.raises(apollo.ApolloError) as raised:
        apollo.enrich(contact)
    assert "not Qualtrics" in str(raised.value)


def test_three_emails_is_the_cap_on_the_job_not_on_one_run(tmp_path, monkeypatch):
    """A second preparation finds different people; four at one employer is the bug."""
    job, _ = application(tmp_path)
    crowd = Apollo(people=[
        Contact.create("hiring_manager", f"Manager{index}", title="Engineering Manager",
                       company="Acme", linkedin_url=f"https://example.com/in/m{index}",
                       source="apollo")
        for index in range(3)
    ])
    drafter = wire(monkeypatch, crowd)
    follow.start(job.id)
    follow.wait(job.id)
    assert len(outreach_store.get(job.id).drafts) == follow.MAX_CONTACTS

    # A second run, with a person nobody had seen before.
    crowd.people.append(Contact.create(
        "recruiter", "Rita", title="Recruiter", company="Acme",
        linkedin_url="https://example.com/in/rita", source="apollo"))
    follow.start(job.id, force=True)
    follow.wait(job.id)

    state = outreach_store.get(job.id)
    assert len(state.drafts) == follow.MAX_CONTACTS
    assert len(drafter.calls) == follow.MAX_CONTACTS
    assert "already waiting" in state.lookup_note


# --- the employer, when the row never recorded one ------------------------
#
# Fifty of the 363 submitted rows carry no company: a capture that landed on
# an embed, a page whose title was the role alone. The web search then has
# nothing to search for, which is how the first backfill rows failed.


def _row(url, title=""):
    return Job(url=url, title=title, company="", status=Status.SUBMITTED)


def test_the_employer_is_read_off_the_url_when_the_row_has_none():
    assert follow.company_for(_row("https://thatch.com/jobs/software-engineer-5113")) == "Thatch"
    # The ATS is the first label and the employer the second.
    assert follow.company_for(
        _row("https://avature.sandisk.com/Careers/JobDetail/Software-Engineer/29")) == "Sandisk"
    # And in the path, for the systems that put it there.
    assert follow.company_for(
        _row("https://apply.workable.com/ottimate/j/1B2646E985/apply/")) == "Ottimate"
    assert follow.company_for(
        _row("https://ats.rippling.com/en-GB/oxbridge-health/jobs/196c")) == "Oxbridge Health"
    assert follow.company_for(
        _row("https://jobs.gem.com/imagen-technologies/am9icG9zdDof")) == "Imagen Technologies"


def test_a_hiring_system_is_never_mistaken_for_the_employer():
    """A wrong employer is worse than none: it emails a real stranger.

    Every URL here produced a wrong name on the first attempt - Avature,
    App (careerpuck), Career41 (SuccessFactors) - and a recruiter at the
    applicant-tracking vendor is exactly the person who must not be written
    to about somebody else's opening.
    """
    for url in ("https://app.careerpuck.com/job-board/color-health/job/41fa",
                "https://career41.sapsf.com/careers",
                "https://eppr.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/s"):
        name = follow.company_for(_row(url))
        assert name.lower() not in ("app", "career41", "careerpuck", "sapsf",
                                    "eppr", "fa", "oraclecloud", "avature"), url


def test_a_company_the_row_does_name_is_never_second_guessed():
    job = Job(url="https://avature.sandisk.com/Careers/JobDetail/x/1",
              title="Engineer", company="Western Digital", status=Status.SUBMITTED)
    assert follow.company_for(job) == "Western Digital"


# --- nobody to write to is an ending, not a failure (2026-11-07) -----------

def test_an_exhausted_ladder_counts_the_outreach_as_done(tmp_path, monkeypatch):
    """The web found nobody, and Apollo is connected and has nobody either.

    There is no third rung, so the job must not sit in "emails to send"
    waiting for a send that cannot happen: `done_at` puts it back under
    Applied, which is what the person's own "done with the outreach" does,
    and the card offers "Reopen it".
    """
    job, _ = application(tmp_path)
    empty = Apollo()
    empty.people = []
    wire(monkeypatch, empty)
    free_web(monkeypatch, found=[])

    follow.start(job.id)
    follow.wait(job.id)

    state = outreach_store.get(job.id)
    assert empty.searched == 1                   # the paid rung was reached
    assert state.done_at, state.lookup_note
    assert state.prepare_status == "ready"
    assert state.error == ""
    assert "counted as done" in state.lookup_note
    # Still not a stage of the application: the job's own status never moved.
    assert queue.get(job.id).status == Status.SUBMITTED


def test_a_missing_apollo_key_is_not_an_ending(tmp_path, monkeypatch):
    """A retry could fix this one, so it may not be counted as done: the card
    has to keep saying the key is missing until somebody adds it."""
    job, _ = application(tmp_path)
    wire(monkeypatch)                            # no Apollo key at all
    free_web(monkeypatch, found=[])

    follow.start(job.id)
    follow.wait(job.id)

    state = outreach_store.get(job.id)
    assert state.done_at is None
    assert state.prepare_status == "failed"
    assert "Apollo is not connected" in state.error


def test_an_apollo_search_that_threw_is_not_an_ending(tmp_path, monkeypatch):
    job, _ = application(tmp_path)
    crowd = Apollo()
    wire(monkeypatch, crowd)
    free_web(monkeypatch, found=[])

    def angry(company, per_role=3, client=None):
        raise apollo.ApolloError("502 from Apollo")

    monkeypatch.setattr(apollo, "discover", angry)

    follow.start(job.id)
    follow.wait(job.id)

    state = outreach_store.get(job.id)
    assert state.done_at is None
    assert state.prepare_status == "failed"
