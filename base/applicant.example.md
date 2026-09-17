# Applicant

Copy this file to `base/applicant.md` and fill it in. The real file is
gitignored; this repo is public. `browser/fill.py` reads it for form fields
the autofill misses, and `tailor/screen.py` reads the Facts section to judge
whether a posting is an auto-reject.

## Facts

Keep these literal. The screen compares them to the posting's requirements,
so a vague line here means a vague verdict.

- Years of professional experience: 2
- Level: entry level (new grad / junior). Anything titled senior, staff,
  lead, principal, or manager is out of scope.
- Work authorisation: authorised to work in the United States now on OPT;
  will need H-1B sponsorship in the future.
- Security clearance: none, and not eligible (not a US citizen).
- Country: United States only. Roles located outside the US are a reject.
- Location: Austin, TX. Remote or hybrid anywhere in the US is fine.
- Relocation: acceptable anywhere in the US.
- Earliest start date: immediately.
- Degrees held: MS Computer Science (2025), BE Computer Science (2022).
- Graduation: already graduated. New-grad windows for 2026 or 2027
  graduates do not apply.

## Form details

Free-text used by the browser when a form asks for something the autofill
left blank. Plain facts, no prose.

- Phone: +1 555 000 0000
- LinkedIn: https://linkedin.com/in/example
- GitHub: https://github.com/example
- Portfolio: https://example.com
- Pronouns: leave blank
- How did you hear about us: Jobright
