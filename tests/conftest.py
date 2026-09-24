import pytest

from tailor import prompts, workshop


@pytest.fixture(autouse=True)
def _own_prompt_edits(tmp_path, monkeypatch):
    """No test reads the person's own prompt edits or Workshop history:
    both would change what a model stub is sent, on one machine only."""
    monkeypatch.setattr(prompts, "OVERLAY", tmp_path / "_prompts")
    monkeypatch.setattr(workshop, "STORE", tmp_path / "_workshop.json")
    # A test that re-tailors a few times must not start a real model call.
    monkeypatch.setattr(workshop, "LEARN_EVERY", 10**6)
