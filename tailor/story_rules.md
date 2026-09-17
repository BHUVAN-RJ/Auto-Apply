# Story documents — what to derive from an interview

Two documents are generated from each experience's main document, the one
written in the candidate's own words. Both are regenerated whenever the main
document changes, so nothing is ever edited in them by hand. Use only what
the main document says. A line marked "not discussed" stays unknown: never
fill it in, never estimate a number, never invent a name.

## Tailor document

For the resume tailor, the cover letter, and form answers. Dense facts, no
prose, no advice. Markdown, in exactly this order:

```
Summary: <one sentence, what was built and for whom, at most twenty-five words>
Stack: <comma-separated technologies, as the candidate named them>
Dates: <when and how long, or "not discussed">
Scale: <users, data, requests, latency, model size; or "not discussed">
Own contribution: <which parts were the candidate's own, one or two lines>
Numbers: <every figure the candidate gave, one per line, with what it measures>
Outcome: <shipped or not, used by whom, what happened after>

## Candidate bullets
- <one resume bullet per notable fact, result first, then the metric, then the method; the candidate's own action as the verb; no bullet without a fact behind it>
```

The Summary and Stack lines are what the index shows the tailor before it
decides which stories to read, so they must stand alone.

## Star document

For the candidate to reread before an interview. Markdown, these sections in
this order:

- **Situation / Task / Action / Result / Reflection** — the story in five
  short paragraphs, first person, in the candidate's own words where the
  main document has them. Action is the longest. Reflection holds what they
  would do differently and what they learned.
- **Numbers to say out loud** — every figure, one per line, with what it
  measures and, where the candidate said it, the before and after.
- **Questions an interviewer will ask** — eight to twelve likely questions
  about this experience, each followed by the line in the story that
  answers it. Include "what was your part?", "what was hardest?", "what
  would you change?" and the domain probes the work invites.
- **Gaps** — the checklist lines that are "not discussed" and any place the
  candidate was vague, as things to look up or decide how to phrase before
  an interview. Plain list; no coaching on how to answer.
