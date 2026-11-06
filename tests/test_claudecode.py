"""The Claude Code path: the subscription on this machine instead of an API.

The CLI is not called here. These are about the three things that decide
whether the path is safe and whether it is cheap: which flags go on the
command, how a four-turn retry becomes one prompt, and what happens when it
fails. The cost measurements are in the module's own docstring, taken live.
"""
from __future__ import annotations

import json

import pytest

from tailor import claudecode


def test_the_sentinel_is_not_a_model_name():
    """The same decision `byhand.BY_HAND` makes: nothing can hand this to a
    provider believing it is a model."""
    assert "/" not in claudecode.CLAUDE_CODE
    assert "claude-3" not in claudecode.CLAUDE_CODE
    assert claudecode.CLAUDE_CODE == "claude-code"


def test_the_flags_that_make_a_completion_out_of_an_agent():
    """Each one is load bearing and each one was measured. A bare `claude -p`
    loaded 100,268 tokens of tools and settings before reading the prompt;
    `--setting-sources ""` is what keeps this repository's own CLAUDE.md -
    its largest file - off every call."""
    flags = list(claudecode.FLAGS)
    assert "-p" in flags
    assert flags[flags.index("--allowed-tools") + 1] == "", "no tools, or it is an agent"
    assert flags[flags.index("--setting-sources") + 1] == "", "no CLAUDE.md on every call"
    assert "--strict-mcp-config" in flags, "the person's MCP servers are not ours to use"
    assert flags[flags.index("--output-format") + 1] == "json"


def test_a_four_turn_retry_becomes_one_prompt_with_the_roles_kept():
    """`claude -p` takes a prompt, not a conversation, and the tailor's retry
    is four turns: the rules, the request, the attempt, the rejection. An
    attempt and the rejection of it read as the same thing unless they are
    marked."""
    system, user = claudecode.flatten([
        {"role": "system", "content": "THE RULES"},
        {"role": "user", "content": "tailor this"},
        {"role": "assistant", "content": "attempt one"},
        {"role": "user", "content": "rejected: bullet 7 grew"},
    ])
    assert system == "THE RULES"
    assert user.index("tailor this") < user.index("attempt one") < user.index("rejected")
    assert "## Your previous reply" in user
    assert "THE RULES" not in user, "the stable half stays a stable prefix, so it caches"


def test_a_single_turn_is_sent_as_itself():
    """Most calls are one user turn, and wrapping it in headings would change
    the prompt every call site spent attention on."""
    system, user = claudecode.flatten([
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "the posting"},
    ])
    assert user == "the posting"


def test_several_system_prompts_join_rather_than_the_last_one_winning():
    system, _ = claudecode.flatten([
        {"role": "system", "content": "one"},
        {"role": "system", "content": "two"},
        {"role": "user", "content": "go"},
    ])
    assert system == "one\n\ntwo"


def test_nothing_to_send_is_refused_rather_than_sent_empty(monkeypatch):
    monkeypatch.setattr(claudecode, "available", lambda: True)
    with pytest.raises(ValueError):
        claudecode.reply([{"role": "system", "content": "rules"}])


def test_the_api_key_is_kept_out_of_the_environment(monkeypatch):
    """This path is the subscription. The CLI prefers an exported
    `ANTHROPIC_API_KEY` if it finds one, which would quietly bill the API
    instead - the one outcome the whole module exists to avoid."""
    seen = {}

    class Done:
        returncode = 0
        stdout = json.dumps({"result": "ok", "usage": {}})
        stderr = ""

    def fake_run(command, **kwargs):
        seen["env"] = kwargs.get("env") or {}
        seen["command"] = command
        seen["stdin"] = kwargs.get("input")
        return Done()

    monkeypatch.setattr(claudecode.subprocess, "run", fake_run)
    monkeypatch.setattr(claudecode.shutil, "which", lambda _b: "/usr/bin/claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-travel")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "also-not")
    text, _ = claudecode.reply([{"role": "user", "content": "hello"}])
    assert text == "ok"
    assert "ANTHROPIC_API_KEY" not in seen["env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in seen["env"]
    assert seen["stdin"] == "hello", "the prompt goes on stdin, not in the argument list"


def test_a_model_is_pinned_on_the_command(monkeypatch):
    class Done:
        returncode = 0
        stdout = json.dumps({"result": "x", "usage": {}})
        stderr = ""

    seen = {}
    monkeypatch.setattr(claudecode.subprocess, "run",
                        lambda c, **k: (seen.update(command=c), Done())[1])
    monkeypatch.setattr(claudecode.shutil, "which", lambda _b: "/usr/bin/claude")
    claudecode.reply([{"role": "user", "content": "hi"}], model="opus")
    assert seen["command"][seen["command"].index("--model") + 1] == "opus"


@pytest.mark.parametrize("stdout,returncode,why", [
    ("", 1, "exited 1"),
    ("not json at all", 0, "did not answer JSON"),
    (json.dumps({"is_error": True, "result": "rate limit"}), 0, "rate limit"),
    (json.dumps({"result": "", "usage": {}}), 0, "returned no text"),
    (json.dumps({"result": None}), 0, "returned no text"),
])
def test_every_way_it_can_fail_is_said_out_loud(monkeypatch, stdout, returncode, why):
    """A caller that cannot tell "the model said nothing" from "the model was
    not asked" writes an empty resume to disk."""
    class Done:
        pass

    Done.returncode, Done.stdout, Done.stderr = returncode, stdout, ""
    monkeypatch.setattr(claudecode.subprocess, "run", lambda *a, **k: Done())
    monkeypatch.setattr(claudecode.shutil, "which", lambda _b: "/usr/bin/claude")
    with pytest.raises(RuntimeError) as caught:
        claudecode.reply([{"role": "user", "content": "hi"}])
    assert why in str(caught.value)


def test_a_missing_cli_is_not_a_crash(monkeypatch):
    monkeypatch.setattr(claudecode.shutil, "which", lambda _b: None)
    with pytest.raises(claudecode.NotAvailable):
        claudecode.reply([{"role": "user", "content": "hi"}])


def test_the_one_door_routes_the_sentinel_and_nothing_else(monkeypatch):
    """`llm.chat` is the only place that talks to a model. A failure comes
    back as `LLMError`, like every other provider failure, so a pipeline that
    handles one handles this."""
    from tailor import llm

    monkeypatch.setattr(claudecode, "reply",
                        lambda messages, **k: ("tailored", {"input_tokens": 2,
                                                            "cache_read_input_tokens": 28769,
                                                            "output_tokens": 125,
                                                            "seconds": 3.1}))
    assert llm.chat([{"role": "user", "content": "x"}],
                    model=claudecode.CLAUDE_CODE) == "tailored"

    def boom(messages, **k):
        raise RuntimeError("claude -p exited 1: whatever")

    monkeypatch.setattr(claudecode, "reply", boom)
    with pytest.raises(llm.LLMError):
        llm.chat([{"role": "user", "content": "x"}], model=claudecode.CLAUDE_CODE)


def test_the_premium_path_prefers_the_subscription_then_the_paste(monkeypatch):
    from server import settings
    from tailor import byhand, llm

    monkeypatch.setattr(claudecode, "available", lambda: True)
    monkeypatch.setattr(settings, "claude_code", lambda: True)
    assert llm.premium_model() == claudecode.CLAUDE_CODE

    # The switch is on but the CLI is gone: the paste, not a silent API bill.
    monkeypatch.setattr(claudecode, "available", lambda: False)
    monkeypatch.setattr(settings, "opus_by_hand", lambda: True)
    assert llm.premium_model() == byhand.BY_HAND

    monkeypatch.setattr(settings, "claude_code", lambda: False)
    monkeypatch.setattr(settings, "opus_by_hand", lambda: False)
    assert llm.premium_model() == llm.premium_api_model()


def test_it_is_off_until_the_person_turns_it_on():
    """It spends their Claude allowance rather than an OpenRouter balance,
    and which of those they would rather spend is theirs to say."""
    from server import settings
    assert settings.DEFAULTS["claude_code"] is False
