# Page fixtures

Each file is the shape of a real application form, kept so the scripts that
read those forms (`browser/forms/engine.py`, `putFile` in
`capture/content.js`) can be run against them in a real browser by
`tests/test_dom.py`. The suite stubs the browser everywhere else, and every
bug these guard against survived a green suite:

- `ashby_application.html` - the markup of the two file inputs on
  `jobs.ashbyhq.com/<org>/<id>/application`, copied off the live Deepgram
  form on 2026-10-06, class names and all. The first is Ashby's
  "Autofill from resume" parser and has **no id, no name and no
  aria-label**; the second is the real `_systemfield_resume` slot.
- `ashby_resume_taken.html` - the same page on the second pass: a file is on
  the real slot, Ashby has dropped its input, and the parser is the only
  file input left on the page.
- `workday_resume_slot.html` - a slot that keeps its input **beside** the
  attachment it already holds, which is what Workday does and what the
  remove-the-old-file step was failing on.
- `greenhouse_resume_taken.html` - the other shape: the input is gone once a
  file is on the slot and a "Remove file" button stands in its place.
- `workday_review.html` - the last page of a Workday application, which
  shows the filename and has no input anywhere.
- `workday_two_resumes.html` - the bin icon with no readable label in it,
  at the depth Workday really nests it: the heading that names the slot is
  nine ancestors above the button that clears it.
- `workday_work_experience.html` and `workday_two_resumes_live.html` - two
  pages of one real Workday application (2026-10-11), captured by
  `tools/dev_workday_recorder.py` and scrubbed: no filename, no email, no
  answers. The first is the trap that broke the clear - two bare
  `<button>Delete</button>` controls belonging to Work Experience entries,
  no automation id on either, the Resume/CV heading seventeen thousand
  characters further down and its slot **empty**. The second is the same
  application three steps on, with the same tailored resume on that slot
  **twice**, each row with its own `delete-file` bin. The hand-drawn
  fixtures above were green through the whole of that bug, which is why
  these two are here at full size.
