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
