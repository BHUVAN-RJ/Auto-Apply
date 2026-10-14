"""The identity ladder, against the URL shapes that are actually on disk.

Every URL here was taken out of `data/queue.json` on this install and then
stripped of its Jobright tag unless the tag is what the test is about. None
of them is invented, because the shapes that break a canonicaliser are not
the ones anybody would think to invent: Greenhouse's one path for every
job, Eightfold's `?pid=`, RippleHire's job id in the fragment, and the
`/apply` tail that made one Vagaro job two rows in the real queue.

The suite stubs no model and needs no network: nothing in `identity/`
makes a request.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import identity
from identity import index, keys, ladder, sources, store

# --- the corpus's own URL shapes -------------------------------------------

GREENHOUSE_EMBED = "https://job-boards.greenhouse.io/embed/job_app?for=togetherai&token=5211582007"
GREENHOUSE_BOARD = "https://boards.greenhouse.io/togetherai/jobs/5211582007"
GREENHOUSE_NEW = "https://job-boards.greenhouse.io/togetherai/jobs/5211582007"
GREENHOUSE_OTHER = "https://job-boards.greenhouse.io/embed/job_app?for=togetherai&token=5211582008"

ASHBY_FORM = ("https://jobs.ashbyhq.com/fieldguide/"
              "c7af5e2a-7522-4c8c-91f0-2c3542d42548/application")
ASHBY_OVERVIEW = "https://jobs.ashbyhq.com/fieldguide/c7af5e2a-7522-4c8c-91f0-2c3542d42548"

LEVER_FORM = "https://jobs.lever.co/palantir/dab396d4-2f14-4796-aac0-0d82883dccf0/apply"
LEVER_POSTING = "https://jobs.lever.co/palantir/dab396d4-2f14-4796-aac0-0d82883dccf0"

WORKDAY = ("https://nvidia.wd5.myworkdayjobs.com/nvidiaexternalcareersite/job/"
           "US-CA-Santa-Clara/Systems-Software-Engineer_JR2000123")
WORKDAY_LOCALE = ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/"
                  "US-CA-Santa-Clara/Systems-Software-Engineer_JR2000123")

EIGHTFOLD_ONE = "https://nvidia.eightfold.ai/careers?start=0&pid=893397728612&sort_by=hot"
EIGHTFOLD_TWO = "https://nvidia.eightfold.ai/careers?start=0&pid=893397846100&sort_by=hot"

BREEZY_POSTING = "https://vagaro.breezy.hr/p/1a62d98b9483-software-engineer"
BREEZY_FORM = "https://vagaro.breezy.hr/p/1a62d98b9483-software-engineer/apply"

VAGARO_TAGGED = BREEZY_POSTING + "?jr_id=6aa894d1a77a53f5a157a176"

LINKEDIN_VIEW = "https://www.linkedin.com/jobs/view/4123456789"
LINKEDIN_SEARCH = "https://www.linkedin.com/jobs/search?currentJobId=4123456789&keywords=swe"


# --- a corpus of our own ---------------------------------------------------

def posting(role: str, extra: str = "") -> str:
    """A text long enough and prose enough to be a posting.

    `quality.too_thin` wants 150 cleaned words with a section heading, and
    `keys.usable_posting` is what the simhash rule asks before comparing
    anything, so a fixture that does not clear it is not testing the rule.
    """
    lines = ["Responsibilities",
             f"You will build and operate the {role} systems our customers depend on.",
             extra or "You will work with a small team on the data path end to end."]
    for number in range(20):
        lines.append(f"Design, ship and measure part {number} of the {role} platform "
                     f"with the team that owns it.")
    return "\n".join(lines)


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    """An index of our own, over a queue and an applications folder of our own.

    Nothing here touches the real data: the three paths are resolved inside
    the functions that use them, which is the only reason an environment
    variable can move them at all.
    """
    data = tmp_path / "data"
    applications = tmp_path / "applications"
    data.mkdir()
    applications.mkdir()
    monkeypatch.setenv("AUTOPILOT_QUEUE", str(data / "queue.json"))
    monkeypatch.setenv("AUTOPILOT_APPLICATIONS", str(applications))
    monkeypatch.setenv("AUTOPILOT_IDENTITY_DB", str(data / "identity.sqlite3"))

    class Corpus:
        def __init__(self) -> None:
            self.rows: list[dict] = []

        def add(self, url: str, title: str = "", company: str = "", status: str = "submitted",
                location: str = "", text: str = "", added_at: str = "2026-09-01T00:00:00+00:00",
                decided_at: str = "") -> str:
            display_id = hashlib.sha256(url.encode()).hexdigest()[:4]
            folder = ""
            if location or text:
                folder = applications / f"{display_id}_{len(self.rows)}"
                folder.mkdir()
                header = [f"# {title}", "", f"**Source:** {url}", ""]
                if company:
                    header.append(f"**Company:** {company}")
                if location:
                    header.append(f"**Location:** {location}")
                (folder / "posting.md").write_text(
                    "\n".join(header) + "\n\n---\n" + (text or ""))
                (folder / "status.json").write_text(json.dumps({
                    "status": status,
                    "history": [{"status": status, "at": decided_at or added_at}],
                }))
                folder = str(folder)
            self.rows.append({"url": url, "title": title, "company": company,
                              "status": status, "added_at": added_at,
                              "app_dir": folder or None})
            self.write()
            return display_id

        def write(self) -> None:
            (data / "queue.json").write_text(json.dumps(self.rows))

        def build(self) -> dict:
            return identity.backfill()

    return Corpus()


# --- level 1: the exact page ------------------------------------------------

def test_tracking_goes_and_the_job_stays():
    assert keys.canonical(VAGARO_TAGGED) == keys.canonical(BREEZY_POSTING)
    # Greenhouse's embed is one path for every job and the job is `token`.
    assert "token=5211582007" in keys.canonical(GREENHOUSE_EMBED)
    assert "for=togetherai" in keys.canonical(GREENHOUSE_EMBED)
    assert keys.canonical(GREENHOUSE_EMBED) != keys.canonical(GREENHOUSE_OTHER)


def test_two_jobs_at_one_eightfold_tenant_stay_two_jobs():
    """`server.seen` keeps an allow-list of job parameters and `pid` is not
    on it, so both of these canonicalise to one page there."""
    assert keys.canonical(EIGHTFOLD_ONE) != keys.canonical(EIGHTFOLD_TWO)
    assert keys.ats_identity(EIGHTFOLD_ONE) != keys.ats_identity(EIGHTFOLD_TWO)


@pytest.mark.parametrize("form, overview", [
    (ASHBY_FORM, ASHBY_OVERVIEW),
    (LEVER_FORM, LEVER_POSTING),
    (BREEZY_FORM, BREEZY_POSTING),
])
def test_the_form_and_the_overview_are_one_page(form, overview):
    """The real queue holds this Vagaro job twice, four minutes apart, one
    failed and one submitted, because `/apply` made it a second page."""
    assert keys.canonical(form) == keys.canonical(overview)


def test_the_fragment_is_kept_when_it_is_the_job():
    # RippleHire, JobDiva and HealthcareSource put the job id after the `#`.
    one = "https://mphasis.ripplehire.com/candidate/?token=abc#detail/job/909050"
    two = "https://mphasis.ripplehire.com/candidate/?token=abc#detail/job/882979"
    assert keys.canonical(one) != keys.canonical(two)
    assert keys.canonical(one).endswith("#detail/job/909050")


def test_the_session_token_is_not_the_job_on_ripplehire():
    one = "https://mphasis.ripplehire.com/candidate/?token=aaa#detail/job/909050"
    two = "https://mphasis.ripplehire.com/candidate/?token=bbb#detail/job/909050"
    assert keys.canonical(one) == keys.canonical(two)


# --- level 2: the board's own id -------------------------------------------

@pytest.mark.parametrize("url", [GREENHOUSE_EMBED, GREENHOUSE_BOARD, GREENHOUSE_NEW])
def test_three_greenhouse_shapes_are_one_job(url):
    assert keys.ats_identity(url) == ("greenhouse", "togetherai", "5211582007")


def test_a_different_greenhouse_token_is_a_different_job():
    assert keys.ats_identity(GREENHOUSE_OTHER) == ("greenhouse", "togetherai", "5211582008")


def test_workday_reads_the_same_job_through_a_locale_path():
    assert keys.ats_identity(WORKDAY) == keys.ats_identity(WORKDAY_LOCALE)


@pytest.mark.parametrize("url, expected", [
    (LINKEDIN_VIEW, ("linkedin", "", "4123456789")),
    (LINKEDIN_SEARCH, ("linkedin", "", "4123456789")),
    ("https://simplify.jobs/p/2B617501-CC1A-4629-8A30-BD0DCED697FD",
     ("simplify", "", "2b617501-cc1a-4629-8a30-bd0dced697fd")),
    ("https://jobs.smartrecruiters.com/oneclick-ui/company/ServiceNow/publication/"
     "2e308b2a-49ac-4d9f-9ec9-84a590665b28",
     ("smartrecruiters", "servicenow", "2e308b2a-49ac-4d9f-9ec9-84a590665b28")),
    ("https://employees-hrblock.icims.com/jobs/76907/machine-learning-engineer/job",
     ("icims", "employees-hrblock", "76907")),
    ("https://apply.workable.com/multimediallc/j/CDA26CF98C/apply/",
     ("workable", "multimediallc", "CDA26CF98C")),
    ("https://jobs.jobvite.com/tylertech/job/oJBGAfwk", ("jobvite", "tylertech", "oJBGAfwk")),
    ("https://app.dover.com/apply/Caretria/134f2e86-b466-49f9-a5da-4fe89f40aefb",
     ("dover", "caretria", "134f2e86-b466-49f9-a5da-4fe89f40aefb")),
    ("https://koch.avature.net/en_US/careers/JobDetail/195477", ("avature", "koch", "195477")),
    ("https://flatironssolutions.pinpointhq.com/postings/"
     "2b617501-cc1a-4629-8a30-bd0dced697fd",
     ("pinpoint", "flatironssolutions", "2b617501-cc1a-4629-8a30-bd0dced697fd")),
    ("https://cfopitt.taleo.net/careersection/pitt_temp_arer/jobdetail.ftl?job=452189",
     ("taleo", "cfopitt.taleo.net", "452189")),
    ("https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/"
     "sites/CX_1/job/31660", ("oracle", "fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com", "31660")),
])
def test_the_board_and_its_job_id_are_read_out_of_a_real_url(url, expected):
    assert keys.ats_identity(url) == expected


def test_a_page_with_no_job_on_it_has_no_board_identity():
    # SuccessFactors' bare company page: a list, not a posting.
    assert keys.ats_identity("https://career4.successfactors.com/careers?company=skyworks") is None


def test_the_jobright_tag_links_the_employer_page_to_the_jobright_page():
    assert (keys.jobright_id(VAGARO_TAGGED)
            == keys.jobright_id("https://jobright.ai/jobs/info/6aa894d1a77a53f5a157a176"))


# --- level 3 and 4: the words ----------------------------------------------

def test_a_company_is_not_its_legal_suffix_or_the_board_s_url_slug():
    assert keys.norm_company("Exelixis Inc") == "exelixis"
    assert keys.norm_company("Sift Stack, Inc.") == "sift stack"
    assert keys.norm_company("0001 Applied Materials, Inc") == "applied materials"
    # SmartRecruiters' own slug stood in for six different employers.
    assert keys.norm_company("oneclick-ui") == ""
    assert keys.norm_company("Careers") == ""


def test_the_form_s_own_title_is_the_role():
    assert (keys.norm_title("Job Application for Software Engineer, New Grad (2027) at Together AI",
                            "Together AI")
            == "software engineer")
    assert keys.norm_title("Jobs | Careers | McKinsey & Company") == ""


def test_what_is_in_brackets_is_part_of_the_role():
    """Deleting the brackets merged two real Fieldguide openings."""
    audit = keys.norm_title("[Remote] Software Engineer, (Internal Audit) [All Levels]")
    plain = keys.norm_title("Software Engineer (All Levels)")
    assert audit == "software engineer internal audit"
    assert plain == "software engineer"
    assert audit != plain


def test_a_level_number_survives_normalising():
    assert keys.norm_title("Software Engineer III") == "software engineer 3"
    assert keys.norm_title("Engr, Software 1") == "engr software 1"


@pytest.mark.parametrize("a, b", [
    ("Software Engineer I", "Software Engineer II"),
    # Applied Materials has these two, sharing one 578-word description.
    ("New College Grad Software Development Engineer", "New College Grad Software Engineer I"),
    ("Software Engineer", "Software Engineer Intern"),
    ("Data Scientist", "Data Scientist Co-Op"),
])
def test_two_openings_that_differ_by_level_or_kind_are_never_one_job(a, b):
    assert keys.conflicting(a, b)


def test_titles_are_compared_as_token_sets_not_as_characters():
    """`seen.title_similarity` takes the greater of a token set and a
    character ratio, and these two are 0.95 alike as characters."""
    assert keys.title_similarity("Software Engineer I", "Software Engineer II") < 0.6


@pytest.mark.parametrize("place, expected", [
    ("San Francisco, CA", "san-francisco-ca"),
    ("San Francisco, California, United States", "san-francisco-ca"),
    ("US, CA, Santa Clara, United States of America", "santa-clara-ca"),
    ("Santa Clara,CA", "santa-clara-ca"),
    ("Remote-WA", "remote-wa"),
    ("United States, US", ""),
    ("", ""),
])
def test_a_place_is_read_the_same_whichever_way_it_is_written(place, expected):
    assert keys.norm_location(place) == expected


def test_remote_is_a_place():
    assert keys.norm_location("San Francisco, CA or Remote (USA)") == "san-francisco-ca"
    assert keys.norm_location("Remote (USA)") == "remote-us"


# --- the verdict -----------------------------------------------------------

def test_a_submitted_job_blocks_its_own_url_again(corpus):
    corpus.add(GREENHOUSE_EMBED, "Job Application for Software Engineer at Together AI",
               "Together AI", status="submitted")
    corpus.build()
    answer = identity.lookup(GREENHOUSE_EMBED)
    assert answer.verdict == identity.BLOCK
    assert answer.best.rule == "exact url"
    assert answer.best.confidence == identity.CERTAIN
    assert answer.best.evidence[0].mine == answer.best.evidence[0].theirs


def test_a_submitted_job_blocks_the_other_two_greenhouse_shapes(corpus):
    corpus.add(GREENHOUSE_EMBED, "Software Engineer", "Together AI")
    corpus.build()
    for shape in (GREENHOUSE_BOARD, GREENHOUSE_NEW):
        answer = identity.lookup(shape)
        assert answer.verdict == identity.BLOCK, shape
        assert answer.best.rule == "same job at the same board"


def test_a_different_token_on_the_same_board_is_clear(corpus):
    corpus.add(GREENHOUSE_EMBED, "Software Engineer", "Together AI")
    corpus.build()
    assert identity.lookup(GREENHOUSE_OTHER).verdict == identity.CLEAR


def test_a_job_in_the_pipeline_blocks_too(corpus):
    corpus.add(ASHBY_FORM, "Software Engineer", "Fieldguide", status="tailoring")
    corpus.build()
    answer = identity.lookup(ASHBY_OVERVIEW)
    assert answer.verdict == identity.BLOCK
    assert "already in autopilot" in answer.reason


def test_a_rejected_job_warns_rather_than_blocks(corpus):
    corpus.add(ASHBY_FORM, "Software Engineer", "Fieldguide", status="skipped",
               added_at="2026-10-01T00:00:00+00:00")
    corpus.build()
    answer = identity.lookup(ASHBY_FORM)
    assert answer.verdict == identity.WARN
    assert answer.best.confidence == identity.CERTAIN


def test_a_failed_job_warns_so_it_can_be_tried_again(corpus):
    corpus.add(BREEZY_POSTING, "Software Engineer", "Vagaro", status="failed")
    corpus.build()
    assert identity.lookup(BREEZY_FORM).verdict == identity.WARN


def test_nothing_known_is_clear(corpus):
    corpus.add(GREENHOUSE_EMBED, "Software Engineer", "Together AI")
    corpus.build()
    answer = identity.lookup(LINKEDIN_VIEW, title="Software Engineer", company="Someone Else")
    assert answer.verdict == identity.CLEAR
    assert answer.matches == []


def test_the_same_title_at_another_company_does_not_match(corpus):
    corpus.add(GREENHOUSE_EMBED, "Software Engineer I", "Together AI",
               location="San Francisco, CA", text=posting("inference"))
    corpus.build()
    answer = identity.lookup(LEVER_POSTING, title="Software Engineer I", company="Palantir",
                             location="San Francisco, CA", text=posting("inference"))
    assert answer.verdict == identity.CLEAR


def test_the_same_role_at_the_same_company_and_place_is_certain(corpus):
    """NVIDIA has one requisition listed on Workday and on Eightfold; the
    two pages share nothing but the company, the title and the place."""
    corpus.add(WORKDAY, "Systems Software Engineer, AI and Cloud - New College Grad 2026",
               "NVIDIA", location="US, CA, Santa Clara")
    corpus.build()
    answer = identity.lookup(EIGHTFOLD_ONE,
                             title="Systems Software Engineer,  AI and Cloud - New College Grad 2026",
                             company="NVIDIA", location="Santa Clara, CA,US")
    assert answer.verdict == identity.BLOCK
    assert answer.best.rule == "same company, title and place"
    assert answer.best.level == 3


def test_the_same_role_in_another_city_is_a_warning_not_a_block(corpus):
    corpus.add(WORKDAY, "Deployed Engineer", "LangChain", location="San Francisco, CA")
    corpus.build()
    answer = identity.lookup(LEVER_POSTING, title="Deployed Engineer", company="LangChain",
                             location="New York, NY")
    assert answer.verdict == identity.WARN
    assert answer.best.confidence == identity.LIKELY
    assert answer.best.rule == "same company and title, different place"


def test_the_same_role_with_no_place_on_either_side_is_only_likely(corpus):
    corpus.add(WORKDAY, "Deployed Engineer", "LangChain")
    corpus.build()
    answer = identity.lookup(LEVER_POSTING, title="Deployed Engineer", company="LangChain")
    assert answer.verdict == identity.WARN
    assert answer.best.confidence == identity.LIKELY


def test_one_role_spelled_two_ways_matches_on_the_title(corpus):
    """Exelixis's "Associate Engineer - AI and Agentic" and "AI and Agentic
    Engineer Associate" are one job, and the only rule that says so is the
    token set."""
    corpus.add(WORKDAY, "Associate Engineer - AI and Agentic", "Exelixis Inc")
    corpus.build()
    answer = identity.lookup(LEVER_POSTING, title="AI and Agentic Engineer Associate",
                             company="Exelixis")
    assert answer.verdict == identity.WARN
    assert answer.best.rule == "same company, near-identical title"
    assert answer.best.level == 4


def test_two_palantir_roles_that_read_alike_are_not_matched(corpus):
    """0.80 alike by token set, and two real openings. The threshold is
    above them on purpose."""
    corpus.add(LEVER_POSTING, "Forward Deployed Software Engineer", "Palantir")
    corpus.build()
    answer = identity.lookup(GREENHOUSE_EMBED,
                             title="Forward Deployed Software Engineer, New Grad - Commercial",
                             company="Palantir")
    assert answer.verdict == identity.CLEAR


def test_the_same_description_at_one_company_is_a_warning(corpus):
    text = posting("payments")
    corpus.add(WORKDAY, "Deployed Engineer", "LangChain", text=text)
    corpus.build()
    answer = identity.lookup(LEVER_POSTING, title="Solutions Architect", company="LangChain",
                             text=text)
    assert answer.verdict == identity.WARN
    assert answer.best.rule == "same company, same description"


def test_a_sign_in_wall_is_not_a_description(corpus):
    """Six rows in the real corpus are the same SmartRecruiters shell for
    six different employers, 0 bits apart."""
    shell = "Easy apply\nSign in to continue\nThe form has been closed."
    assert not keys.usable_posting(shell)
    corpus.add(WORKDAY, "Machine Learning Engineer", "LangChain", text=shell)
    corpus.build()
    answer = identity.lookup(LEVER_POSTING, title="Technology Engineer", company="LangChain",
                             text=shell)
    assert answer.verdict == identity.CLEAR


def test_a_whole_board_read_as_one_posting_is_not_a_description():
    """A Lever `/apply` fetch read the whole Palantir board: 10,702 words."""
    # Unique lines, because `quality.clean` keeps each line once and a
    # repeated paragraph would be thrown away rather than counted.
    board = "\n".join(posting(f"team-{number}") for number in range(40))
    assert not keys.usable_posting(board)


# --- the 90-day rule -------------------------------------------------------

def test_a_rejection_stops_matching_by_resemblance_after_ninety_days(corpus):
    corpus.add(WORKDAY, "Deployed Engineer", "LangChain", status="skipped",
               added_at="2020-01-01T00:00:00+00:00", location="San Francisco, CA",
               decided_at="2020-01-01T00:00:00+00:00")
    corpus.build()
    answer = identity.lookup(LEVER_POSTING, title="Deployed Engineer", company="LangChain",
                             location="San Francisco, CA")
    assert answer.verdict == identity.CLEAR


def test_an_expired_rejection_still_answers_for_its_own_url(corpus):
    """The age rule is about resemblance. The same page is the same page."""
    corpus.add(WORKDAY, "Deployed Engineer", "LangChain", status="skipped",
               added_at="2020-01-01T00:00:00+00:00")
    corpus.build()
    answer = identity.lookup(WORKDAY)
    assert answer.verdict == identity.WARN
    assert answer.best.rule == "exact url"


# --- staleness -------------------------------------------------------------

def test_a_job_added_after_the_index_was_built_is_not_clear(corpus):
    corpus.add(GREENHOUSE_EMBED, "Software Engineer", "Together AI")
    corpus.build()
    corpus.add(ASHBY_FORM, "Software Engineer", "Fieldguide")  # writes the queue again
    answer = identity.lookup(ASHBY_OVERVIEW)
    assert answer.verdict == identity.BLOCK
    assert answer.stale == ["queue"]


def test_a_lookup_that_refuses_to_catch_up_says_so(corpus):
    corpus.add(GREENHOUSE_EMBED, "Software Engineer", "Together AI")
    corpus.build()
    corpus.add(ASHBY_FORM, "Software Engineer", "Fieldguide")
    answer = identity.lookup(ASHBY_OVERVIEW, refresh=False)
    assert answer.verdict == identity.CLEAR
    assert answer.stale == ["queue"]  # and the caller has been told not to trust it


def test_a_status_that_moved_under_the_index_is_picked_up(corpus):
    corpus.add(GREENHOUSE_EMBED, "Software Engineer", "Together AI", status="queued")
    corpus.build()
    corpus.rows[0]["status"] = "submitted"
    corpus.write()
    answer = identity.lookup(GREENHOUSE_EMBED)
    assert answer.best.status == "submitted"
    assert "already applied for" in answer.reason


def test_adding_one_job_does_not_need_a_rebuild(corpus):
    corpus.add(GREENHOUSE_EMBED, "Software Engineer", "Together AI")
    corpus.build()

    class Row:
        url = ASHBY_FORM
        id = "abcd"
        title = "Software Engineer"
        company = "Fieldguide"
        status = "queued"
        added_at = "2026-10-01T00:00:00+00:00"
        app_dir = None

    identity.add(Row())
    with store.connect() as connection:
        assert store.by_key(connection, "url", keys.canonical(ASHBY_OVERVIEW))


def test_a_backfill_is_idempotent(corpus):
    corpus.add(GREENHOUSE_EMBED, "Software Engineer", "Together AI",
               location="San Francisco, CA", text=posting("inference"))
    corpus.add(ASHBY_FORM, "Software Engineer", "Fieldguide")
    first = corpus.build()
    second = corpus.build()
    assert first["jobs"] == second["jobs"] == 2
    assert second["dropped"] == 0
    assert second["reused"] >= 1  # the posting was not hashed a second time


def test_a_job_that_left_the_queue_leaves_the_index(corpus):
    corpus.add(GREENHOUSE_EMBED, "Software Engineer", "Together AI")
    corpus.add(ASHBY_FORM, "Software Engineer", "Fieldguide")
    corpus.build()
    corpus.rows.pop()
    corpus.write()
    assert corpus.build()["jobs"] == 1
    assert identity.lookup(ASHBY_FORM).verdict == identity.CLEAR


# --- the short id -----------------------------------------------------------

def _colliding_urls() -> tuple[str, str]:
    """Two URLs whose four-hex `url_hash` is the same. Found by search, not
    by invention: 65,536 values is few enough that a few hundred tries
    find a pair, which is the whole point of KNOWN_ISSUES #1."""
    seen: dict[str, str] = {}
    for number in range(100000):
        url = f"https://boards.greenhouse.io/acme/jobs/{number}"
        short = hashlib.sha256(url.encode()).hexdigest()[:4]
        if short in seen:
            return seen[short], url
        seen[short] = url
    raise AssertionError("no collision in 100,000 urls, which cannot happen")


def test_the_long_key_separates_two_jobs_the_short_id_cannot(corpus):
    one, two = _colliding_urls()
    assert (hashlib.sha256(one.encode()).hexdigest()[:4]
            == hashlib.sha256(two.encode()).hexdigest()[:4])
    assert keys.identity_key(one) != keys.identity_key(two)
    corpus.add(one, "Software Engineer I", "Acme")
    corpus.add(two, "Software Engineer II", "Acme")
    corpus.build()
    found = identity.collisions()
    assert len(found) == 1
    assert len(next(iter(found.values()))) == 2
    # And each job still answers for itself, which is what the short id
    # stopped doing when two of them collided.
    assert identity.lookup(one).best.url == one
    assert identity.lookup(two).best.url == two


def test_the_long_key_is_stable_across_the_links_one_job_arrives_as():
    assert keys.identity_key(VAGARO_TAGGED) == keys.identity_key(BREEZY_FORM)


# --- what it must never do -------------------------------------------------

def test_a_lookup_writes_nothing_to_the_queue_or_the_applications(corpus):
    corpus.add(GREENHOUSE_EMBED, "Software Engineer", "Together AI",
               location="San Francisco, CA", text=posting("inference"))
    corpus.build()
    before = {path: path.stat().st_mtime_ns
              for path in [sources.queue_path(), *sources.applications_path().rglob("*")]}
    identity.lookup(GREENHOUSE_EMBED)
    identity.backfill()
    after = {path: path.stat().st_mtime_ns for path in before}
    assert before == after


def test_the_index_lives_beside_the_data_and_the_path_is_not_frozen(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOPILOT_IDENTITY_DB", str(tmp_path / "elsewhere.sqlite3"))
    assert store.db_path() == tmp_path / "elsewhere.sqlite3"
    monkeypatch.delenv("AUTOPILOT_IDENTITY_DB")
    import paths
    assert store.db_path() == paths.DATA / "identity.sqlite3"


def test_every_match_names_the_rule_and_what_it_compared(corpus):
    corpus.add(GREENHOUSE_EMBED, "Software Engineer", "Together AI",
               location="San Francisco, CA")
    corpus.build()
    answer = identity.lookup(GREENHOUSE_BOARD, title="Software Engineer",
                             company="Together AI", location="San Francisco, CA")
    assert answer.matches
    for match in answer.matches:
        assert match.rule
        assert match.evidence
        for item in match.evidence:
            assert item.compared


# --- the router ------------------------------------------------------------

def test_the_router_answers_a_lookup(corpus):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from identity.api import router

    corpus.add(GREENHOUSE_EMBED, "Software Engineer", "Together AI")
    corpus.build()
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    answer = client.post("/identity/lookup", json={"url": GREENHOUSE_BOARD}).json()
    assert answer["verdict"] == "block"
    assert answer["matches"][0]["rule"] == "same job at the same board"
    assert client.get("/identity/stats").json()["jobs"] == 1
    assert client.get("/identity/collisions").json()["count"] == 0


def test_the_cli_exits_two_on_a_block(corpus, capsys):
    from identity.__main__ import main

    corpus.add(GREENHOUSE_EMBED, "Software Engineer", "Together AI")
    corpus.build()
    assert main(["check", GREENHOUSE_BOARD]) == 2
    assert main(["check", LINKEDIN_VIEW]) == 0
    assert "BLOCK" in capsys.readouterr().out


# ------------------------------------------------- wired into the app (2026-10-14)
#
# Three things found while pointing `server/seen.py` and `queue.add` at this
# service. Each is a real regression that a green suite did not catch until
# the whole app ran on it.


@pytest.mark.parametrize("url", [
    # `^utm_` alone kept a bare `?utm=x`, and the deny-list keeps what it
    # does not recognise - so one job became two keys and `/autofilled`
    # stopped recognising a job it had just filled.
    "https://example.com/jobs/1?utm=x",
    "https://example.com/jobs/1?utm_source=jobright&utm_medium=email",
    "https://example.com/jobs/1?gclid=abc123",
    "https://example.com/jobs/1?fbclid=1&msclkid=2&li_fat_id=3",
    "https://example.com/jobs/1?_ga=2.1.1&mc_cid=9",
    "https://www.example.com/jobs/1/",
])
def test_a_click_id_is_never_part_of_a_job(url):
    assert keys.canonical(url) == keys.canonical("https://example.com/jobs/1")


def test_a_job_id_that_looks_like_tracking_is_still_kept():
    """The other direction, which is the dangerous one: a dropped job
    parameter is a missed match."""
    embed = keys.canonical(
        "https://boards.greenhouse.io/embed/job_app?for=acme&token=7&utm=x")
    assert "for=acme" in embed and "token=7" in embed
    # ...except where that name is a session, which is host-specific.
    assert "token" not in keys.canonical("https://x.ripplehire.com/p?token=s&jobid=8")
    assert "jobid=8" in keys.canonical("https://x.ripplehire.com/p?token=s&jobid=8")


def test_the_index_follows_the_queue_the_app_is_actually_using(tmp_path, monkeypatch):
    """Invariant 8 forbids importing the module that writes the queue, so
    the location comes through a registered reader. Without it the index
    reads the default path and answers CLEAR about a job that is right
    there - the one answer that costs a second application."""
    from identity import sources

    moved = tmp_path / "elsewhere" / "queue.json"
    moved.parent.mkdir(parents=True)
    moved.write_text("[]")
    monkeypatch.delenv("AUTOPILOT_QUEUE", raising=False)
    monkeypatch.setitem(sources.PATH_HOOKS, "queue", lambda: moved)
    assert sources.queue_path() == moved

    # An explicit override still wins, because that is what it is for.
    monkeypatch.setenv("AUTOPILOT_QUEUE", str(tmp_path / "named.json"))
    assert sources.queue_path() == tmp_path / "named.json"

    # And a reader that raises is not fatal: the default answers.
    monkeypatch.delenv("AUTOPILOT_QUEUE")
    monkeypatch.setitem(sources.PATH_HOOKS, "queue",
                        lambda: (_ for _ in ()).throw(RuntimeError("no queue here")))
    assert sources.queue_path().name == "queue.json"


def test_importing_seen_registers_the_readers():
    from identity import sources
    from server import queue, seen                   # noqa: F401 - the import is the point

    assert sources.PATH_HOOKS["queue"]() == queue.QUEUE_PATH
