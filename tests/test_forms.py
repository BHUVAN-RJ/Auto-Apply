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


def run_documents(page, resume=None, cover=None, adapter=None, answerer=None, corrections=None):
    return asyncio.run(engine.run_documents(page, adapter or forms.Greenhouse(), "TARGET1234",
                                            resume=resume, cover_letter=cover, answerer=answerer,
                                            corrections=corrections))


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
