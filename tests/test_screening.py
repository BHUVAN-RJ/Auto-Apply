"""The screening rules: what fires, what deliberately does not, and the tab.

Every false positive here was made by the model this replaced, on a real
posting; the corpus that proved it is in `applications/`. A rule that stops
catching one of these is a regression whatever else it improves.
"""

import json

import pytest
from fastapi.testclient import TestClient

from server import screening as server_screening
from server.app import app
from tailor import profile, screening

FACTS = """- Years of professional experience: 2
- Work authorisation: F-1 OPT; will need visa sponsorship now or in the future
- Security clearance: none
- Location: Los Angeles, California
- Relocation: Anywhere in the country
- Graduation: 2026 December
"""


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(screening, "STORE_PATH", tmp_path / "screening.json")
    monkeypatch.setattr(server_screening.paths, "APPLICATIONS", tmp_path / "applications")
    (tmp_path / "applications").mkdir()
    monkeypatch.setattr(profile, "screen_facts", lambda: (FACTS, "applicant.md"))


def fires(text, title="", rule=None):
    """The names of the rules that fire, or whether one named rule does."""
    hit = screening.fired(text, title=title, facts=FACTS)
    names = [r.name for r, _ in hit]
    return rule in names if rule else names


# --- what must fire --------------------------------------------------------

@pytest.mark.parametrize("text", [
    "We are unable to sponsor or take over sponsorship of an employment visa at this time.",
    "Please note we do not offer visa sponsorship for this role.",
    "Applicants must be authorized to work in the US without sponsorship.",
    "No visa sponsorship is available.",
])
def test_a_refusal_to_sponsor_fires(text):
    assert fires(text, rule="sponsorship_refused")


@pytest.mark.parametrize("text,rule", [
    ("This role requires an active security clearance.", "clearance_required"),
    ("Must hold a TS/SCI with polygraph.", "clearance_required"),
    ("Due to ITAR, this position is restricted.", "export_control"),
    ("Subject to the Export Administration Regulations (EAR).", "export_control"),
    ("U.S. Person Required for this position.", "export_control"),
    ("US citizens only for this position.", "citizen_only"),
    ("A PhD in a related field is required.", "phd_required"),
])
def test_the_hard_blocks_fire(text, rule):
    assert fires(text, rule=rule)


def test_export_control_was_the_models_blind_spot():
    """Seven postings on the corpus carried one of these and the model
    passed every one. Raised 2026-09-21, caught here."""
    for text in ("ITAR/EAR Requirements",
                 "To conform to U.S. Government space technology export regulations, "
                 "including the International Traffic in Arms Regulations (ITAR), you must be a U.S. citizen.",
                 "Certain job-related technical data is subject to U.S. Export Administration Regulations (EAR)."):
        names = fires(text)
        assert "export_control" in names, text


def test_experience_reads_the_floor_of_a_range():
    assert fires("Minimum 5 years of professional development experience.", rule="experience_years")
    assert not fires("0-1 years of experience in software engineering.", rule="experience_years")
    assert not fires("1-3 years of experience preferred.", rule="experience_years")


def test_the_experience_threshold_is_the_persons_own():
    text = "Requires 3+ years of experience building backend services."
    assert not fires(text, rule="experience_years")
    screening.set_value("experience_years", 3)
    assert fires(text, rule="experience_years")


# --- what must not fire ----------------------------------------------------

def test_a_form_question_is_never_a_flag():
    """"Will you now or in the future require sponsorship?" is on half the
    application forms in the country and says nothing about the employer.
    The model called it a hard reject four times."""
    assert fires("Will you now, or in the future, require sponsorship for employment "
                 "visa status (e.g., H-1B visa status)?") == []


def test_an_offer_to_sponsor_is_not_a_refusal():
    """A real reject on the corpus: the model flagged the words "Will sponsor"."""
    assert not fires("Will sponsor: yes, we sponsor H-1B transfers.", rule="sponsorship_refused")
    assert not fires("We are happy to sponsor the right candidate.", rule="sponsorship_refused")


def test_a_hedge_is_not_a_refusal():
    for text in ("Please note that this does not guarantee sponsorship for this specific role.",
                 "We cannot successfully sponsor a visa for every role and every candidate.",
                 "Axon cannot guarantee that work authorization will be sought."):
        assert not fires(text, rule="sponsorship_refused"), text


def test_work_eligibility_boilerplate_is_not_a_visa_block():
    """Both were hard visa rejects from the model. Both appear in postings
    that sponsor."""
    for text in ("This employer participates in the federal E-Verify program.",
                 "All persons hired will be required to verify identity and eligibility "
                 "to work in the United States."):
        assert fires(text) == [], text


def test_a_posting_that_needs_no_clearance_is_not_a_clearance_block():
    """A hard reject on a live posting (2026-10-04): the sentence saying no
    clearance is needed names the clearance to say so."""
    for text in ("No Clearance: Position does not require a security clearance.",
                 "This position does not require an active security clearance.",
                 "Security clearance is not required for this role.",
                 "Security Clearance: None",
                 "No security clearance required."):
        assert not fires(text, rule="clearance_required"), text


def test_a_clearance_to_obtain_later_still_fires():
    """Not needing one today is not the same as never needing one."""
    for text in ("Does not require an active security clearance, but you must be able "
                 "to obtain a security clearance.",
                 "No clearance required at start; candidates must be eligible for a "
                 "security clearance."):
        assert fires(text, rule="clearance_required"), text


def test_a_us_onsite_role_is_not_a_location_block():
    """Relocation is open, so onsite in San Francisco is not a flag. The
    model rejected four jobs on exactly this."""
    for text in ("This is not a remote opportunity; it is 100% onsite in San Francisco.",
                 "Location Type: On-site. Our office is in New York, New York.",
                 "Fluidstack is an in-person company (this is not a remote position)."):
        assert fires(text) == [], text


def test_member_of_technical_staff_is_not_a_senior_title():
    """What Cerebras and Perplexity call the job they hire new graduates
    into. So is Associate anything."""
    for title in ("Member of Technical Staff, Backend & Product",
                  "Member of Technical Staff, AI Products (Early Career)",
                  "Associate Product Manager, New Grad (2027 Start)",
                  "Associate Solutions Architect, Early Career"):
        assert not fires("A posting.", title=title, rule="seniority_title"), title
    assert fires("A posting.", title="Senior Software Engineer", rule="seniority_title")
    assert fires("A posting.", title="Staff Engineer, Platform", rule="seniority_title")


def test_the_description_mentioning_senior_people_is_not_a_senior_title():
    assert not fires("You will work alongside senior engineers and staff architects.",
                     title="Software Engineer, New Grad", rule="seniority_title")


# --- switches and the person's own rules -----------------------------------

def test_a_rule_switched_off_stops_firing():
    text = "We are unable to sponsor an employment visa."
    assert fires(text, rule="sponsorship_refused")
    screening.set_enabled("sponsorship_refused", False)
    assert not fires(text, rule="sponsorship_refused")
    screening.set_enabled("sponsorship_refused", True)
    assert fires(text, rule="sponsorship_refused")


def test_a_rule_of_your_own_is_used_and_can_be_deleted():
    rule = screening.Rule(name="night_shift", label="Night shift", category="other",
                          severity="soft", note="", patterns=[r"\bnight shift\b"])
    screening.save_custom(rule)
    assert fires("This role works the night shift.", rule="night_shift")
    screening.delete_custom("night_shift")
    assert not fires("This role works the night shift.", rule="night_shift")


def test_a_rule_that_matches_ordinary_prose_is_refused():
    """A pattern that fires on every posting rejects every job, which is a
    worse failure than the rule never existing."""
    bad = screening.Rule(name="too_wide", label="x", category="other", severity="hard",
                         note="", patterns=[r"\bengineer\b"])
    assert "more specific" in screening.invalid(bad)
    with pytest.raises(ValueError):
        screening.save_custom(bad)


def test_a_broken_pattern_is_refused_before_it_is_saved():
    bad = screening.Rule(name="broken", label="x", category="other", severity="hard",
                         note="", patterns=[r"[unclosed"])
    assert "not a valid regular expression" in screening.invalid(bad)


def test_a_stock_name_cannot_be_shadowed():
    with pytest.raises(ValueError):
        screening.save_custom(screening.Rule(
            name="clearance_required", label="mine", category="clearance",
            severity="hard", note="", patterns=[r"\bmine\b"]))


# --- the tab ---------------------------------------------------------------

def client():
    return TestClient(app)


def test_the_tab_lists_every_rule_and_switches_one():
    res = client().get("/screening").json()
    assert len(res["rules"]) == len(screening.STOCK)
    assert res["facts_source"] == "applicant.md"
    off = client().post("/screening/rule/contract_only", json={"enabled": True}).json()
    assert next(r for r in off["rules"] if r["name"] == "contract_only")["enabled"] is True


def test_a_stock_rule_cannot_be_deleted():
    assert client().delete("/screening/rule/clearance_required").status_code == 400
    assert client().delete("/screening/rule/nonesuch").status_code == 404


def write_posting(tmp_path, name, text, title=""):
    folder = tmp_path / "applications" / name
    folder.mkdir(parents=True)
    (folder / "posting.md").write_text(text)
    (folder / "job.json").write_text(json.dumps({"title": title}))


def test_a_proposed_rule_is_previewed_against_your_own_postings(tmp_path):
    """A rule reads well and still rejects half the jobs already applied
    for. The number is shown before it is saved."""
    write_posting(tmp_path, "a", "This role requires a security clearance.")
    write_posting(tmp_path, "b", "A normal posting about building services.")
    res = client().post("/screening/preview", json={
        "name": "clearance_mine", "label": "x", "category": "clearance",
        "severity": "hard", "patterns": [r"\bsecurity clearance\b"]}).json()
    assert res["postings"] == 2 and res["hits"] == 1
    assert "security clearance" in res["samples"][0]["quote"]
    assert res["too_wide"] is False


def test_the_assistant_writes_a_rule_and_saves_nothing_by_itself(monkeypatch, tmp_path):
    write_posting(tmp_path, "a", "This role requires a security clearance.")
    monkeypatch.setattr(server_screening.llm, "complete", lambda *a, **k: json.dumps({
        "rule": {"name": "clearance_mine", "label": "Clearance", "category": "clearance",
                 "severity": "hard", "note": "n", "patterns": [r"\bsecurity clearance\b"], "unless": []},
        "explain": "Fires when a posting asks for a clearance.", "problem": "",
    }))
    res = client().post("/screening/ask", json={"request": "flag anything wanting a clearance"}).json()
    assert res["rule"]["name"] == "clearance_mine" and res["problem"] == ""
    assert res["preview"]["hits"] == 1
    # Proposed, not saved.
    assert screening.get("clearance_mine") is None
    client().post("/screening/rule", json=res["rule"])
    assert screening.get("clearance_mine").origin == "yours"


def test_the_assistant_cannot_save_a_rule_that_matches_everything(monkeypatch):
    monkeypatch.setattr(server_screening.llm, "complete", lambda *a, **k: json.dumps({
        "rule": {"name": "everything", "label": "x", "category": "other", "severity": "hard",
                 "note": "", "patterns": [r"\bteam\b"], "unless": []},
        "explain": "", "problem": "",
    }))
    res = client().post("/screening/ask", json={"request": "flag teams"}).json()
    assert "more specific" in res["problem"]
    assert client().post("/screening/rule", json=res["rule"]).status_code == 400


def test_a_reply_that_is_not_a_rule_is_said_plainly(monkeypatch):
    monkeypatch.setattr(server_screening.llm, "complete", lambda *a, **k: "I cannot help with that")
    res = client().post("/screening/ask", json={"request": "what is the weather"}).json()
    assert res["rule"] is None and "try saying it differently" in res["problem"]


# -- a form's own answers are not the employer's policy -------------------

ASHBY_FORM = """Autofill with Greenhouse
Resume
Click to upload or drag and drop here

LinkedIn Profile Link
If hired, in which country will you be based?
For the country in which you will be based, do you now, or will you at any time in the future, require visa/work permit sponsorship to work in the United States or Canada?
No, I do not require visa sponsorship to work in the United States
Yes, I require visa sponsorship to work in the United States
No, I do not require work permit sponsorship to work in Canada
Yes, I require work permit sponsorship to work in Canada
Submit Application
"""


def test_an_answer_under_a_question_is_not_a_refusal():
    """Ashby renders a question and its options as plain lines, and a capture
    that lands on the application page rather than the overview screens that
    text. The question is passed over, but the option under it is a flat
    statement - "No, I do not require visa sponsorship to work in the United
    States" - which read as the employer refusing to sponsor and auto-
    rejected the job on the strength of the candidate's own answer."""
    assert screening.fired(ASHBY_FORM) == []


def test_the_employers_own_refusal_still_fires():
    """The guard is about who is speaking, not about the words."""
    posting = ("About the role\n"
               "We are unable to sponsor or take over sponsorship of an employment visa at this time.\n")
    fired = [r.category for r, _ in screening.fired(posting)]
    assert "visa" in fired


def test_an_answer_is_recognised_by_either_sign():
    # It opens the way an answer opens...
    assert screening.is_answer("No, I do not require sponsorship", 0, "No, I do not require sponsorship")
    # ...or the line above it is the question it answers.
    text = "Do you require sponsorship?\nI require visa sponsorship to work in the US\n"
    start = text.index("I require")
    assert screening.is_answer(text, start, "I require visa sponsorship to work in the US")
    # A policy under a heading is neither.
    text = "Work authorisation\nWe do not sponsor visas for this role.\n"
    start = text.index("We do not")
    assert not screening.is_answer(text, start, "We do not sponsor visas for this role.")
