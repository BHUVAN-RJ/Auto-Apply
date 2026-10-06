"""The code filler: fields matched from base/form.json, set by kind, the
resume put on its own input and read back, visa questions never touched,
and everything else listed for the agent."""
import asyncio
import json
import re

import pytest

from browser import forms
from browser.forms import engine
from browser.forms.profile import Profile, normalise_label

PROFILE = Profile({
    "first_name": "Jane", "last_name": "Doe", "email": "jane@example.com",
    "phone": "5550001111", "linkedin": "https://linkedin.com/in/jane",
    "city": "Austin", "state": "Texas", "country": "United States",
    "how_heard": "Jobright", "gender": "Female",
    "answers": {"Are you willing to relocate? *": "Yes", "Notice period": "2 weeks"},
})


def gh_field(ref, **kw):
    base = {"ref": ref, "tag": "input", "type": "text", "kind": "text", "id": "", "name": "",
            "autocomplete": "", "placeholder": "", "label": "", "group": "", "required": False,
            "value": "", "options": [], "accept": ""}
    base.update(kw)
    return base


class FakePage:
    """Answers the engine's scripts from a list of fields, recording what
    was set. Values written show up on the next scan."""

    def __init__(self, fields, suggestions=None):
        self.fields = {f["ref"]: dict(f) for f in fields}
        self.suggestions = suggestions or []
        self.sent = []
        self.clicked = []
        self.typed = []
        self.marked = []   # (ref, note): the logo in front of a field we set
        self.mark_args = []
        self.iframe = ""

    def _call(self, expression):
        for name, fn in (("text", engine.SET_TEXT_FN), ("select", engine.SET_SELECT_FN),
                         ("click", engine.CLICK_FN), ("centre", engine.CENTER_FN),
                         ("options", engine.OPTIONS_FN), ("mark", engine.MARK_UPLOAD_FN),
                         ("file", engine.FILE_NAME_FN), ("find_remove", engine.FIND_REMOVE_FN),
                         ("click_remove", engine.CLICK_REMOVE_FN), ("dot", engine.MARK_FN)):
            head = f"({fn.strip()})("
            if expression.startswith(head):
                args = json.loads("[" + expression[len(head):-1] + "]")
                return name, args
        return None, None

    async def evaluate(self, expression):
        if expression == "document.readyState":
            return "complete"
        if expression == engine.SCAN_JS:
            return list(self.fields.values())
        if expression == engine.IFRAME_JS:
            return self.iframe
        if expression == engine.ACTIVE_EDITABLE_JS:
            return True
        name, args = self._call(expression)
        if name == "text":
            self.fields[args[0]]["value"] = args[1]
            return "ok"
        if name == "select":
            f = self.fields[args[0]]
            f["value"] = f["options"][args[1]]["value"]
            return "ok"
        if name == "click":
            f = self.fields[args[0]]
            self.clicked.append(args[0])
            if f["kind"] in ("radio", "checkbox"):
                for other in self.fields.values():
                    if other["kind"] == "radio" and other["group"] == f["group"]:
                        other["value"] = ""
                f["value"] = "on"
            return "ok"
        if name == "centre":
            return {"x": 100, "y": 200}
        if name == "options":
            return self.suggestions
        if name == "mark":
            return True
        if name == "dot":
            self.marked.append((args[0], args[1]))
            self.mark_args.append(args)
            return True
        if name == "file":
            return self.fields[args[0]]["value"]
        if name == "find_remove":
            # A slot holding a file: `attached` = (label, filename, ref of
            # the input that comes back once it is removed, field dict).
            slot = getattr(self, "attached", None)
            if slot and re.search(args[0], slot[0], re.I) and not (args[1] and re.search(args[1], slot[0], re.I)):
                return {"label": slot[3].get("button", "Remove file"), "title": slot[0], "file": slot[1]}
            return None
        if name == "click_remove":
            slot = getattr(self, "attached", None)
            if not slot:
                return False
            self.removed = slot[1]
            self.attached = None
            self.fields[slot[2]] = dict(slot[3])
            return True
        raise AssertionError(expression[:80])

    async def send(self, method, params=None):
        self.sent.append((method, params or {}))
        if method == "Runtime.evaluate":
            ref = params["expression"].rsplit(", ", 1)[1].rstrip(")")
            return {"result": {"objectId": "obj:" + json.loads(ref)}}
        if method == "DOM.describeNode":
            return {"node": {"backendNodeId": 7}}
        if method == "DOM.setFileInputFiles":
            # The engine finds the input by ref through Runtime.evaluate first.
            ref = [p["expression"] for m, p in self.sent if m == "Runtime.evaluate"][-1].rsplit(", ", 1)[1].rstrip(")")
            self.fields[json.loads(ref)]["value"] = params["files"][0].rsplit("/", 1)[-1]
            return {}
        if method == "Input.dispatchKeyEvent" and params.get("type") == "keyDown" and params.get("text"):
            self.typed.append(params["text"])
            return {}
        return {}


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(engine, "POLL", 0.0)
    monkeypatch.setattr(engine, "OPTIONS_TIMEOUT", 0.05)
    monkeypatch.setattr(engine, "FIELDS_TIMEOUT", 0.05)


def run(page, adapter=None, resume=None, cover=None):
    return asyncio.run(engine.run(page, adapter or forms.Greenhouse(), PROFILE, "TARGET1234",
                                  resume=resume, cover_letter=cover))


def test_greenhouse_fields_are_filled_by_id_and_the_resume_read_back(tmp_path):
    resume = tmp_path / "Jane_Doe_Resume.pdf"
    resume.write_bytes(b"%PDF")
    page = FakePage([
        gh_field("1", id="first_name", label="First Name *", required=True),
        gh_field("2", id="last_name", label="Last Name *", required=True),
        gh_field("3", id="email", type="email", label="Email *", required=True),
        gh_field("4", id="phone", type="tel", label="Phone"),
        gh_field("5", id="resume", type="file", kind="file", name="resume", label="Resume/CV *"),
        gh_field("6", id="cover_letter", type="file", kind="file", name="cover_letter", label="Cover Letter"),
        gh_field("7", id="question_9", label="LinkedIn Profile"),
        gh_field("8", id="question_10", label="Why do you want to work here? *", kind="textarea", required=True),
    ])
    report = run(page, resume=resume)
    assert report.attempted and report.tab_id == "1234"
    assert page.fields["1"]["value"] == "Jane" and page.fields["2"]["value"] == "Doe"
    assert page.fields["3"]["value"] == "jane@example.com"
    assert page.fields["7"]["value"] == "https://linkedin.com/in/jane"
    assert report.resume_uploaded and report.resume_name == "Jane_Doe_Resume.pdf"
    assert page.fields["5"]["value"] == "Jane_Doe_Resume.pdf"
    assert page.fields["6"]["value"] == "", "the resume never lands on the cover letter input"
    # The open question is left for the agent, marked required.
    assert report.missing == [{"label": "Why do you want to work here? *", "kind": "textarea", "required": True}]
    assert "Why do you want" in report.task_lines() and "(required)" in report.task_lines()
    assert "5 field(s) filled" in report.summary() and "resume attached" in report.summary()


def test_visa_questions_are_never_filled_even_with_an_answer_on_file():
    profile = Profile({"first_name": "Jane", "answers": {"Will you require sponsorship?": "No"}})
    page = FakePage([
        gh_field("1", id="first_name", label="First Name"),
        gh_field("2", id="question_1", kind="radio", type="radio", label="No", group="Will you now or in the future require sponsorship? *"),
        gh_field("3", id="question_1b", kind="radio", type="radio", label="Yes", group="Will you now or in the future require sponsorship? *"),
        gh_field("4", id="question_2", label="Are you authorized to work in the United States?", kind="select",
                 options=[{"value": "", "text": "Select..."}, {"value": "1", "text": "Yes"}, {"value": "0", "text": "No"}]),
    ])
    report = asyncio.run(engine.run(page, forms.Greenhouse(), profile))
    assert page.clicked == [] and page.fields["4"]["value"] == ""
    assert report.protected == ["Will you now or in the future require sponsorship? *",
                                "Are you authorized to work in the United States?"]
    assert not any("sponsorship" in m["label"] for m in report.missing)


def test_answers_on_file_win_and_pick_radios_and_selects():
    page = FakePage([
        gh_field("1", kind="radio", type="radio", label="Yes", group="Are you willing to relocate? *", required=True),
        gh_field("2", kind="radio", type="radio", label="No", group="Are you willing to relocate? *", required=True),
        gh_field("3", label="Notice period"),
        gh_field("4", kind="select", tag="select", label="Gender",
                 options=[{"value": "", "text": "Select..."}, {"value": "f", "text": "Female"}, {"value": "m", "text": "Male"}]),
        gh_field("5", kind="select", tag="select", label="How did you hear about us?",
                 options=[{"value": "", "text": "Select..."}, {"value": "li", "text": "LinkedIn"}, {"value": "jr", "text": "Jobright"}]),
        gh_field("6", kind="select", tag="select", label="Country", options=[
            {"value": "", "text": "Select..."}, {"value": "IN", "text": "India"}, {"value": "US", "text": "United States of America"}]),
    ])
    report = run(page)
    assert page.clicked == ["1"] and page.fields["1"]["value"] == "on"
    assert page.fields["3"]["value"] == "2 weeks"
    assert page.fields["4"]["value"] == "f" and page.fields["5"]["value"] == "jr"
    assert page.fields["6"]["value"] == "US"
    assert report.missing == []


def test_a_combobox_is_typed_into_and_the_suggestion_clicked():
    page = FakePage(
        [gh_field("1", id="candidate-location", kind="combobox", label="Location (City) *", required=True)],
        suggestions=[{"text": "Austin, Minnesota, United States", "x": 1, "y": 1},
                     {"text": "Austin, Texas, United States", "x": 5, "y": 9}],
    )
    report = run(page)
    assert "".join(page.typed) == "Austin, Texas, United States"
    clicks = [p for m, p in page.sent if m == "Input.dispatchMouseEvent"]
    # One click on the widget, one on the suggestion.
    assert [(c["type"], c["x"], c["y"]) for c in clicks] == [
        ("mousePressed", 100, 200), ("mouseReleased", 100, 200), ("mousePressed", 5, 9), ("mouseReleased", 5, 9)]
    assert "Location (City) *" in report.filled


def test_a_lever_form_is_filled_by_name_and_the_posting_url_becomes_apply():
    lever = forms.Lever()
    assert lever.apply_url("https://jobs.lever.co/acme/12345678-1234-1234-1234-123456789abc") == \
        "https://jobs.lever.co/acme/12345678-1234-1234-1234-123456789abc/apply"
    page = FakePage([
        gh_field("1", name="name", label="Full name *", required=True),
        gh_field("2", name="email", label="Email *", required=True),
        gh_field("3", name="org", label="Current company"),
        gh_field("4", name="urls[LinkedIn]", label="LinkedIn URL"),
        gh_field("5", name="resume", kind="file", type="file", label="Resume/CV"),
        gh_field("6", name="comments", kind="textarea", label="Additional information"),
    ])
    report = run(page, adapter=lever)
    assert page.fields["1"]["value"] == "Jane Doe"
    assert page.fields["3"]["value"] == "", "no company on file, left for the agent"
    assert page.fields["4"]["value"] == "https://linkedin.com/in/jane"
    assert {m["label"] for m in report.missing} == {"Current company", "Additional information"}
    assert not report.resume_uploaded and "no resume" not in " ".join(report.errors)


def test_ashby_uses_the_full_name_field_and_the_application_url():
    ashby = forms.Ashby()
    url = "https://jobs.ashbyhq.com/acme/12345678-1234-1234-1234-123456789abc"
    assert ashby.apply_url(url) == url + "/application"
    assert ashby.apply_url(url + "/application?utm=1") == url + "/application"
    page = FakePage([gh_field("1", id="_systemfield_name", name="_systemfield_name", label="Name *")])
    run(page, adapter=ashby)
    assert page.fields["1"]["value"] == "Jane Doe"


def test_an_empty_host_page_follows_the_embedded_form_iframe():
    page = FakePage([])
    page.iframe = "https://boards.greenhouse.io/embed/job_app?token=1"
    report = run(page)
    assert ("Page.navigate", {"url": "https://boards.greenhouse.io/embed/job_app?token=1"}) in page.sent
    assert not report.attempted and "no form fields" in report.note


def test_the_adapter_is_picked_by_url_and_the_profile_by_file(tmp_path, monkeypatch):
    assert forms.adapter_for("https://job-boards.greenhouse.io/acme/jobs/1").NAME == "greenhouse"
    assert forms.adapter_for("https://jobs.ashbyhq.com/acme/1").NAME == "ashby"
    assert forms.adapter_for("https://jobs.lever.co/acme/1").NAME == "lever"
    assert forms.adapter_for("https://acme.wd5.myworkdayjobs.com/x") is None
    assert forms.load(tmp_path / "missing.json") is None
    (tmp_path / "form.json").write_text(json.dumps({"first_name": "A", "last_name": "B", "city": "Austin", "country": "United States"}))
    profile = forms.load(tmp_path / "form.json")
    assert profile.get("full_name") == "A B" and profile.get("location") == "Austin, United States"
    assert normalise_label("Are you willing to relocate? *") == normalise_label("are you willing to relocate")


def test_option_picking_prefers_exact_then_prefix_then_contains():
    options = [{"value": "", "text": "Select..."}, {"value": "1", "text": "United Kingdom"},
               {"value": "2", "text": "United States of America"}, {"value": "3", "text": "Minor Outlying Islands (United States)"}]
    assert engine.pick_option(options, "United States") == 2
    assert engine.pick_option(options, "Nowhere") is None
    assert engine.pick_option([{"value": "", "text": ""}], "") is None
    assert engine.pick_suggestion([{"text": "Austin, MN, USA"}, {"text": "Austin, TX, USA"}], "Austin, Texas") is None
    assert engine.pick_suggestion([{"text": "Austin, MN, USA"}, {"text": "Austin, TX, USA"}], "Austin") is None, "two candidates, no guess"
    assert engine.pick_suggestion([{"text": "Austin, TX, USA"}], "Austin, Texas")["text"] == "Austin, TX, USA"


def test_the_engine_never_clicks_a_submit_control():
    import inspect
    source = inspect.getsource(engine)
    assert "describes_submit" in source
    assert "kill" not in source and "Target.closeTarget" not in source and "Page.close" not in source


def run_documents(page, resume=None, cover=None, adapter=None, answerer=None, corrections=None,
                  profile=None):
    return asyncio.run(engine.run_documents(page, adapter or forms.Greenhouse(), "TARGET1234",
                                            resume=resume, cover_letter=cover, answerer=answerer,
                                            corrections=corrections, profile=profile))


def test_the_contact_fields_are_filled_when_nothing_else_filled_them():
    """Jobright's autofill is step one everywhere, and on a form it has no
    button for - Ashby serves its application on a page of its own - nothing
    filled the name, the email or the phone: the tailored resume went on,
    five questions were answered, and the first four boxes were empty
    (Deepgram, 2026-10-04). Only an empty field is written, so Jobright's
    values and the person's own stand, and a visa question is still refused."""
    page = FakePage([
        gh_field("1", id="_systemfield_name", name="_systemfield_name", label="Full Name *"),
        gh_field("2", id="_systemfield_email", name="_systemfield_email", label="Email *"),
        gh_field("3", id="_systemfield_phone", name="_systemfield_phone", label="Phone",
                 value="+1 555 999 8888"),
        gh_field("4", id="_systemfield_visa", name="visa",
                 label="Will you now or in the future require sponsorship? *"),
        gh_field("5", tag="textarea", kind="textarea", label="What excites you about us?",
                 value="already answered"),
    ])
    report = run_documents(page, adapter=forms.Ashby(), profile=PROFILE)
    assert page.fields["1"]["value"] == "Jane Doe"
    assert page.fields["2"]["value"] == "jane@example.com"
    assert page.fields["3"]["value"] == "+1 555 999 8888", "what was already there stands"
    assert page.fields["4"]["value"] == "", "a visa question is never answered"
    assert page.fields["5"]["value"] == "already answered"
    assert any(ref == "1" for ref, _ in page.marked), "a field we set carries the logo"


def test_the_contact_fallback_is_off_unless_the_caller_asks():
    page = FakePage([gh_field("1", id="_systemfield_name", name="_systemfield_name",
                              label="Full Name *")])
    assert run_documents(page, adapter=forms.Ashby()).filled == []
    assert page.fields["1"]["value"] == ""


def test_corrected_fields_go_over_what_autofill_left_by_label_only():
    """The correction store after Jobright: the location the human fixed
    on the last form goes over Jobright's wrong city here, by exact label
    with punctuation ignored; a radio picks its option; a value already
    right is counted and left; a textarea, a visa question and a field
    with no correction on file are never touched. Each corrected field
    carries the logo with what autofill had."""
    store = Profile({"corrections": {
        "Location": {"value": "Los Angeles, California, United States", "was": "Delhi, India", "system": "greenhouse"},
        "Are you willing to relocate?": "Yes",
        "Phone": {"value": "5550001111"},
        "Why us?": {"value": "canned prose"},
        "Will you require sponsorship?": {"value": "No"},
    }})
    page = FakePage([
        gh_field("1", id="first_name", label="First Name *", value="Jane"),
        gh_field("2", id="job_application_location", label="Location *", value="Delhi, India"),
        gh_field("3", kind="radio", tag="input", type="radio", group="Are you willing to relocate? *", label="No"),
        gh_field("4", kind="radio", tag="input", type="radio", group="Are you willing to relocate? *", label="Yes"),
        gh_field("5", id="phone", label="Phone", value="5550001111"),
        gh_field("6", tag="textarea", kind="textarea", label="Why us?", value="my own words"),
        gh_field("7", kind="select", label="Will you require sponsorship?", options=[{"text": "Yes", "value": "y"}, {"text": "No", "value": "n"}]),
    ])
    report = run_documents(page, corrections=store)
    assert page.fields["2"]["value"] == "Los Angeles, California, United States"
    assert page.fields["4"]["value"] and not page.fields["3"]["value"]
    assert page.fields["6"]["value"] == "my own words", "prose is per job, never carried over"
    assert page.fields["7"]["value"] == "", "a visa question is never touched"
    assert page.fields["1"]["value"] == "Jane"
    assert report.corrected == ["Location *", "Are you willing to relocate? *", "Phone"]
    assert not report.errors
    marks = dict(page.marked)
    assert marks["2"] == "corrected: Los Angeles, California, United States (autofill had Delhi, India)"
    assert marks["5"] == "corrected: 5550001111"
    assert "1" not in marks and "6" not in marks and "7" not in marks
    # An empty store is a no-op, not an error.
    assert run_documents(FakePage([gh_field("2", label="Location *", value="Delhi")]), corrections=Profile({})).corrected == []


def test_documents_only_uploads_and_touches_no_field(tmp_path):
    resume = tmp_path / "Jane_Doe_Resume.pdf"
    resume.write_bytes(b"%PDF")
    cover = tmp_path / "Jane_Doe_cover_letter.pdf"
    cover.write_bytes(b"%PDF")
    page = FakePage([
        gh_field("1", id="first_name", label="First Name *", required=True),
        gh_field("5", id="resume", type="file", kind="file", name="resume", label="Resume/CV *"),
        gh_field("6", id="cover_letter", type="file", kind="file", name="cover_letter", label="Cover Letter"),
    ])
    report = run_documents(page, resume=resume, cover=cover)
    assert not report.attempted, "the agent's task stays the Jobright one"
    assert report.resume_uploaded and report.cover_letter_uploaded
    assert page.fields["5"]["value"] == "Jane_Doe_Resume.pdf"
    assert page.fields["6"]["value"] == "Jane_Doe_cover_letter.pdf"
    assert page.fields["1"]["value"] == "" and not page.typed
    # Each document we put on gets the dot; the untouched field does not.
    assert [ref for ref, _ in page.marked] == ["5", "6"]
    assert "tailored resume" in page.marked[0][1] and "cover letter" in page.marked[1][1]


def test_free_questions_are_answered_by_the_model_and_marked(tmp_path):
    """Every free-form question goes to the answerer and its reply into
    the box, over Jobright's generic prose, with the logo in front. Short
    fields and visa questions never reach the model."""
    resume = tmp_path / "Jane_Doe_Resume.pdf"
    resume.write_bytes(b"%PDF")
    asked = []

    def answerer(question):
        asked.append(question)
        return f"Answer to: {question[:20]}"
    page = FakePage([
        gh_field("1", id="first_name", label="First Name *"),
        gh_field("2", id="linkedin", label="LinkedIn Profile"),
        gh_field("3", tag="textarea", kind="textarea", label="What are the most interesting aspects of Perplexity that you are excited to work on?"),
        gh_field("4", kind="text", label="Why do you want to work here?"),
        gh_field("5", id="resume", type="file", kind="file", name="resume", label="Resume/CV *"),
        gh_field("6", tag="textarea", kind="textarea", label="Anything else?", value="Jobright's generic prose"),
        gh_field("7", tag="textarea", kind="textarea", label="Will you now or in the future require sponsorship? Explain."),
        gh_field("8", kind="text", label="Tell us about a project you are proud of and what you learned from it"),
    ])
    report = run_documents(page, resume=resume, answerer=answerer)
    assert asked == [
        "What are the most interesting aspects of Perplexity that you are excited to work on?",
        "Why do you want to work here?",
        "Anything else?",
        "Tell us about a project you are proud of and what you learned from it",
    ]
    assert page.fields["3"]["value"].startswith("Answer to: What are")
    assert page.fields["4"]["value"].startswith("Answer to: Why do")
    assert page.fields["8"]["value"].startswith("Answer to: Tell us")
    assert page.fields["1"]["value"] == page.fields["2"]["value"] == page.fields["7"]["value"] == ""
    assert page.fields["6"]["value"].startswith("Answer to: Anything"), "Jobright's prose is replaced"
    assert report.answered == asked and report.resume_uploaded
    assert [ref for ref, _ in page.marked] == ["5", "3", "4", "6", "8"]
    assert all(args[2] == engine.LOGO_SVG for args in page.mark_args), "the mark is the assistant's orb"
    assert "question(s) answered" not in report.summary(), "documents-only summary is the Jobright one"


def test_a_refused_or_failed_answer_leaves_the_box_and_says_so(tmp_path):
    def answerer(question):
        raise RuntimeError("model down")
    page = FakePage([gh_field("3", tag="textarea", kind="textarea", label="Why us?")])
    report = run_documents(page, answerer=answerer)
    assert page.fields["3"]["value"] == "" and not report.answered
    assert report.errors and "model down" in report.errors[-1] and not page.marked


def test_a_slot_already_holding_jobrights_resume_is_cleared_first(tmp_path):
    # Greenhouse removes the input once a file is on the slot; only the
    # cover letter input is left, and "Remove file" brings the other back.
    resume = tmp_path / "Jane_Doe_Resume.pdf"
    resume.write_bytes(b"%PDF")
    page = FakePage([
        gh_field("6", id="cover_letter", type="file", kind="file", name="cover_letter", label="Attach"),
    ])
    page.attached = ("Resume/CV *", "generic_jobright_resume.pdf", "5",
                     gh_field("5", id="resume", type="file", kind="file", name="resume", label="Resume/CV *"))
    report = run_documents(page, resume=resume)
    assert page.removed == "generic_jobright_resume.pdf"
    assert report.resume_uploaded and page.fields["5"]["value"] == "Jane_Doe_Resume.pdf"
    assert page.fields["6"]["value"] == "", "the resume never lands on the cover letter input"


def test_a_greenhouse_form_whose_only_slot_is_occupied_is_still_a_form(tmp_path):
    """Greenhouse drops the file input once a file is on the slot, and
    Jobright's autofill attaches its own resume before this runs - so the
    whole form has no file input on it. That read as "no application form on
    this page", and a perfectly good Greenhouse application went back to the
    person as a job behind a sign-in, its tailored resume never attached
    (Skild AI, 2026-10-04). The attachment is the slot: clear it, and the
    input comes back."""
    resume = tmp_path / "Jane_Doe_Resume.pdf"
    resume.write_bytes(b"%PDF")
    page = FakePage([
        gh_field("1", kind="text", label="First Name"),
        gh_field("2", kind="text", label="Last Name"),
        gh_field("3", kind="text", label="Email"),
    ])
    page.attached = ("Resume/CV *", "generic_jobright_resume.pdf", "5",
                     gh_field("5", id="resume", type="file", kind="file", name="resume",
                              label="Resume/CV *"))
    report = run_documents(page, resume=resume)
    assert engine.NOT_A_FORM not in report.errors
    assert page.removed == "generic_jobright_resume.pdf"
    assert report.resume_uploaded and page.fields["5"]["value"] == "Jane_Doe_Resume.pdf"


def test_a_remove_button_that_reads_as_submit_is_never_pressed(tmp_path):
    resume = tmp_path / "Jane_Doe_Resume.pdf"
    resume.write_bytes(b"%PDF")
    page = FakePage([
        gh_field("6", id="cover_letter", type="file", kind="file", name="cover_letter", label="Attach"),
    ])
    page.attached = ("Resume/CV *", "x.pdf", "5",
                     dict(gh_field("5", id="resume", type="file", kind="file", name="resume", label="Resume/CV *"),
                          button="Remove and submit application"))
    report = run_documents(page, resume=resume)
    assert not getattr(page, "removed", None)
    assert not report.resume_uploaded and any("refused" in e for e in report.errors)


def test_a_rescan_never_reuses_a_ref_a_control_already_carries():
    """Greenhouse puts the resume input back after "Remove file"; the scan
    gave it ref "1", the first-name field's, and the upload landed on a
    text input ("Node is not a file input element")."""
    counter_reset = "let counter = 0;" in engine.SCAN_JS
    assert counter_reset
    head = engine.SCAN_JS.split("const walk")[0]
    assert "data-autopilot-ref" in head and "Math.max" in head, "the counter starts above the highest ref on the page"


def test_a_cover_letter_slot_holding_another_file_is_cleared_first(tmp_path):
    # A previous run's letter on the slot (Greenhouse drops the input once a
    # file is on it): the new letter must replace it, never sit behind it.
    cover = tmp_path / "Jane_cover_letter.pdf"
    cover.write_bytes(b"%PDF")
    page = FakePage([
        gh_field("5", id="resume", type="file", kind="file", name="resume", label="Resume/CV *"),
    ])
    page.attached = ("Cover Letter", "someone_elses_letter.pdf", "6",
                     gh_field("6", id="cover_letter", type="file", kind="file", name="cover_letter", label="Cover Letter"))
    report = run_documents(page, cover=cover)
    assert page.removed == "someone_elses_letter.pdf"
    assert report.cover_letter_uploaded and page.fields["6"]["value"] == "Jane_cover_letter.pdf"
    assert page.fields["5"]["value"] == ""


def test_a_fact_asked_with_a_question_mark_is_not_a_question():
    assert engine.describes_fact("What is your legal first name?")
    assert engine.describes_fact("Preferred pronouns?")
    assert engine.describes_fact("LinkedIn URL")
    assert engine.describes_fact("What is your expected compensation range?")
    assert not engine.describes_fact("Why do you want to work here?")
    assert not engine.describes_fact("What are you looking for in your next role?")
    assert not engine.describes_fact("Tell us about a project you are proud of")


def test_free_questions_skips_the_facts():
    fields = [
        engine.Field(ref="a", kind="text", label="Preferred pronouns?"),
        engine.Field(ref="b", kind="text", label="What is your legal first name?"),
        engine.Field(ref="c", kind="textarea", label="Why this role?"),
    ]
    assert [f.ref for f in engine.Engine.free_questions(None, fields)] == ["c"]


def test_a_one_line_box_gets_one_sentence():
    long = ("I use he/him pronouns. Happy to share this on the form, and I appreciate "
            "the question being asked.")
    assert engine.Engine.one_line(long) == "I use he/him pronouns."
    assert engine.Engine.one_line("Austin, TX") == "Austin, TX"


def test_a_posting_page_is_not_filled_at_all(tmp_path):
    """Adobe's Workday posting: the apply step is behind a sign-in, so the
    tab holds a search box and a "Job Description" panel and no file input.
    The fill used to press autofill, write a tailored sentence into that
    box and report "no resume file input found", which read as "approve did
    nothing". Nowhere to attach a resume means nothing here is ours."""
    resume = tmp_path / "Jane_Doe_Resume.pdf"
    resume.write_bytes(b"%PDF")
    asked = []
    page = FakePage([
        gh_field("1", kind="text", label="Search for jobs"),
        gh_field("2", tag="textarea", kind="textarea", label="Job Description"),
    ])
    report = run_documents(page, resume=resume, answerer=lambda q: asked.append(q) or "prose")
    assert asked == [] and page.fields["2"]["value"] == ""
    assert not report.resume_uploaded and not page.marked
    assert any("no application form on this page" in e for e in report.errors)


def test_a_sign_in_page_is_not_an_application_form(tmp_path):
    """Workday's sign-in page has more than five controls (email, password,
    verify, a checkbox, a button), so the old field-count gate let it
    through: the fill found no resume slot, typed a tailored sentence into
    a hidden box labelled "This input is for robots only", and reported
    failure. Everything this fill does is putting the resume on the form,
    so no file input anywhere means the form is not here yet."""
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4")
    page = FakePage([
        gh_field("1", kind="text", label="Email Address"),
        gh_field("2", type="password", kind="text", label="Password"),
        gh_field("3", type="password", kind="text", label="Verify New Password"),
        gh_field("4", kind="checkbox", label="I agree to the Terms"),
        gh_field("5", kind="text", label="Phone"),
        gh_field("6", kind="text", label="Country"),
    ])
    report = run_documents(page, resume=resume)
    assert engine.NOT_A_FORM in report.errors
    assert not report.resume_uploaded
    assert page.fields["1"]["value"] == "", "nothing on a sign-in page is ours to touch"


def test_a_honeypot_is_never_written_into(tmp_path):
    """Anything written into one marks the application as spam. Morgan
    Stanley's said "This input is for robots only, do not enter it" and the
    fill tried to answer it with a tailored sentence."""
    assert engine.HONEYPOT.search("This input is for robots only, do not enter it")
    assert engine.HONEYPOT.search("Leave this blank")
    assert not engine.HONEYPOT.search("Why do you want to work here?")
    assert not engine.HONEYPOT.search("Enter your website")

    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4")
    page = FakePage([
        gh_field("1", id="resume", type="file", kind="file", name="resume", label="Resume/CV *"),
        gh_field("2", tag="textarea", kind="textarea",
                 label="Enter website. This input is for robots only, do not enter it"),
        gh_field("3", tag="textarea", kind="textarea", label="Why do you want to work here?"),
    ])
    report = run_documents(page, resume=resume, answerer=lambda q: f"Answer to: {q}")
    assert report.answered == ["Why do you want to work here?"]
    assert page.fields["2"]["value"] == ""


def test_an_occupied_slot_is_cleared_before_ours_goes_on():
    """Workday keeps the file input *and* the attachment, so setting ours put
    a second resume on the form - Jobright's and ours, with no way for a
    reader to tell which was sent. Greenhouse drops the input instead, which
    is the case that was already handled."""
    source = (engine.__file__ and open(engine.__file__).read())
    body = source.split("async def run_documents(")[1].split("async def upload_documents(")[0]
    # Both cases: the input gone, and the input there with a file on it.
    assert "if have is None:" in body
    assert "engine.remove_attached(pattern, exclude, DELETE_LABELS)" in body
    # The delete list is the narrow one: "Replace" and "Change" open a native
    # file chooser on some systems, which froze a tab.
    assert "replace" not in engine.DELETE_LABELS and "change" not in engine.DELETE_LABELS
    assert "replace" in engine.REMOVE_LABELS and "change" in engine.REMOVE_LABELS
    # And the fields are read again, so the upload uses the input that came back.
    assert "resume_input, cover_input = engine.file_inputs(fields)" in body


# ------------------------------------------------- which slot takes what --


def test_the_resume_never_goes_in_the_systems_own_autofill_slot(tmp_path):
    """Ashby, Workday and Lever offer "upload a resume and we will fill the
    form in". It usually sits above the real Resume field and usually has
    "resume" in its label, so it won the slot on name alone - and what it
    does is parse the file over the fields Jobright already filled rather
    than attach it (2026-10-03, asked for)."""
    resume = tmp_path / "Jane_Doe_Resume.pdf"
    resume.write_bytes(b"%PDF")
    page = FakePage([
        gh_field("1", id="autofill_resume", type="file", kind="file", name="resume",
                 label="Autofill with Resume"),
        gh_field("2", id="resume", type="file", kind="file", name="resume", label="Resume/CV *"),
    ])
    report = run_documents(page, resume=resume)
    assert page.fields["2"]["value"] == "Jane_Doe_Resume.pdf"
    assert page.fields["1"]["value"] == "", "nothing of ours goes in the parse slot"
    assert report.skipped_parse == ["Autofill with Resume"]


def test_a_form_whose_only_file_slot_is_an_autofill_says_so(tmp_path):
    resume = tmp_path / "Jane_Doe_Resume.pdf"
    resume.write_bytes(b"%PDF")
    page = FakePage([
        gh_field("1", id="parse", type="file", kind="file", label="Autofill with Resume"),
        gh_field("2", id="first_name", type="text", kind="text", label="First Name *"),
        gh_field("3", id="last_name", type="text", kind="text", label="Last Name *"),
        gh_field("4", id="email", type="text", kind="text", label="Email *"),
        gh_field("5", id="phone", type="text", kind="text", label="Phone *"),
    ])
    report = run_documents(page, resume=resume)
    assert not report.resume_uploaded and page.fields["1"]["value"] == ""
    assert any("parse a resume into the form" in e for e in report.errors)


def test_the_cover_letter_goes_on_an_additional_attachments_slot(tmp_path):
    """Greenhouse boards word the second slot "Additional Attachments" or
    "Other Documents", and nothing recognised either, so the letter went
    nowhere on those forms (2026-10-03, asked for)."""
    cover = tmp_path / "Jane_cover_letter.pdf"
    cover.write_bytes(b"%PDF")
    for label in ("Additional Attachments", "Other Documents", "Supporting documents"):
        page = FakePage([
            gh_field("5", id="resume", type="file", kind="file", name="resume", label="Resume/CV *"),
            gh_field("6", id="extra", type="file", kind="file", name="extra", label=label),
        ])
        report = run_documents(page, cover=cover)
        assert report.cover_letter_uploaded, label
        assert page.fields["6"]["value"] == "Jane_cover_letter.pdf", label
        assert page.fields["5"]["value"] == "", f"{label}: the resume slot stays empty"


def test_an_additional_attachments_slot_never_takes_the_resume(tmp_path):
    resume = tmp_path / "Jane_Doe_Resume.pdf"
    resume.write_bytes(b"%PDF")
    page = FakePage([
        gh_field("6", id="extra", type="file", kind="file", label="Additional Attachments"),
        gh_field("5", id="resume", type="file", kind="file", label="Resume/CV *"),
    ])
    report = run_documents(page, resume=resume)
    assert page.fields["5"]["value"] == "Jane_Doe_Resume.pdf" and page.fields["6"]["value"] == ""
    assert report.resume_uploaded


# ------------------------------------------- the mark vouches for a value --


def test_the_mark_records_what_it_vouches_for(tmp_path):
    """A logo that stays after the person has rewritten the answer or
    swapped the file is a claim that is no longer true, and telling at a
    glance which values are ours is the only thing the mark is for
    (2026-10-03, asked for). So every mark carries the value it was put
    there for."""
    resume = tmp_path / "Jane_Doe_Resume.pdf"
    resume.write_bytes(b"%PDF")
    cover = tmp_path / "Jane_cover_letter.pdf"
    cover.write_bytes(b"%PDF")
    page = FakePage([
        gh_field("5", id="resume", type="file", kind="file", name="resume", label="Resume/CV *"),
        gh_field("6", id="cover_letter", type="file", kind="file", name="cover_letter", label="Cover Letter"),
        gh_field("7", tag="textarea", kind="textarea", label="Why us?"),
    ])
    report = run_documents(page, resume=resume, cover=cover,
                           answerer=lambda q, **k: "Because of the work.")
    # (ref, note, logo, value) - the fourth argument is the promise.
    vouched = {args[0]: args[3] for args in page.mark_args}
    assert vouched["5"] == "Jane_Doe_Resume.pdf"
    assert vouched["6"] == "Jane_cover_letter.pdf"
    assert vouched["7"] == "Because of the work."
    assert report.resume_uploaded and report.answered


def test_the_mark_goes_when_the_value_does_and_stays_when_it_cannot_be_read():
    """Verified live against Chrome on a throwaway form: the dot survived a
    redraw of its label, vanished the moment the textarea was rewritten and
    the city box changed, left the other field's dot alone, and came off a
    file slot when another file was put on it."""
    js = engine.MARK_FN
    # The value is checked on the page's own events, not only on a redraw.
    assert 'document.addEventListener(kind, look, true)' in js
    assert '["input", "change"]' in js
    # A value that cannot be read is not a changed value.
    assert "if (now === null) return true;" in js
    # A dropped mark is forgotten, or the restore round would put it back.
    assert "delete state.items[entry.ref]" in js
    assert "if (!stillOurs(entry)) { drop(entry); continue; }" in js
    # A file is matched by name, wherever the name shows.
    assert 'if (entry.kind === "file") return now.indexOf(entry.value) >= 0;' in js


# -- the system's own "upload a resume and we'll fill the form in" ---------

def _file_field(**kw):
    base = dict(ref="1", kind="file", label="", group="", id="", name="",
                autocomplete="", placeholder="", context="")
    base.update(kw)
    return engine.Field(**base)


def test_a_parse_slot_known_only_by_the_words_round_it_is_skipped():
    """Ashby's "Autofill from resume" input has no id, no name and no label:
    the label walk reaches past it to the next field's ("Full Name"), so a
    check that reads identifiers saw nothing at all. Read off the live form
    2026-10-06."""
    parse = _file_field(ref="1", context="Autofill from resume Upload your resume here to "
                                         "autofill key application fields. Upload file")
    real = _file_field(ref="5", id="_systemfield_resume", label="Resume")
    page = FakePage([])
    eng = engine.Engine(page, engine.Adapter(), engine.Profile({}))
    resume, cover = eng.file_inputs([parse, real])
    assert resume is real and cover is None
    assert eng.report.skipped_parse and "Autofill from resume" in eng.report.skipped_parse[0]


def test_the_only_file_input_left_is_not_given_the_resume_when_it_parses():
    """The second pass is where this bit: a file is on the real slot, Ashby
    has dropped its input, and the parse slot is the only one left - so the
    one-input fallback handed it the tailored resume."""
    parse = _file_field(ref="1", context="Autofill from resume Upload your resume here to "
                                         "autofill key application fields.")
    page = FakePage([])
    eng = engine.Engine(page, engine.Adapter(), engine.Profile({}))
    resume, cover = eng.file_inputs([parse])
    assert resume is None and cover is None


def test_the_only_file_input_left_is_still_the_resume_when_it_is_a_slot():
    plain = _file_field(ref="1", context="Resume Upload File or drag and drop here")
    page = FakePage([])
    eng = engine.Engine(page, engine.Adapter(), engine.Profile({}))
    resume, _ = eng.file_inputs([plain])
    assert resume is plain


# `FIND_REMOVE_FN` and `TAG_NAMED_FILE_FN` are scripts that read somebody
# else's markup, so they are tested against markup, in a browser, by
# `tests/test_dom.py`. Two tests stood here that read their source text
# instead; they were deleted on 2026-10-06 for being unable to tell the
# difference between a rewrite and a regression. The same Workday bug,
# written `const slot = 'input[type="file"]'` instead of inline, passed the
# source check and failed the browser one.
