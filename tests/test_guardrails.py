"""The tests that may not quietly leave, and the tripwire on the agent that guards them.

Every real defect in this project's history survived a green suite, so the
suite's worth is not that it passes: it is that it has never been allowed to
get smaller for a bad reason. Two things are written down here.

**The registry.** A list of test ids - `module::test_name` - that encode a
rule the app promises the person using it, rather than an implementation
detail. Anyone may add to it; a test leaves it only through the test
guardian (`.claude/agents/test-guardian.md`), whose judgement is whether the
*rule* still exists, never whether the test is currently passing. The checks
below catch the four ways a test leaves without being deleted: renamed away,
decorated with skip or xfail, hidden behind a module-level `pytestmark`, or
moved into a file the runner never reaches.

**The digest.** A sha256 of the guardian's own definition. Be honest about
what that is: Claude Code agent definitions cannot deny another agent
filesystem access, and nothing in a Python test can either. The guarantee is
not a sandbox and not a permission boundary - it is a **tripwire plus a rule
written down**. Any edit to the guardian, by anyone, for any reason, turns
the suite red and names the file, so the edit is impossible to land
unnoticed and has to be explained in the same breath as whatever it was
made for. That is the whole mechanism, and it is enough on a machine where
one person reads every diff.
"""

import ast
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

GUARDIAN = ROOT / ".claude" / "agents" / "test-guardian.md"
# Recorded 2026-10-29. This is the tripwire described above. Changing the
# guardian means changing this line in the same diff, which is exactly the
# moment the change has to be justified out loud.
GUARDIAN_SHA256 = "605f678eac6886a567bf8232ee3ead602af2bb60ee451e43b5dc3878d0c97607"


# Each entry says which promise the test keeps, because that sentence is what
# the guardian judges a removal against. A test with no sentence to write here
# does not belong in the registry.
PROTECTED = {
    # 1-8: the invariants. CLAUDE.md, "The rule the whole project exists to
    # protect". These are not duplicated anywhere here; they are listed so a
    # deletion in that file is caught by this one too.
    "test_invariants.py::test_a_submit_control_is_refused":
        "the agent may never click a submit/apply/send/finish control",
    "test_invariants.py::test_a_visa_question_is_protected":
        "no visa, sponsorship or work-authorisation question is ever answered",
    "test_invariants.py::test_the_answering_model_refuses_a_visa_question":
        "the tailor model refuses a protected question too, not just the clicker",
    "test_invariants.py::test_the_filler_never_fills_authorisation_from_the_profile":
        "authorisation is collected for the human and never auto-filled",
    "test_invariants.py::test_nothing_kills_or_owns_the_browser":
        "the fill leaves the window open; browser-use never launches Chrome",
    "test_invariants.py::test_the_data_directory_is_not_the_clone":
        "an update can never touch the person's data",
    "test_invariants.py::test_no_prompt_grants_submission":
        "a prompt edit cannot reach the never-submit rule",
    "test_invariants.py::test_only_the_review_endpoint_presses_submit":
        "invariant 6: nothing the agent runs may import the submit presser",
    "test_invariants.py::test_the_guard_still_refuses_the_control_the_button_presses":
        "the human's Submit aims at exactly what the agent is refused",
    "test_invariants.py::test_the_apply_press_can_never_be_a_submit":
        "Apply is pressed only on a page with nothing on it that could be sent",
    "test_invariants.py::test_automation_cannot_reach_outbound_mail":
        "delivery is the person's click; the pipeline may not import mailing",
    "test_invariants.py::test_the_identity_service_cannot_act_on_a_job":
        "the duplicate check reads; it never writes or acts",
    "test_invariants.py::test_the_table_driven_runner_cannot_press_submit":
        "the selector-table runner never resolves a submit expression",

    # A press is not a submission. The job moves only on a confirmation page.
    "test_press_submit.py::test_a_cancel_or_a_save_for_later_is_never_pressed":
        "the presser skips cancel / save-for-later / sign-in labels",
    "test_press_submit.py::test_a_confirmation_needs_the_words_and_the_form_gone":
        "a receipt is the phrase AND the form gone, never the phrase alone",
    "test_press_submit.py::test_a_form_still_on_the_screen_is_not_a_submission":
        "a press that did not land leaves the job exactly where it was",

    # The capture-to-queue path: the way a job enters the app at all.
    "test_capture.py::test_capture_starts_the_pipeline_at_once":
        "capturing is the instruction to tailor, and it starts immediately",
    "test_capture.py::test_capture_never_starts_the_fill":
        "nothing past checkpoint 1 starts from a capture",
    "test_capture.py::test_a_newly_queued_employer_tab_closes_itself_and_nothing_else_does":
        "only the employer tab Apply opened may close itself",
    "test_capture.py::test_add_opens_the_captured_job_in_autopilot":
        "adding a job points the Autopilot tab at it without taking the front",
    "test_seen.py::test_capture_refuses_source_pages_and_dedupes":
        "a listing site's page is not a job, and one posting is one row",
    "test_capture_path.py::test_a_capture_puts_the_posting_in_the_queue_before_anything_models_it":
        "the queue row is created, at QUEUED, carrying the posting's own URL",
    "test_capture_path.py::test_a_listing_sites_page_queues_nothing_and_starts_nothing":
        "refusing a source page starts no pipeline either",
    "test_capture_path.py::test_the_text_the_browser_saw_is_kept_for_the_fetch_that_may_get_a_shell":
        "the employer page's own text survives the capture, keyed by job id",

    # Colour is signal only. The chain is status -> bucket -> tone -> variable.
    "test_signal_colours.py::test_the_three_signal_colours_are_defined_in_both_themes":
        "purple/green/red exist, and are distinct, in light and in dark",
    "test_signal_colours.py::test_every_status_sits_on_the_bucket_its_colour_promises":
        "purple = the agent is on it, green = it needs you, red = rejected",
    "test_signal_colours.py::test_each_bucket_is_painted_with_its_own_signal_colour":
        "the CSS half of the chain: agent->purple, you/done->green, stop->red",

    # Fixed 2026-10-29; the ordering of two statements is the whole promise.
    "test_signal_colours.py::test_an_auto_attached_target_is_resumed_before_anything_else":
        "a paused target freezes its opener, so it is resumed first of all",
    # The other half of the same fix, from the behaviour's side: the resume is
    # unconditional, and it is the only thing that may be unconditional.
    "test_inject.py::test_only_page_targets_are_touched":
        "nothing but the auto-attach resume reaches a target that is not a page",
}

# The files the registry names. Collected in one subprocess below; none of
# them needs browser-use, so none of them aborts on this machine.
MODULES = sorted({entry.split("::", 1)[0] for entry in PROTECTED})

SILENCERS = ("skip", "skipif", "xfail")


def _decorator_names(node: ast.AST) -> set[str]:
    """Every dotted name mentioned in a decorator, flattened."""
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            names.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            names.add(sub.attr)
    return names


def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _tests(tree: ast.Module) -> dict[str, list[ast.AST]]:
    return {
        node.name: list(node.decorator_list)
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    }


@pytest.mark.parametrize("entry", sorted(PROTECTED), ids=lambda e: e)
def test_a_protected_test_still_exists_under_its_own_name(entry):
    """Renaming a test away is a removal: the registry is keyed on the name,
    and a rule nobody can look up is a rule nobody maintains."""
    module, name = entry.split("::", 1)
    path = TESTS / module
    assert path.exists(), f"{module} is gone; {PROTECTED[entry]}"
    assert name in _tests(_module(path)), (
        f"{entry} is gone. It protects: {PROTECTED[entry]}. "
        "Only the test guardian may remove it."
    )


@pytest.mark.parametrize("entry", sorted(PROTECTED), ids=lambda e: e)
def test_a_protected_test_is_never_decorated_away(entry):
    """skip, skipif and xfail take a test out of the suite while leaving it
    in the file, which reads as coverage and is not. A rule that genuinely
    moved is a deletion the guardian can judge; a silencer is not."""
    module, name = entry.split("::", 1)
    # A missing test is the other check's failure, not this one's.
    decorators = _tests(_module(TESTS / module)).get(name, [])
    for decorator in decorators:
        offending = _decorator_names(decorator) & set(SILENCERS)
        assert not offending, (
            f"{entry} is marked {sorted(offending)}. It protects: "
            f"{PROTECTED[entry]}."
        )


@pytest.mark.parametrize("module", MODULES)
def test_no_module_holding_a_protected_test_is_silenced_whole(module):
    """The same trick one level up: a module-level `pytestmark` with a skip
    on it empties the file without touching a single test."""
    tree = _module(TESTS / module)
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets):
            continue
        offending = _decorator_names(node.value) & set(SILENCERS)
        assert not offending, f"{module} is silenced whole by pytestmark {sorted(offending)}"


def test_every_protected_test_is_still_collected_by_pytest():
    """The file checks above read source; this one asks pytest itself, which
    is the only way to catch a test moved into a file the runner ignores, a
    conftest that deselects it, or a name the collector does not see.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:warnings",
         *(f"tests/{module}" for module in MODULES)],
        cwd=ROOT, capture_output=True, text=True,
    )
    # pytest prints node ids relative to the rootdir, which is the clone.
    collected = {
        line.strip().removeprefix("tests/")
        for line in result.stdout.splitlines() if "::" in line
    }
    # A parametrised test is collected as `name[case]`; the registry names
    # the function, so compare on the part before the bracket.
    bare = {item.split("[", 1)[0] for item in collected}
    missing = sorted(entry for entry in PROTECTED if entry not in bare)
    assert not missing, (
        "pytest no longer collects: " + ", ".join(missing)
        + f"\n--collect-only said:\n{result.stdout[-2000:]}"
    )


def test_the_guardians_own_definition_is_the_one_that_was_agreed():
    """A tripwire, not a sandbox.

    Nothing here can stop an agent editing `.claude/agents/test-guardian.md`
    - a Claude Code agent definition grants no filesystem authority over
    another agent, and a test grants none at all. What this does is make an
    edit impossible to land unnoticed: the suite goes red, names this file,
    and whoever changed the guardian has to say so and re-record the digest
    deliberately. The protection is the visibility, plus the rule in
    CLAUDE.md that only the guardian removes a test.
    """
    assert GUARDIAN.exists(), (
        "the test guardian's definition is gone; no path is left by which a "
        "test may legitimately be removed"
    )
    digest = hashlib.sha256(GUARDIAN.read_bytes()).hexdigest()
    assert digest == GUARDIAN_SHA256, (
        "the test guardian's definition has changed.\n"
        f"recorded: {GUARDIAN_SHA256}\n"
        f"on disk:  {digest}\n"
        "If the change was intended, say what rule moved and record the new "
        "digest in the same diff. If it was not, revert it."
    )


def test_the_guardian_still_refuses_a_removal_whose_reason_is_a_red_suite():
    """The one sentence the guardian exists for, and the one standard it
    inherits from CLAUDE.md. Both are prose in a prompt, so the only honest
    check is that they are still said - an agent definition has no behaviour
    to assert from here.
    """
    # Wrapped prose: compare on one line so a reflow is not a failure.
    text = " ".join(GUARDIAN.read_text().split())
    assert "A failing test is never a justification." in text
    assert "does the rule this test protects still exist?" in text
    # The house standard on source-reading tests, and the three places where
    # the text genuinely is the promise.
    # Quoted from CLAUDE.md as a blockquote, so the marker sits mid-sentence.
    assert "cannot tell a rewrite from a" in text
    assert "press_submit" in text and "open_apply" in text
    assert "runIfWaitingForDebugger" in text
