"""Follow-up preparation that runs itself the moment a job is submitted.

The person asked for this in their own words: drafts with a resume and
everything attached, waiting for them to just click on send. So everything
up to the click happens here, by itself, in the background - find the
people, write one email each, make sure the tailored resume is really on
disk under the name it will go out as - and the click stays theirs.

The standing rule does not move: lookup and drafting are automation,
delivery is not. Nothing in this module imports `mailing`, and a guessed or
manually entered address still needs the person's "I verified this address"
tick before the send route will touch it.

Three things this has to survive, because the submission is the one event it
hangs off and a submission must be recorded whether or not anybody answers:

- It runs on a worker thread and raises nothing into its caller. Apollo
  being down, a model refusing three times, a missing folder: each lands as
  a readable line on the card (`lookup_note`, `prepare_status`) and changes
  no job status.
- It is idempotent. `prepare_status` is a claim on the work written before
  the thread starts, so a second trigger for the same job - a watch and a
  button firing together, a server restart, the person pressing it again -
  does nothing rather than writing a second set of drafts or spending a
  second Apollo credit on each person. Apollo's own search and enrich
  caches are the second line of that defence.
- It degrades rather than failing. With no Apollo key there is nobody to
  look up, so the people already on file are tried through Jobright's Find
  Any Email instead and every address it returns is labelled a guess. The
  card says which key was missing; it never says nothing at all.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import apollo, jobright, people, store
from .models import Contact, Draft, Outreach, now

log = logging.getLogger("autopilot.outreach")

ENV = "AUTOPILOT_OUTREACH"      # 0 turns the automatic preparation off
MAX_CONTACTS = 3                # "these three emails", the person's phrasing
RECRUITERS = 1                  # per employer, always: see `choose`
PER_ROLE = 3                    # searched for per role, before the pick - one
                                # recruiter is kept, so the slots behind it can
                                # only be filled by managers
STALE_SECONDS = 30 * 60         # a "running" claim older than this was left by a crash


class PrepareError(RuntimeError):
    """A reason worth showing the person on the card."""


_busy: set[str] = set()                     # job ids being prepared right now
_lock = threading.RLock()
_threads: dict[str, threading.Thread] = {}  # kept so tests and callers can wait


def enabled() -> bool:
    return os.environ.get(ENV, "1").strip() not in ("0", "false", "no")


# --- the pieces the server route shares with the automatic path ------------

def candidate_name(app_dir: Path) -> str:
    """The person's own name, for signing the email. "" when unknown.

    Form details first, since that is where they typed it; the cover letter's
    own signature second, since one was written for this very job.
    """
    import json
    import re

    import paths

    form = paths.BASE / "form.json"
    try:
        data = json.loads(form.read_text()) if form.exists() else {}
    except (OSError, ValueError):
        data = {}
    if isinstance(data, dict):
        full = str(data.get("full_name") or "").strip()
        if full:
            return full
        name = " ".join(
            str(data.get(key) or "").strip() for key in ("first_name", "last_name")
        ).strip()
        if name:
            return name
    letter = app_dir / "cover_letter.md"
    if letter.exists():
        match = re.search(r"(?im)^(?:Sincerely|Best),\s*\n([^\n]+)", letter.read_text())
        if match:
            return match.group(1).strip()
    return ""


def build_context(app_dir: Path):
    """The same grounding the resume and the cover letter were written from."""
    import paths
    from tailor import profile, tailor
    from tailor.answers import Context

    applicant = paths.BASE / "applicant.md"
    return Context.from_app_dir(
        app_dir,
        profile=tailor.load_profile(stories=profile.read_used(app_dir)),
        applicant=applicant.read_text(errors="replace") if applicant.exists() else "",
    )


def attachment_for(app_dir: Path) -> Optional[Path]:
    """The tailored resume under the name it will be sent as, made if needed.

    Done during preparation rather than at send time so that "resume
    attached" on the card is something that has already been checked on
    disk. `apply.upload_copy` is the one place that name is decided.
    """
    import apply as apply_script

    return apply_script.upload_copy(app_dir)


# Hosts that name a hiring system rather than an employer. A slug on one of
# these is the employer; the host itself never is.
BOARD_LABELS = {
    "greenhouse", "lever", "ashbyhq", "ashby", "workday", "myworkdayjobs",
    "myworkdaysite", "wd1", "wd3", "wd5", "icims", "jobvite", "taleo",
    "successfactors", "sapsf", "oracle", "oraclecloud", "fa", "smartrecruiters",
    "bamboohr", "workable", "eightfold", "dover", "gem", "rippling", "adp",
    "paylocity", "paycom", "ultipro", "jobright", "linkedin", "indeed",
    "glassdoor", "simplify", "ycombinator", "createyourowncareer", "applytojob",
    "breezy", "recruitee", "teamtailor", "pinpointhq", "jazzhr", "avature",
    "atozpublic",
    "careerpuck", "idprism-auth", "phenompeople", "brassring", "silkroad",
    "jobs2web", "cornerstoneondemand", "csod", "ats", "apply", "applications",
    "careers", "career", "jobs", "job", "myjobs", "jobsearch", "recruiting",
    "hire", "hiring", "talent", "www", "app", "apps", "portal", "secure",
    "my", "eppr", "hcmui", "hcm", "en", "us", "global", "sandbox",
}
# A label that is a hiring system, a generic word, or carries a digit tells
# us nothing about an employer. `career41`, `wd5`, `app` are all real
# examples off this person's own rows.
SUFFIXES = {"com", "net", "org", "io", "ai", "co", "edu", "gov", "us", "uk",
            "eu", "de", "ca", "in", "auto", "jobs", "careers", "dev", "app"}

# Systems that put the employer in the path or the query rather than the host.
# Each one was read off a real row of this person's queue; `pipeline.company_from`
# covers Greenhouse, Lever and Ashby already and these are the rest.
PATH_COMPANY = (
    re.compile(r"apply\.workable\.com/([^/?#]+)", re.I),
    re.compile(r"ats\.rippling\.com/(?:[a-z]{2}(?:-[A-Za-z]{2})?/)?([^/?#]+)", re.I),
    re.compile(r"myjobs\.adp\.com/([^/?#]+)", re.I),
    re.compile(r"careerpuck\.com/job-board/([^/?#]+)", re.I),
    re.compile(r"(?:sapsf|successfactors)\.[a-z.]+/careers\?company=([A-Za-z]+)", re.I),
    re.compile(r"jobs\.gem\.com/([^/?#]+)", re.I),
    re.compile(r"jobs\.jobvite\.com/([^/?#]+)", re.I),
    # A company's own short domain with the job under /careers: `ex.co`,
    # `jobs.ea.com`. The host's own label is the employer here, and it is
    # only reached when nothing above matched.
    re.compile(r"https?://(?:jobs\.)?([A-Za-z][\w-]{1,})\.(?:co|com)/(?:en_US/)?careers?/", re.I),
)


def company_for(job) -> str:
    """The employer to look people up at, however poorly the row names one.

    Fifty of the 363 submitted rows carry no company at all - a capture that
    landed on an embed, a page whose title was the role alone - and the web
    search then has nothing to search for, which is how a third of the first
    backfill rows failed with "the job has no company name". The name is in
    the URL in almost every one of those cases, so it is read from there
    rather than skipped: `pipeline.company_from` is the same reader the
    folder names come from, and the host is the last resort when it is not a
    hiring system's own host.

    Nothing is written back to the queue row. The row is the record of what
    was captured, and a name good enough to search with is not necessarily
    one good enough to file under.
    """
    named = (getattr(job, "company", "") or "").strip()
    if named:
        return named
    import urllib.parse

    import pipeline

    read = pipeline.company_from(job.title or "", job.url or "")
    if read:
        return read
    for pattern in PATH_COMPANY:
        match = pattern.search(job.url or "")
        if not match:
            continue
        words = [word for word in re.split(r"[-_\s]+", match.group(1)) if word]
        if words and words[0].lower() not in BOARD_LABELS:
            return " ".join(word[:1].upper() + word[1:] for word in words)
    # Left to right through the host, skipping whatever names a hiring
    # system rather than a company: `avature.sandisk.com` is SanDisk's
    # job on Avature, and `atozpublic.idprism-auth.amazon.com` is Amazon's.
    # A host that is only systems and suffixes answers nothing, which is
    # the right answer - a wrong employer is worse than none, because an
    # email to a real recruiter at the wrong company is the failure this
    # whole feature is for.
    host = (urllib.parse.urlsplit(job.url or "").hostname or "").lower()
    labels = host.split(".")
    for index, label in enumerate(labels):
        if index >= len(labels) - 1:
            break                      # the last label is the suffix
        if label in BOARD_LABELS or label in SUFFIXES:
            continue
        if any(character.isdigit() for character in label) or len(label) < 3:
            continue
        return label[:1].upper() + label[1:]
    return ""


# --- who to write to ------------------------------------------------------

def choose(contacts: list[Contact], limit: int = MAX_CONTACTS) -> list[Contact]:
    """One recruiter, then hiring managers. Never a second recruiter.

    A recruiter reads the mailbox the application landed in, so one comes
    first - and exactly one, the maintainer's own rule: "always do 1
    recruiter per account". Two or three recruiters at one employer is the
    same mailbox twice, which is how a first email becomes a thing to
    apologise for; a hiring manager is a different reader, so the remaining
    slots are theirs. Nothing is padded: fewer people found means fewer
    drafts, and the card says so.
    """
    order = {"recruiter": 0, "hiring_manager": 1}
    by_role: dict[str, list[Contact]] = {}
    for contact in sorted(contacts, key=lambda row: order.get(row.role, 9)):
        by_role.setdefault(contact.role, []).append(contact)
    picked: list[Contact] = []
    picked.extend(by_role.get("recruiter", [])[:RECRUITERS])
    picked.extend(by_role.get("hiring_manager", []))
    return picked[:limit]


def _with_email(job_id: str, contact: Contact, notes: list[str]) -> Optional[Contact]:
    """An address for one person, by the cheapest route that works.

    Free first, paid last, which is the maintainer's own instruction: "just
    to save credits". The subscription reads the public web (`people.address`,
    an allowance), then Jobright's Find Any Email (free), and only then
    Apollo's enrichment, which is the one step that spends a credit per
    person. Its own cache means a second preparation of the same job spends
    nothing again.

    The order has a cost worth knowing: Apollo is the only route that returns
    a *verified* address, so under this order most drafts carry a guess and
    the send route asks the person to tick it. That is the trade - a tick per
    email instead of a credit per email - and the card's own Apollo button
    buys certainty for one person at a time when it is worth it.
    """
    if contact.email:
        return contact
    if people.available():
        try:
            found = people.address(contact)
            if found is not None:
                store.add_contacts(job_id, [found])
                notes.append(f"{contact.name}: address found on the web, "
                             "tick it before sending")
                return found
        except Exception as exc:              # noqa: BLE001 - free step, two left
            log.info("web address lookup failed for %s: %s", contact.name, exc)
    if contact.linkedin_url:
        try:
            guessed = asyncio.run(jobright.lookup(contact))
            store.add_contacts(job_id, [guessed])
            notes.append(f"{contact.name}: address is a Jobright guess, tick it before sending")
            return guessed
        except jobright.JobrightError as exc:
            notes.append(f"{contact.name}: no free address ({exc})")
    if apollo.configured() and (contact.linkedin_url or contact.apollo_id
                                or (contact.name and contact.company)):
        try:
            enriched = apollo.enrich(contact)
            store.add_contacts(job_id, [enriched])
            notes.append(f"{contact.name}: verified by Apollo (one credit)")
            return enriched
        except apollo.ApolloError as exc:
            notes.append(f"{contact.name}: Apollo had no address ({exc})")
            return None
    notes.append(f"{contact.name}: no address and nothing left to look one up by")
    return None


def _find_people(job_id: str, company: str, title: str,
                 notes: list[str], blocked: list[str]) -> list[Contact]:
    """The people to write to. The subscription looks first, Apollo second.

    `blocked` collects the reasons a retry could fix - a missing key, a
    search that threw, a row with no employer on it. An empty `blocked` and
    nobody found means the ladder ran to the end and there is simply nobody
    to write to, which `_finish` treats as an ending rather than a failure.

    `AUTOPILOT_OUTREACH_CLAUDE=0` goes straight to Apollo, as it did before.
    Apollo's *search* costs nothing either - the credit is spent on the
    address, in `_with_email` - so asking the subscription first is not about
    this step's price: it is what makes the whole thing work with no Apollo
    key at all, and what keeps a company Apollo has no row for from being a
    dead end.

    Whoever is already on file is kept either way: a contact added by hand,
    or found on an earlier run, is a person the person themselves chose.
    """
    if people.available():
        try:
            found = people.find(company, title, PER_ROLE)
            if found:
                store.add_contacts(job_id, found)
                return store.get(job_id).contacts
            notes.append("Claude found nobody on the web for this company.")
        except Exception as exc:              # noqa: BLE001 - Apollo is still to come
            notes.append(f"the web search for people failed ({exc})")
            blocked.append("the web search for people failed")
    existing = store.get(job_id).contacts
    if not apollo.configured():
        # Said whoever is on file, because it is the reason every address
        # below is a guess rather than a verified one.
        notes.append(
            "Apollo is not connected, so no address here is verified. Add the "
            "Apollo key in Settings and press Prepare again for a verified one.",
        )
        blocked.append("Apollo is not connected")
        return existing
    if not company.strip():
        notes.append("the job has no company name, so Apollo had nothing to search for")
        blocked.append("the job has no company name")
        return existing
    try:
        found = apollo.discover(company, PER_ROLE)
    except apollo.ApolloError as exc:
        notes.append(f"Apollo search failed: {exc}")
        blocked.append(f"Apollo search failed: {exc}")
        return existing
    if found:
        store.add_contacts(job_id, found)
    return store.get(job_id).contacts


# --- the preparation itself -----------------------------------------------

def prepare(job_id: str) -> Outreach:
    """Find the people, write one email each, attach the resume. Sends nothing.

    Synchronous and safe to call directly; `start` is what the submission
    paths use. Every failure is recorded on the returned state rather than
    raised, because the caller of this work is a submission that has already
    happened.
    """
    notes: list[str] = []
    blocked: list[str] = []
    try:
        job, app_dir = _job_and_dir(job_id)
        company = company_for(job)
        if company and company != (job.company or "").strip():
            notes.append(f'the row names no employer, so "{company}" was read off the posting URL.')
        contacts = choose(_find_people(job_id, company, job.title, notes, blocked))
        written = 0
        attachment = attachment_for(app_dir)
        if attachment is None:
            notes.append("the tailored resume PDF is missing, so nothing was attached")
        context = build_context(app_dir) if contacts else None
        signature = candidate_name(app_dir)
        for contact in contacts:
            if _draft_for(job_id, contact.id):
                continue                      # already has an email waiting; leave it alone
            if _live_drafts(job_id) >= MAX_CONTACTS:
                # The cap is on the job, not on one run of this. A second
                # preparation picks different people - the web found two the
                # first run did not - and three emails plus one more is four
                # people at one employer, which is the thing the cap exists
                # to prevent (measured on a Qualtrics job, 2026-11-03).
                notes.append(f"{MAX_CONTACTS} emails are already waiting, so no more "
                             "were written.")
                break
            with_email = _with_email(job_id, contact, notes)
            if with_email is None or not with_email.email:
                continue
            if not signature:
                notes.append("your own name is missing from Form details and the cover letter")
                blocked.append("your own name is missing")
                break
            try:
                store.put_draft(job_id, _write(
                    context, with_email, job, app_dir, signature, attachment,
                ))
                written += 1
            except Exception as exc:          # noqa: BLE001 - one readable line per person
                notes.append(f"{with_email.name}: the email could not be written ({exc})")
        return _finish(job_id, written, len(contacts), notes, status="ready",
                       blocked=blocked)
    except Exception as exc:                  # noqa: BLE001 - nothing may reach the caller
        log.warning("outreach preparation failed for %s: %s", job_id, exc)
        notes.append(str(exc))
        return _finish(job_id, 0, 0, notes, status="failed")


def _write(context, contact: Contact, job, app_dir: Path, signature: str,
           attachment: Optional[Path]) -> Draft:
    from tailor import outreach as writer

    draft = writer.draft(
        context, contact, job.title, job.company, signature,
        True, app_dir.name,
    )
    return draft.model_copy(update={
        "attach_resume": attachment is not None,
        "attachment_name": attachment.name if attachment else "",
    })


def _live_drafts(job_id: str) -> int:
    """Emails already waiting on this job, however many runs wrote them."""
    return len([row for row in store.get(job_id).drafts
                if row.status in store.LIVE_DRAFTS])


def _draft_for(job_id: str, contact_id: str) -> Optional[Draft]:
    return next(
        (row for row in store.get(job_id).drafts
         if row.contact_id == contact_id and row.status in store.LIVE_DRAFTS),
        None,
    )


def _job_and_dir(job_id: str):
    from server import queue           # imported here: the server imports this module

    job = queue.get(job_id)
    if job is None:
        raise PrepareError(f"no job {job_id}")
    if not job.app_dir or not Path(job.app_dir).exists():
        raise PrepareError("the tailored application folder is missing")
    return job, Path(job.app_dir)


def _finish(job_id: str, written: int, people: int, notes: list[str],
            status: str, blocked: Optional[list[str]] = None) -> Outreach:
    outreach = store.get(job_id)
    total = len([row for row in outreach.drafts if row.status in store.LIVE_DRAFTS])
    ended = False
    if status == "ready" and total == 0:
        if blocked:
            # Something a retry could fix stopped it - a missing key, a search
            # that threw. That is a failure, and the reason belongs in `error`
            # where the card shows it.
            status = "failed"
        else:
            # The ladder ran to the end, free rung to paid, and there is
            # nobody at this company to write to. Asked for by the maintainer
            # (2026-11-07): count it done rather than leaving it failed, so
            # the application goes back under Applied instead of sitting in
            # "emails to send" for ever, nagging for a send that cannot
            # happen. Not a status change on the job: `done_at` is the same
            # field the person's own "done with the outreach" sets, and the
            # card's "Reopen it" undoes this in one press.
            ended = True
    line = _summary(written, total, people, ended)
    outreach.lookup_note = " ".join([line] + notes).strip()
    outreach.error = "; ".join(notes) if status == "failed" else ""
    outreach.prepare_status = status
    outreach.prepared_at = now()
    if ended and not outreach.done_at:
        outreach.done_at = now()
    if not outreach.active_draft and outreach.drafts:
        outreach.active_draft = outreach.drafts[0].id
    return store.save(outreach)


def _summary(written: int, total: int, people: int, ended: bool = False) -> str:
    """What was prepared, counted honestly. Fewer than three is said, not padded."""
    if total == 0 and ended:
        return ("Nobody at this company could be reached - the web and Apollo both "
                "came back empty - so the outreach is counted as done.")
    if total == 0:
        return "No follow-up emails are ready yet."
    short = "" if total >= MAX_CONTACTS else f" Only {total} of {MAX_CONTACTS} people were found."
    fresh = f"Wrote {written} new." if written else "Nothing new to write."
    return f"{total} follow-up email(s) ready to send, resume attached. {fresh}{short}"


# --- the trigger ----------------------------------------------------------

def start(job_id: str, force: bool = False) -> bool:
    """Start the follow-up preparation for one job in the background.

    This is the entry point a submission calls. It returns at once - True if
    this call started the work, False if there was nothing to start - and it
    never raises, so a submission is recorded whether or not any of this
    succeeds.

    Safe to call more than once for the same job: a job whose preparation is
    running or already ready is left alone, so no second set of drafts is
    written and no second Apollo credit is spent. `force=True` is the
    person's own "Prepare again" and runs it anyway; it still skips a
    contact who already has an email waiting.

    With no Apollo API key nothing fails: the search is skipped, the people
    already on file get a Jobright guess apiece, and the card says the key
    is missing. With no Gmail App Password the drafts are still written and
    still unsent, because that key belongs to the click, not to this.
    `AUTOPILOT_OUTREACH=0` turns the whole thing off.
    """
    try:
        if not enabled():
            return False
        with _lock:
            if job_id in _busy:
                return False
            state = store.get(job_id)
            if not force and state.prepare_status in ("running", "ready") \
                    and not _stale(state):
                return False
            _busy.add(job_id)
        # The claim is written before the thread exists, so a trigger landing
        # a millisecond later - or after a restart - sees the work is taken.
        claimed = store.get(job_id)
        claimed.prepare_status = "running"
        claimed.prepared_at = now()
        store.save(claimed)
        thread = threading.Thread(
            target=_run, args=(job_id,),
            name=f"autopilot-outreach-{job_id}", daemon=True,
        )
        with _lock:
            _threads[job_id] = thread
        thread.start()
        return True
    except Exception as exc:                  # noqa: BLE001 - a submission must not fail here
        log.warning("outreach preparation could not start for %s: %s", job_id, exc)
        with _lock:
            _busy.discard(job_id)
        return False


def _run(job_id: str) -> None:
    try:
        prepare(job_id)
    finally:
        with _lock:
            _busy.discard(job_id)


def _stale(state: Outreach) -> bool:
    """A "running" claim nobody is honouring any more, from a killed process.

    Only a claim can go stale. A job already prepared is not stale however
    long ago it was done, or every submission would be re-prepared on the
    next trigger and the drafts the person has been editing would be joined
    by a second set.
    """
    if state.prepare_status != "running":
        return False
    if not state.prepared_at:
        return True
    try:
        started = datetime.fromisoformat(state.prepared_at)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - started).total_seconds() > STALE_SECONDS


def running(job_id: str) -> bool:
    with _lock:
        return job_id in _busy


def wait(job_id: str, timeout: float = 30.0) -> None:
    """Block until this job's preparation thread is done. For tests and scripts."""
    with _lock:
        thread = _threads.get(job_id)
    if thread is not None:
        thread.join(timeout)
