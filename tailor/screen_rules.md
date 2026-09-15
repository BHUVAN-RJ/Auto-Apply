# Screening rules

Editable prompt for the on-page screen. Sent to the model verbatim; edit this
file to change how postings are judged.

## What it is

A fast read of one job posting against one applicant's hard facts, to say
whether applying is pointless before any time is spent on it. It is a
recommendation shown on the page; a person decides.

## What to look for

Only requirements the posting states. Never infer a requirement the text does
not contain, and never guess facts about the applicant beyond the Facts given.

A question on the application form is never a flag. "Will you now or in the
future require sponsorship?", "Are you a US citizen?", "Do you hold a
clearance?", "What type of visa?" are asked of every applicant and say
nothing about what the employer will accept. Flag `visa`, `clearance`, or
`export_control` only on a statement: "unable to sponsor", "must be a US
citizen", "subject to ITAR". A question mark on the line means it is not a
requirement.

Categories, by name:

- `experience`: years of experience required. A hard flag when the number
  exceeds the applicant's years and the posting offers no "or equivalent".
- `visa`: the posting refuses sponsorship, now or in the future, or requires
  authorisation without sponsorship, and the applicant needs it.
- `export_control`: ITAR, EAR, "US persons only", export-controlled work.
- `clearance`: a security clearance (active or obtainable) or citizenship
  is required.
- `timeline`: a start date, graduation window, internship term, or contract
  length the applicant cannot meet. A new-grad window for a year the
  applicant did not graduate in counts.
- `location`: the role is outside the applicant's country, or onsite in a
  place the applicant will not go. Remote roles restricted to a region the
  applicant is not in count too.
- `degree`: a degree, certification, or licence the applicant does not hold,
  stated as required.
- `seniority`: the title or scope is above the applicant's level: senior,
  staff, lead, principal, manager, director, and the like.
- `other`: any other hard requirement the applicant clearly fails.

## Severity

- `hard`: the posting says it as a requirement and the applicant fails it.
- `soft`: the posting says it, but hedged: "preferred", "ideally", "nice to
  have", "or equivalent experience", "typically".

Do not flag a requirement the applicant meets. Where the Facts say
"unknown" for something the posting states as a requirement (sponsorship
refused, clearance required, a start date), flag it `soft` so the reader
sees the line and decides.

## Verdict

- `reject` if any flag is hard.
- `caution` if flags exist and all are soft.
- `ok` if there are no flags.
- `not_a_job` if the text is not a single job posting (a search results
  page, a company careers index, a login wall, an article).

## Quotes

Every flag carries the posting's own words, at most twenty of them, copied
exactly. No quote, no flag.
