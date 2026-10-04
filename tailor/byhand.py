"""Use Opus without paying for Opus: hand the prompt over, take the LaTeX back.

The strongest model rewrites five of six experience bullets where the cheap
field rewrites one, and it costs about twenty times as much per job through
the API. But most people are already paying for Claude, and pasting a prompt
into a chat they have open costs nothing at all.

So "Use Opus" stops rather than spends. The job is screened, the posting is
fetched, the stories are picked, the whole prompt is built exactly as the API
call would have built it — and then the pipeline puts it on the review page
behind a **Copy** button instead of sending it. The person pastes it into
Claude, pastes the tailored LaTeX back into the box underneath, and the
pipeline picks up from there: validate, compile, cover letter, checkpoint 1.

Two things make this safe rather than a side door:

- **The prompt is built by the same code that builds the API one**
  (`tailor._build_user_message`, `tailor.system_prompt`). There is no second
  copy of the rules to drift out of step, so what a person pastes into a chat
  is what the model would have been sent.
- **The reply goes through the same checkers** (`accept`). Sections, preamble,
  links, counts, printed lines, invented figures and names: a resume typed in
  by hand from a chat window is held to every rule one from the API is. The
  only thing missing is the retry loop, because here the person is the loop —
  a rejection comes back as words for them to act on, with the LaTeX they
  pasted kept in the box.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from . import tailor as tailor_module
from .fetch import Posting

# What `Job.tailor_model` holds for a job whose resume the person is going to
# fetch themselves. It is deliberately not a model name: nothing may send it
# to a provider, and `llm.complete` would fail loudly if anything tried.
BY_HAND = "by-hand"


def is_by_hand(model: Optional[str]) -> bool:
    return (model or "").strip().lower() == BY_HAND


# The one paragraph of scaffolding a chat window needs and an API call does
# not: the API sends a system prompt and a user message as two parts, and a
# chat has only one box.
HEADER = """You are being given a resume-tailoring job to do, exactly as an
automated pipeline would have sent it to you. Everything below the line is
that request: the rules first, then the posting, the candidate's material and
the master resume.

Follow it exactly as written, including the reply format it asks for. Reply
with the ```tex block (and the ```rationale block); I am going to paste the
LaTeX straight back into the pipeline, and it runs the same checks on it that
it would run on an API answer.

---

"""


@dataclass
class Handoff:
    """The prompt to paste, and what it was built from."""
    prompt: str
    resume: str          # the master LaTeX, as it went into the prompt
    profile: str
    posting: str

    @property
    def characters(self) -> int:
        return len(self.prompt)


def build(posting: Posting, resume_tex: Optional[str] = None,
          profile: Optional[str] = None, extra_instruction: str = "") -> Handoff:
    """The whole prompt, ready to paste into a chat.

    Built from `tailor`'s own pieces, in the order the API call assembles
    them, so the two can never say different things.
    """
    original = resume_tex if resume_tex is not None else tailor_module.load_base_resume()
    if profile is None:
        profile = tailor_module.load_profile()

    message = tailor_module._build_user_message(posting, original, profile)
    if extra_instruction.strip():
        message += (
            "\n\n## The reviewer asked for a change\n\n"
            f"{extra_instruction.strip()}\n\n"
            "Apply this while still obeying every rule above."
        )
    prompt = HEADER + tailor_module.system_prompt() + "\n\n---\n\n" + message
    return Handoff(prompt=prompt, resume=original, profile=profile, posting=posting.text)


# What a person is likely to paste back. A chat reply may arrive as the whole
# message (```tex fence and all), or as the bare LaTeX if they copied out of
# the code block, and either is fine.
def extract(reply: str) -> Optional[str]:
    """The LaTeX out of whatever was pasted, or None."""
    block = tailor_module._extract_block(reply, ("tex", "latex"))
    if block and block.strip():
        return block.strip()
    # An empty fence is somebody copying the wrong half of a chat turn.
    text = (reply or "").strip()
    # No fence: accept it only if it reads as the document itself, so a
    # pasted apology or half a chat turn is refused rather than compiled.
    if r"\begin{document}" in text and r"\end{document}" in text:
        return text
    return None


def accept(reply: str, posting: Posting, resume_tex: Optional[str] = None,
           profile: Optional[str] = None,
           page_check: Optional[Callable[[str], Optional[int]]] = None,
           target_pages: int = tailor_module.TARGET_PAGES) -> tailor_module.TailorResult:
    """Take a pasted reply and hold it to every rule the API path applies.

    Raises `TailorError` with the reason in plain words when it fails, which
    the page shows above the box with what was pasted still in it. The
    volume floor and the swap floor are warnings here rather than
    rejections: on the API path they are rejections only while attempts
    remain, and a person who has been to a chat window and back has spent
    more than an attempt.
    """
    original = resume_tex if resume_tex is not None else tailor_module.load_base_resume()
    if profile is None:
        profile = tailor_module.load_profile()

    tailored = extract(reply)
    if tailored is None:
        raise tailor_module.TailorError(
            "no LaTeX found in what you pasted: paste the whole ```tex block, "
            "or the document itself from \\documentclass to \\end{document}")

    tailored, restored = tailor_module.restore_preamble(original, tailored)
    warnings = tailor_module._validate(original, tailored, profile, posting.text)
    if page_check is not None:
        pages = page_check(tailored)
        if pages is None:
            raise tailor_module.TailorError(
                "could not verify that the compiled resume is one page")
        if pages != target_pages:
            raise tailor_module.TailorError(
                f"compiled to {pages} pages, must be {target_pages}. "
                "Shorten the pasted resume and try again.")
    if restored:
        warnings = warnings + ["the preamble was restored from the master"]
    for soft in (tailor_module.under_tailored(original, tailored),
                 tailor_module.unused_swap(tailored, profile, posting.text)):
        if soft:
            warnings = warnings + [soft]

    rationale = tailor_module._extract_block(reply, ("rationale",)) or ""
    if not rationale.strip():
        rationale = "_Pasted in by hand; the reply carried no rationale block._"
    return tailor_module.TailorResult(
        tex=tailored,
        suggestions=rationale.strip(),
        diff=tailor_module.make_diff(original, tailored),
        model=f"{BY_HAND} (pasted in)",
        attempts=1,
        warnings=warnings,
    )
