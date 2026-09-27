"""The Screening tab: which rules run, and an assistant that writes new ones.

Screening itself asks no model. What lives here is the person's control over
it - the switches, their own rules, a preview of what a rule would have done
to the postings already on disk - and one model call per *rule written*,
which is a different thing entirely from one per job screened.

Nothing here changes a job's status or re-screens anything. A rule saved now
is used by the next posting that opens.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import paths
from tailor import llm, profile, prompts, screening

router = APIRouter()

# How many past postings the preview reads. Every folder on disk, capped so
# a person with a thousand applications still gets an answer in a second.
PREVIEW_LIMIT = 200
PREVIEW_SAMPLES = 3


class Toggle(BaseModel):
    enabled: Optional[bool] = None
    value: Optional[int] = None


class RuleBody(BaseModel):
    name: str
    label: str
    category: str
    severity: str = "hard"
    note: str = ""
    patterns: list[str] = []
    unless: list[str] = []
    check: str = ""
    value: Optional[int] = None


class AskBody(BaseModel):
    request: str


def _rule(body: RuleBody) -> screening.Rule:
    return screening.Rule(
        name=body.name.strip().lower(),
        label=body.label.strip(),
        category=body.category.strip().lower(),
        severity=body.severity.strip().lower(),
        note=body.note.strip(),
        patterns=[p for p in body.patterns if p.strip()],
        unless=[p for p in body.unless if p.strip()],
        check=body.check.strip(),
        value=body.value,
        origin="yours",
    )


def corpus() -> list[tuple[str, str, str]]:
    """(folder, title, text) for every posting already on disk.

    The person's own postings are the only honest test set for a rule: a
    pattern that reads well and fires on forty of their last hundred jobs is
    a pattern that would have rejected forty of them.
    """
    out: list[tuple[str, str, str]] = []
    for folder in sorted(paths.APPLICATIONS.glob("*/"), reverse=True)[:PREVIEW_LIMIT]:
        posting, job = folder / "posting.md", folder / "job.json"
        if not posting.exists():
            continue
        title = ""
        if job.exists():
            try:
                title = json.loads(job.read_text()).get("title") or ""
            except json.JSONDecodeError:
                pass
        out.append((folder.name, title, posting.read_text(errors="replace")))
    return out


def preview(rule: screening.Rule) -> dict:
    """What this rule would have done to the postings already applied for."""
    facts, _ = profile.screen_facts()
    hits: list[dict] = []
    total = 0
    for folder, title, text in corpus():
        total += 1
        fired = screening.fired(text, title=title, facts=facts, catalog=[rule])
        if fired:
            hits.append({"folder": folder, "title": title, "quote": fired[0][1]})
    return {
        "postings": total,
        "hits": len(hits),
        "samples": hits[:PREVIEW_SAMPLES],
        # A rule that fires on most of the corpus is almost certainly too
        # wide, and is worth saying out loud before it is saved.
        "too_wide": bool(total) and len(hits) > max(3, total // 3),
    }


def listing() -> dict:
    facts, source = profile.screen_facts()
    return {
        "rules": [rule.to_dict() for rule in screening.rules()],
        "categories": list(screening.CATEGORIES),
        "checks": sorted(screening.CHECKS),
        "facts": facts,
        "facts_source": source,
    }


@router.get("/screening")
def get_screening() -> dict:
    return listing()


@router.post("/screening/rule/{name}")
def set_rule(name: str, body: Toggle) -> dict:
    if screening.get(name) is None:
        raise HTTPException(404, f"no rule called {name}")
    if body.enabled is not None:
        screening.set_enabled(name, body.enabled)
    if body.value is not None:
        screening.set_value(name, body.value)
    return listing()


@router.post("/screening/rule")
def save_rule(body: RuleBody) -> dict:
    try:
        screening.save_custom(_rule(body))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return listing()


@router.delete("/screening/rule/{name}")
def delete_rule(name: str) -> dict:
    rule = screening.get(name)
    if rule is None:
        raise HTTPException(404, f"no rule called {name}")
    if rule.origin != "yours":
        raise HTTPException(400, "a stock rule can be switched off, not deleted")
    screening.delete_custom(name)
    return listing()


@router.post("/screening/preview")
def preview_rule(body: RuleBody) -> dict:
    rule = _rule(body)
    problem = screening.invalid(rule)
    if problem:
        raise HTTPException(400, problem)
    return preview(rule)


# ------------------------------------------------------------- the assistant


def author_prompt() -> str:
    return prompts.text("screening.author") + "\n" + prompts.text("screening.author.format")


def author_message(request: str, facts: str) -> str:
    existing = "\n".join(
        f"- {rule.name}: {rule.label}" for rule in screening.rules())
    return (
        f"## The person\n\n{facts}\n\n"
        f"## Rules they already have\n\n{existing}\n\n"
        f"## What they asked for\n\n{request.strip()}"
    )


def parse_author_reply(reply: str) -> tuple[Optional[screening.Rule], str, str]:
    """(rule, explain, problem). A reply that is not JSON is a problem, not
    an exception: the tab shows it and the person tries different words."""
    match = re.search(r"```json[^\n]*\n(.*?)```", reply, re.S)
    try:
        data = json.loads(match.group(1) if match else reply)
    except (json.JSONDecodeError, AttributeError):
        return None, "", "the assistant did not reply with a rule; try saying it differently"
    if not isinstance(data, dict):
        return None, "", "the assistant's reply was not a rule"
    explain = str(data.get("explain") or "").strip()
    problem = str(data.get("problem") or "").strip()
    raw = data.get("rule")
    if not isinstance(raw, dict):
        return None, explain, problem or "the assistant wrote no rule"
    rule = screening.Rule(
        name=str(raw.get("name") or "").strip().lower(),
        label=str(raw.get("label") or "").strip(),
        category=str(raw.get("category") or "other").strip().lower(),
        severity=str(raw.get("severity") or "hard").strip().lower(),
        note=str(raw.get("note") or "").strip(),
        patterns=[str(p) for p in (raw.get("patterns") or []) if str(p).strip()],
        unless=[str(p) for p in (raw.get("unless") or []) if str(p).strip()],
        origin="yours",
    )
    return rule, explain, problem


@router.post("/screening/ask")
def ask(body: AskBody) -> dict:
    """Turn a sentence into a proposed rule. Nothing is saved by this call.

    The reply is checked the same way a hand-written rule is - the patterns
    must compile and must not match ordinary prose - and then run over the
    postings on disk, so the person sees what it would have caught before
    they decide.
    """
    request = body.request.strip()
    if not request:
        raise HTTPException(400, "say what you want flagged")
    facts, _ = profile.screen_facts()
    try:
        reply = llm.complete(author_prompt(), author_message(request, facts),
                             temperature=0.0, max_tokens=1500)
    except Exception as exc:  # noqa: BLE001 - shown on the tab
        raise HTTPException(502, f"the assistant could not be reached: {exc}")

    rule, explain, problem = parse_author_reply(reply)
    if rule is None:
        return {"rule": None, "explain": explain, "problem": problem}
    if rule.name in screening.BY_NAME:
        rule.name = f"{rule.name}_yours"
    problem = problem or screening.invalid(rule)
    if problem:
        return {"rule": rule.to_dict(), "explain": explain, "problem": problem,
                "preview": None}
    return {"rule": rule.to_dict(), "explain": explain, "problem": "",
            "preview": preview(rule)}
