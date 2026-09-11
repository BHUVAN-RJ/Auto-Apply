"""The never-submit guarantee. These are the tests that matter most."""

import pytest

from browser.guard import (
    FORBIDDEN_ACTIONS,
    SubmitBlocked,
    check_click,
    describes_submit,
)


@pytest.mark.parametrize("label", [
    "Submit", "SUBMIT", "Submit application", "Submit Application",
    "Apply", "Apply now", "Apply Now", "apply for this job",
    "Send application", "Send now", "Finish", "Complete application",
    "Confirm and submit", "Confirm and send",
])
def test_submit_labels_are_blocked(label):
    assert describes_submit(label), f"{label!r} must be treated as a submit control"
    with pytest.raises(SubmitBlocked):
        check_click(text=label)


@pytest.mark.parametrize("label", [
    "Save and continue", "Save draft", "Next", "Next step", "Continue",
    "Search", "Upload resume", "Apply filters", "Back", "Cancel",
    "Add another", "Choose file",
])
def test_navigation_labels_are_allowed(label):
    assert not describes_submit(label), f"{label!r} must not be blocked"
    check_click(text=label)  # must not raise


def test_an_icon_button_is_caught_by_its_aria_label():
    """Visible text can be empty; the accessible name still gives it away."""
    with pytest.raises(SubmitBlocked):
        check_click(text="", aria_label="Submit application", id="btn-1")


def test_a_button_is_caught_by_its_id():
    with pytest.raises(SubmitBlocked):
        check_click(text="→", id="submit-application-button")


def test_a_button_is_caught_by_its_name_attribute():
    with pytest.raises(SubmitBlocked):
        check_click(text="Go", name="commit_submit")


def test_surrounding_prose_does_not_block_an_innocent_button():
    """Long text is a paragraph, not a control label."""
    prose = ("By clicking apply you agree to our terms and conditions, and you "
             "consent to us processing your application data for this role. " * 3)
    check_click(text=prose)  # must not raise


def test_blocked_message_says_why():
    with pytest.raises(SubmitBlocked, match="never submits"):
        check_click(text="Submit application")


def test_empty_and_missing_fields_are_safe():
    check_click(text=None, aria_label="", id=None)


def test_javascript_and_enter_key_actions_are_removed():
    """Either would route around every other guard here."""
    assert "evaluate" in FORBIDDEN_ACTIONS, "arbitrary JS would make the guard decorative"
    assert "send_keys" in FORBIDDEN_ACTIONS, "Enter submits a single-input form"
