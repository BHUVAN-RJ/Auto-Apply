"""The profile interviewer: one experience at a time, into base/stories/.

The loop is a state machine on disk. base/stories/_interview.json holds the
phase, the list of experiences and which one is being asked about;
base/stories/<slug>/state.json holds that experience's transcript and
checklist coverage. Stop mid-way, come back, and the next turn continues.

A turn is one candidate message in, one model reply out, streamed. Every
model reply starts with a short header the code reads (which checklist
lines the answer covered, whether the experience is done) and a `---`
line; only the text after it is shown. The header is parsed here, never
trusted blindly: unknown line numbers are dropped and the ten-question
limit is enforced in code.

Documents: when an experience closes, the light model writes main.md from
the transcript. The heavy model then writes tailor.md and star.md in a
background thread, and index.md is rebuilt from the tailor documents.
Everything stays under base/stories/; base/resume.tex and base/applicant.md
are never written here.
"""

from __future__ import annotations

import json
import os
import re
import threading
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from . import facts as facts_module
from . import llm, profile

ROOT = Path(__file__).resolve().parent.parent
RULES = Path(__file__).resolve().parent / "interview_rules.md"
STORY_RULES = Path(__file__).resolve().parent / "story_rules.md"
STATE_NAME = "_interview.json"

# Ten questions, then "anything I missed?", then the next experience.
MAX_QUESTIONS = 10

# The candidate asking to move on ends the experience whatever the model
# says. On the first real run it answered "nothing more, move on" with
# another question.
MOVE_ON = re.compile(r"\b(move on|next (one|experience|question)|skip (this|it|that)|"
                     r"nothing (more|else)( to add)?|that'?s (all|it|everything)|i'?m done)\b", re.I)

CHECKLIST = {
    "1": "Context", "2": "Problem", "3": "What was built", "4": "Own contribution",
    "5": "Decisions and trade-offs", "6": "Hardest part", "7": "Numbers",
    "8": "Outcome", "9": "Lessons",
    "10": "Day to day", "11": "Feedback", "12": "Ending",
    "13": "Production ownership", "14": "Working with others", "15": "Scope and deadlines",
}
ROLE_LINES = ("10", "11", "12")
EXPERIENCED_LINES = ("13", "14", "15")

TARGET_ROLES = "software engineering, backend, full-stack, data, machine learning, infrastructure"


class InterviewError(RuntimeError):
    pass


def _write_atomic(path: Path, text: str) -> None:
    """The children thread saves state while a turn reads it. A plain
    write_text truncates first, and the reader saw an empty file once."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def interview_model() -> str:
    return os.environ.get("OPENROUTER_INTERVIEW_MODEL",
                          os.environ.get("OPENROUTER_SCREEN_MODEL", "deepseek/deepseek-v4-flash"))


def stories_dir() -> Path:
    # Resolved per call so a test can point profile.STORIES elsewhere.
    return profile.STORIES


# ---------------------------------------------------------------- state --


@dataclass
class Experience:
    slug: str
    title: str
    kind: str  # "role" or "project"
    resume_entry: str = ""
    transcript: list[dict] = field(default_factory=list)
    coverage: dict = field(default_factory=dict)  # line id -> "covered"
    asked: int = 0
    wrapup_asked: bool = False
    closed: bool = False
    children: str = "none"  # none | pending | ready | error
    children_error: str = ""

    def lines(self, experienced: bool) -> list[str]:
        ids = [str(i) for i in range(1, 10)]
        if self.kind == "role":
            ids += list(ROLE_LINES)
        if experienced:
            ids += list(EXPERIENCED_LINES)
        return ids

    def uncovered(self, experienced: bool) -> list[str]:
        return [i for i in self.lines(experienced) if self.coverage.get(i) != "covered"]

    def path(self) -> Path:
        return stories_dir() / self.slug / "state.json"

    def save(self) -> None:
        _write_atomic(self.path(), json.dumps(asdict(self), indent=2) + "\n")

    @classmethod
    def load(cls, slug: str) -> "Experience":
        path = stories_dir() / slug / "state.json"
        if not path.exists():
            raise InterviewError(f"no experience {slug!r}")
        data = json.loads(path.read_text())
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def summary(self, experienced: bool) -> dict:
        lines = self.lines(experienced)
        return {
            "slug": self.slug, "title": self.title, "kind": self.kind,
            "covered": sum(1 for i in lines if self.coverage.get(i) == "covered"),
            "total": len(lines), "asked": self.asked, "closed": self.closed,
            "children": self.children, "children_error": self.children_error,
            "documents": [name for name in ("main.md", "tailor.md", "star.md")
                          if (stories_dir() / self.slug / name).exists()],
        }


@dataclass
class State:
    phase: str = "new"  # new | facts | setup | interviewing | open
    experiences: list[dict] = field(default_factory=list)  # {slug, title, kind, resume_entry}
    current: Optional[str] = None
    experienced: bool = False
    seed: str = ""  # extra text handed over at start; discarded after setup
    transcript: list[dict] = field(default_factory=list)  # setup and open-phase chat
    resume_phase: str = ""  # where the stories were when the facts were redone

    @classmethod
    def load(cls) -> "State":
        path = stories_dir() / STATE_NAME
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def save(self) -> None:
        _write_atomic(stories_dir() / STATE_NAME, json.dumps(asdict(self), indent=2) + "\n")

    def queue(self) -> list[str]:
        """Slugs still to interview: roles first, then projects, resume order."""
        pending = [e for e in self.experiences if not _is_closed(e["slug"])]
        roles = [e["slug"] for e in pending if e["kind"] == "role"]
        projects = [e["slug"] for e in pending if e["kind"] != "role"]
        return roles + projects


def _is_closed(slug: str) -> bool:
    path = stories_dir() / slug / "state.json"
    return path.exists() and bool(json.loads(path.read_text()).get("closed"))


def status() -> dict:
    """Everything the Profile tab needs to draw itself."""
    state = State.load()
    experiences = []
    for entry in state.experiences:
        try:
            experiences.append(Experience.load(entry["slug"]).summary(state.experienced))
        except InterviewError:
            experiences.append({"slug": entry["slug"], "title": entry["title"], "kind": entry["kind"],
                                "covered": 0, "total": 0, "asked": 0, "closed": False,
                                "children": "none", "children_error": "", "documents": []})
    transcript = list(state.transcript)
    facts = facts_module.Facts.load()
    if state.phase == "facts":
        transcript = list(facts.transcript)
    if state.phase == "interviewing" and state.current:
        try:
            transcript = state.transcript + Experience.load(state.current).transcript
        except InterviewError:
            pass
    return {
        "phase": state.phase,
        "current": state.current,
        "experiences": experiences,
        "transcript": [m for m in transcript if m["role"] in ("user", "assistant")],
        "has_resume": profile.BASE_RESUME.exists(),
        "has_facts": facts_module.has_applicant(),
        "facts": {"phase": facts.phase, "asked": min(facts.index, len(facts_module.QUESTIONS)),
                  "total": len(facts_module.QUESTIONS)},
        "model": interview_model(),
    }


def document(slug: str, name: str) -> str:
    if name not in ("main.md", "tailor.md", "star.md"):
        raise InterviewError(f"not a story document: {name}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
        raise InterviewError(f"bad slug: {slug}")
    path = stories_dir() / slug / name
    if not path.exists():
        raise InterviewError(f"{slug}/{name} is not written yet")
    return path.read_text()


# ------------------------------------------------------------ seed files --

SEED_CHAR_CAP = 40_000


def extract_text(name: str, data: bytes) -> str:
    """Plain text from a seed file: PDF through pypdf, anything else read
    as UTF-8. Scanned PDFs have no text layer and come back empty; the
    caller says so rather than seeding the interview with nothing."""
    if name.lower().endswith(".pdf") or data[:5] == b"%PDF-":
        import io
        from pypdf import PdfReader
        try:
            reader = PdfReader(io.BytesIO(data))
            pages = [page.extract_text() or "" for page in reader.pages]
        except Exception as exc:  # noqa: BLE001 - pypdf raises many types
            raise InterviewError(f"could not read {name}: {exc}") from exc
        text = "\n\n".join(p.strip() for p in pages if p.strip())
        if not text.strip():
            raise InterviewError(f"{name} has no text layer (a scanned PDF); paste the text instead")
        return text
    return data.decode("utf-8", errors="replace")


# --------------------------------------------------------------- prompts --


def resume_body() -> str:
    """The resume's document body: the preamble is macros, not experience."""
    if not profile.BASE_RESUME.exists():
        raise InterviewError(f"no master resume at {profile.BASE_RESUME}")
    text = profile.BASE_RESUME.read_text()
    match = re.search(r"\\begin\{document\}(.*)\\end\{document\}", text, re.S)
    return (match.group(1) if match else text).strip()


EXTRACT_PROMPT = f"""You read a resume (LaTeX body) and any extra notes the candidate handed
over, and list every experience an interviewer would ask about: each job or
internship as a "role", each course, personal, open-source, or hackathon
project as a "project". Skip education entries, skills lists, and awards
unless an award is for a project, in which case list the project.

Reply with one fenced json block and nothing else:

```json
{{
  "experienced": <true if the dated roles add up to more than one year of professional work, internships at half; else false>,
  "experiences": [
    {{"title": "<role title, or project name>",
     "company": "<employer for a role; empty for a project>",
     "kind": "role | project",
     "resume_entry": "<the resume's own words for this entry: heading, dates, bullets, as plain text>"}}
  ]
}}
```

Order: roles as they appear on the resume, then projects as they appear.
Use the resume's own words for titles. Never merge two entries."""

SETUP_PROMPT = f"""You are about to interview a candidate about their past work, one
experience at a time, to write a document per experience for their resume
tailoring and interview prep. Before starting, you showed them the list of
experiences you found and the roles the questions will target
({TARGET_ROLES}), and asked whether anything is missing. They have replied.

Update the list from their reply: add experiences they name (kind "role" for
jobs and internships, "project" otherwise; resume_entry is whatever they
said about it), remove any they say do not belong, keep the rest untouched.
Then answer them in one or two plain sentences. If they are happy with the
list, or they added what was missing and nothing is left open, say the
interview starts now; otherwise ask the one thing still unclear.

Reply in exactly this shape: a header, a line with three dashes, then your
reply text. Nothing before the header.

EXPERIENCES: <the full updated list as one line of json: [{{"title": ..., "kind": ..., "resume_entry": ...}}, ...]>
START: <yes if the interview should begin now, else no>
---
<your reply>"""

REPLY_FORMAT = """
## How to reply

Every reply has a header the code reads, a line with three dashes, then the
words the candidate sees. Nothing before the header.

COVERED: <comma-separated checklist line numbers the candidate's latest
answer settled concretely; empty if none>
DONE: <yes when nothing on the checklist is worth another question, or the
candidate says they have nothing more, or asks to move on or skip; else no>
---
ack: <at most six words acknowledging what they just said, e.g. "Got it."
or "Thanks, that helps."; left out on the first question of an experience>
ask: <one question in plain words, or, when DONE is yes, one sentence closing
this experience>

The `ack:` and `ask:` lines are read aloud to the candidate; anything else
in the reply is shown as text only. Keep the acknowledgement short: no
summary of what they said, no praise, no lead-in to the question.

The header lists only lines answered concretely in the candidate's own
latest message. The resume entry covers nothing: its numbers and stack are
what the interview is meant to get behind. A topic mentioned in passing is
not covered; "Numbers" needs a figure the candidate just gave, "Decisions"
needs an alternative they rejected and why, "Day to day" needs how work
was assigned or reviewed. Ask one thing at a time: a question with "and"
in it is two questions. Never write anything the candidate did not say."""

WRITE_MAIN_PROMPT = """You are given the transcript of an interview about one experience and
the checklist it followed. Write the experience's main document: markdown,
one `## ` section per checklist line, in the order given, each holding what
the candidate said about it, in their words, cleaned of filler but not
reworded. Names, numbers, dates, and product names exactly as said. A line
the transcript does not answer gets the single line "not discussed". Start
with a `# <title>` heading and one line `Kind: role|project`. Nothing else:
no advice, no summary, no invention."""

UPDATE_PROMPT = """You keep the candidate's experience documents up to date. The interview
is complete; the candidate may now tell you something new about an existing
experience, describe an experience not on file, correct a fact, or say
something that needs no change. Decide which.

Reply in exactly this shape: a header, a line with three dashes, then your
reply text. Nothing before the header.

ACTION: <update | new | none>
SLUG: <the existing experience's slug, for update; empty otherwise>
TITLE: <the new experience's name, for new; empty otherwise>
KIND: <role | project, for new; empty otherwise>
---
<one or two plain sentences: what you are doing with what they said, or
the answer to their question. For "new", say you will ask about it now.>

Use "update" when the message adds or corrects a fact about one existing
experience. Use "new" when it describes work not on file. Use "none" for
anything else, including questions, which you answer from the documents."""

REWRITE_MAIN_PROMPT = """You are given an experience's main document and new information from
the candidate about it. Rewrite the document with the new information
folded into the right checklist sections: a correction replaces the old
fact, an addition goes under the line it belongs to, and a section that
was "not discussed" is filled only with what was just said. Change nothing
else. Keep the heading, the Kind line, and the section order. Reply with
the whole document and nothing else."""


def checklist_text(experience: Experience, experienced: bool) -> str:
    return "\n".join(f"{i}. {CHECKLIST[i]}" for i in experience.lines(experienced))


def system_prompt(experience: Experience, experienced: bool) -> str:
    return (
        RULES.read_text() + REPLY_FORMAT
        + f"\n\n## This experience\n\nTitle: {experience.title}\nKind: {experience.kind}\n"
        f"\nChecklist lines for it:\n\n{checklist_text(experience, experienced)}\n"
        f"\nQuestions asked so far: {experience.asked} of {MAX_QUESTIONS}."
        f"\nLines still uncovered: {', '.join(experience.uncovered(experienced)) or 'none'}."
    )


# ------------------------------------------------------------ streaming --


def _field(header: str, name: str) -> str:
    match = re.search(rf"^{name}:\s*(.*)$", header, re.M | re.I)
    return match.group(1).strip() if match else ""


def _covered_ids(header: str, allowed: list[str]) -> list[str]:
    return [i for i in re.findall(r"\d+", _field(header, "COVERED")) if i in allowed]


# ----------------------------------------------------------------- turns --


def _slugify(title: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if len(base) > 48:
        base = base[:48].rsplit("-", 1)[0]
    base = base or "experience"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def _parse_json_block(reply: str):
    match = re.search(r"```(?:json)?\s*(.*?)```", reply, re.S)
    text = match.group(1) if match else reply
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise InterviewError(f"model reply was not json: {exc}") from exc


def start(seed: str = "") -> dict:
    """Read the resume and the seed text, list the experiences, ask what is
    missing. Refused once an interview exists; the open phase takes
    additions instead."""
    state = State.load()
    if state.phase != "new":
        raise InterviewError("an interview already exists; add experiences in the chat instead")
    user = f"## Resume\n\n{resume_body()}"
    if seed.strip():
        user += f"\n\n## Extra notes from the candidate\n\n{seed.strip()[:SEED_CHAR_CAP]}"
    reply = llm.complete(EXTRACT_PROMPT, user, model=interview_model(),
                         temperature=0.0, max_tokens=8000, reasoning=llm.minimal_reasoning(interview_model()))
    data = _parse_json_block(reply)
    found = data.get("experiences") or []
    if not found:
        raise InterviewError("the model found no experiences on the resume")
    state.experienced = bool(data.get("experienced"))
    state.experiences = _with_slugs(found, set())
    state.seed = seed.strip()[:SEED_CHAR_CAP]
    state.transcript = []
    # The facts come first: they are what the screen and the fill need,
    # and they take two minutes. The stories follow.
    facts = facts_module.start()
    state.phase = "facts"
    state.save()
    return {"message": facts.transcript[0]["content"], "status": status()}


def _begin_setup(state: State) -> Iterator[dict]:
    """Facts done or skipped: show the experiences found and ask what is
    missing, the way the interview used to open."""
    state.phase = "setup"
    opening = _opening_message(state)
    state.transcript = [{"role": "assistant", "content": opening}]
    state.save()
    yield {"type": "break"}
    yield {"type": "delta", "text": opening, "part": "ask", "spoken": True}
    yield {"type": "phase", "phase": state.phase, "experiences": status()["experiences"]}


def _facts_turn(state: State, message: str) -> Iterator[dict]:
    facts = facts_module.Facts.load()
    if facts.phase != "asking":
        yield from _begin_setup(state)
        return
    done = False
    for event in facts_module.turn(facts, message):
        if event["type"] == "facts":
            done = event["phase"] == "done"
        else:
            yield event
    if done and state.resume_phase:
        # The facts were redone mid-way through the stories; go back.
        state.phase, state.resume_phase = state.resume_phase, ""
        state.save()
        closing = "Facts file updated. Back to the stories where we left off."
        yield {"type": "break"}
        yield {"type": "delta", "text": closing, "part": "ack", "spoken": True}
        yield {"type": "phase", "phase": state.phase, "current": state.current,
               "experiences": status()["experiences"]}
    elif done:
        closing = "That is the facts file written. Now the stories."
        yield {"type": "break"}
        yield {"type": "delta", "text": closing, "part": "ack", "spoken": True}
        yield from _begin_setup(state)


def skip_facts() -> dict:
    """Leave the facts for later and go on to the stories. The file is not
    written; the screen keeps using facts derived from the resume."""
    state = State.load()
    if state.phase != "facts":
        raise InterviewError("the facts interview is not running")
    facts = facts_module.Facts.load()
    facts.phase = "skipped"
    facts.save()
    events = list(_begin_setup(state))
    return {"message": next(e["text"] for e in events if e["type"] == "delta"), "status": status()}


def restart_facts() -> dict:
    """Ask the facts again, from the top, whatever phase the stories are
    in. The stories interview resumes where it was once the facts are done."""
    state = State.load()
    if state.phase == "new":
        raise InterviewError("press Start first; nothing has been read from the resume yet")
    facts = facts_module.start()
    state.resume_phase = state.phase if state.phase != "facts" else state.resume_phase
    state.phase = "facts"
    state.save()
    return {"message": facts.transcript[0]["content"], "status": status()}


def _with_slugs(entries: list[dict], taken: set[str]) -> list[dict]:
    out = []
    taken = set(taken)
    for entry in entries:
        title = str(entry.get("title") or "").strip()
        if not title:
            continue
        kind = "role" if str(entry.get("kind", "")).lower().startswith("role") else "project"
        company = str(entry.get("company") or "").strip()
        if company and company.lower() not in title.lower():
            title = f"{title}, {company}"
        slug = _slugify(title, taken)
        taken.add(slug)
        out.append({"slug": slug, "title": title, "kind": kind,
                    "resume_entry": str(entry.get("resume_entry") or "").strip()})
    return out


def _opening_message(state: State) -> str:
    roles = [e["title"] for e in state.experiences if e["kind"] == "role"]
    projects = [e["title"] for e in state.experiences if e["kind"] != "role"]
    parts = ["Here is what I found on your resume."]
    if roles:
        parts.append("Roles:\n" + "\n".join(f"- {t}" for t in roles))
    if projects:
        parts.append("Projects:\n" + "\n".join(f"- {t}" for t in projects))
    parts.append(f"The questions will target these kinds of roles: {TARGET_ROLES}.")
    parts.append("Is anything missing from either list, or anything there that should not be? "
                 "Say so, or say it looks right and we start.")
    return "\n\n".join(parts)


def turn(message: str) -> Iterator[dict]:
    """One candidate message. Yields events the page renders as they come:
    delta (text), break (start a new bubble), experience (coverage), phase,
    done, error."""
    message = message.strip()
    if not message:
        return
    state = State.load()
    try:
        if state.phase == "new":
            raise InterviewError("press Start first; nothing has been read from the resume yet")
        if state.phase == "facts":
            yield from _facts_turn(state, message)
        elif state.phase == "setup":
            yield from _setup_turn(state, message)
        elif state.phase == "interviewing":
            yield from _interview_turn(state, message)
        else:
            yield from _open_turn(state, message)
        yield {"type": "done"}
    except (llm.LLMError, InterviewError) as exc:
        yield {"type": "error", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 - a dead stream shows nothing; an event shows why
        traceback.print_exc()
        yield {"type": "error", "message": f"{type(exc).__name__}: {exc}"}


# The reply body is labelled by line: `ack:` and `ask:` are spoken, `note:`
# is text only. Labels are stripped; the transcript keeps the words.
PART_RE = re.compile(r"^(ack|note|ask):[ \t]*", re.I)
PART_LABELS = ("ack:", "note:", "ask:")
SPOKEN_PARTS = ("ack", "ask")


class _Parts:
    """Splits streamed reply text into labelled parts without waiting for
    whole lines: at a line start, text is held only as long as it could
    still become a label. Unlabelled text (a model that ignored the
    format) comes through with no part, and the page falls back to its own
    rule for what to speak."""

    def __init__(self) -> None:
        self.part: Optional[str] = None
        self.hold = ""
        self.line_start = True

    def feed(self, text: str, final: bool = False) -> list[tuple[str, Optional[str]]]:
        self.hold += text
        out: list[tuple[str, Optional[str]]] = []
        while self.hold:
            if self.line_start:
                match = PART_RE.match(self.hold)
                if match and match.end() == len(self.hold) and not final:
                    return out  # the label's trailing spaces may still be coming
                if match:
                    self.part = match.group(1).lower()
                    self.hold = self.hold[match.end():]
                    self.line_start = False
                    continue
                head = self.hold.lower()
                if any(label.startswith(head) for label in PART_LABELS):
                    return out  # could still be a label; wait for more
                self.line_start = False
            cut = self.hold.find("\n")
            if cut < 0:
                out.append((self.hold, self.part))
                self.hold = ""
            else:
                out.append((self.hold[:cut + 1], self.part))
                self.hold = self.hold[cut + 1:]
                self.line_start = True
        return out

    def flush(self) -> list[tuple[str, Optional[str]]]:
        out = self.feed("", final=True)
        rest, self.hold, self.line_start = self.hold, "", False
        return out + ([(rest, self.part)] if rest else [])


def _stream_reply(messages: list[dict], model: Optional[str] = None) -> Iterator[dict]:
    """Stream the model's reply as delta events, holding back the header.

    Text after the first `---` line is forwarded as it arrives, split into
    labelled parts (see `_Parts`); a delta from a labelled part carries
    `part` and `spoken`. The model may skip the header entirely (it happens
    under a small model on a short turn); then everything is reply text and
    the header is empty. The last event, `_reply`, carries the header and
    the shown text for the caller; the caller does not forward it.
    """
    shown: list[str] = []
    header: Optional[str] = None
    buffer = ""
    parts = _Parts()

    def show(text: str, final: bool = False) -> list[dict]:
        pieces = parts.feed(text) if text else []
        if final:
            pieces += parts.flush()
        events = []
        for piece, part in pieces:
            shown.append(piece)
            event: dict = {"type": "delta", "text": piece}
            if part:
                event["part"] = part
                event["spoken"] = part in SPOKEN_PARTS
            events.append(event)
        return events

    model = model or interview_model()
    chunks = llm.stream(messages, model=model, temperature=0.4,
                        max_tokens=4000, reasoning=llm.minimal_reasoning(model))
    for chunk in chunks:
        if header is not None:
            if not shown:
                # The `---` line's own newline may arrive in the next chunk.
                chunk = chunk.lstrip("\n")
            if chunk:
                yield from show(chunk)
            continue
        buffer += chunk
        match = re.search(r"^---[ \t]*$\n?", buffer, re.M)
        if match:
            header = buffer[:match.start()]
            rest = buffer[match.end():].lstrip("\n")
            if rest:
                yield from show(rest)
        elif len(buffer) > 600 and ":" not in buffer.split("\n", 1)[0]:
            # No header coming. Show what we have and stream the rest.
            header = ""
            yield from show(buffer)
    if header is None:
        # Stream ended before any `---`: header-only, or no header at all.
        if re.match(r"^[A-Z]+:", buffer):
            header = buffer
        else:
            header = ""
            if buffer:
                yield from show(buffer)
    yield from show("", final=True)
    yield {"type": "_reply", "header": header, "text": "".join(shown).strip()}


def _setup_turn(state: State, message: str) -> Iterator[dict]:
    state.transcript.append({"role": "user", "content": message})
    listing = json.dumps([{k: e[k] for k in ("title", "kind", "resume_entry")} for e in state.experiences])
    messages = [{"role": "system", "content": SETUP_PROMPT},
                {"role": "user", "content": f"## Current list\n\n{listing}"}]
    messages += state.transcript
    reply = None
    for event in _stream_reply(messages):
        if event["type"] == "_reply":
            reply = event
        else:
            yield event
    header, text = reply["header"], reply["text"]
    state.transcript.append({"role": "assistant", "content": text})
    raw = _field(header, "EXPERIENCES")
    if raw:
        try:
            updated = json.loads(raw)
            if isinstance(updated, list) and updated:
                state.experiences = _with_slugs(updated, set())
        except json.JSONDecodeError:
            pass
    if _field(header, "START").lower().startswith("y") or not header:
        state.phase = "interviewing"
        state.seed = ""  # used once; the documents hold what mattered
        state.save()
        yield {"type": "phase", "phase": state.phase, "experiences": status()["experiences"]}
        yield from _next_experience(state)
    else:
        state.save()
        yield {"type": "phase", "phase": state.phase, "experiences": status()["experiences"]}


def _next_experience(state: State) -> Iterator[dict]:
    """Open the next experience in the queue and ask its first question, or
    close the interview when none is left."""
    queue = state.queue()
    if not queue:
        state.current = None
        state.phase = "open"
        closing = ("That covers everything on the resume. Anything you worked on that is not "
                   "written down anywhere? Describe it and I will ask about it. Otherwise we are "
                   "done for now; come back any time to add or correct something.")
        state.transcript.append({"role": "assistant", "content": closing})
        state.save()
        yield {"type": "break"}
        yield {"type": "delta", "text": closing}
        yield {"type": "phase", "phase": state.phase, "current": None}
        return
    slug = queue[0]
    entry = next(e for e in state.experiences if e["slug"] == slug)
    try:
        experience = Experience.load(slug)
    except InterviewError:
        experience = Experience(slug=slug, title=entry["title"], kind=entry["kind"],
                                resume_entry=entry.get("resume_entry", ""))
        experience.save()
    state.current = slug
    state.save()
    yield {"type": "phase", "phase": state.phase, "current": slug}
    yield {"type": "break"}
    if not experience.transcript:
        yield from _ask(state, experience)


def _ask(state: State, experience: Experience) -> Iterator[dict]:
    """Stream the next question for the current experience and record it."""
    entry = f"## Resume entry\n\n{experience.resume_entry or experience.title}"
    if not experience.transcript:
        entry += ("\n\n(Ask the first question: name the experience and ask for the story "
                  "behind its first bullet. Do not read the entry back; they wrote it.)")
    messages = [{"role": "system", "content": system_prompt(experience, state.experienced)},
                {"role": "user", "content": entry}]
    messages += experience.transcript
    reply = None
    for event in _stream_reply(messages):
        if event["type"] == "_reply":
            reply = event
        else:
            yield event
    experience.transcript.append({"role": "assistant", "content": reply["text"]})
    experience.asked += 1
    experience.save()
    yield {"type": "experience", **experience.summary(state.experienced)}


def _interview_turn(state: State, message: str) -> Iterator[dict]:
    if not state.current or _is_closed(state.current):
        # A turn that died between closing one experience and opening the
        # next leaves `current` on the closed one; pick up from the queue.
        yield from _next_experience(state)
        return
    experience = Experience.load(state.current)
    experience.transcript.append({"role": "user", "content": message})
    experience.save()

    # The reply both grades the answer and asks the next question; the
    # header is read here even when the code overrides what to ask next.
    messages = [{"role": "system", "content": system_prompt(experience, state.experienced)},
                {"role": "user", "content": f"## Resume entry\n\n{experience.resume_entry or experience.title}"}]
    messages += experience.transcript
    closing = experience.wrapup_asked or bool(MOVE_ON.search(message))
    if closing:
        messages.append({"role": "system", "content":
                         "This was the answer to the wrap-up question. Mark what it covered, set "
                         "DONE: yes, and close in one sentence."})
    reply = None
    for event in _stream_reply(messages):
        if event["type"] == "_reply":
            reply = event
        else:
            yield event
    header, text = reply["header"], reply["text"]
    if not header.strip():
        # The model skipped the header (seen on the first real run: a good
        # question, no COVERED line). Grade the answer in a second, cheap
        # call rather than lose the coverage.
        header = _grade(experience, state, message)
    if not MOVE_ON.search(message):
        # "Nothing more, move on" carries no facts; one model marked every
        # line covered on it, which would have hidden the gaps for good.
        for line in _covered_ids(header, experience.lines(state.experienced)):
            experience.coverage[line] = "covered"
    done = _field(header, "DONE").lower().startswith("y") or closing
    experience.transcript.append({"role": "assistant", "content": text})
    if done:
        experience.save()
        yield {"type": "experience", **experience.summary(state.experienced)}
        yield from _close(state, experience)
        return
    experience.asked += 1
    if experience.asked >= MAX_QUESTIONS and not experience.wrapup_asked:
        # The model asked question ten (or more); the code asks the wrap-up
        # instead of letting the model keep going.
        experience.transcript.pop()
        wrap = "Anything about this one I missed?"
        experience.transcript.append({"role": "assistant", "content": wrap})
        experience.wrapup_asked = True
        experience.save()
        yield {"type": "replace", "text": wrap}
        yield {"type": "experience", **experience.summary(state.experienced)}
        return
    experience.save()
    yield {"type": "experience", **experience.summary(state.experienced)}


GRADE_PROMPT = """You grade one interview answer against a checklist. Reply with the header
only, nothing else:

COVERED: <comma-separated checklist line numbers this answer settled concretely>
DONE: <yes if the candidate said they have nothing more to add; else no>

Only lines the answer itself covers concretely count. A topic mentioned in
passing does not."""


def _grade(experience: Experience, state: State, answer: str) -> str:
    question = ""
    for entry in reversed(experience.transcript[:-1]):
        if entry["role"] == "assistant":
            question = entry["content"]
            break
    try:
        return llm.complete(
            GRADE_PROMPT,
            f"## Checklist\n\n{checklist_text(experience, state.experienced)}\n\n"
            f"## Question\n\n{question}\n\n## Answer\n\n{answer}",
            model=interview_model(), temperature=0.0, max_tokens=200, reasoning=llm.minimal_reasoning(interview_model()),
        )
    except llm.LLMError:
        return ""


def _close(state: State, experience: Experience) -> Iterator[dict]:
    for line in experience.uncovered(state.experienced):
        experience.coverage[line] = "not_discussed"
    experience.closed = True
    experience.save()
    write_main(experience, state.experienced)
    start_children(experience.slug)
    yield {"type": "experience", **experience.summary(state.experienced)}
    yield from _next_experience(state)


def _open_turn(state: State, message: str) -> Iterator[dict]:
    state.transcript.append({"role": "user", "content": message})
    listing = profile.index() or "\n".join(f"- [{e['slug']}] {e['title']}" for e in state.experiences)
    messages = [{"role": "system", "content": UPDATE_PROMPT},
                {"role": "user", "content": f"## Experiences on file\n\n{listing}"}]
    # Recent chat only: the documents are the memory, not the transcript.
    messages += state.transcript[-12:]
    reply = None
    for event in _stream_reply(messages):
        if event["type"] == "_reply":
            reply = event
        else:
            yield event
    header, text = reply["header"], reply["text"]
    state.transcript.append({"role": "assistant", "content": text})
    action = _field(header, "ACTION").lower()
    known = {e["slug"] for e in state.experiences}
    slug = _field(header, "SLUG").strip("[]`")
    if action == "update" and slug in known:
        state.save()
        update_main(slug, message)
        start_children(slug)
        yield {"type": "experience", **Experience.load(slug).summary(state.experienced)}
        return
    if action == "new":
        title = _field(header, "TITLE") or message[:60]
        kind = "role" if _field(header, "KIND").lower().startswith("role") else "project"
        entry = _with_slugs([{"title": title, "kind": kind, "resume_entry": message}], known)[0]
        state.experiences.append(entry)
        state.phase = "interviewing"
        state.save()
        yield {"type": "phase", "phase": state.phase, "experiences": status()["experiences"]}
        yield from _next_experience(state)
        return
    state.save()


# ------------------------------------------------------------ documents --


def write_main(experience: Experience, experienced: bool) -> str:
    transcript = "\n\n".join(f"**{m['role']}**: {m['content']}" for m in experience.transcript)
    user = (f"## Title\n\n{experience.title}\n\n## Kind\n\n{experience.kind}\n\n"
            f"## Checklist\n\n{checklist_text(experience, experienced)}\n\n"
            f"## Resume entry\n\n{experience.resume_entry}\n\n## Transcript\n\n{transcript}")
    text = llm.complete(WRITE_MAIN_PROMPT, user, model=interview_model(),
                        temperature=0.1, max_tokens=8000, reasoning=llm.minimal_reasoning(interview_model()))
    path = stories_dir() / experience.slug / "main.md"
    path.write_text(text.strip() + "\n")
    return text


def update_main(slug: str, new_information: str) -> str:
    path = stories_dir() / slug / "main.md"
    current = path.read_text() if path.exists() else f"# {slug}\n"
    text = llm.complete(REWRITE_MAIN_PROMPT,
                        f"## Document\n\n{current}\n\n## New information\n\n{new_information}",
                        model=interview_model(), temperature=0.1, max_tokens=8000,
                        reasoning=llm.minimal_reasoning(interview_model()))
    path.write_text(text.strip() + "\n")
    return text


def _story_rules(section: str) -> str:
    text = STORY_RULES.read_text()
    intro = text.split("\n## ", 1)[0]
    match = re.search(rf"^## {section}\s*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    if not match:
        raise InterviewError(f"story_rules.md has no section {section!r}")
    return f"{intro}\n\n## {section}\n{match.group(1)}"


def generate_children(slug: str) -> None:
    """tailor.md and star.md from main.md, with the heavy model, then the
    index. Status lands in state.json so the page can show it."""
    experience = Experience.load(slug)
    main = stories_dir() / slug / "main.md"
    if not main.exists():
        raise InterviewError(f"{slug}/main.md is not written yet")
    experience.children = "pending"
    experience.children_error = ""
    experience.save()
    try:
        document_text = main.read_text()
        for name, section in (("tailor.md", "Tailor document"), ("star.md", "Star document")):
            text = llm.complete(_story_rules(section), f"## Main document\n\n{document_text}",
                                model=llm.tailor_model(), temperature=0.2, max_tokens=8000)
            _write_atomic(stories_dir() / slug / name, _unfenced(text) + "\n")
        rebuild_index()
        experience = Experience.load(slug)
        experience.children = "ready"
    except Exception as exc:  # noqa: BLE001 - recorded, shown, retried from the page
        experience = Experience.load(slug)
        experience.children = "error"
        experience.children_error = f"{type(exc).__name__}: {exc}"
    experience.save()


def start_children(slug: str) -> threading.Thread:
    """The heavy model takes a while; the next question must not wait for it."""
    experience = Experience.load(slug)
    experience.children = "pending"
    experience.save()
    thread = threading.Thread(target=_children_safely, args=(slug,), daemon=True)
    thread.start()
    return thread


def _children_safely(slug: str) -> None:
    try:
        generate_children(slug)
    except Exception as exc:  # noqa: BLE001 - thread; record, never raise
        try:
            experience = Experience.load(slug)
            experience.children = "error"
            experience.children_error = f"{type(exc).__name__}: {exc}"
            experience.save()
        except InterviewError:
            pass


def _unfenced(text: str) -> str:
    """The rules show the format in a code fence, and the model copies the
    fence around its whole reply. The document is the inside."""
    text = text.strip()
    match = re.fullmatch(r"```[a-z]*\n(.*)\n```", text, re.S)
    return match.group(1).strip() if match else text


def _first_line(text: str, name: str) -> str:
    match = re.search(rf"^{name}:\s*(.*)$", text, re.M)
    return match.group(1).strip() if match else ""


def rebuild_index() -> str:
    """index.md from the tailor documents: `- [slug] Summary. Stack: ...`."""
    lines = []
    for folder in profile.story_dirs():
        path = folder / profile.TAILOR_DOC
        if not path.exists():
            continue
        text = path.read_text()
        summary = _first_line(text, "Summary") or folder.name
        stack = _first_line(text, "Stack")
        lines.append(f"- [{folder.name}] {summary.rstrip('.')}." + (f" Stack: {stack}" if stack else ""))
    listing = "\n".join(lines)
    (stories_dir() / profile.INDEX_NAME).write_text(listing + ("\n" if listing else ""))
    return listing
