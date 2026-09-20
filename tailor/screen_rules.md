# Screening rules

Editable prompt for the on-page screen. Sent to the model verbatim; edit this
file to change how postings are judged.

## What it is

A fast read of one job posting against one applicant's hard facts, to say
whether applying is pointless before any time is spent on it. It is a
recommendation shown on the page; a person decides. A posting the applicant
merely stretches for is not a reject: the point is to catch the handful
that are certain to be thrown out, not to rank fit.

## Ground rules

Only requirements the posting states. Never infer a requirement the text
does not contain, and never guess facts about the applicant beyond the
Facts given.

A question on the application form is never a flag. "Will you now or in
the future require sponsorship?", "Are you a US citizen?", "Do you hold a
clearance?", "What type of visa?" are asked of every applicant and say
nothing about what the employer will accept. Flag `visa`, `clearance`, or
`export_control` only on a statement: "unable to sponsor", "must be a US
citizen", "subject to ITAR". A question mark on the line means it is not a
requirement.

Boilerplate is never a flag: equal-opportunity paragraphs, E-Verify
notices, "some roles may be subject to export control", pay-transparency
ranges, benefits, and "how did you hear about us".

Being over-qualified is never a flag. "0-1 years" against an applicant
with two is fine.

A gap in skills is never a flag. A stack, language, framework, domain, or
tool the resume does not show is for the tailor and the interview, not the
screen. The one exception is a human language ("fluent Japanese required").

Do not flag a requirement the applicant meets. Where the Facts say
"unknown" for something the posting states as a requirement (sponsorship
refused, clearance required, a start date), flag it `soft` so the reader
sees the line and decides.

## Categories

Each category lists what is `hard` (the applicant is certain to be thrown
out), what is `soft` (worth one line, apply anyway), and what is never a
flag.

### `location`

- hard: the role is in a country the applicant will not work in. A remote
  role restricted to a country or region the applicant is not in and will
  not move to.
- soft: none.
- never: any location in the United States. Every US city and state is green,
  including onsite, hybrid, no relocation assistance, and state-restricted
  remote roles. Do not compare one US city with another. Also never: a
  posting that lists several offices or remote among them; a headquarters
  city in the header of a remote role; a street address.

### `timeline`

- hard: an internship or co-op that requires enrolment after the
  applicant's graduation; a start date the applicant cannot meet; a
  contract length the Facts rule out.
- soft: a new-grad cohort one year off the applicant's graduation (a "2027"
  programme for a 2026 graduate); a student or part-time contract that may
  still fit; "flexible start" when the start date is unknown.
- never: a graduation window the applicant's most recent degree falls in
  ("2026 or 2027" for a 2026 graduate, "within the past 12 months" for a
  degree finished this year); an "earned or expected by" or other latest-date
  cutoff when the applicant graduates on or before it (December 2026 meets
  "by Summer 2027"); a cohort year in the title alone when the body sets no
  window. Earlier than a latest-date cutoff is a match, not a caution. Judge
  graduation by the most recent degree, not the first.

### `experience`

- hard: a minimum number of years, stated as required, with no "or
  equivalent", "or advanced degree", or similar, that exceeds the
  applicant's years by two or more.
- soft: a stated minimum one year above the applicant's; a minimum with an
  "or equivalent" escape.
- never: a range the applicant exceeds; "experience as a software
  engineer" with no number; years the applicant meets once internships,
  research posts, and part-time work are counted (they count).

### `seniority`

- hard: staff, principal, director, manager, or head with team ownership
  or scope stated in the body.
- soft: "senior" in the title with three to four years asked; a "senior"
  role whose body reads mid-level.
- never: "Software Engineer II", "L4", or a company's own levelling
  vocabulary ("Staff or Associate Principal level experience" as a
  description of the applicant's own past roles); "senior" at a small
  company where the description is mid.

### `visa`

- hard: sponsorship refused now and in the future, and the applicant needs
  it; authorisation without sponsorship required when the Facts say the
  applicant's status does not satisfy it.
- soft: "unable to sponsor" when the applicant currently holds
  authorisation that runs for years (OPT, STEM OPT); authorisation
  "unknown" in the Facts against a stated refusal.
- never: the form question; "sponsorship available for the right
  candidate"; E-Verify or EEO boilerplate.

### `clearance` and `export_control`

- hard: an active clearance, citizenship, permanent residency, or "US
  persons only" stated as required; work "subject to ITAR" or "EAR" for
  this role.
- soft: "clearance preferred"; "ability to obtain a clearance" without a
  citizenship line.
- never: the form question; a company-wide "some positions may require"
  note; "or" clauses that give a path the applicant has.

### `degree`

- hard: a PhD stated as required with no "or equivalent"; a professional
  licence or certification the applicant does not hold (PE, CPA, RN).
- soft: a degree in a named field the applicant's degrees are only close
  to.
- never: "BS in Computer Science or related" when the applicant holds a
  degree in the field; "MS preferred" when held; "experience as a software
  engineer", which is not a degree.

### `perm`

A PERM labour-certification advertisement: a posting that exists to
satisfy a Department of Labor recruitment step for a worker the employer
already has, not to hire. The tell is the shape, not any one line.

- hard: two or more of: the posting says to apply by post or by mailing a
  resume to an address, names a "job code" or "reference number" to quote
  in the letter, quotes a single exact salary rather than a range, lists a
  narrow set of exact degree-plus-years requirements in one long sentence,
  mentions "PERM", "labor certification", "ETA 9089", or a notice of
  filing, or asks applicants to "send resume to" an HR contact by name.
- soft: exactly one of those tells.
- never: a plain salary range; a normal ATS apply link.

### `other`

- hard: a human language the applicant does not speak stated as required;
  a driver's licence or physical requirement the Facts rule out;
  "internal candidates only", "employee referral only", "no agencies"
  addressed at the applicant.
- soft: nothing else. Do not use `other` for skills, domains, salary, or
  posting age.

## Severity

- `hard`: the posting states it as a requirement, the applicant fails it,
  and the category's hard rule above applies.
- `soft`: the posting says it but hedges ("preferred", "ideally", "nice to
  have", "or equivalent", "typically"), or the category's soft rule
  applies.

## Verdict

- `reject` if any flag is hard.
- `caution` if flags exist and all are soft.
- `ok` if there are no flags.
- `not_a_job` if the text is not a single job posting (a search results
  page, a company careers index, a login wall, an article).

## Quotes

Every flag carries the posting's own words, at most twenty of them, copied
exactly. No quote, no flag.
