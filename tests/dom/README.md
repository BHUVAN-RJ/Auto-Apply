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
- `workday_autofill_caption.html` - a panel whose own heading is Workday's
  "Autofill with Resume" offer, with the real Resume/CV dropzone inside it.
  `slotWords` climbs until an ancestor's text mentions a resume, so on this
  shape it hands back the caption as the slot's name and the put chip read
  the system's own parser hint on the one control that is not one. The
  dropzone's markup is `workday_two_resumes_live.html`'s; the heading above
  it is **constructed**, because no capture of that page is on disk. It is
  always asserted together with `ashby_resume_taken.html`, whose parser
  really does describe itself in the block its input sits in: a rescue that
  cannot still refuse that one is a hole, not a fix.
- `jobright_apply_row.html` - the shape of Jobright's own button row, with
  the `min-width: 1200px` they hard-code on the main content, so that the
  **Add to autopilot** button inserted beside their Apply can be measured
  against their row's own box rather than assumed to fit. Their class names
  are hashed and rotate, so only the part before the hash is kept - which is
  what `content.js` matches on. Constructed from the rules in the declutter
  section rather than captured.
