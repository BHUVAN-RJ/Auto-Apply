# Cover letter rules

Editable prompt rules for the cover letter step. Everything here is sent to
the model verbatim, so changing how the slots read means editing this file,
not the Python.

The letter itself is a standing template. You fill only the named slots.
The greeting, the standing paragraphs, and the sign-off are already written
and are not yours to rewrite.

## What you write

A labeled block for each slot you were given, nothing else. No greeting, no
sign-off, no letter around the slots, no preamble, no commentary.

Each slot must sound like the sentences already in the template: complete,
traditional, first person, a person writing to a hiring team. Not a
telegram, not a list, not a second resume.

## What each slot is for

- OPENING: two sentences about something specific the company is building
  or solving, and why that problem caught the candidate's attention. Name
  the company or a product from the posting. It must be obvious this was
  written for this company. Do not begin with "I am excited to apply" or
  "I am applying for".
- ROLE_SPECIFIC_EVIDENCE: one example from the resume or the profile that
  matches the posting's strongest requirement. Two or three sentences. A
  concrete result if the resume has one. Do not retell the whole resume.
- COMPANY_SPECIFIC_PARAGRAPH: why this company and this problem are a
  natural next step. Refer to a product, team, technology, customer, or
  problem from the posting. No generic statements about innovation,
  mission, or culture.
- SPECIFIC_GOAL_OR_TEAM: a short phrase for the closer, such as a team
  name or a concrete goal from the posting. Not a sentence.

## What it may say

- Only what the resume and profile support. Never invent an employer, a
  project, a metric, a skill, or an enthusiasm the candidate has not shown.
  Every number comes from the resume unchanged.
- Every language, tool, and technology the slots name must appear in the
  resume or the profile. If the posting wants one that is not there, say
  nothing about it.
- Use the posting's own vocabulary where it truthfully describes work the
  candidate did.
- Say nothing about visa, sponsorship, work authorisation, salary, or start
  date. Those are handled elsewhere and are never the letter's business.

## Tone

Write the way a person writes to another person. Direct and concrete,
first person, active voice, short sentences, everyday words.

- No clichés: not "I am excited to apply", "I am applying for",
  "fast-paced environment", "passionate about", "leverage", "align",
  "resonate", "I believe I would be a great fit". No flattery of the
  company beyond one specific reason the work is interesting.
- No dashes of any kind for punctuation: no em dash, no en dash, no
  spaced hyphen. Use a comma, a full stop, or a new sentence instead.
  Hyphens inside words ("end-to-end", "high-throughput") are fine.
- No semicolons, no colons introducing a list, no lists at all.
- At most one number-heavy sentence across all the slots. The resume
  carries the metrics; the letter says why they matter here.
- Read it back as the candidate. Anything they would not say out loud to
  the hiring team is cut.
