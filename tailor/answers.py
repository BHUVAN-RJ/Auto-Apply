"""Answers for a form's free-form questions, written by the tailoring model.

The browser agent is a small, fast vision model chosen for following the
action schema, not for prose. When it meets an open question it hands the
question here, and the same model that wrote the resume and the letter
writes the answer, from the same material. The agent only types it.

Questions about visa, sponsorship, and work authorisation are refused here as
well as at the input guard, so no answer to one is ever produced.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from browser import guard

from . import cover, llm, prompts

RULES = Path(__file__).resolve().parent / "answer_rules.md"

REPLY_FORMAT = """
## Reply format

Reply with the answer only. No preamble, no commentary, no code fence, no
label, no quotation marks around it.
"""

MIN_WORDS, MAX_WORDS = 15, 130
ATTEMPTS = 2
# The answering model, when it should differ from the tailor's.
MODEL_ENV = "OPENROUTER_ANSWER_MODEL"
# A label this long, or with a question mark, is a question, not a field.
QUESTION_CHARS = 60


def answer_model() -> str:
    return os.environ.get(MODEL_ENV, "").strip() or llm.tailor_model()


def open_questions(snapshot: dict) -> list[str]:
    """The free-form questions a form snapshot (question -> value) left
    empty: what the review page offers to answer. Short labels are fields
    (name, phone), and visa questions are never offered."""
    found = []
    for label, value in (snapshot or {}).items():
        text = " ".join(str(label).split())
        if value or not text or guard.describes_protected(text):
            continue
        if "?" in text or len(text) > QUESTION_CHARS:
            found.append(text)
    return found


@dataclass
class Context:
    """Everything the answer may draw on. All optional; missing pieces are omitted."""

    posting: str = ""
    resume_tex: str = ""
    profile: str = ""
    cover_letter: str = ""
    applicant: str = ""
    # What the review page's thread about this job already said. Context,
    # never the question: a visa word said earlier must not make the next
    # question look protected.
    thread: str = ""

    @classmethod
    def from_app_dir(cls, app_dir: Optional[Path], profile: str = "", applicant: str = "") -> "Context":
        def read(name: str) -> str:
            path = app_dir / name if app_dir else None
            return path.read_text(errors="replace") if path and path.exists() else ""

        return cls(
            posting=read("posting.md"),
            resume_tex=read("resume.tex"),
            profile=profile,
            cover_letter=read("cover_letter.md"),
            applicant=applicant,
        )

    def as_message(self) -> str:
        sections = []
        if self.posting:
            sections.append(f"## Job posting\n\n{self.posting}")
        if self.profile:
            sections.append(f"## Candidate profile\n\n{self.profile}")
        if self.applicant:
            sections.append(f"## Applicant details\n\n{self.applicant}")
        if self.resume_tex:
            sections.append(f"## The tailored resume, as LaTeX\n\n```tex\n{self.resume_tex}\n```")
        if self.cover_letter:
            sections.append(f"## The cover letter already written for this posting\n\n{self.cover_letter}")
        if self.thread:
            sections.append("## Earlier in this conversation, for context\n\n" + self.thread)
        return "\n\n".join(sections)


@dataclass
class Answer:
    question: str
    text: str
    model: str
    attempts: int = 1


class AnswerError(RuntimeError):
    pass


def system_prompt() -> str:
    return prompts.text("answers") + "\n" + prompts.text("answers.format")


def problems(text: str) -> list[str]:
    found = []
    words = len(text.split())
    if words < MIN_WORDS or words > MAX_WORDS:
        found.append(f"it was {words} words; write between {MIN_WORDS} and {MAX_WORDS}")
    if cover.has_dash(text):
        found.append("it used a dash as punctuation; use a comma or a full stop instead")
    if re.search(r"^\s*[-*#>]", text, re.M) or "**" in text:
        found.append("it used markdown; plain sentences only")
    return found


def tidy(reply: str) -> str:
    text = cover.clean(reply)
    # A model that wraps the whole answer in quotes despite the rules.
    if len(text) > 2 and text[0] in "\"'“" and text[-1] in "\"'”":
        text = text[1:-1].strip()
    return " ".join(text.split("\n\n")).strip() if text.count("\n\n") == 1 else text


def answer(question: str, context: Context, model: Optional[str] = None) -> Answer:
    """One answer for one question. Refuses protected questions outright."""
    question = " ".join(question.split())
    if not question:
        raise AnswerError("empty question")
    if guard.describes_protected(question):
        raise guard.ProtectedField(
            f"refusing to answer a visa / work-authorisation question ({question[:80]!r}). "
            "Leave it blank and mention it when you call done."
        )
    model = model or answer_model()
    system = system_prompt()
    user = context.as_message() + f"\n\n## The question on the form\n\n{question}"
    last, last_problems = "", []
    for attempt in range(1, ATTEMPTS + 1):
        reply = tidy(llm.complete(system, user, model=model, temperature=0.5, max_tokens=1500))
        last, last_problems = reply, problems(reply)
        if not last_problems:
            return Answer(question, reply, model, attempt)
        user += "\n\n## Your previous reply was rejected\n\n" + "; ".join(last_problems) + ". Write it again."
    if all("dash" in p for p in last_problems):
        return Answer(question, cover.strip_dashes(last), model, ATTEMPTS)
    raise AnswerError(f"answer broke the rules on {ATTEMPTS} attempts: " + "; ".join(last_problems))


@dataclass
class Answerer:
    """Answers questions for one fill, remembering each so the review can show them."""

    context: Context
    model: Optional[str] = None
    answers: list[Answer] = field(default_factory=list)

    def __call__(self, question: str) -> str:
        for done in self.answers:
            if done.question == " ".join(question.split()):
                return done.text
        result = answer(question, self.context, self.model)
        self.answers.append(result)
        return result.text

    def to_markdown(self) -> str:
        if not self.answers:
            return ""
        blocks = [f"**{a.question}**\n\n{a.text}\n" for a in self.answers]
        return "# Free-form answers\n\n" + "\n".join(blocks)
