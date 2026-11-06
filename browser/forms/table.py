"""What each application system's form looks like, as data.

`browser/forms/engine.py` matches a control by its identifiers and then by
the look of its label, which is the only thing that can work on a form
nobody has seen. Where a system has been read - its own `data-automation-id`
or `name` attributes, the control that clears an occupied upload slot, the
element that says which page of the flow is on screen - that knowledge is a
fact about that system and belongs in a file, next to the timings it needs,
rather than as constants in a module per system.

`table.json` is that file. A system is added to it only once its anchors
have been seen on a real page (`tests/dom/` holds the markup; the Workday
entry is written against `workday_work_experience.html` and
`workday_two_resumes_live.html`, both scrubbed copies of real pages).
Anything not in the table keeps the engine's own matching, unchanged.

Nothing here touches a browser: `steps.py` runs what this reads, and
`invalid()` is what the tests check a hand-written entry against.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Iterator, Optional

TABLE = Path(__file__).resolve().parent / "table.json"

# The methods `steps.py` knows. A step naming anything else is invalid, so a
# typo in the table is caught by a test rather than by a silent no-op on a
# form.
METHODS = ("click", "set_text", "select_option", "upload", "expect_name", "expect_gone")

# What a step's `path` may be written against. `%FIELD%` is the element the
# field's own `paths` resolved to, so a step can stay inside its own slot
# instead of searching the page - the whole of the 2026-10-11 "cleared the
# wrong control" class of bug.
FIELD_TOKEN = "%FIELD%"

# A page's named XPaths. `submit` is here to be *recognised*, never pressed:
# `steps.Runner` has no method that takes it, and `advance` excludes it by
# its own text so a walk cannot reach Submit by asking for Continue.
PAGE_KEYS = ("container", "review", "progress", "advance", "submit", "signed_in")


@dataclass(frozen=True)
class Step:
    method: str
    path: str = ""
    document: str = ""          # which document an upload puts on
    value: str = ""             # a literal, for select_option
    optional: bool = False      # a failure is not the field failing
    repeat: int = 1             # press until it stops changing anything
    settle: float = 0.0         # seconds after the step
    wait: str = ""              # a key in `params`, for the methods that wait


@dataclass(frozen=True)
class FieldRule:
    key: str
    paths: tuple[str, ...]
    steps: tuple[Step, ...] = ()

    @property
    def is_text(self) -> bool:
        """A field with no steps is a text box: find it, write the profile's
        value into it. That is most of them."""
        return not self.steps


@dataclass(frozen=True)
class Rules:
    name: str
    hosts: tuple[str, ...]
    params: dict
    pages: dict
    fields: dict
    slots: dict = dataclass_field(default_factory=dict)
    options: dict = dataclass_field(default_factory=dict)
    checked: str = ""

    def field(self, key: str) -> Optional[FieldRule]:
        return self.fields.get(key)

    def page(self, key: str) -> str:
        return self.pages.get(key, "")

    def param(self, key: str, default: float = 0.0) -> float:
        value = self.params.get(key, default)
        return float(value) if isinstance(value, (int, float)) else default

    def wait_for(self, step: Step, default: float = 0.0) -> float:
        return self.param(step.wait, default) if step.wait else default


def _steps(raw: list) -> tuple[Step, ...]:
    out = []
    for s in raw or []:
        if not isinstance(s, dict):
            continue
        out.append(Step(method=str(s.get("method", "")),
                        path=str(s.get("path", "")),
                        document=str(s.get("document", "")),
                        value=str(s.get("value", "")),
                        optional=bool(s.get("optional")),
                        repeat=int(s.get("repeat", 1) or 1),
                        settle=float(s.get("settle", 0.0) or 0.0),
                        wait=str(s.get("wait", ""))))
    return tuple(out)


def _rules(name: str, raw: dict) -> Rules:
    fields = {}
    for key, spec in (raw.get("fields") or {}).items():
        if not isinstance(spec, dict):
            continue
        fields[key] = FieldRule(key=key,
                                paths=tuple(str(p) for p in (spec.get("paths") or [])),
                                steps=_steps(spec.get("steps")))
    return Rules(name=name,
                 hosts=tuple(str(h) for h in (raw.get("hosts") or [])),
                 params=dict(raw.get("params") or {}),
                 pages=dict(raw.get("pages") or {}),
                 fields=fields,
                 slots=dict(raw.get("slots") or {}),
                 options=dict(raw.get("options") or {}),
                 checked=str(raw.get("checked") or ""))


def load(path: Optional[Path] = None) -> dict[str, Rules]:
    """The table, read at call time. Resolved here and not in the signature,
    because a module-level default freezes at import and a test that points
    the table somewhere else would be ignored."""
    source = path or TABLE
    raw = json.loads(source.read_text())
    return {name: _rules(name, spec) for name, spec in (raw.get("systems") or {}).items()}


def _host_of(url: str) -> str:
    m = re.match(r"https?://([^/?#]+)", url or "", re.I)
    return (m.group(1) if m else "").lower().split(":")[0]


def rules_for(url: str, path: Optional[Path] = None) -> Optional[Rules]:
    """The system whose hosts the URL is under, or None - which means the
    engine's own matching, as before."""
    host = _host_of(url)
    if not host:
        return None
    for rules in load(path).values():
        for suffix in rules.hosts:
            if host == suffix or host.endswith("." + suffix):
                return rules
    return None


def _xpath_shape(expr: str) -> str:
    """Not an XPath parser: the cheap mistakes a hand-written entry makes.
    Whether an expression selects what it means to is answered by running it
    in a browser against real markup, which is what tests/test_dom.py does."""
    if not expr.strip():
        return "empty"
    if not re.match(r"^[(.]|^//|^/", expr.strip()):
        return "does not start at a node (. or / or ()"
    for pair in ("()", "[]"):
        if expr.count(pair[0]) != expr.count(pair[1]):
            return f"unbalanced {pair}"
    if expr.count("'") % 2 or expr.count('"') % 2:
        return "unbalanced quotes"
    return ""


def invalid(rules: Rules) -> list[str]:
    """Everything wrong with one system's entry, as sentences. Empty is the
    entry being usable."""
    out: list[str] = []
    if not rules.hosts:
        out.append("no hosts, so nothing will ever match it")
    for key, expr in rules.pages.items():
        if key not in PAGE_KEYS:
            out.append(f"page '{key}' is not one of {', '.join(PAGE_KEYS)}")
        why = _xpath_shape(expr)
        if why:
            out.append(f"page '{key}': {why}")
    advance, submit = rules.page("advance"), rules.page("submit")
    if advance and submit and advance == submit:
        out.append("advance and submit are the same expression")
    for key, rule in rules.fields.items():
        if not rule.paths:
            out.append(f"field '{key}' has no paths")
        for expr in rule.paths:
            why = _xpath_shape(expr)
            if why:
                out.append(f"field '{key}': {why}")
            if FIELD_TOKEN in expr:
                out.append(f"field '{key}': a field's own path cannot use {FIELD_TOKEN}")
        for i, step in enumerate(rule.steps, 1):
            if step.method not in METHODS:
                out.append(f"field '{key}' step {i}: '{step.method}' is not a method")
            if not step.path:
                out.append(f"field '{key}' step {i}: no path")
            else:
                why = _xpath_shape(step.path.replace(FIELD_TOKEN, "."))
                if why:
                    out.append(f"field '{key}' step {i}: {why}")
            if step.method == "upload" and not step.document:
                out.append(f"field '{key}' step {i}: an upload names no document")
            if step.wait and step.wait not in rules.params:
                out.append(f"field '{key}' step {i}: waits on '{step.wait}', which is not a param")
    return out


def problems(path: Optional[Path] = None) -> Iterator[tuple[str, str]]:
    for name, rules in load(path).items():
        for why in invalid(rules):
            yield name, why
