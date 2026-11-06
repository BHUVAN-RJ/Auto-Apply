"""A sentinel can never reach the provider call.

`byhand.BY_HAND` and `claudecode.CLAUDE_CODE` sit in the slot a model name
goes in (`Job.tailor_model`) and are deliberately not model names. One
escaped anyway: `workshop.workshop_model()` falls back to
`llm.premium_model()`, which answers `by-hand` when the paste switch is on,
and `logs/server.log` carries three
`OpenRouter returned 400: {"message":"by-hand is not a valid model ID"}`
from `workshop.learn` running in the background. So the rule is enforced at
the door rather than at each call site: `llm.chat` routes a sentinel or
refuses it, and never puts it in a payload.
"""

import pytest

from tailor import byhand, claudecode, llm

SENTINELS = [byhand.BY_HAND, claudecode.CLAUDE_CODE]


@pytest.fixture(autouse=True)
def never_on_the_wire(monkeypatch):
    """Any HTTP call during these tests is the bug itself."""
    def forbidden(*args, **kwargs):
        raise AssertionError(f"a sentinel reached the provider: {kwargs.get('json')}")

    monkeypatch.setattr(llm.httpx, "post", forbidden)
    monkeypatch.setattr(llm.httpx, "stream", forbidden)


@pytest.mark.parametrize("sentinel", SENTINELS)
def test_a_sentinel_is_recognised_however_it_is_spelled(sentinel):
    assert llm.is_sentinel(sentinel)
    assert llm.is_sentinel(f"  {sentinel.upper()}  ")
    assert not llm.is_sentinel("anthropic/claude-opus-5.5")
    assert not llm.is_sentinel(llm.DEFAULT_MODEL)
    assert not llm.is_sentinel(None)


@pytest.mark.parametrize("sentinel", SENTINELS)
def test_a_sentinel_is_routed_to_the_subscription_not_to_a_provider(monkeypatch, sentinel):
    monkeypatch.setattr(claudecode, "available", lambda: True)
    monkeypatch.setattr(claudecode, "reply",
                        lambda messages, **k: ("tailored", {"input_tokens": 2,
                                                            "output_tokens": 9,
                                                            "seconds": 1.0}))
    assert llm.chat([{"role": "user", "content": "x"}], model=sentinel) == "tailored"
    assert llm.complete("sys", "user", model=sentinel) == "tailored"


@pytest.mark.parametrize("sentinel", SENTINELS)
def test_a_sentinel_with_no_subscription_is_refused_in_words(monkeypatch, sentinel):
    """Refused, never forwarded: the caller gets an `LLMError` like any other
    provider failure, with the sentinel named so the miswiring is findable."""
    monkeypatch.setattr(claudecode, "available", lambda: False)
    monkeypatch.setattr(claudecode, "shutil", claudecode.shutil)
    monkeypatch.setattr(claudecode, "reply",
                        lambda *a, **k: (_ for _ in ()).throw(
                            claudecode.NotAvailable("claude is not on PATH")))
    with pytest.raises(llm.LLMError) as caught:
        llm.chat([{"role": "user", "content": "x"}], model=sentinel)
    # Either refusal says which path could not answer; neither is a 400 from
    # a provider that was handed a word instead of a model.
    assert "OpenRouter returned" not in str(caught.value)
    assert sentinel in str(caught.value) or "Claude Code" in str(caught.value)


@pytest.mark.parametrize("sentinel", SENTINELS)
def test_a_sentinel_never_streams(sentinel):
    with pytest.raises(llm.LLMError) as caught:
        list(llm.stream([{"role": "user", "content": "x"}], model=sentinel))
    assert sentinel in str(caught.value)


@pytest.mark.parametrize("sentinel", SENTINELS)
def test_the_default_model_being_a_sentinel_is_caught_too(monkeypatch, sentinel):
    """A caller that passes no model at all gets the same protection, since
    `OPENROUTER_TAILOR_MODEL` is read from a file a person edits."""
    monkeypatch.setenv("OPENROUTER_TAILOR_MODEL", sentinel)
    monkeypatch.setattr(claudecode, "available", lambda: False)
    monkeypatch.setattr(claudecode, "reply",
                        lambda *a, **k: (_ for _ in ()).throw(
                            claudecode.NotAvailable("claude is not on PATH")))
    with pytest.raises(llm.LLMError):
        llm.chat([{"role": "user", "content": "x"}])


def test_the_workshop_cannot_send_the_paste_sentinel_to_openrouter(monkeypatch):
    """The actual path from the log: the Workshop's background `learn()` asks
    `premium_model()` for a name it is going to send."""
    from server import settings
    from tailor import workshop

    monkeypatch.delenv(workshop.MODEL_ENV, raising=False)
    monkeypatch.setattr(settings, "claude_code", lambda: False)
    monkeypatch.setattr(settings, "opus_by_hand", lambda: True)
    assert workshop.workshop_model() == byhand.BY_HAND

    monkeypatch.setattr(claudecode, "available", lambda: False)
    monkeypatch.setattr(claudecode, "reply",
                        lambda *a, **k: (_ for _ in ()).throw(
                            claudecode.NotAvailable("claude is not on PATH")))
    # The forbidden-httpx fixture is the assertion: this must not be a 400
    # from OpenRouter, it must be an `LLMError` raised before the request.
    with pytest.raises(llm.LLMError):
        llm.chat([{"role": "user", "content": "x"}], model=workshop.workshop_model())
