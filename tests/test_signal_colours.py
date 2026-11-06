"""Colour is signal only, and the one ordering that keeps a tab from freezing.

Two small primitives that everything else is read through, which is exactly
why they are easy to break while fixing something else.

**The colours.** The review page says it at the top of its own stylesheet:
purple = the agent is on it, green = it needs you (or is done), red =
rejected or failed. That promise is a chain of three links - a status is
sorted into a bucket (`BUCKET`), a bucket is painted with a tone
(`nav .row[data-tone=...]`), a tone resolves to one of three variables - and
a break anywhere in it turns "leave this alone" into "your turn". So the
chain is what is tested, in both themes, and not the hex of every variable:
the shade is taste, the mapping is meaning.

**The resume.** `browser/inject.py` must resume an auto-attached target
before it does anything else at all.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAGE = (ROOT / "review" / "index.html").read_text()

SIGNALS = ("--purple", "--green", "--red")


def _block(source: str, start: int) -> str:
    """The text of one brace-balanced block, from the brace at `start`."""
    depth = 0
    for i in range(start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
    raise AssertionError("unbalanced braces in review/index.html")


def _theme(selector: str) -> str:
    at = PAGE.index(selector)
    return _block(PAGE, PAGE.index("{", at))


def _declared(block: str) -> dict[str, str]:
    return {name: value.strip() for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block)}


LIGHT = _declared(_theme(":root {"))
DARK = _declared(_theme("@media (prefers-color-scheme: dark)"))


@pytest.mark.parametrize("theme,declared", [("light", LIGHT), ("dark", DARK)])
def test_the_three_signal_colours_are_defined_in_both_themes(theme, declared):
    """A signal colour missing from the dark block falls back to the light
    one, which on a black page is the difference between "the agent is on
    it" and an unreadable smudge - and the page has no explicit background
    toggle to catch it. They must also differ from each other: three
    meanings sharing a value is no signal at all.
    """
    missing = [name for name in SIGNALS if name not in declared]
    assert not missing, f"{missing} not declared in the {theme} theme"
    values = [declared[name].lower() for name in SIGNALS]
    assert len(set(values)) == 3, f"the {theme} signals are not distinct: {values}"
    # Soft and wash variants are what the rows and panes are filled with;
    # a missing one silently paints a row in the wrong family.
    for name in ("--purple-soft", "--green-soft", "--green-wash", "--red-soft"):
        assert name in declared, f"{name} not declared in the {theme} theme"


def _map(name: str) -> dict[str, str]:
    """A `const NAME = { ... }` object literal from the page, as a dict."""
    at = PAGE.index(f"const {name} = {{")
    body = _block(PAGE, PAGE.index("{", at))
    return {
        key: value
        for key, value in re.findall(r'(\w+)\s*:\s*"([^"]*)"', body)
    }


def test_every_status_sits_on_the_bucket_its_colour_promises():
    """`BUCKET` is the whole decision about what a row means, and the two
    halves that matter are the ones a person acts on: anything the agent is
    carrying must be the agent's colour so it is left alone, and the two
    statuses that are actually waiting on a human - a tailored job to
    approve, a filled form to submit - must be green. A status landing in
    the wrong bucket is a job that never gets looked at, or one the person
    interrupts for nothing.
    """
    assert _map("BUCKET") == {
        "awaiting_review": "you", "filled": "you",
        "queued": "agent", "tailoring": "agent", "approved": "agent", "filling": "agent",
        "submitted": "done", "skipped": "stop", "failed": "stop",
    }


def test_each_bucket_is_painted_with_its_own_signal_colour():
    """The CSS half of the chain. Checked as "which variable does this tone
    resolve to", not as a hex, because the shade is free to change and the
    family is not.
    """
    # One tone has several rules; every declaration for it counts.
    tones: dict[str, str] = {}
    for tone, body in re.findall(r'nav \.row\[data-tone="(\w+)"\][^{]*\{([^}]*)\}', PAGE):
        tones[tone] = tones.get(tone, "") + body
    assert set(tones) == {"agent", "you", "done", "stop"}, tones
    assert "var(--purple)" in tones["agent"] and "var(--green)" not in tones["agent"]
    assert "var(--green)" in tones["you"]
    assert "var(--green)" in tones["done"]
    assert "var(--red)" in tones["stop"]
    # The job pane itself, which is the larger surface the person reads.
    assert "section.detail.you" in PAGE and "section.detail.agent" in PAGE
    pane = dict(re.findall(r'section\.detail\.(you|agent)\s*\{([^}]*)\}', PAGE))
    assert "--green-wash" in pane["you"]
    assert "--purple-soft" in pane["agent"]


def test_an_auto_attached_target_is_resumed_before_anything_else():
    """A source-reading test, and justified: the text IS the promise.

    Fixed 2026-10-29. A browser-level auto-attach pauses any target a page
    opens, and in Chrome 156 it does so even with
    `waitForDebuggerOnStart: False`. A popup shares its opener's renderer,
    so until somebody resumes it the opener's main thread is stopped:
    Jobright's Apply left a blank tab and froze jobright.ai behind it.

    What is promised is the *position* of one statement - the resume has to
    be sent first in the `Target.attachedToTarget` branch, and in
    particular before the `type != page` return, because a paused blank tab
    has no URL and nothing we filter on is known yet. No runtime assertion
    tells the fix from the bug: both produce a tab that reports nothing, and
    a test that only asserts the call happens passes with it placed after
    the return, which is the bug. So the ordering is read off the source,
    and that is the honest check rather than a weak one.
    """
    source = (ROOT / "browser" / "inject.py").read_text()
    branch = source.index('elif method == "Target.attachedToTarget":')
    # The branch runs to the next `elif method ==` at the same level.
    end = source.index("elif method ==", branch + 10)
    body = source[branch:end]

    resume = body.index('await self.send("Runtime.runIfWaitingForDebugger"')
    guard = body.index('info.get("type") != "page"')
    assert resume < guard, (
        "the auto-attach resume must come before the non-page return: a "
        "paused target has no URL yet, and its opener is frozen until it is "
        "resumed"
    )
    # Nothing may be awaited before it either - a send that blocks leaves the
    # opener stopped for as long as it takes.
    assert "await self.send" not in body[:resume], (
        "something is awaited before the resume in the attach branch"
    )
