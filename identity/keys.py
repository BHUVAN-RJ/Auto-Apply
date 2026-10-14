"""Every key a posting can be recognised by, derived in string work alone.

No model, no network, no randomness: the same URL and the same title always
produce the same keys, so a verdict built from them can be audited by
reading the two values the rule compared.

Four kinds of key, in the order the ladder asks for them:

1. ``canonical(url)`` - the posting's page with the visitor stripped out of
   it. Tracking and session parameters go; the parameters that *are* the
   job stay, which is a rule the Greenhouse embed taught this project the
   hard way (`/embed/job_app` is one path for every job, and the job is
   `token`).
2. ``ats_identity(url)`` - ``(board, org, job)`` where a board publishes an
   id of its own. Three Greenhouse URL shapes for one job answer the same
   triple, which is what makes a posting found twice through two different
   links one posting.
3. ``normal_triple`` - ``(company, title, location)`` each normalised.
4. ``title_similarity`` / simhash - resemblance, for when nothing above
   lines up. Deterministic, and fenced by ``conflicting()`` so two levels
   of one role at one employer can never be called the same job.

``identity_key(url)`` is the long, stable primary key this package uses in
place of the four hex characters of `server.models.url_hash` - see
``identity/README`` in the module docstring of ``identity/__init__.py``.
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional
from urllib.parse import parse_qsl, urlsplit

from tailor import boards, quality

# A posting whose text is worth comparing at all. Measured over the 301
# jobs on disk: 27 captures are a sign-in wall, a bare form or an expired
# session (`quality.too_thin` names each one), and 12 are not one posting
# but a whole page - a Lever board read end to end (Palantir, 10,702
# cleaned words), an Eightfold tenant's theme JSON (Microsoft, 49,425).
# Both kinds hash nearly identically to each other, so two unrelated jobs
# at one employer came out 0 bits apart. The 95th percentile of a real
# posting is 2,076 words, so 2,500 is above every description in the
# corpus and below every page that is not one.
MAX_POSTING_WORDS = 2500


def usable_posting(text: str) -> bool:
    """Is this text a job description, rather than a form or a whole page?

    The simhash rule is only ever asked about text that passes this, on
    both sides.
    """
    if not text:
        return False
    cleaned = quality.clean(text)
    if len(cleaned.split()) > MAX_POSTING_WORDS:
        return False
    return quality.too_thin(text) is None

# How much of the URL's digest the long key keeps. Sixteen hex characters
# is 64 bits: at the corpus size that made four hex characters collide
# (241 rows, ~43% chance of a collision) the chance of a 64-bit collision
# is about 3e-16, and it stays negligible for any number of jobs one person
# will ever apply to.
KEY_LENGTH = 16

# Parameters that describe the visitor, the referral or the session rather
# than the job. Compared lowercased. Anything not named here is kept,
# because a dropped job parameter is a missed match and a missed match is
# the failure this package exists to prevent: `seen.KEEP_PARAMS` is an
# allow-list and it loses Eightfold's `pid`, which is the only thing
# distinguishing two jobs at one Eightfold tenant.
TRACKING_PARAMS = {
    "jr_id", "gh_src", "ref", "referrer", "referer", "source", "sourceid",
    "src", "trk", "trackingid", "refid", "lever-origin", "lever-source",
    "codes", "dcr_ci", "iis", "iisn", "campaign", "cid", "mkt_tok",
    # Window geometry and chrome that iCIMS and friends put on a link.
    "mobile", "width", "height", "bga", "needsredirect", "from", "csrf",
    "hashed", "ssbgcolor", "searchscreenid", "countryid", "language",
    "lang", "locale", "rcm_site_locale", "tz", "tzname", "navbarlevel",
    "sort_by", "start", "filter_job_type", "domain", "feed", "fee",
    "_s.crb", "b_id", "fid", "adid", "a_id", "as_id", "msg", "s", "s_id",
    "l_id", "lo_id", "searchid", "ss", "team",
}

# Parameters that are a session on one host and a job id on another.
# `token` is Greenhouse's job and RippleHire's session.
HOST_TRACKING = {
    "ripplehire.com": {"token"},
}

# Analytics tags, by prefix and by name. `^utm_` alone missed a bare
# `?utm=x`, which is exactly the shape that splits one job into two rows:
# the deny-list keeps whatever it does not recognise, so an unlisted
# tracking parameter is a missed match (`/autofilled` stopped recognising
# a job it had just filled). Prefix-matching the family is the fix; a
# click id is never a job id.
_UTM = re.compile(r"^(?:utm\b|utm_|_hs|hs_|pk_|mtm_|at_)")
CLICK_IDS = {
    "utm", "gclid", "gbraid", "wbraid", "dclid", "fbclid", "msclkid",
    "yclid", "ttclid", "twclid", "igshid", "li_fat_id", "mc_cid", "mc_eid",
    "_ga", "_gl", "vero_id", "oly_enc_id", "oly_anon_id", "epik", "s_kwcid",
}

# Path tails that mean "the form for this job" rather than another job.
# Stripping them collapses the application URL Jobright's Apply lands on
# with the overview URL the same job is listed under.
FORM_TAILS = {"apply", "application", "applications", "apply-now", "candidate"}

# A fragment is normally decoration, and on four of the systems in the
# corpus (JobDiva, RippleHire, HealthcareSource, Bullhorn) it is the only
# place the job id appears.
_FRAGMENT_JOB = re.compile(r"job", re.I)

_UUID = r"[0-9a-fA-F-]{36}"

# One id per applicant-tracking system, wherever that system is hosted.
# `boards.identify` already covers Greenhouse, Lever, Ashby and Workday in
# both their posting and their application-form shapes, and it is asked
# first; these are the systems it does not know. The org is included so an
# id that is only unique within a tenant cannot collide across tenants.
_SUBDOMAIN_BOARDS = ("icims", "eightfold", "avature", "breezy", "workable",
                     "recruitee", "pinpointhq", "ripplehire", "applytojob")


def _subdomain_org(host: str, board: str) -> str:
    """`employees-hrblock.icims.com` is H&R Block's own iCIMS tenant."""
    head = host.split(".")[0]
    return "" if head in {"www", "jobs", "apply", "careers", board} else head


def _query(url: str) -> dict[str, str]:
    """The query as a lowercase-keyed mapping, first value per key."""
    found: dict[str, str] = {}
    for name, value in parse_qsl(urlsplit(url).query, keep_blank_values=False):
        found.setdefault(name.lower(), value)
    return found


def ats_identity(url: str) -> Optional[tuple[str, str, str]]:
    """``(board, org, job)`` for a URL on a board that publishes an id.

    Two URLs answering the same triple are the same posting, whichever
    shape of link they arrived as.
    """
    if not url:
        return None
    board = boards.identify(url)
    if board:
        return (board.system, board.org.lower(), board.job)

    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = parts.path
    query = _query(url)

    if "linkedin.com" in host:
        # The guest endpoint and the signed-in page name the job the same
        # way; `currentJobId` is the one a search result carries.
        found = re.search(r"/jobs/view/(\d+)", path)
        job = found.group(1) if found else query.get("currentjobid", "")
        return ("linkedin", "", job) if job else None

    if "simplify.jobs" in host:
        found = re.match(rf"^/p/({_UUID})", path)
        return ("simplify", "", found.group(1).lower()) if found else None

    if "smartrecruiters.com" in host:
        found = re.search(rf"/company/([^/]+)/publication/({_UUID})", path)
        if found:
            return ("smartrecruiters", found.group(1).lower(), found.group(2).lower())
        found = re.match(r"^/([^/]+)/(\d{6,})", path)
        return ("smartrecruiters", found.group(1).lower(), found.group(2)) if found else None

    if "jobvite.com" in host:
        found = re.match(r"^/([^/]+)/job/([A-Za-z0-9]+)", path)
        return ("jobvite", found.group(1).lower(), found.group(2)) if found else None

    if "app.dover.com" in host:
        found = re.match(rf"^/apply/([^/]+)/({_UUID})", path)
        return ("dover", found.group(1).lower(), found.group(2).lower()) if found else None

    if "taleo.net" in host or "brassring.com" in host:
        job = query.get("job") or query.get("jobid")
        board = "taleo" if "taleo.net" in host else "brassring"
        return (board, host, job) if job else None

    if "successfactors.com" in host or "sapsf.com" in host:
        job = query.get("career_job_req_id") or query.get("jobreqid")
        return ("successfactors", host, job) if job else None

    if "oraclecloud.com" in host:
        found = re.search(r"/job/(\d+)", path)
        return ("oracle", host, found.group(1)) if found else None

    for board in _SUBDOMAIN_BOARDS:
        if f"{board}." not in host and not host.endswith(f".{board}.com"):
            continue
        org = _subdomain_org(host, board)
        if board == "icims":
            found = re.search(r"/jobs/(\d+)", path)
            return ("icims", org, found.group(1)) if found else None
        if board == "eightfold":
            job = query.get("pid") or ""
            found = re.search(r"/careers/job/(\d+)", path)
            job = found.group(1) if found else job
            return ("eightfold", org, job) if job else None
        if board == "avature":
            found = re.search(r"/JobDetail(?:/[^/]+)?/(\d+)", path)
            return ("avature", org, found.group(1)) if found else None
        if board == "workable":
            found = re.search(r"/([^/]+)/j/([A-Za-z0-9]+)", path)
            if found:
                return ("workable", found.group(1).lower(), found.group(2).upper())
            found = re.search(r"/j/([A-Za-z0-9]+)", path)
            return ("workable", org, found.group(1).upper()) if found else None
        if board == "breezy":
            found = re.search(r"/p/([0-9a-f]+)", path)
            return ("breezy", org, found.group(1)) if found else None
        if board == "pinpointhq":
            found = re.search(rf"/postings/({_UUID})", path)
            return ("pinpoint", org, found.group(1).lower()) if found else None
        if board == "recruitee":
            found = re.search(r"/o/([^/?#]+)", path)
            return ("recruitee", org, found.group(1).lower()) if found else None
        if board == "applytojob":
            found = re.search(r"/apply/([A-Za-z0-9]+)", path)
            return ("jazzhr", org, found.group(1)) if found else None
        if board == "ripplehire":
            found = re.search(r"job/(\d+)", parts.fragment or "")
            return ("ripplehire", org, found.group(1)) if found else None
    return None


def jobright_id(url: str) -> str:
    """Jobright's own posting id, from its page or from the `jr_id` tag it
    puts on every link its Apply opens."""
    found = re.search(r"jobright\.ai/jobs/info/([A-Za-z0-9_-]+)", url or "")
    if found:
        return found.group(1)
    return _query(url or "").get("jr_id", "")


def canonical(url: str) -> str:
    """The posting's address with the visitor taken out of it.

    Lowercase host without `www.`, no tracking or session parameters, no
    trailing slash, and no `/apply`-style tail, so the form and the
    overview of one job collapse together. The path keeps its case: a
    Workable code and a Gem id are case-sensitive.
    """
    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = re.sub(r"/+$", "", parts.path)
    for _ in range(2):
        head, _, tail = path.rpartition("/")
        if tail.lower() in FORM_TAILS:
            path = head
        else:
            break
    dropped = set(TRACKING_PARAMS) | CLICK_IDS
    for suffix, extra in HOST_TRACKING.items():
        if host.endswith(suffix):
            dropped |= extra
    kept = sorted(
        (name.lower(), value)
        for name, value in parse_qsl(parts.query, keep_blank_values=False)
        if name.lower() not in dropped and not _UTM.match(name.lower())
    )
    seen: set[str] = set()
    tail = "&".join(f"{n}={v}" for n, v in kept if not (n in seen or seen.add(n)))
    fragment = parts.fragment or ""
    anchor = ""
    if _FRAGMENT_JOB.search(fragment) and re.search(r"\d", fragment):
        anchor = "#" + fragment.strip("/").lower()
    return f"{host}{path or '/'}" + (f"?{tail}" if tail else "") + anchor


def identity_key(url: str) -> str:
    """The long, stable id of a posting: a digest of its canonical URL.

    `server.models.url_hash` keeps four hex characters of the raw URL,
    which is 65,536 values and collided at 270 rows (KNOWN_ISSUES #1). This
    is the same idea with enough digits to be an identity rather than a
    label, and it is taken over the *canonical* URL, so the same job
    reached through a tracking link answers the same key.
    """
    return hashlib.sha256(canonical(url).encode()).hexdigest()[:KEY_LENGTH]


# --- the words that are not the job ----------------------------------------

# Greenhouse titles every form "Job Application for <role> at <employer>".
_FORM_TITLE = re.compile(r"^\s*job application for\s+(?P<role>.+?)\s+at\s+(?P<org>[^|]+)$", re.I)

# Sites that put their own name where the employer's would go.
_SITE = re.compile(r"jobright|linkedin|indeed|glassdoor|ziprecruiter|simplify|"
                   r"job recommendations|careers?|jobs", re.I)

COMPANY_NOISE = re.compile(
    r"\b(inc|incorporated|llc|l\.l\.c|ltd|limited|plc|gmbh|corp|corporation|"
    r"co|company|holdings|technologies|labs|careers|jobs|the)\b\.?", re.I)

# Words a posting's title carries that are not the role. Deliberately not
# in here: a level number, `intern`, `co-op`, `backend`, `frontend` - the
# words that tell two openings at one employer apart.
TITLE_NOISE = re.compile(
    r"\b(remote|hybrid|onsite|on-site|all levels|new grad|new grads|"
    r"new college grad|new college graduate|university grad|university graduate|"
    r"college grad|early career|entry level|entry-level|graduate level|"
    r"us|usa|united states|full[- ]?time|part[- ]?time|immediate start|"
    r"start|open to remote|fall|spring|summer|winter)\b", re.I)

# Requisition numbers, which are the same job under two spellings as often
# as they are two jobs.
REQ_NUMBER = re.compile(r"\b(?:jr|req|r)[-_]?\d{3,}\b|\b\d{5,}\b", re.I)

YEAR = re.compile(r"\b20\d\d\b")

ROMAN = {"I": "1", "II": "2", "III": "3", "IV": "4", "V": "5"}
_ROMAN_TOKEN = re.compile(r"\b(I{1,3}|IV|V)\b")

LEVEL_TOKEN = re.compile(r"^[1-5]$")

# Words that make two openings at one employer two jobs, whatever the rest
# of the title says.
KINDS = {
    "intern": ("intern", "internship"),
    "coop": ("co-op", "coop"),
    "contract": ("contract", "contractor"),
}


def strip_site(title: str, company: str = "") -> str:
    """"<role> | <employer>", "<role> @ <employer>", "Job Application for
    <role> at <employer>" - the role without whoever is hosting the page.

    Every part that names a site or the employer goes, not only the last
    one: "Jobs | Careers | McKinsey & Company" is three of them and no
    role at all, and a title that normalises to nothing is better than a
    title that normalises to "jobs careers".
    """
    text = (title or "").strip()
    form = _FORM_TITLE.match(text)
    if form:
        text = form.group("role")
    parts = re.split(r"\s+[|@]\s+|\s+[–—]\s+", text)
    if len(parts) >= 2:
        # With no company given, the title's own shape names one: a page
        # titled "Jobs | Careers | <employer>" has no role in it at all,
        # and "mckinsey company" is a worse answer than nothing.
        mine = norm_company(company) or norm_company(company_from_title(title))
        kept = [part for part in parts
                if not _SITE.search(part)
                and not (mine and norm_company(part) == mine)]
        text = " ".join(kept) if kept else ""
    return text.strip()


# A company name that is really the page, the vendor or nothing. These
# arrive from a page title or a posting header - "oneclick ui" is six rows
# in the corpus and is SmartRecruiters' own URL slug standing in for six
# different employers, which would bucket them together for levels 3 and 4.
UNUSABLE_COMPANY = {
    "", "careers", "career", "jobs", "job", "job search", "job searches",
    "job detail", "job details", "job application", "job recommendations",
    "job board", "oneclick ui", "candidate portal", "candidate", "apply",
    "application", "home", "login", "sign in", "search", "unknown",
    "unknown company", "n a", "none", "null", "portal", "workday",
    "greenhouse", "lever", "ashby", "smartrecruiters", "icims", "taleo",
    "successfactors", "myworkdayjobs", "eightfold", "jobvite", "workable",
    "linkedin", "indeed", "glassdoor", "ziprecruiter", "jobright", "simplify",
}


def norm_company(company: str) -> str:
    """Lowercase, no punctuation, no legal suffix, "" for a name that is
    not an employer.

    A company called nothing but a legal suffix keeps its own spelling; a
    company that is the job board's URL slug or the word "Careers" is not
    a company, and answering "" keeps it out of the two rules that work by
    employer.
    """
    text = re.sub(r"^\s*\d{3,}\s+", "", company or "")  # "0001 Applied Materials"
    stripped = COMPANY_NOISE.sub(" ", text)
    stripped = " ".join(re.sub(r"[^a-z0-9 ]", " ", stripped.lower()).split())
    if not stripped:
        stripped = " ".join(re.sub(r"[^a-z0-9 ]", " ", (company or "").lower()).split())
    return "" if stripped in UNUSABLE_COMPANY else stripped


def norm_title(title: str, company: str = "") -> str:
    """The role, lowercased, with the noise a job board adds taken out.

    A roman numeral becomes its digit, so Google's `III` survives as `3`
    and is still there to tell level 3 from level 2 - the thing a
    normaliser that deletes it can no longer do.

    What is in brackets is *kept*, with the noise words taken out of it by
    the same rule as the rest: deleting the brackets wholesale was
    measured on this corpus and it merged "Software Engineer, (Internal
    Audit) [All Levels]" with "Software Engineer (All Levels)", which are
    two different openings at one employer - the false positive that
    matters most here.
    """
    text = strip_site(title, company)
    text = _ROMAN_TOKEN.sub(lambda m: ROMAN[m.group(1)], text)
    text = REQ_NUMBER.sub(" ", text)
    text = YEAR.sub(" ", text)
    text = TITLE_NOISE.sub(" ", text)
    text = re.sub(r"[^a-z0-9+#]+", " ", text.lower())
    words = [w for w in text.split() if w not in {"and", "or", "a", "the", "of", "at", "in"}]
    return " ".join(words)


def title_tokens(title: str, company: str = "") -> frozenset[str]:
    return frozenset(norm_title(title, company).split())


def title_similarity(a: str, b: str, company_a: str = "", company_b: str = "") -> float:
    """Token-set Jaccard of two normalised titles, and nothing else.

    Deliberately not a character ratio: "Software Engineer I" and "Software
    Engineer II" are 0.95 alike as strings and are two different jobs.
    """
    ta, tb = title_tokens(a, company_a), title_tokens(b, company_b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def levels(title: str, company: str = "") -> frozenset[str]:
    return frozenset(t for t in title_tokens(title, company) if LEVEL_TOKEN.match(t))


def kinds(title: str) -> frozenset[str]:
    text = (title or "").lower()
    return frozenset(name for name, words in KINDS.items()
                     if any(word in text for word in words))


def conflicting(a: str, b: str, company_a: str = "", company_b: str = "") -> bool:
    """True when two titles name two openings whatever their similarity.

    Different level numbers (`Engineer I` against `Engineer II`, and
    `Engineer` against `Engineer I`: Applied Materials has two openings
    that differ by exactly that and share an identical 578-word
    description), or one an internship or a contract and the other not.

    This is the fence around the fuzzy levels of the ladder: the dangerous
    false positive is two real roles at one employer, and these are the
    two forms of it that are decidable in code. It costs recall - a job
    captured once with its level and once without is no longer matched by
    resemblance - and that is the right way round, because the same
    posting twice is nearly always caught by its URL or its board id.
    """
    if levels(a, company_a) != levels(b, company_b):
        return True
    return kinds(a) != kinds(b)


_STATES = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
    "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn",
    "mississippi": "ms", "missouri": "mo", "montana": "mt", "nebraska": "ne",
    "nevada": "nv", "new hampshire": "nh", "new jersey": "nj",
    "new mexico": "nm", "new york": "ny", "north carolina": "nc",
    "north dakota": "nd", "ohio": "oh", "oklahoma": "ok", "oregon": "or",
    "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc",
    "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut",
    "vermont": "vt", "virginia": "va", "washington": "wa",
    "west virginia": "wv", "wisconsin": "wi", "wyoming": "wy",
    "district of columbia": "dc", "washington dc": "dc",
}
_ABBREVIATIONS = set(_STATES.values())
_US = re.compile(r"\b(us|usa|u\.s\.|u\.s\.a\.|united states|america)\b", re.I)
_REMOTE = re.compile(r"\bremote\b|\bwork from home\b|\bhome office\b", re.I)


def norm_location(place: str) -> str:
    """A coarse place: `san-francisco-ca`, `remote-us`, `remote`, or "".

    Coarse on purpose. Two captures of one posting write the place three
    ways ("San Francisco, CA", "US-CA-San Francisco", "San Francisco,
    California, United States"), and the only question asked of it is
    whether a company-and-title match is the same opening or the same
    opening in another city.
    """
    text = (place or "").replace("_", " ")
    # "San Francisco, CA or Remote (USA)": the connector has to go, or the
    # state reads as "ca or" and the place loses its state.
    text = re.sub(r"[/|]|\b(?:or|and)\b", ",", text, flags=re.I)
    remote = bool(_REMOTE.search(text))
    text = _REMOTE.sub(" ", text)
    pieces = [p.strip() for p in re.split(r"[,\-]| {2,}", text) if p.strip()]
    state = ""
    city = ""
    for piece in pieces:
        low = " ".join(re.sub(r"[^a-z ]", " ", piece.lower()).split())
        if not low or _US.fullmatch(low or " "):
            continue
        if low in _STATES:
            state = _STATES[low]
        elif len(low) == 2 and low in _ABBREVIATIONS:
            state = low
        elif not city and not re.fullmatch(r"[a-z]{1,2}", low):
            city = low.replace(" ", "-")
    if remote and not city:
        if state:
            return f"remote-{state}"
        return "remote-us" if _US.search(place or "") else "remote"
    if city and state:
        return f"{city}-{state}"
    return city or state


def company_from_title(title: str) -> str:
    """The employer out of a page title, for the rows that carry no company.

    "Job Application for <role> at <employer>" first, since Greenhouse
    writes it on every form, then "<role> @ <employer>" and
    "<role> | <employer>".
    """
    form = _FORM_TITLE.match(title or "")
    if form:
        return form.group("org").strip()
    found = re.search(r"\s@\s(.+?)(?:\s[|–—-]\s|$)", title or "")
    if found:
        return found.group(1).strip()
    parts = re.split(r"\s[|–—]\s", title or "")
    if len(parts) >= 2:
        tail = parts[-1].strip()
        if not _SITE.search(tail):
            return tail
    return ""
