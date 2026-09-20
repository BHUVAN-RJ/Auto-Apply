# Per-system notes for the form fill

Sent to the browser model verbatim, one section, chosen by the URL
(`browser/ats.py: detect`). Each section is what a person who had filled a
few of that system's forms would tell the next person: where things are,
what the autofill gets wrong, which button moves on and which one files
the application. Edit this, not the Python. Nothing here relaxes the
absolute rules in the task: submit stays refused, visa questions stay
untouched.

## oracle

Oracle HCM (`oraclecloud.com/hcmUI/CandidateExperience`). One long page of
numbered sections that open one at a time.

- "Continue" at the foot of a section opens the next one. It files nothing
  and is allowed. "Submit" at the very end is the application and is
  refused.
- The Jobright panel on this system often stops around 85% and never
  reports done. Two checks with the same percentage means it is finished:
  move on.
- Documents live in one section, "Additional Documents", with a single
  "Upload Attachment" control used once per file. Upload the tailored
  resume through it, then the cover letter through the same control. Each
  upload shows a row with the file name; remove Jobright's generic resume
  row with its own delete control. Check both names are listed before
  moving on.
- The Address section comes prefilled with the wrong country (it has said
  India). Set Country first, wait for the section to re-render, then State
  (Region), then City. The postal code field may clear when the country
  changes; re-enter it.
- Phone: the country code is its own dropdown, +1, before the number.
- Experience and Education sections are filled by the autofill; only touch
  a field that is empty and required.
- The Questions section is yes/no radios and short dropdowns. Answer from
  the applicant details; the visa and sponsorship ones stay as found.

## greenhouse

Greenhouse (`greenhouse.io`, `job-boards.greenhouse.io`, or embedded on a
careers page with `gh_jid` in the URL). One page, one form.

- Resume and cover letter each have their own "Attach" control and their
  own file input, named `resume` and `cover_letter`. The resume goes only
  on the first, the cover letter only on the second; the wrong one is
  refused. Ignore "Enter manually" and "Dropbox / Google Drive".
- Location is an autocomplete: type the city, wait for the list, pick the
  entry that reads "City, State, Country". Typing without picking leaves
  it invalid.
- LinkedIn, website, and "How did you hear about us" come from the
  applicant details.
- The demographic / EEO block at the bottom (gender, race, veteran,
  disability) is optional; leave it as found.
- "Submit application" is the application and is refused.

## ashby

Ashby (`jobs.ashbyhq.com`). One page; the form is under the "Application"
tab.

- Each file field has its own labelled input. The autofill attaches its
  generic resume; remove it with the field's X, then upload the tailored
  one to that same field. The cover letter has its own field when the
  posting asks for one.
- Location is a free-text autocomplete; pick the suggestion after typing.
- "Submit Application" is the application and is refused.

## workday

Workday (`myworkdayjobs.com`). Several pages; each "Save and Continue" is
allowed and moves to the next page. "Submit" on the review page is the
application and is refused.

- The first page is often a sign-in or "Create Account" wall. Do not
  create an account or sign in. If the form cannot be reached without
  one, call done with `left blank: login required` and nothing else.
- "My Information" then "My Experience": the resume upload is on "My
  Experience" ("Select files" or "Upload"). Workday parses it and may
  overwrite fields it filled a moment before; check the ones it changed.
- Workday's own "Autofill with Resume" is fine to use.
- Country and phone code are dropdowns; the phone type is "Mobile".
- The "Application Questions" and "Voluntary Disclosures" pages: answer
  what the applicant details cover; the disclosures are optional and stay
  as found. Visa and sponsorship questions stay as found.

## lever

Lever (`jobs.lever.co`). One page.

- One resume upload ("Attach resume"); the cover letter is a text box, so
  leave it empty. Location is free text: the applicant's city and state.
- "Submit application" is the application and is refused.
