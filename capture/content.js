// Runs on job pages. Sends the visible text to the local server for a quick
// auto-reject check and paints the verdict as a banner across the top.
//
// The banner is advice only. Nothing here captures, applies, or changes any
// state on the server; the "Add to autopilot" button is the same call as the
// context menu item.

// Injected twice when a page is both in the match list and opened from
// Jobright; the second copy must do nothing.
(() => {
if (window.__jobAutopilotScreen) return;
window.__jobAutopilotScreen = true;

const SERVER = "http://127.0.0.1:8787";
const MIN_TEXT = 400; // shorter than this is a shell page, not a posting
const SETTLE_MS = 1500;
// An ATS page can be a shell at load and fill in from an XHR seconds later
// (Oracle does). Too little text is waited on, not given up on.
const WAIT_MS = 1000;
const WAIT_MAX = 20;
const HOST_ID = "job-autopilot-screen";

// Some sites send a Trusted Types CSP - Google's own careers pages do - and
// under one every HTML sink throws instead of rendering: `innerHTML`, and
// `DOMParser.parseFromString` with it, which is why parsing the markup
// somewhere else is not the way out. The badge, and with it every way into
// the autopilot, was simply absent on those pages, with one "script threw"
// line in the injector's log to say so (2026-11-03, found while putting the
// Add button on every page).
//
// A policy of our own is the sanctioned way through: the markup here is
// ours, written in this file, and never anything the page said. A site whose
// `trusted-types` directive names its policies refuses to make this one, so
// both of the other sinks are tried after it rather than throwing - a page
// with no Trusted Types at all, which is nearly all of them, never reaches
// the policy in the first place.
let htmlPolicy;
function trustedHTML(html) {
  const tt = window.trustedTypes;
  if (!tt || !tt.createPolicy) return html;
  if (htmlPolicy === undefined) {
    try {
      htmlPolicy = tt.createPolicy("job-autopilot", { createHTML: (s) => s });
    } catch {
      htmlPolicy = null;
    }
  }
  return htmlPolicy ? htmlPolicy.createHTML(html) : html;
}

function setHTML(root, html) {
  root.innerHTML = trustedHTML(html);
}

const LABELS = {
  perm: "PERM ad",
  experience: "Experience",
  visa: "Visa",
  export_control: "Export control",
  clearance: "Clearance",
  timeline: "Timeline",
  location: "Location",
  degree: "Degree",
  seniority: "Seniority",
  other: "Other",
};

// The bar is black with one colour for the verdict: green ok, amber
// caution, red reject, purple while the screen runs, grey when it could
// not run. Same vocabulary as the review page.
const COLOURS = {
  reject: { tone: "#ff5c5c", tag: "NOT OK", hint: "Hard requirement you do not meet. Applying is likely wasted." },
  caution: { tone: "#b48cff", tag: "CAUTION", hint: "Something to weigh; nothing hard against you." },
  ok: { tone: "#3ddc84", tag: "OK", hint: "No auto-reject found in the posting." },
  pending: { tone: "#b48cff", tag: "SCREENING", hint: "" },
  error: { tone: "#a0a0a0", tag: "SCREEN FAILED", hint: "" },
  not_a_job: { tone: "#a0a0a0", tag: "NO POSTING", hint: "Could not find a job description on this page. If it is still loading, press Again." },
  submitted: { tone: "#3ddc84", tag: "SUBMITTED", hint: "" },
};

const CHECKS = "years of experience, visa and sponsorship, export control, clearance and citizenship, start date and graduation window, location, degree, seniority, PERM ads";

// Nothing on this page acts by itself any more (2026-10-30, asked for).
//
// The countdown used to queue a job, and on Jobright's own posting page to
// press their Apply, two seconds after a clean verdict. What it actually
// queued, measured off the rows it left behind: `login.usc.edu/login`
// twice, a SAP "Career Opportunities: Sign In" page, a bare careers
// homepage - two of them marked submitted - and a row whose entire title
// is "Apply". A marked tab stays marked through every navigation, so a
// single-sign-on redirect inside one was screened and queued as though it
// were the job.
//
// The verdict is still screened and still shown, because a verdict is
// information and costs nothing but a local rule run. What is gone is
// anything that acts on it: `act()` and `openJob()` are now reached only
// from a click, and the person decides on a page they are looking at.
// "We will enhance the human and will not replace him."
// Once the bar has a verdict to give, it has done its work: it closes
// itself after this long, counted down in red across the ✕, and becomes
// the corner badge in the verdict's colour. Every banner, on every page -
// applied, rejected, filled, screened clean. Not while the screen is still
// running, and not while the add countdown is going: on Jobright's page
// the job is queued by the employer tab its Apply opened, so the bar asks
// the server whether that has happened, this often, for this long, and
// only then starts the countdown. A job that never lands leaves the bar
// open, saying so.
const AUTO_COLLAPSE_MS = 3000;
const QUEUED_POLL_MS = 2000;
const QUEUED_WAIT_MS = 90000;

// On Jobright itself only the posting pages carry a job; the recommend
// list, search, and the rest are shells around many. A Jobright posting
// page is screened but never queued: its Apply button leads to the
// employer's page, which is the URL worth queueing, and queueing both
// would run the pipeline twice for one job. There the button (and the
// countdown) presses Apply instead, and the employer's page queues itself.
const onJobright = /(^|\.)jobright\.ai$/.test(location.hostname);

// Every page in this browser gets this script, because a posting can be on
// any host and a host list could only ever guess. Most of those pages are
// not a job: email, a bank, a search. A cold page reads nothing and calls
// nothing - no `/screen`, no posting text, no request of any kind - it draws
// the badge and waits. The click on it is the whole decision, and from
// there the page behaves like any other: it screens, it offers, and the
// click on its Add button is the only thing that queues anything.
const cold = !!window.__autopilotCold;
// `window.__autopilotHandOpened` is still set by the injector and by the
// extension, and nothing here reads it any more (2026-10-30). It existed to
// hold the countdown off a page nobody had pointed at a job; with no
// countdown on any page there is nothing for it to hold off, and a flag read
// in one place and set in three is how "it never queues a hand-opened tab"
// turned out to be true of one injection path and not the other.
// Whether the person asked for this page to be screened by tapping its
// cold badge. Nothing counts down any more, so this no longer decides
// whether the banner may act - it decides whether the tab is the person's:
// a tab they are standing in front of is never closed under them once the
// job is queued, where the tab Jobright's Apply opened was.
let askedByHand = false;
// Whether that press was the person asking for this page to go in. The cold
// button says "Add to autopilot", so the banner it renders into owes them
// the add without a second press; it is spent the moment it is honoured, so
// a later re-render of the same banner never adds anything twice.
let coldAdd = false;

// "Use Opus": tailor this one job with the expensive model. Armed by the
// button and read when Add is pressed, so the two are one decision in
// either order. Per tab, per job; the default is the cheap model.
let usePremium = false;

// A posting already in autopilot (`seen` on the screen reply) is never
// auto-added again; the bar says where it stands and the button opens it
// on the review page. "high" is the same posting, provably; "confident" is
// the same company and role or description, which is right often enough to
// stop the countdown and wrong often enough to keep an "add anyway".
// After the agent has filled a form, the person submits it. The page that
// follows says so, and this script, still in that tab, tells the server, so
// the job is marked submitted without the button. The watch starts when
// the screen reply says this page's job is filled (or being filled), and
// the job id is kept in sessionStorage so it survives the navigation to a
// confirmation page. Nothing here presses anything: the person did.
const CONFIRMED = /thank(s| you) for (applying|your (application|interest|submission))|application (has been |was )?(submitted|received|sent|complete)|we('ve| have) received your application|your application (is|has been) (in|complete)|successfully (submitted|applied)/i;
const CONFIRM_POLL_MS = 1500;
const JOB_KEY = "autopilotJob";
// A confirmation is a page with the phrase and no form left: a posting's
// own text says "thank you for your interest" often enough, and a filled
// form is still a form. Fewer than this many controls = the form is gone.
const FORM_GONE = 3;
let submissionWatched = false;   // this tab is a filled form or what it became; never screen it again
function formControls() {
  let n = 0;
  for (const el of document.querySelectorAll("input:not([type=hidden]), select, textarea")) {
    const r = el.getBoundingClientRect();
    if (r.width > 0 || r.height > 0) n += 1;
  }
  return n;
}

// While the human works on a filled form, the server is asked to look at
// it now and then (it reads the fields itself over CDP; this script sends
// nothing about the form). The last look before the page leaves is what
// the correction loop diffs against the agent's fill. Sent on a timer, on
// any form submit, and on the way out.
// What automation is doing to this tab, told to the page over CDP by the
// injector (pressing Autofill) and the fill (the browser agent). The badge
// core turns purple and flows while it is "working"; the bar shows the
// note. Survives a re-render: the state is kept here, not in the DOM.
const AUTOMATION = { state: "idle", note: "" };
let applyAutomation = () => {};
window.__autopilotAutomation = (state, note) => {
  // Jobright's autofill (pressed by the injector) usually reports done after
  // this job was queued; the pipeline is still running then, so the core
  // stays purple and only the note moves on. The agent, or a failure, ends it.
  const fromAutofill = /^autofill:/i.test(note || "");
  if (fromAutofill && AUTOMATION.pipeline && state !== "error") {
    state = "working";
    note = "Autofill done; tailoring the resume";
  }
  AUTOMATION.state = state || "idle";
  AUTOMATION.note = note || "";
  applyAutomation();
};

// What the person changed on the form since autofill, as the server read
// it back (label, what autofill had, what it is now). Shown in the banner
// as a pick list: tick the ones Jobright got wrong, press Remember, and
// every next form gets the corrected value on that field. Nothing is
// remembered without the tick; the list is advice until then.
const CHANGES = { jobId: null, rows: [], picked: new Set(), busy: false, note: "", popped: false };
let paintChanges = () => {};
let expandBanner = () => {};
function takeChanges(jobId, rows) {
  CHANGES.jobId = jobId;
  CHANGES.rows = Array.isArray(rows) ? rows : [];
  // The first change worth remembering pops the bar open once; after
  // that the badge keeps it and the person opens it when they want.
  if (!CHANGES.popped && CHANGES.rows.some((r) => !r.remembered)) { CHANGES.popped = true; expandBanner(); }
  for (const label of [...CHANGES.picked]) if (!CHANGES.rows.some((r) => r.label === label && !r.remembered)) CHANGES.picked.delete(label);
  paintChanges();
}
async function rememberPicked() {
  if (CHANGES.busy || !CHANGES.jobId || !CHANGES.picked.size) return;
  CHANGES.busy = true; CHANGES.note = "Remembering…"; paintChanges();
  try {
    const res = await post(`/review/${CHANGES.jobId}/remember`, { labels: [...CHANGES.picked] });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `server ${res.status}`);
    CHANGES.note = data.remembered ? `Remembered ${data.remembered}; next form gets ${data.remembered === 1 ? "it" : "them"}.` : (data.reason || "Nothing remembered");
    CHANGES.picked.clear();
    takeChanges(CHANGES.jobId, data.changes);
  } catch (err) {
    CHANGES.note = `Could not remember: ${err.message}`;
  } finally {
    CHANGES.busy = false; paintChanges();
  }
}

// Which job in autopilot this page is about, when the screen could not say.
//
// 2026-10-30, found on a YC-sourced job. `/screen` matches the page against
// the queue, and for a job captured from `ycombinator.com/companies/...` the
// URL on file is the **listing**, not the employer's form - YC links out to
// the company's own ATS, so the Workday page the person signs in to has no
// `jr_id`, no resemblance to the URL on file, and the identity ladder
// correctly answers "not the same job". No match meant no chips, no Finder
// button and no drag: the tailored resume was never offered, rather than
// offered and broken.
//
// So the person can say which job it is. The list comes back from
// `/start-now` when it answers `no_job` (in-flight jobs, newest first), and
// the same list serves both this and Start now - one question, asked once.
// Kept here rather than in the DOM: the banner is re-rendered on every
// screen and collapse.
let jobHint = null;      // {id, title, company}, chosen by hand

// Start now: the page is a form the person has reached - signed in, past the
// account wall, the fields on the screen - and the autopilot takes it from
// here. The whole point of the button is that the fill **waited**: "It has
// to stop. It should not automatically start playing."
//
// The server's contract (2026-10-30): POST /start-now {url} answers
// {started, state: started|synced|no_job, job_id, note}, and takes an
// explicit {job_id} for a page it could not match itself. `synced` means a
// fill is already running for that job and this one joined it rather than
// starting a second, which is the one answer that has to be said out loud:
// otherwise a second press reads as the first one having failed.
const START = { note: "", candidates: [], busy: false, state: "" };
let paintStart = () => {};

// One question about this job, then another: a thread, not a one-shot.
// "In some cases where we need to give answers to questions, we'll maybe
// make another call to answer that particular question... It sort of should
// be like a chat." Ashby autofills; Workday asks things.
//
// This is `POST /review/<id>/thread` with `action: "ask"` - the endpoint the
// review page's own thread uses, so the turns are kept in the job's one
// conversation and are there on the review page afterwards. A visa /
// sponsorship / work-authorisation question is refused server-side, and the
// refusal is shown as it comes back rather than worked around.
//
// Nothing here types anything into the form. The answer is to read and copy.
const ASK = { jobId: null, turns: [], busy: false, note: "", open: false, draft: "" };
let paintAsk = () => {};

const STATE_PING_MS = 5000;
let stateTimer = null;
function watchState(jobId) {
  if (stateTimer) return;
  const ping = async () => {
    try {
      const res = await post(`/review/${jobId}/form-state`, {});
      const data = await res.json();
      if (res.ok && data.changes) takeChanges(jobId, data.changes);
    } catch { /* the server's own watch reads the tab too */ }
  };
  stateTimer = setInterval(ping, STATE_PING_MS);
  document.addEventListener("submit", ping, true);
  document.addEventListener("click", (event) => {
    const button = event.target?.closest?.("button, [type=submit], [role=button]");
    if (button && /submit|apply|send|finish/i.test(button.innerText || button.value || "")) ping();
  }, true);
  window.addEventListener("pagehide", ping);
  ping();
}

let confirmTimer = null;
// The watch exists to stop the banner painting a verdict over the
// confirmation page a submitted form turns into. It was never meant to
// silence the tab for good, and it did: a Workday tab that had held a
// fill was then used to sign in and open the real application form, and
// the banner never came back — no verdict, and no way to put the tailored
// resume on the slot, because `submissionWatched` is restored from
// sessionStorage on every page in that tab.
//
// So the suppression lasts only while the page in front of us actually
// reads as a confirmation. The `confirmTimer` and the stored job id stay,
// so a confirmation reached later is still marked; and `/screen` answers
// `submitted` for a confirmation page in string work before any rule
// runs, which is the second guard against painting over it.
function notTheConfirmation() {
  if (CONFIRMED.test(document.body?.innerText || "")) return false;
  submissionWatched = false;
  return true;
}

function watchSubmission(jobId) {
  if (confirmTimer) return;
  submissionWatched = true;
  try { sessionStorage.setItem(JOB_KEY, jobId); } catch { /* storage blocked */ }
  watchState(jobId);
  confirmTimer = setInterval(async () => {
    const text = (document.body?.innerText || "");
    const match = CONFIRMED.exec(text);
    if (!match || formControls() >= FORM_GONE) return;
    clearInterval(confirmTimer);
    confirmTimer = null;
    if (stateTimer) { clearInterval(stateTimer); stateTimer = null; }
    try { sessionStorage.removeItem(JOB_KEY); } catch { /* storage blocked */ }
    const start = Math.max(0, match.index - 40);
    const quote = text.slice(start, match.index + match[0].length + 60).replace(/\s+/g, " ").trim();
    try {
      const res = await post(`/review/${jobId}/submitted-seen`, { url: location.href, quote });
      const data = await res.json();
      if (data.marked) render({ verdict: "submitted", summary: "Application submitted — marked in autopilot." });
    } catch { /* the button on the review page still works */ }
  }, CONFIRM_POLL_MS);
}

const SEEN_STATUS = {
  queued: "Queued", tailoring: "Being tailored", awaiting_review: "Waiting for your review",
  approved: "Approved", filling: "Being filled", filled: "Filled, waiting for you to submit",
  submitted: "Applied", skipped: "Rejected", failed: "Failed",
};
// The statuses that mean this posting has already been applied for, or is on
// a form that is filled and waiting for a click. Re-applying to one of these
// is the mistake the banner exists to stop, so it is said in the loudest way
// the bar has and the Add button goes away with it.
const APPLIED_STATUS = new Set(["submitted", "filled", "filling"]);

// When it happened, to the minute and in the reader's own time zone. "on
// 2026-09-20" is not an answer to "did I apply to this one?" if two
// applications went out that day.
function appliedWhen(at) {
  if (!at) return "";
  const when = new Date(at);
  if (isNaN(when)) return at.slice(0, 10);
  const today = new Date();
  const sameDay = when.toDateString() === today.toDateString();
  const time = when.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return sameDay ? `today at ${time}`
    : `on ${when.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })} at ${time}`;
}

function appliedNotice(seen) {
  if (!seen || !APPLIED_STATUS.has(seen.status)) return "";
  const when = appliedWhen(seen.at);
  // The heading has to be true. "ALREADY APPLIED" over a job that is only
  // filled - the tailored form sitting in a tab waiting for the person's
  // own Submit - says the one thing that has not happened yet, and reads as
  // the block pointing at the wrong job. Only a submitted job is applied;
  // the other two are in flight and say so, in amber rather than red.
  const head = seen.status === "submitted" ? "ALREADY APPLIED"
    : seen.status === "filled" ? "ALREADY IN AUTOPILOT" : "FILLING THIS NOW";
  const what = seen.status === "submitted" ? `Applied${when ? ` ${when}` : ""}`
    : seen.status === "filled"
      ? `Form filled${when ? ` ${when}` : ""} and waiting for your Submit — do not start it again`
      : "Autopilot is filling this job's form right now";
  const which = [seen.company, seen.title].filter(Boolean).join(" — ");
  // One line under the heading, and the whole block is the button: it said
  // the same thing five ways (what, when, company, "Same posting.", "Show me
  // what I sent →") and the bar carried a second button saying it again.
  // What is left is what has to be read from across the room; the rest is
  // the tooltip, and the cursor says it can be clicked.
  const line = seen.level === "high" ? `${what}${which ? ` · ${which}` : ""}`
    : `Looks like the same job · ${what}${which ? ` · ${which}` : ""}`;
  const promise = seen.status === "submitted"
    ? "Show me what I sent: the posting, the resume and letter that went with it, and the whole history"
    : "Open this job in Autopilot: the resume and letter on it, and where the fill has got to";
  return `<div class="applied${seen.status === "submitted" ? "" : " pending"}"
       data-act="open" role="button" tabindex="0" title="${esc(promise)}">
      <b>${head}</b>
      <span>${esc(line)}</span>
    </div>`;
}

// A job that is finished with is described by its outcome, not by where
// it is kept. "Already in autopilot · Rejected" led with the plumbing and
// buried the answer; on a posting someone is looking at a second time the
// answer is "Applied", "Rejected — visa", or "Failed".
const DECIDED = {
  submitted: "APPLIED",
  skipped: "REJECTED",
  failed: "FAILED",
};

function seenLine(seen) {
  const decided = DECIDED[seen.status];
  const when = appliedWhen(seen.at);
  if (decided) {
    const why = seen.status === "skipped" && seen.reject ? ` — ${seen.reject}` : "";
    const head = `${decided}${why}${when ? ` ${when}` : ""}`;
    // At `confident` the match may be another role at the same company, so
    // the outcome is prefixed rather than stated.
    return seen.level === "high" ? head : `LOOKS LIKE THE SAME JOB · ${head}`;
  }
  // Still in flight: where it has got to is the useful thing.
  const state = SEEN_STATUS[seen.status] || seen.status;
  return seen.level === "high"
    ? `In autopilot · ${state}`
    : `Looks like a job already in autopilot: ${seen.company || ""}${seen.company && seen.title ? " — " : ""}${seen.title || ""} · ${state}`;
}
// What the button promises, which is what the person wants next: the
// documents they sent, the reason they rejected it, or the error.
function openLabel(seen) {
  if (APPLIED_STATUS.has(seen.status)) return "Show me what I sent";
  if (seen.status === "skipped") return "Show me why I rejected it";
  if (seen.status === "failed") return "Show me what happened";
  return "Open in autopilot";
}

function isPosting() {
  return !onJobright || location.pathname.startsWith("/jobs/info/");
}

let lastUrl = "";
let bannerCollapsed = false;

function pageText() {
  // innerText honours CSS visibility, so hidden menus and scripts stay out.
  const main =
    document.querySelector("main, [role=main], article") || document.body;
  return (main.innerText || "").replace(/\n{3,}/g, "\n\n").trim();
}

// The button on a page nobody has said anything about: the assistant's own
// orb with its offer written beside it, in the corner. It is a button and
// nothing else - no text read, no fetch - and pressing it is the whole
// decision (2026-11-03, asked for: "When I open a page from outside, I want
// a start button... I need to click 'Add this to job queue' or something.
// Let's show that button on every page"). The orb alone said only "click to
// screen this posting", which is the machine's first step and not the
// person's intent, and it left the real decision behind a second press on a
// banner they had not asked to read.
//
// So the press screens the page and then queues it, one press for both,
// through the banner's own Add. Nothing is applied for: the job goes in,
// the resume is tailored, and the fill still stops at the filled form.
function renderCold() {
  document.getElementById(HOST_ID)?.remove();
  const host = document.createElement("div");
  host.id = HOST_ID;
  const shadow = host.attachShadow({ mode: "closed" });
  setHTML(shadow, `
    <style>
      :host { all: initial; }
      button.badge { position: fixed; top: 6px; left: 6px; z-index: 2147483647;
                     display: flex; align-items: center; gap: 2px; padding: 0 14px 0 0;
                     background: #15131c; color: #fff; border: 2px solid #000;
                     border-radius: 28px; box-shadow: 3px 3px 0 #000; cursor: pointer;
                     font: 600 12px/1 ui-monospace, SFMono-Regular, Menlo, monospace;
                     letter-spacing: .02em; transition: transform .15s ease; }
      button.badge:hover, button.badge:focus-visible { transform: translateY(-1px); outline: none; }
      button.badge[disabled] { cursor: default; }
      .badge svg { width: 44px; height: 44px; overflow: visible; flex: 0 0 auto; }
      .badge span { white-space: nowrap; }
      .assistant-shadow { fill: #000; }
      .assistant-ring { fill: #fff; stroke: #000; stroke-width: 5; }
      .assistant-core { fill: #b48cff; transform-box: fill-box; transform-origin: center;
                        transition: transform .15s ease; }
      .badge:hover .assistant-core, .badge:focus-visible .assistant-core { transform: scale(1.35); }
      .badge.live .assistant-core { animation: flow 1.1s ease-in-out infinite; }
      @keyframes flow { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.7); } }
      @media (prefers-reduced-motion: reduce) { .badge.live .assistant-core { animation: none; transform: scale(1.4); } }
    </style>
    <button class="badge" aria-label="Add this job to Job Autopilot"
            title="Add this posting to Job Autopilot: it screens it, tailors the resume and the letter, and fills the form. It never submits.">
      <svg class="assistant-logo" viewBox="-70 -70 140 140" aria-hidden="true">
        <circle class="assistant-shadow" cx="7" cy="7" r="50"/>
        <circle class="assistant-ring" r="50"/>
        <circle class="assistant-core" r="11"/>
      </svg>
      <span>Add to autopilot</span>
    </button>`);
  const button = shadow.querySelector("button.badge");
  button.addEventListener("click", () => {
    // `screen` waits for a client-rendered page's text, which can take
    // seconds; the orb says something is happening until it renders over
    // this host.
    button.classList.add("live");
    button.disabled = true;
    button.querySelector("span").textContent = "Reading the posting…";
    askedByHand = true;
    coldAdd = true;
    screen();
  });
  document.documentElement.appendChild(host);
}

function render(result, { pending = false } = {}) {
  document.getElementById(HOST_ID)?.remove();
  const host = document.createElement("div");
  host.id = HOST_ID;
  const shadow = host.attachShadow({ mode: "closed" });

  const verdict = pending ? "pending" : result.verdict in COLOURS ? result.verdict : "error";
  const seen = pending ? null : result.seen || null;
  // The job this page is about: the one the screen matched, else the one the
  // person picked off the candidate list when it matched nothing. Both get
  // the same thing - the files, which resume and letter went on, the Finder
  // button and the question thread - because a form reached through a
  // listing site is exactly where those are wanted.
  const job = seen
    ? { id: seen.id, status: seen.status, title: seen.title || "", company: seen.company || "" }
    : jobHint
      ? { id: jobHint.id, status: jobHint.status || "", title: jobHint.title || "",
          company: jobHint.company || "", picked: true }
      : null;
  const c = COLOURS[verdict];
  const tag = c.tag;
  // Facts derived from the resume say nothing about visa status or dates,
  // so those checks did not happen. That is a line of its own under the
  // verdict, not a clause buried in the summary.
  const fromResume = result.facts_source === "resume";
  const facts = fromResume ? "" : result.facts_source ? "; facts from your profile" : "";
  const about = pending
    ? `Reading this posting against your profile. Checks: ${CHECKS}.`
    : verdict === "error" || verdict === "not_a_job" || verdict === "submitted" ? c.hint
    : `${c.hint} Checked ${CHECKS}${facts}.${result.cached ? " Cached." : ""}`;

  const unchecked = fromResume && !["error", "not_a_job", "submitted"].includes(verdict)
    ? `<li class="unchecked"><b>Unchecked</b>: visa and dates. The facts come from your resume only; add your applicant facts in the app's Profile tab.</li>`
    : "";
  const flags = unchecked + (result.flags || [])
    .map(
      (f) =>
        `<li><b>${esc(LABELS[f.category] || f.category)}</b>` +
        (f.severity === "soft" ? ` <i>(soft)</i>` : "") +
        `: “${esc(f.quote)}”` +
        (f.reason ? ` — ${esc(f.reason)}` : "") +
        `</li>`
    )
    .join("");
  const applied = appliedNotice(seen);
  // An applied job is never added again from here when the match is certain.
  // At `confident` the person still gets "Add anyway", because the match may
  // be another role at the same company.
  const canAdd = !pending && !(seen && seen.level === "high") && verdict !== "submitted";
  const canRetry = !pending && (verdict === "error" || verdict === "not_a_job");

  setHTML(shadow, `
    <style>
      :host { all: initial; }
      /* Wraps rather than squeezes. The controls do not shrink (their
         labels do not break), so in a half-screen window the
         verdict was crushed into a column one word wide running the whole
         height of the page - "No hard or soft flags found" down the left
         edge in nine lines (2026-10-07). Below the floor the buttons take a
         row of their own instead. */
      .bar { position: fixed; top: 0; left: 0; right: 0; z-index: 2147483647;
             background: #000; color: #fff; border-bottom: 3px solid ${c.tone};
             font: 12px/1.45 "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
             padding: 8px 12px; display: flex; flex-wrap: wrap; gap: 8px 14px;
             align-items: flex-start; }
      button.badge { position: fixed; top: 6px; left: 6px; z-index: 2147483647; width: 52px; height: 52px;
                     display: none; align-items: center; justify-content: center; padding: 0;
                     background: transparent; border: 0; box-shadow: none; cursor: pointer; }
      button.badge:hover { background: transparent; }
      .badge svg { width: 52px; height: 52px; overflow: visible; }
      .assistant-shadow { fill: #000; }
      .assistant-ring { fill: #fff; stroke: #000; stroke-width: 5; }
      .assistant-core { fill: ${c.tone}; transform-box: fill-box; transform-origin: center;
                        transition: transform .15s ease; }
      .badge:hover .assistant-core { transform: scale(1.35); }
      .shell.working .assistant-core { fill: #b48cff; animation: flow 1.1s ease-in-out infinite; }
      /* Purple flowing = automation is working, ours or Jobright's. Green and
         still = the form is filled and the only thing left is your look at it
         and your click on Submit. Red = something did not go on. */
      .shell.done .assistant-core { fill: #35d07f; animation: none; }
      .shell.error .assistant-core { fill: #ff5c5c; }
      @keyframes flow { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.7); } }
      @media (prefers-reduced-motion: reduce) { .shell.working .assistant-core { animation: none; transform: scale(1.4); } }
      .auto { display: none; color: #b48cff; font-weight: 600; margin-top: 3px; }
      .shell.done .auto { color: #35d07f; }
      .shell.working .auto, .shell.done .auto, .shell.error .auto { display: block; }
      .shell.error .auto { color: #ff5c5c; }
      .shell.working .auto::after { content: "…"; animation: dots 1.2s steps(4) infinite; }
      .shell.collapsed .bar { display: none; }
      .shell.collapsed .badge { display: flex; }
      .tag { background: ${c.tone}; color: #000; font-weight: 800; font-size: 12px; letter-spacing: .1em;
             padding: 5px 8px; white-space: nowrap; text-transform: uppercase; }
      .tag.live::after { content: "…"; animation: dots 1.2s steps(4) infinite; }
      @keyframes dots { from { clip-path: inset(0 100% 0 0); } to { clip-path: inset(0 -20% 0 0); } }
      @media (prefers-reduced-motion: reduce) { .tag.live::after { animation: none; } }
      .about { display: block; color: #a0a0a0; font-size: 11px; margin-top: 2px; }
      /* The floor the wrap is measured against: under this the summary
         reads as a column of single words, which is worse than a second
         row of buttons. */
      .body { flex: 1 1 280px; min-width: 280px; padding-top: 4px; }
      .summary { color: #fff; }
      ul { margin: 4px 0 0; padding-left: 18px; }
      li { margin: 2px 0; }
      li b { color: ${c.tone}; text-transform: uppercase; font-size: 11px; letter-spacing: .06em; }
      li i { color: #a0a0a0; font-style: normal; }
      button { background: #000; color: #fff; border: 2px solid #fff; padding: 4px 10px;
               font: inherit; font-size: 11px; font-weight: 600; letter-spacing: .08em;
               text-transform: uppercase; cursor: pointer; }
      button:hover { background: #fff; color: #000; }
      button:disabled { opacity: .5; cursor: default; }
      button.add { background: ${c.tone}; color: #000; border-color: ${c.tone}; }
      button.add:hover { background: #fff; border-color: #fff; }
      button.add.done { background: #000; color: ${c.tone}; border-color: ${c.tone}; cursor: default; }
      button.opus { background: #000; color: #3ddc84; border-color: #3ddc84; padding: 8px 16px; font-size: 13px; }
      button.opus:hover { background: #3ddc84; color: #000; }
      button.opus[aria-pressed="true"] { background: #3ddc84; color: #000; }
      button.opus[aria-pressed="true"]:hover { background: #000; color: #3ddc84; }
      button.close { position: relative; overflow: hidden; }
      button.close .fill { position: absolute; top: 0; bottom: 0; left: 0; width: 0; background: #ff5c5c; }
      button.close span { position: relative; z-index: 1; }
      li.unchecked b { color: #a0a0a0; }
      .seen { display: block; color: #fff; font-weight: 600; margin-top: 3px; }
      /* A decided job says its outcome, and the line is the way to it. */
      .seen.decided { cursor: pointer; letter-spacing: .06em; text-decoration: underline;
                      text-underline-offset: 3px; }
      .seen.decided:hover, .seen.decided:focus-visible { color: #ffd479; outline: none; }
      /* The one thing on this bar that has to be read from across the room. */
      /* One row, not a panel: the words that have to carry across the room
         are "ALREADY APPLIED", and everything after them is one line of
         detail beside it. Three stacked lines in a boxed block took a
         third of the bar to say what fits on one. */
      .applied { display: flex; align-items: baseline; gap: 8px; margin: 4px 0 2px;
                 padding: 3px 8px; background: #2a0d0d; border: 2px solid #ff5c5c;
                 min-width: 0; }
      .applied b { color: #ff5c5c; font-size: 13px; font-weight: 800;
                   letter-spacing: .1em; line-height: 1.3; white-space: nowrap; }
      .applied span { color: #fff; font-size: 12px; font-weight: 600; min-width: 0;
                      overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .applied { cursor: pointer; }
      .applied:hover, .applied:focus-visible { background: #3a1010; outline: none; }
      /* Filled or filling: still a job not to start twice, but nothing has
         been applied for yet, so it is not the red one. */
      .applied.pending { background: #2a1f06; border-color: #ffb74d; }
      .applied.pending b { color: #ffb74d; }
      .applied.pending:hover, .applied.pending:focus-visible { background: #3a2a08; }
      .files { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 5px; align-items: center; }
      .files .hint { color: #a0a0a0; font-size: 11px; }
      .files .what { color: #a0a0a0; font-size: 11px; display: block; width: 100%; }
      .files .what b { color: #fff; font-weight: 600; }
      button.finder { background: #000; color: #fff; border-color: #fff; }
      /* Start now: the standing control on a job's own tab. The bar folds
         itself away after three seconds and the orb is what is left, so this
         sits under the orb when collapsed and under the bar while it is
         open - never on top of either. The offset is set in code, because
         the bar's height depends on how far its own row wrapped. */
      .startnow { position: fixed; left: 6px; top: 64px; z-index: 2147483646;
                  display: flex; flex-direction: column; align-items: flex-start; gap: 4px;
                  max-width: min(360px, calc(100vw - 16px)); }
      .startnow.off { display: none; }
      button.start { background: #3ddc84; color: #000; border-color: #3ddc84;
                     font-size: 13px; padding: 8px 14px; box-shadow: 4px 4px 0 #000; }
      button.start:hover, button.start:focus-visible { background: #000; color: #3ddc84; }
      button.start:disabled { opacity: .6; cursor: default; }
      .startnow .note { background: #000; color: #fff; border: 2px solid #3ddc84;
                        padding: 4px 8px; font-size: 11px; line-height: 1.35; }
      .startnow .picks { background: #000; border: 2px solid #3ddc84; padding: 5px 6px;
                         display: flex; flex-direction: column; gap: 3px; max-height: 40vh;
                         overflow-y: auto; }
      .startnow .picks button { background: #000; color: #fff; border-color: #333;
                                text-align: left; text-transform: none; letter-spacing: 0;
                                font-size: 11px; padding: 4px 6px; white-space: normal; }
      .startnow .picks button:hover { background: #3ddc84; color: #000; border-color: #3ddc84; }
      /* The picks box sets its own display, and an explicit display
         declaration beats the user agent rule that hides an element with
         the hidden attribute - so hiding it did nothing, and an empty
         candidate list rendered as a small black square with a green
         border under the button (reported 2026-10-31 with a screenshot).
         Fixed here rather than by swapping the attribute for a class,
         because everything else in this sheet hides by the attribute.
         Note for whoever edits this stylesheet: it lives inside a
         template literal, so a backtick in a comment ends the string. */
      .startnow .picks[hidden] { display: none; }
      /* The question thread. A transcript and a box, in the bar's own body:
         the answer is read and copied, never typed into the form by us. */
      .ask { margin-top: 6px; border-top: 1px solid #333; padding-top: 6px; }
      .ask .turns { display: flex; flex-direction: column; gap: 5px; max-height: 40vh;
                    overflow-y: auto; margin-bottom: 5px; }
      .ask .turn { font-size: 11px; line-height: 1.45; white-space: pre-wrap; }
      .ask .turn.you { color: #3ddc84; }
      .ask .turn.them { color: #fff; border-left: 2px solid #b48cff; padding-left: 6px; }
      .ask .turn.refused { color: #ff5c5c; border-left-color: #ff5c5c; }
      .ask .turn button.copy { margin-left: 6px; padding: 1px 5px; font-size: 10px; }
      .ask .compose { display: flex; gap: 6px; align-items: flex-start; }
      .ask textarea { flex: 1 1 auto; min-width: 0; background: #111; color: #fff;
                      border: 2px solid #333; font: inherit; font-size: 11px; padding: 4px 6px;
                      resize: vertical; min-height: 46px; }
      .ask textarea:focus { border-color: #b48cff; outline: none; }
      .ask .note { color: #b48cff; font-size: 11px; display: block; margin-top: 4px; }
      .ask button.send { background: #b48cff; color: #000; border-color: #b48cff; }
      .changes { display: none; margin-top: 6px; border-top: 1px solid #333; padding-top: 6px; }
      .changes.on { display: block; }
      .changes .hint { color: #a0a0a0; font-size: 11px; display: block; margin-bottom: 4px; }
      .changes label.row { display: flex; align-items: baseline; gap: 6px; font-size: 11px; color: #fff; cursor: pointer; padding: 2px 0; }
      .changes label.row input { accent-color: #b48cff; margin: 0; flex: none; position: relative; top: 1px; }
      .changes label.row .l { color: #fff; font-weight: 600; }
      .changes label.row .was { color: #ff5c5c; text-decoration: line-through; }
      .changes label.row .now { color: #3ddc84; }
      .changes label.row.kept { color: #a0a0a0; cursor: default; }
      .changes label.row.kept .now { color: #a0a0a0; }
      .changes .row-actions { display: flex; gap: 6px; align-items: center; margin-top: 4px; }
      .changes button.remember { background: #b48cff; color: #000; border-color: #b48cff; }
      .changes button.remember:disabled { opacity: .5; cursor: default; }
      .changes .note { color: #b48cff; font-size: 11px; }
      .chip { display: inline-flex; align-items: center; gap: 6px; padding: 4px 8px; cursor: grab;
              background: #111; color: #fff; border: 2px solid #b48cff; font-size: 11px; font-weight: 600;
              letter-spacing: .04em; user-select: none; }
      .chip:active { cursor: grabbing; }
      .chip b { color: #b48cff; font-weight: 800; }
      .chip.put { border-color: #3ddc84; cursor: pointer; }
      .chip.put b { color: #3ddc84; }
      button.open { background: #fff; color: #000; border-color: #fff; }
      button.open:hover { background: #000; color: #fff; }
      button:focus-visible { outline: 2px solid #8ab4ff; outline-offset: 2px; }
      .actions { display: flex; flex: 0 0 auto; gap: 6px; white-space: nowrap;
                 align-items: center; margin-left: auto; flex-wrap: wrap; max-width: 100%; }
      /* Narrower than any half of a normal screen. The longest label -
         "Adding to autopilot - click to stop" - is wider than the window
         by then, and a label that reads over two lines beats one that runs
         off the edge. */
      @media (max-width: 560px) {
        .actions, .actions button { white-space: normal; }
      }
      label.approve { display: inline-flex; align-items: center; gap: 4px; font-size: 11px; color: #fff; cursor: pointer; user-select: none; }
      label.approve input { accent-color: ${c.tone}; margin: 0; cursor: pointer; }
    </style>
    <div class="shell${bannerCollapsed ? " collapsed" : ""}">
      <button class="badge" data-act="expand" aria-label="Open Job Autopilot screening" title="Job Autopilot">
        <svg class="assistant-logo" viewBox="-70 -70 140 140" aria-hidden="true">
          <circle class="assistant-shadow" cx="7" cy="7" r="50"/>
          <circle class="assistant-ring" r="50"/>
          <circle class="assistant-core" r="11"/>
        </svg>
      </button>
      <div class="startnow off">
        <button class="start" data-act="start" type="button"
          title="Hand this form to the autopilot: it fills it in and stops at the filled form. It never submits.">Start now</button>
        <span class="note" hidden></span>
        <span class="picks" hidden></span>
      </div>
      <div class="bar">
        <span class="tag${pending ? " live" : ""}">${tag}</span>
        <div class="body">
          <span class="summary">${esc(result.summary || result.error || "")}</span>
          ${about ? `<span class="about">${esc(about)}</span>` : ""}
          <span class="auto"></span>
          ${applied}
          ${seen && !applied ? `<span class="seen${DECIDED[seen.status] ? " decided" : ""}"
            ${DECIDED[seen.status] ? 'data-act="open" role="button" tabindex="0"' : ""}
            >${esc(seenLine(seen))}</span>` : ""}
          ${job ? `<div class="files" id="files"></div>` : ""}
          <div class="changes" id="changes"></div>
          ${job ? `<div class="ask" id="ask"></div>` : ""}
          ${flags ? `<ul>${flags}</ul>` : ""}
        </div>
        <div class="actions">
          ${seen && !applied ? `<button class="open" data-act="open"><span>${esc(openLabel(seen))}</span></button>` : ""}
          ${canRetry ? `<button data-act="again">Again</button>` : ""}
          ${canAdd ? `<label class="approve" title="Checked: once the resume is tailored the fill starts by itself. Unchecked: it waits for your approval on the review page."><input type="checkbox" checked> auto-approve</label>` : ""}
          ${canAdd ? `<button class="opus" data-act="opus" aria-pressed="${usePremium}" title="Tailor this job's resume and cover letter with the expensive model. About eight times the cost of a normal job, and it rewrites far more of the resume. Nothing is added until you press Add.">${usePremium ? "Opus on" : "Use Opus"}</button>` : ""}
          ${canAdd ? `<button class="add" data-act="add"><span>${seen ? "Add anyway" : onJobright ? "Open job page" : "Add to autopilot"}</span></button>` : ""}
          <button class="close" data-act="close" aria-label="Minimize Job Autopilot"><i class="fill"></i><span>−</span></button>
        </div>
      </div>
    </div>`);

  // On the employer's page the button queues the job; on Jobright's own
  // posting page it presses Jobright's Apply, which opens the employer's
  // page in a new tab, where this script runs again and queues from there.
  // Neither happens without this button being pressed.
  const add = shadow.querySelector("button.add");
  // Auto-approve, per job, decided here before Add. On the employer's page
  // it travels with the capture; on Jobright's page the employer tab queues
  // the job, so the choice goes ahead by posting id.
  const approve = shadow.querySelector("label.approve input");
  const autoFill = () => (approve ? approve.checked : null);
  approve?.addEventListener("change", () => {
    if (onJobright && postingId()) post("/prefs", { jr_id: postingId(), auto_fill: approve.checked }).catch(() => {});
  });
  const label = (text) => { add.querySelector("span").textContent = text; };
  const closeBar = shadow.querySelector("button.close .fill");
  // The id of the job this bar put into autopilot, while it can still be
  // taken out again.
  let added = null;
  const shell = shadow.querySelector(".shell");
  const badge = shadow.querySelector("button.badge");
  const auto = shadow.querySelector(".auto");
  expandBanner = () => { if (bannerCollapsed) expand(); };
  paintChanges = () => {
    const box = shadow.getElementById("changes");
    if (!box) return;
    const rows = CHANGES.rows;
    if (!rows.length && !CHANGES.note) { box.classList.remove("on"); setHTML(box, ""); return; }
    box.classList.add("on");
    const open = rows.filter((r) => !r.remembered);
    setHTML(box, `<span class="hint">${open.length
        ? "Changed since autofill. Tick what Jobright got wrong and every next form gets your value:"
        : rows.length ? "Everything you changed here is on file." : ""}</span>`
      + rows.map((r) => `<label class="row${r.remembered ? " kept" : ""}" title="${esc(r.label)}">
          <input type="checkbox" data-label="${esc(r.label)}" ${r.remembered ? "checked disabled" : CHANGES.picked.has(r.label) ? "checked" : ""}>
          <span class="l">${esc(r.label.length > 48 ? r.label.slice(0, 47) + "…" : r.label)}</span>
          ${r.was ? `<span class="was">${esc(r.was)}</span>` : ""}
          <span class="now">${esc(r.now)}</span>${r.remembered ? " ✓" : ""}</label>`).join("")
      + `<div class="row-actions">${open.length ? `<button class="remember" data-act="remember" ${CHANGES.busy || !CHANGES.picked.size ? "disabled" : ""}>Remember ${CHANGES.picked.size || ""}</button>` : ""}
          ${CHANGES.note ? `<span class="note">${esc(CHANGES.note)}</span>` : ""}</div>`);
    for (const input of box.querySelectorAll("input[type=checkbox]:not(:disabled)")) {
      input.addEventListener("change", () => {
        if (input.checked) CHANGES.picked.add(input.dataset.label); else CHANGES.picked.delete(input.dataset.label);
        const button = box.querySelector("button.remember");
        if (button) { button.disabled = CHANGES.busy || !CHANGES.picked.size; button.textContent = `Remember ${CHANGES.picked.size || ""}`; }
      });
    }
  };
  paintChanges();

  // ---- Start now ---------------------------------------------------------
  const startBox = shadow.querySelector(".startnow");
  const startButton = shadow.querySelector("button.start");
  const startNote = shadow.querySelector(".startnow .note");
  const startPicks = shadow.querySelector(".startnow .picks");
  // Nothing to start on a page whose job has already gone out, and nothing
  // to start while the screen is still running.
  const canStart = !pending && verdict !== "submitted";
  paintStart = () => {
    startBox.classList.toggle("off", !canStart);
    if (!canStart) return;
    startButton.disabled = START.busy;
    // The server answers with more than three states, and the button says
    // which one it got rather than sitting on "Start now" over a refusal:
    // `waiting` is another job's fill holding the one tab, `submitted` and
    // `skipped` are a job that is finished with. The note carries the
    // server's own sentence underneath in every case.
    startButton.textContent = START.busy ? "Starting…"
      : START.state === "synced" ? "Already applying — joined it"
      : START.state === "started" ? "Applying now"
      : START.state === "waiting" ? "Waiting — another job is filling"
      : START.state === "submitted" ? "Already submitted"
      : START.state === "skipped" ? "This job was rejected"
      : job && job.picked ? `Start now · ${job.company || job.title || "this job"}`
      : "Start now";
    startNote.hidden = !START.note;
    startNote.textContent = START.note;
    startPicks.hidden = !START.candidates.length;
    if (START.candidates.length) {
      setHTML(startPicks, START.candidates.map((c) =>
        `<button type="button" data-act="pickjob" data-job="${esc(c.id)}"
          title="Use this job's tailored resume and letter on this page">${
          esc([c.company, c.title].filter(Boolean).join(" — ") || c.id)}</button>`).join(""));
    }
  };
  paintStart();
  // Under the orb when the bar has folded away, under the bar while it is
  // open. The bar's height is not knowable in CSS - it wraps to a second row
  // at half a screen - so it is measured.
  const placeStart = () => {
    const bar = shadow.querySelector(".bar");
    const top = bannerCollapsed ? 64 : Math.round(bar.getBoundingClientRect().height) + 6;
    startBox.style.top = `${top}px`;
  };

  // ---- the question thread ----------------------------------------------
  paintAsk = () => {
    const box = shadow.getElementById("ask");
    if (!box || !job) return;
    if (ASK.jobId !== job.id) { ASK.jobId = job.id; ASK.turns = []; ASK.note = ""; }
    if (!ASK.open) {
      setHTML(box, `<button data-act="ask-open" type="button"
        title="Ask about this job - a question off the form, answered from the posting, your tailored resume, the letter and your profile">Ask about this job</button>`);
      return;
    }
    // `kind` says what a turn is, not who said it: question and change are
    // the person's, answer and result are the model's, error is a refusal
    // (a visa question, server-side) or a failure.
    const turns = ASK.turns.map((t, i) => {
      const mine = t.kind === "question" || t.kind === "change";
      const refused = t.kind === "error";
      const body = esc(t.text || "");
      return `<div class="turn ${mine ? "you" : "them"}${refused ? " refused" : ""}">${
        mine ? "› " : ""}${body}${mine || refused ? "" :
        `<button class="copy" data-act="copy" data-i="${i}" type="button">copy</button>`}</div>`;
    }).join("");
    setHTML(box, `<div class="turns">${turns || ""}</div>
      <div class="compose">
        <textarea rows="2" placeholder="The form's question, in its own words"
          ${ASK.busy ? "disabled" : ""}></textarea>
        <button class="send" data-act="ask-send" type="button" ${ASK.busy ? "disabled" : ""}>${
          ASK.busy ? "…" : "Ask"}</button>
      </div>
      ${ASK.note ? `<span class="note">${esc(ASK.note)}</span>` : ""}`);
    const text = box.querySelector("textarea");
    text.value = ASK.draft;
    text.addEventListener("input", () => { ASK.draft = text.value; });
    // The answer is what is wanted, not the markup round it: copy takes that
    // turn's own text off the state by its index, never the rendered node.
    for (const button of box.querySelectorAll("button.copy")) {
      button.addEventListener("click", (e) => {
        e.stopPropagation();
        const turn = ASK.turns[Number(button.dataset.i)];
        navigator.clipboard?.writeText(turn?.text || "").then(
          () => { button.textContent = "copied"; }, () => { button.textContent = "copy failed"; });
      });
    }
  };
  paintAsk();

  applyAutomation = () => {
    shell.classList.remove("working", "done", "error");
    if (AUTOMATION.state !== "idle") shell.classList.add(AUTOMATION.state);
    auto.textContent = AUTOMATION.note;
    badge.title = AUTOMATION.note ? `Job Autopilot: ${AUTOMATION.note}` : "Job Autopilot";
  };
  applyAutomation();
  host.dataset.collapsed = bannerCollapsed ? "true" : "false";
  const collapse = () => {
    bannerCollapsed = true;
    host.dataset.collapsed = "true";
    shell.classList.add("collapsed");
    placeStart();
  };
  const expand = () => {
    openedByHand = true;
    bannerCollapsed = false;
    host.dataset.collapsed = "false";
    shell.classList.remove("collapsed");
    placeStart();
    // A bar that opened itself folds itself away; a bar the person opened
    // stays open. Tapping the badge is how someone asks for the files, the
    // verdict's reasons, or the way back to the job - all of which take
    // longer than three seconds and none of which the bar should take back
    // from under them (2026-09-30). The ✕ is how it closes from here.
    stopCollapse();
  };
  // Clicks crossing a closed shadow root are retargeted to its host on some
  // ATS pages.  Let the host restore the badge as well as its inner button.
  host.addEventListener("click", () => { if (bannerCollapsed) expand(); });
  // After the job is queued the bar counts down, then becomes the persistent
  // corner badge. Restoring it only expands this DOM; it never screens again.
  let closing = 0;
  let collapseTimer = null;
  // Whether this bar is on the screen because the person put it there. One
  // that opened itself folds itself away; one they opened does not.
  let openedByHand = false;
  const stopCollapse = () => {
    clearTimeout(collapseTimer);
    collapseTimer = null;
    cancelAnimationFrame(closing);
    closeBar.style.width = "0";
  };
  const autoCollapse = () => {
    stopCollapse();
    const from = performance.now();
    collapseTimer = setTimeout(() => {
      collapseTimer = null;
      cancelAnimationFrame(closing);
      closeBar.style.width = "100%";
      collapse();
    }, AUTO_COLLAPSE_MS);
    const tick = () => {
      const done = Math.min(1, (performance.now() - from) / AUTO_COLLAPSE_MS);
      closeBar.style.width = `${done * 100}%`;
      if (done < 1 && collapseTimer) closing = requestAnimationFrame(tick);
    };
    closing = requestAnimationFrame(tick);
  };
  // Reached from a click and from nowhere else. On Jobright's posting page
  // that click presses their Apply, which is how the employer URL - the
  // only URL worth queueing - is reached at all; on an employer page it
  // queues this posting.
  const act = async () => {
    add.disabled = true;
    // A job that has gone in, and can still be taken straight back out: the
    // decision is the person's, so the button that added it is the button
    // that undoes it. It drops the job (the same reject the review page
    // makes) and stops the tailoring with it.
    const landed = async (queuedId) => {
      added = queuedId;
      add.disabled = false;
      add.classList.remove("done");
      add.dataset.act = "undo";
      label("Undo · take it out again");
      // The review tab is pointed at the job but stays where it is: the
      // person is reading the posting, and the fill opens its own tab
      // when the tailoring is done. Only "Open in autopilot" focuses.
      await openReview(queuedId, { focus: false });
      autoCollapse();
    };
    if (onJobright) {
      label("Opening job page…");
      // The same press, carried to the tab that can queue it. See the big
      // button's own call for why this is not the countdown returning.
      if (postingId()) {
        await post("/prefs", { jr_id: postingId(), auto_fill: autoFill(),
                               premium: usePremium, asked: true }).catch(() => {});
      }
      if (await openJob()) {
        // Their Apply opens the employer's page in a tab of its own, and
        // that page is where the job goes in: its URL is the one worth
        // queueing (the server refuses a jobright.ai URL outright), and the
        // decision belongs on the page being looked at. Nothing is awaited
        // here - the poll exists only so this bar can offer Undo if the job
        // does land, and awaiting it left this button dead for the ninety
        // seconds it waits.
        label("Opened — adding it there…");
        waitQueued().then((id) => { if (id) landed(id); });
      } else {
        label("No Apply button found");
      }
      return;
    }
    label("Adding…");
    const result = await addToQueue(autoFill(), usePremium);
    label(result.label);
    // The pipeline is now tailoring; keep the core purple until the
    // browser agent (or a failure) says otherwise, so the badge never
    // reads "done" while something is still running for this job.
    if (result.created) {
      AUTOMATION.pipeline = true;
      window.__autopilotAutomation("working", "Added; tailoring the resume");
    }
    add.classList.add("done");
    if (result.id) await landed(result.id);
    // This tab has done its job once the pipeline has it: the fill opens
    // its own tab on approve. Only a job created just now; an "already in
    // autopilot" tab may be the one a fill is working on.
    // A tab the person is standing in front of is never taken away from
    // them: they clicked the badge on a page they are reading, and the
    // posting is the page. Only the tab Apply opened goes.
    if (!onJobright && result?.created && !askedByHand) closeSelf();
  };
  // A bar being read is a bar in use: hovering it, or tabbing into it, holds
  // the countdown, and leaving starts it again. Without this the three
  // seconds ran out under the cursor of someone halfway through the reason
  // a job was rejected.
  shell.addEventListener("mouseenter", stopCollapse);
  shell.addEventListener("focusin", stopCollapse);
  const leave = () => { if (!bannerCollapsed && !pending && !openedByHand) autoCollapse(); };
  shell.addEventListener("mouseleave", leave);
  shell.addEventListener("focusout", leave);

  // Every banner folds itself away, not only the ones that had nothing left
  // to ask. The bar's job is to say the verdict; the badge is what stays,
  // in the verdict's own colour, and one tap brings the bar back with the
  // same result and no second screen. The one exception is the only state
  // that is not a finished answer: a screen still running. (It used to be
  // two - the other was a countdown about to queue the job, and there is
  // no countdown any more.)
  //
  // The applied block used to be held out of this, because a line inside a
  // bar that folded away after five seconds is how one job was applied for
  // twice. It is not a line inside a bar any more - it is the loudest thing
  // the banner draws, and the badge behind it keeps the colour - so it
  // folds like everything else.
  if (!pending) {
    autoCollapse();
  }

  // The applied block is a div acting as a button, so it needs the keys a
  // button would have had.
  shadow.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    const el = e.target?.closest?.("[role=button][data-act]");
    if (!el) return;
    e.preventDefault();
    el.click();
  });
  shadow.addEventListener("click", (e) => {
    const action = e.target?.closest?.("[data-act]")?.dataset?.act;
    if (action === "close") {
      // The host's own listener re-expands a collapsed bar on any click
      // that reaches it; this one must not.
      e.stopPropagation();
      clearTimeout(collapseTimer); collapseTimer = null;
      cancelAnimationFrame(closing);
      collapse();
    }
    if (action === "expand") expand();
    if (action === "again") screen({ force: true });
    if (action === "open" && seen) openReview(seen.id);
    if (action === "start") {
      e.stopPropagation();
      stopCollapse();
      startNow(job && job.picked ? job.id : null).then(() => {
        // The server may have named a job this page did not know it was.
        // Then the banner owes it the rest: its files, its names and its
        // thread, which only a re-render puts on.
        if (jobHint && !job) render(result);
      });
    }
    if (action === "pickjob") {
      // The person has said which job this page is: from here it behaves
      // exactly like a page the screen matched - the files, the context, the
      // Finder button and the thread - and Start now carries the id, so the
      // fill runs in this tab rather than opening one on the job's own URL.
      e.stopPropagation();
      stopCollapse();
      const id = e.target.closest("[data-job]").dataset.job;
      jobHint = START.candidates.find((c) => c.id === id) || { id };
      START.candidates = [];
      START.note = "Using this job's files. Press Start now when the form is on the screen.";
      render(result);
    }
    if (action === "reveal" && job) { e.stopPropagation(); revealFiles(shadow, job.id); }
    if (action === "ask-open") { e.stopPropagation(); stopCollapse(); ASK.open = true; paintAsk(); }
    if (action === "ask-send") { e.stopPropagation(); stopCollapse(); askThread(job); }
    if (action === "remember") { e.stopPropagation(); rememberPicked(); }
    if (action === "opus") {
      // Armed, not pressed: the model is a decision about the job, not an
      // instruction to add it, so this never adds anything by itself.
      e.stopPropagation();
      usePremium = !usePremium;
      const button = shadow.querySelector("button.opus");
      button.setAttribute("aria-pressed", String(usePremium));
      button.textContent = usePremium ? "Opus on" : "Use Opus";
      // On Jobright's own page the employer tab its Apply opens is what
      // queues the job, so the choice goes ahead by posting id, the same
      // way auto-approve does.
      if (onJobright && postingId()) post("/prefs", { jr_id: postingId(), premium: usePremium }).catch(() => {});
    }
    if (action === "add" && !add.disabled) act();
    if (action === "undo" && added && !add.disabled) {
      e.stopPropagation();
      stopCollapse();
      add.disabled = true;
      label("Taking it out…");
      undo(added).then((ok) => {
        label(ok ? "Taken out of autopilot" : "Could not take it out");
        added = null;
        add.dataset.act = "add";
      });
    }
  });
  document.documentElement.appendChild(host);
  placeStart();
  // The tailored files are offered for any match, certain or likely: the
  // form a person opened by hand is exactly where "put" is wanted, and the
  // match there is often `confident` rather than `high`. Nothing is put on
  // the form without the click.
  if (job) loadFiles(shadow, job.id, job.status);
  // One press, on the posting, is the whole decision - and this is the tab
  // that can act on it. See `tagId` and `app.ASKED_PREFS`.
  //
  // `canAdd` is not the gate: it is true for a `reject`, a failed screen
  // and a `not_a_job` page, because the *button* is offered there on
  // purpose - the person overruling a verdict is a decision they are
  // allowed to make, with the verdict in front of them. Honouring a press
  // made on the posting is not that: the page this tab landed on may not be
  // the posting at all. A `jr_id` tag survives a redirect, so a marked tab
  // that follows single sign-on to USC or an employer's SuccessFactors
  // login still carries it - which is exactly the three bad captures the
  // countdown made, and a press behind them would not make them good.
  // `QUEUEABLE` is therefore the same narrow pair the countdown itself
  // acted on, and anything else waits for a click on this page.
  if (canAdd && QUEUEABLE.has(verdict) && !seen && !onJobright && !cold) {
    honourTheAdd(label, act);
  }
  // The cold button's own press, carried into the banner it rendered into.
  // Unlike the Jobright tag, this is a press made on *this* page by the
  // person reading it, so the verdict is not the gate: a `reject`, a failed
  // screen or a page that does not read as a job is theirs to overrule, and
  // the banner puts the verdict in front of them either way. What does gate
  // it is the pair that is never a posting: a job already in autopilot has
  // no Add to press (`canAdd`), and a sign-in page is a wall however it was
  // reached - the two bad captures the old countdown made were both walls.
  if (coldAdd && canAdd && !seen && !onJobright) {
    coldAdd = false;
    if (looksLikeSignIn()) {
      label("A sign-in page, not a posting — press Add when the job is up");
    } else {
      stopCollapse();
      label("Adding…");
      act();
    }
  }
}


// The verdicts that a press made on the posting may be spent on. A screen
// that failed, a page that does not read as a job, and an auto-reject are
// all pages to put in front of the person, not pages to queue unseen.
const QUEUEABLE = new Set(["ok", "caution"]);


// The `jr_id` Jobright's extension puts on the employer URL its Apply
// opens. `postingId()` reads Jobright's own `/jobs/info/<id>` path, which
// this tab does not have; the tag is the same id by another route, and it
// is what pairs the two tabs for `/queued` and `/asked`.
function tagId() {
  try {
    return new URL(location.href).searchParams.get("jr_id") || "";
  } catch {
    return "";
  }
}


// Queue this posting because the person already pressed Add, on Jobright,
// seconds ago.
//
// This is not the countdown coming back, and the difference is the whole
// point of the 2026-10-30 change. The countdown acted on a *verdict*: it
// queued whatever page it found itself marked in, which is how a USC
// single-sign-on page, a SuccessFactors sign-in and a bare careers
// homepage became jobs. This acts on a *press*, made by the person, on the
// posting they were reading, and spent once (`/asked` pops it, and it
// expires). No press, no tag, or a tag the server does not recognise, and
// this tab waits for a click like every other page in the browser.
async function honourTheAdd(label, act) {
  const id = tagId();
  if (!id) return;
  // A tab Jobright opened is one the person asked for by pressing Apply
  // over there, whether or not they used our own Add button to do it
  // (2026-10-31, asked for: "As soon as we open from Jobright, I want it
  // to be auto-applied... else I have to manually add every job, which is
  // again pointless"). Only Jobright's extension puts the tag on, so
  // carrying it *is* the redirect, and that is the whole gate for queueing.
  //
  // What stops this being the countdown again is the pair of checks either
  // side of it: `QUEUEABLE` at the call site, and `looksLikeSignIn` here.
  // The tag survives a redirect, so a marked tab that followed single sign-
  // on still has it - and a sign-in page is a wall, not a posting, however
  // it was reached and whoever asked for it. That is how `login.usc.edu`
  // and a SuccessFactors sign-in became jobs the first time.
  if (looksLikeSignIn()) return;
  // Whether they pressed our Add or Jobright's own Apply changes what this
  // says and nothing else. Consumed either way, so a press cannot sit in
  // the server's memory until it expires.
  let pressed = false;
  try {
    const res = await post("/asked", { jr_id: id });
    pressed = !!(await res.json()).asked;
  } catch {
    // The server is away - then `act()` has nowhere to queue to either, and
    // it will say so on the button rather than failing silently here.
  }
  label(pressed ? "Adding — you pressed Add on the posting"
                : "Adding — opened from Jobright");
  await act();
}


// Does this page want a password rather than an application?
//
// Deliberately small and deliberately strict. `browser/signin.py` is the
// full version of this question and it takes a page apart to answer it;
// here the only job is to refuse to queue a wall, so the two signals that
// are never wrong on their own are enough: somewhere to type a password,
// or an identity provider's host. A posting that merely has "Sign In" in
// its header is not a wall - every Workday tenant's does - which is why no
// text is matched at all (the 2026-09-30 lesson, applied).
//
// It only ever refuses, and the asymmetry is the point: a false positive
// costs one click on the Add button, a false negative costs a sign-in page
// queued as a job.
//
// **This list must stay a superset of `signin.IDENTITY_HOSTS`**, and a test
// asserts it: the server's list is where a host is added after a bad
// capture, and a host known there but missing here would be refused by the
// fill and queued by the page. The extras below are ours: `login.usc.edu`
// and `shibboleth` are the university single sign-on this actually hit.
const IDENTITY_HOSTS = [
  // Kept in step with browser/signin.py.
  "accounts.google.com", "login.microsoftonline.com", "login.live.com",
  "appleid.apple.com", "www.linkedin.com/oauth", "linkedin.com/oauth",
  "github.com/login", "okta.com", "oktapreview.com", "auth0.com",
  "onelogin.com", "pingidentity.com", "signin.aws.amazon.com",
  "id.workday.com", "authn.workday.com", "wd1.myworkday.com/wday/authgwy",
  // Ours, from what this has met: university single sign-on.
  "login.usc.edu", "shibboleth", "secure.login.gov", "login.salesforce.com",
];

function looksLikeSignIn() {
  const host = location.hostname.toLowerCase();
  // The same three comparisons `signin.host_is_identity` makes, and for the
  // same reason: some entries are a host and some are a host with a path
  // ("github.com/login"), so the whole URL has to be looked at too. A bare
  // `host.includes(h)` was tried and is wrong - it makes "okta.com" match
  // "notokta.command.example.com".
  const whole = location.href.toLowerCase();
  if (IDENTITY_HOSTS.some((h) => host === h || host.endsWith(`.${h}`) || whole.includes(h))) {
    return true;
  }
  return !!document.querySelector('input[type="password"]');
}

// Hand this page to the autopilot, now, because the person can see that the
// form is on the screen - signed in, past the account wall, the fields in
// front of them. This is the whole of the 2026-10-30 change from the other
// side: nothing starts a fill by itself any more, so something has to start
// it, and the one thing that knows the form is ready is the person looking
// at it.
//
// `job_id` is sent only when they have picked the job themselves off the
// candidate list; then the server runs the fill in **this** tab rather than
// opening one on the job's recorded URL, which for a job captured from a
// listing site is the listing and not the form.
//
// Three answers, all of which have to be said out loud:
//   started  - the autopilot has it.
//   synced   - a fill was already running for this job and this one joined
//              it rather than starting a second. Without saying so, a
//              second press reads as the first one having failed.
//   no_job   - this page is not a job in autopilot. Then the in-flight jobs
//              come back as candidates, and Add is the other way out.
async function startNow(jobId) {
  if (START.busy) return;
  START.busy = true; START.note = ""; START.candidates = []; START.state = "";
  paintStart();
  try {
    const body = jobId ? { url: location.href, job_id: jobId } : { url: location.href };
    const res = await post("/start-now", body);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `server ${res.status}`);
    START.state = data.state || "";
    START.note = data.note || (
      data.state === "synced" ? "A fill was already running for this job; this tab joined it."
      : data.state === "started" ? "The autopilot has this form. It stops at the filled form."
      : "");
    // `synced` has to be said whatever the server worded it as: without it a
    // second press reads as the first one having failed, and two fills on one
    // form is the defect the one-at-a-time rule exists for.
    if (data.state === "synced" && !/joined|already/i.test(START.note)) {
      START.note = `A fill was already running for this job; this tab joined it. ${START.note}`.trim();
    }
    if (data.state === "no_job") {
      START.candidates = Array.isArray(data.candidates) ? data.candidates : [];
      START.note = START.candidates.length
        ? "This page is not a job in autopilot. Which of these is it? Or press Add to put it in."
        : "This page is not a job in autopilot, and nothing is in flight. Press Add to put it in.";
    }
    // A job the server named is this page's job from now on: its files and
    // its thread are what the banner should be offering.
    if (data.job_id && data.state !== "no_job" && (!jobHint || jobHint.id !== data.job_id)) {
      jobHint = { ...(jobHint || {}), id: data.job_id };
    }
  } catch (err) {
    START.note = `Could not start: ${err.message}`;
  } finally {
    START.busy = false;
    paintStart();
  }
}

// One turn of the job's own conversation. The endpoint is the review page's
// thread, so the question and its answer are kept in the one place the job's
// conversation lives and are there afterwards; the reply carries every turn,
// so the transcript fills itself in from the server rather than from here.
//
// A visa / sponsorship / work-authorisation question is refused server-side
// (`guard.ProtectedField` -> 422), and the refusal is shown as it comes
// back. There is nothing to work around: that answer is the person's.
async function askThread(job) {
  if (!job || ASK.busy) return;
  const text = (ASK.draft || "").trim();
  if (!text) { ASK.note = "Type the question first."; paintAsk(); return; }
  ASK.busy = true; ASK.note = "Answering…";
  // Shown at once, so the thread reads as a conversation rather than as a
  // box that goes quiet; the server's own copy replaces it on the reply.
  ASK.turns = [...ASK.turns, { kind: "question", text }];
  ASK.draft = "";
  paintAsk();
  try {
    const res = await post(`/review/${job.id}/thread`, { text, action: "ask" });
    const data = await res.json();
    if (!res.ok) {
      // 422 is the protected-question refusal, and it is the answer, not an
      // error to retry: it is shown in the thread in red.
      ASK.turns = [...ASK.turns, { kind: "error", text: data.detail || `server ${res.status}` }];
      ASK.note = "";
      return;
    }
    if (Array.isArray(data.turns)) ASK.turns = data.turns;
    ASK.note = "";
  } catch (err) {
    ASK.turns = [...ASK.turns, { kind: "error", text: `Could not ask: ${err.message}` }];
  } finally {
    ASK.busy = false;
    paintAsk();
  }
}

// The two named copies in Finder, with the resume selected: for the forms
// whose screener never reaches the assistant and whose files are quicker
// dragged in by hand. The same endpoint the review page's own button uses,
// so the named copies are made if the fill has not made them yet.
async function revealFiles(shadow, jobId) {
  const row = shadow.getElementById("files");
  const say = (text) => {
    const note = row?.querySelector(".hint");
    if (note) note.textContent = text;
  };
  try {
    const res = await post(`/review/${jobId}/reveal`, {});
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `server ${res.status}`);
    say(data.opened ? `Showing ${data.revealed?.join(", ") || "the folder"} in Finder.`
                    : (data.note || "Could not open Finder."));
  } catch (err) {
    say(`Finder: ${err.message}`);
  }
}

// The tailored PDFs for this job, as chips. Drag one onto the form's own
// file slot (the drag carries the File, so a drop zone or an <input
// type=file> takes it like a file from the desktop); or click it to save
// the file to Downloads for the picker. Nothing here touches the form by
// itself: the person chooses the slot.
async function loadFiles(shadow, jobId, status) {
  const row = shadow.getElementById("files");
  if (!row) return;
  let data;
  try {
    const res = await post(`/review/${jobId}/files`, {});
    data = await res.json();
    if (!res.ok) throw new Error(data.detail || `server ${res.status}`);
  } catch (err) {
    setHTML(row, `<span class="hint">Files: ${esc(err.message)}</span>`);
    return;
  }
  const entries = [["resume", "Resume"], ["cover_letter", "Cover letter"]].filter(([k]) => data[k]);
  if (!entries.length) { setHTML(row, `<span class="hint">No tailored files yet.</span>`); return; }
  // Which documents this job carries, under the names they go on a form
  // with. The chips say what they do by being chips; this line says what
  // they *are*, which is the one thing a person checking a filled form
  // wants and had to open the review page for.
  const what = `<span class="what">On this job: ${entries
    .map(([k, label]) => `${label.toLowerCase()} <b>${esc(data[k].name)}</b>`).join(" · ")}</span>`;
  setHTML(row, what + entries.map(([k, label]) =>
    `<span class="chip" draggable="true" data-key="${k}"
       title="${esc(data[k].name)} — drag onto a file slot, or click to download"><b>⇣</b> ${label} · ${esc(data[k].name)}</span>` +
    `<span class="chip put" data-put="${k}" title="Set the form's ${label.toLowerCase()} file input to this file"><b>→</b> put</span>`).join("")
    + `<button class="finder" data-act="reveal" type="button"
         title="Open the application folder in Finder with the resume selected, for a form whose own picker is the only way in">Show files in Finder</button>`
    + `<span class="hint"></span>`);
  const files = {};
  for (const [k] of entries) {
    const bytes = Uint8Array.from(atob(data[k].b64), (c) => c.charCodeAt(0));
    files[k] = new File([bytes], data[k].name, { type: data[k].type });
  }
  for (const chip of row.querySelectorAll(".chip")) {
    const file = files[chip.dataset.key];
    chip.addEventListener("dragstart", (e) => {
      // Three channels, because a drop target may read any of them.
      // `items.add(file)` is the one that works: measured in Chrome
      // (tests/test_dom.py), the drag carries the document's own bytes and
      // an in-page dropzone or file input takes it exactly as it takes a
      // file from the desktop. What it does **not** do is reach a target
      // outside the browser - Finder, Mail - because only Chrome's own
      // `DownloadURL` channel leaves the renderer, and that writes a
      // download rather than handing over a file. Hence the Finder button
      // beside the chips: dragging into another application is its job.
      e.dataTransfer.effectAllowed = "copy";
      try { e.dataTransfer.items.add(file); } catch { /* older Chrome */ }
      const url = URL.createObjectURL(file);
      e.dataTransfer.setData("DownloadURL", `${file.type}:${file.name}:${url}`);
      e.dataTransfer.setData("text/plain", file.name);
    });
    chip.addEventListener("click", () => {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(file); a.download = file.name;
      document.body.appendChild(a); a.click(); a.remove();
    });
  }
  for (const button of row.querySelectorAll(".chip.put")) {
    button.addEventListener("click", async () => {
      const key = button.dataset.put;
      // Two writers on one slot is how a Workday form ended up carrying the
      // same tailored resume twice, 843ms apart: the fill uploaded it and
      // this chip put it, and whichever was second added a row rather than
      // replacing one (2026-10-11, Vermeer). While the autopilot is on this
      // job the slot is its to write; the chip is for the forms it cannot
      // finish, and it says so instead of quietly adding a second file.
      if (status === "filling") {
        setHTML(button, `<b>✕</b> autopilot is filling this now`);
        return;
      }
      const result = await putFile(files[key], key);
      setHTML(button, `<b>${result.ok ? "✓" : "✕"}</b> ${esc(result.note)}`);
      // A slot the person filled from here is as much ours as one the fill
      // filled, and it was the one path with no logo in front of it, so a
      // form done this way looked untouched (2026-10-04, asked for). The
      // input is tagged above; the server evaluates the same MARK_FN the
      // fill uses, in this tab, so the mark promises the same thing and
      // goes the same way when the file changes.
      if (!result.ok) return;
      try {
        await post(`/review/${jobId}/mark`, { url: location.href, key });
      } catch { /* the logo is advice; the file is on the form either way */ }
    });
  }
}

// Takes whatever is already on a slot off it, so putting a file replaces
// rather than adds. Scoped to the slot the file is going on: the controls
// are searched upward from that input and no further than the block that
// holds it, so a resume's bin can never answer for the cover letter.
//
// Workday's is a bin icon with no text in it at all - what names it is
// `data-automation-id="delete-file"` - which is why the coded names are
// read as well as the spoken ones. `CLEAR_NOT` is the other half: only a
// control that plainly clears is pressed, never "Replace" or "Change"
// (a native file chooser on some systems) and never anything that reads as
// finishing or sending the application.
// Which slot a file input belongs to, when nothing near it says. Workday's
// resume input sits inside "Drop files here or Select files" and so does its
// cover-letter input, so `describe` below read the two as identical and the
// chip fell through to asking the person to choose between two lines of the
// same words. What tells them apart is further up: the heading
// (<h4>Resume/CV</h4>) or the slot's own `data-fkit-id`
// ("resumeAttachments--attachments", whose camelCase hides the word from a
// plain match, hence `words`). Returns the names of the first ancestor that
// mentions either slot, so the container holding *both* headings is never
// reached and cannot answer for either.
const SLOT_CLIMB = 10;
const SLOT_SAYS = /resume|\bcv\b|curriculum|cover/i;

function slotWords(input) {
  const text = (el) => (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
  const words = (v) => String(v || "")
      .replace(/([a-z0-9])([A-Z])/g, "$1 $2").replace(/[^A-Za-z0-9]+/g, " ").trim();
  let el = input.parentElement;
  for (let i = 0; i < SLOT_CLIMB && el; i++, el = el.parentElement) {
    const names = [
      ...[...el.querySelectorAll("h2, h3, h4, label, legend")].map(text),
      ...["data-fkit-id", "data-automation-id", "aria-label", "id"].map((a) => words(el.getAttribute(a))),
    ].filter((n) => n && n.length <= 80);
    if (names.some((n) => SLOT_SAYS.test(n))) return names.join(" ");
  }
  return "";
}

const CLEAR_IS = /\b(remove|delete|clear)\b|^[×✕x]$/i;
const CLEAR_NOT = /submit|send|finish|complete|appl(y|ication)|continue|next|save|sign\s*in|account/i;
const CLEAR_MAX = 6;
const CLEAR_SETTLE_MS = 400;   // Workday takes the row off on a state update

async function clearSlot(input) {
  const text = (el) => (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
  const spoken = (el) => [el.getAttribute("aria-label"), el.getAttribute("title"), text(el)]
      .filter(Boolean).map((v) => v.trim());
  const coded = (el) => [el.getAttribute("data-automation-id"), el.getAttribute("name"), el.id,
                         (el.className || "").toString()].filter(Boolean).map((v) => String(v).trim());
  const clears = (el) => {
    const names = [...spoken(el), ...coded(el)].filter((n) => n.length <= 60);
    return names.some((n) => CLEAR_IS.test(n)) && !names.some((n) => CLEAR_NOT.test(n));
  };
  // The smallest block above the input that holds a clearing control. Small
  // first: the point is to stay inside this slot.
  let block = input.parentElement, found = [];
  for (let i = 0; i < 8 && block; i++, block = block.parentElement) {
    found = [...block.querySelectorAll("button, [role=button], a")].filter(clears);
    if (found.length) break;
  }
  let gone = 0;
  for (let round = 0; round < CLEAR_MAX && found.length; round++) {
    const before = found.length;
    try { found[0].click(); } catch { break; }
    // The row goes on a re-render, not on the click, so the count is read
    // again only after the page has had a moment to drop it. Read too soon
    // and every slot looks like one that would not clear.
    await new Promise((done) => setTimeout(done, CLEAR_SETTLE_MS));
    const left = [...block.querySelectorAll("button, [role=button], a")].filter(clears);
    if (left.length >= before) break;         // the press changed nothing
    gone++;
    found = left;
  }
  return gone;
}

// Sets a file input on the page to the tailored file, the way a drop
// would. Which input: the one whose name, id, or label reads as the file's
// kind; else the only one; else the person picks from a numbered list.
// Fires the events a real pick fires so the form notices.
async function putFile(file, key) {
  const inputs = [];
  const walk = (root) => {
    for (const el of root.querySelectorAll("input[type=file]")) inputs.push(el);
    for (const host of root.querySelectorAll("*")) if (host.shadowRoot) walk(host.shadowRoot);
  };
  walk(document);
  if (!inputs.length) {
    // Greenhouse (and Ashby) drop the input once a file is on the slot, so
    // "no file input on this page" is usually not an empty page at all - it
    // is a slot that is already taken, which is a different instruction to
    // the person. The remove control is theirs to press: nothing here
    // presses anything on a form.
    const taken = [...document.querySelectorAll("button, a, [role=button]")]
      .some((el) => /^\s*(remove|delete|clear)\b/i.test((el.innerText || el.getAttribute("aria-label") || "").trim()));
    return { ok: false, note: taken ? "a file is already on the slot — remove it, then put" : "no file input on this page" };
  }
  // What the control says about itself and what the page says right beside
  // it, kept apart from what `slotWords` finds by climbing. Both are used;
  // the difference matters only in the rescue below.
  const own = (el) => {
    const label = el.labels && el.labels.length ? [...el.labels].map((l) => l.innerText).join(" ") : "";
    const near = el.closest("label, fieldset, section, div")?.innerText?.slice(0, 80) || "";
    return `${el.name || ""} ${el.id || ""} ${el.getAttribute("aria-label") || ""} ${label} ${near}`.replace(/\s+/g, " ").trim();
  };
  const describe = (el) => `${own(el)} ${slotWords(el)}`.replace(/\s+/g, " ").trim();
  const want = key === "resume" ? /resume|\bcv\b|curriculum/i : /cover\s*letter/i;
  const other = key === "resume" ? /cover\s*letter/i : /resume|\bcv\b|curriculum/i;
  // Ashby, Workday and Lever all offer "upload a resume and we will fill the
  // form in for you". That input is not a slot: what it does is parse the
  // file over the fields rather than attach it, and it is the *first* file
  // input on an Ashby form with "resume" twice in the sentence beside it, so
  // it won every time (2026-10-06). `guard.describes_parse_slot` is the same
  // question asked on the Python side; this is the same answer in the page.
  const parse = /auto\s*-?\s*fill|\bparse\b|\bprefill\b|\bquick\s*apply\b|apply\s+with\s+(your\s+)?(resume|cv)/i;
  const all = inputs.map((el) => ({ el, text: describe(el), self: own(el) }));
  let described = all.filter((d) => !parse.test(d.text));
  if (!described.length) {
    // Every file input on the page read as the system's own "upload a resume
    // and we will fill the form in for you". That is usually true, and is
    // why the filter exists - but `slotWords` climbs ancestors looking for
    // the heading that names a slot, and a heading far enough up names the
    // *panel*, which on a Workday tenant can carry that offer above the real
    // Resume/CV dropzone. Before refusing, the question is asked again of
    // the control's own identity and the block beside it - which is where
    // Ashby's parser describes itself ("Autofill from resume. Upload your
    // resume here to autofill key application fields.") and where a real
    // dropzone says only "Drop files here". So a page with a genuine parser
    // still refuses, and a real slot disqualified by a caption above it does
    // not (2026-10-30).
    described = all.filter((d) => !parse.test(d.self));
  }
  if (!described.length) {
    return { ok: false, note: "the only upload here fills the form in for you — not the resume slot" };
  }
  let target = described.find((d) => want.test(d.text) && !other.test(d.text))
    || (described.length === 1 && !other.test(described[0].text) ? described[0] : null);
  if (!target) {
    const menu = described.map((d, i) => `${i + 1}. ${d.text.slice(0, 60) || "(unlabelled file input)"}`).join("\n");
    const pick = parseInt(prompt(`Which slot for ${file.name}?\n\n${menu}`, "1"), 10);
    if (!(pick >= 1 && pick <= described.length)) return { ok: false, note: "cancelled" };
    target = described[pick - 1];
  }
  // Workday keeps the input *and* the attachment it already holds, and its
  // dropzone takes more than one file - so assigning `input.files` adds a
  // row rather than replacing one, and three presses of this chip left three
  // copies of the same tailored resume on one slot (2026-10-08, reported).
  // On Greenhouse and Ashby the input vanishes once a file is on, so this
  // path never had to clear anything and never learned how. The fill's own
  // side has done it since 2026-09-30 (`engine.remove_attached`); this is
  // the same step, by the person's click.
  const cleared = await clearSlot(target.el);
  try {
    const dt = new DataTransfer();
    dt.items.add(file);
    target.el.files = dt.files;
    // The tag the logo hangs off. Written before the events, so a form that
    // re-renders on change still has it on the input that was set.
    target.el.setAttribute("data-autopilot-ref", key === "resume" ? "put-resume" : "put-cover-letter");
    target.el.dispatchEvent(new Event("input", { bubbles: true }));
    target.el.dispatchEvent(new Event("change", { bubbles: true }));
    const said = cleared ? `replaced ${cleared > 1 ? `${cleared} files` : "the file"} in` : "put in";
    return { ok: true, note: `${said} “${target.text.slice(0, 30) || "file input"}”` };
  } catch (err) {
    return { ok: false, note: err.message };
  }
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[ch]);
}

// Close this tab. The injector closes only an employer tab Apply opened and
// the extension only the sender; window.close is the fallback, which a tab
// no script opened ignores.
// Take a job back out of autopilot, seconds after it went in. The server
// side is the review page's own reject, so the job keeps its folder and its
// record: changing your mind is part of the record, not a deletion.
async function undo(jobId) {
  try {
    const res = await post(`/review/${jobId}/reject`,
                           { reason: "changed_my_mind", note: "cancelled from the posting page" });
    return res.ok;
  } catch {
    return false;
  }
}


async function closeSelf() {
  if (typeof window.__autopilotRequest === "function") {
    try {
      const result = await post("/__close", {});
      if (result.ok) return;
    }
    catch { /* fall through */ }
  }
  if (typeof chrome !== "undefined" && chrome.runtime?.sendMessage) {
    try {
      const result = await chrome.runtime.sendMessage({ type: "close-me" });
      if (result?.ok) return;
    } catch { /* extension background unavailable */ }
  }
  window.close();
}

// Focus the existing Autopilot tab and show this job, or create the tab when
// none exists. The injector and extension background own tabs; window.open is
// only the fallback when neither bridge is available.
async function openReview(id, { focus = true } = {}) {
  const url = `${SERVER}/#${id}`;
  if (typeof window.__autopilotRequest === "function") {
    try {
      const result = await post("/__open", { url, focus });
      if (result.ok) return;
    }
    catch { /* fall through */ }
  }
  if (typeof chrome !== "undefined" && chrome.runtime?.sendMessage) {
    try {
      const result = await chrome.runtime.sendMessage({ type: "open-autopilot", url, focus });
      if (result?.ok) return;
    } catch { /* extension background unavailable */ }
  }
  // window.open cannot open behind the current tab; when the tab must not
  // change and neither bridge is there, the review page is left alone.
  if (focus) window.open(url, "_blank");
}

// Talks to the local server. When browser/inject.py put this script here it
// also bound `__autopilotRequest`, and the request goes out through it:
// Python does the HTTP, so the page's CSP, service worker, or a fetch
// override cannot get in the way (an ATS page's worker has swallowed the
// call before). As an extension content script there is no bridge, and
// the fetch is made from the isolated world as before.
let bridgeSeq = 0;
const bridgeWaiting = new Map();
window.__autopilotReply = (id, ok, status, body) => {
  const settle = bridgeWaiting.get(id);
  if (!settle) return;
  bridgeWaiting.delete(id);
  ok ? settle.resolve({ ok: status < 400, status, json: async () => body })
     : settle.reject(new Error(body));
};
function post(path, body) {
  if (typeof window.__autopilotRequest === "function") {
    const id = ++bridgeSeq;
    return new Promise((resolve, reject) => {
      bridgeWaiting.set(id, { resolve, reject });
      window.__autopilotRequest(JSON.stringify({ id, path, body }));
    });
  }
  return fetch(`${SERVER}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// Presses Jobright's own Apply button. That is a user-level click on the
// posting page, the same as the person pressing it: it opens the employer's
// page and nothing more. Nothing here ever presses anything on a form.
//
// Three things the old one-liner got wrong, each of which looked like
// "clicking Apply is broken":
//
// - It took the first element in document order whose text began with
//   "apply", including one that is hidden, disabled, zero-sized or off in a
//   collapsed filter panel ("Apply filters" starts with "apply" too), so the
//   press went somewhere nothing happens. The label now has to read as the
//   whole label of a visible, enabled control, and `NOT_APPLY` disqualifies a
//   filter, a save, a sign-in and anything that reads as *finishing* an
//   application.
// - `el.click()` is ignored by React on a primary control. The press is real
//   pointer and mouse events at the control's centre, the same way
//   `browser/open_apply.py` and the submit presser do it.
// - It pressed the moment the countdown ended. Jobright renders the posting
//   and then its own extension mounts the Apply panel over it, so the button
//   either was not there yet or was replaced right after the press - and a
//   press into a half-mounted panel is where the stray blank tabs came from.
//   `openJob` now waits for the control and settles before pressing.
const APPLY_LABEL = /^\s*(apply|apply now|apply online|apply externally|apply on company site|apply for this job|apply to this job|easy apply|start (your )?application)\s*[.>\u00bb\u2192]*\s*$/i;
// Never these, whatever else the label says: a filter, a saved search, a
// sign-in, or anything that reads as sending an application rather than
// starting one.
const NOT_APPLY = /filter|search|sort|reset|clear|save|later|sign ?in|log ?in|submit|send|finish|withdraw|cancel|another job|other job/i;
// How long to wait for the Apply control to mount, and how long to let the
// page settle once it has, before pressing it.
const APPLY_WAIT_MS = 6000;
const APPLY_POLL_MS = 250;
const APPLY_SETTLE_MS = 1500;

function applyControl() {
  const controls = document.querySelectorAll(
    'button, a, input[type=button], input[type=submit], [role=button]');
  for (const el of controls) {
    const label = (el.innerText || el.getAttribute("aria-label") || el.value || "").trim();
    if (!APPLY_LABEL.test(label) || NOT_APPLY.test(label)) continue;
    if (el.disabled || el.getAttribute("aria-disabled") === "true") continue;
    const rect = el.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) continue;
    const style = getComputedStyle(el);
    if (style.visibility === "hidden" || style.display === "none" || style.pointerEvents === "none") continue;
    return el;
  }
  return null;
}

// A real press, because a scripted `click()` on a React primary control
// does nothing at all and reports success.
function pressControl(el) {
  el.scrollIntoView({ block: "center", behavior: "instant" });
  const rect = el.getBoundingClientRect();
  const at = {
    bubbles: true, cancelable: true, composed: true, view: window,
    clientX: rect.left + rect.width / 2,
    clientY: rect.top + rect.height / 2,
    button: 0, buttons: 1,
  };
  el.dispatchEvent(new PointerEvent("pointerdown", at));
  el.dispatchEvent(new MouseEvent("mousedown", at));
  el.dispatchEvent(new PointerEvent("pointerup", { ...at, buttons: 0 }));
  el.dispatchEvent(new MouseEvent("mouseup", { ...at, buttons: 0 }));
  el.dispatchEvent(new MouseEvent("click", { ...at, buttons: 0 }));
}

async function openJob() {
  const until = performance.now() + APPLY_WAIT_MS;
  let button = applyControl();
  while (!button && performance.now() < until) {
    await new Promise((r) => setTimeout(r, APPLY_POLL_MS));
    button = applyControl();
  }
  if (!button) return false;
  // Jobright's extension mounts its own panel over the posting; pressing
  // into that while it is still arriving is how a blank tab opens.
  await new Promise((r) => setTimeout(r, APPLY_SETTLE_MS));
  button = applyControl() || button;
  if (!document.contains(button)) return false;
  pressControl(button);
  return true;
}

// The id in /jobs/info/<id>, which Jobright also tags onto the employer
// URL it opens as ?jr_id=<id>; the server pairs the two by it.
function postingId() {
  const match = location.pathname.match(/\/jobs\/info\/([A-Za-z0-9_-]+)/);
  return match ? match[1] : "";
}

// The queued job id once the employer page opened from this Jobright posting
// has added it, or null when it has not appeared within the wait.
async function waitQueued() {
  const id = postingId();
  if (!id) return null;
  const until = performance.now() + QUEUED_WAIT_MS;
  while (performance.now() < until) {
    try {
      const res = await post("/queued", { jr_id: id });
      const data = await res.json();
      if (data.queued && data.id) return data.id;
    } catch { /* server away; try again */ }
    await new Promise((r) => setTimeout(r, QUEUED_POLL_MS));
  }
  return null;
}

async function addToQueue(autoFill = null, premium = false) {
  try {
    const res = await post("/capture", {
      url: location.href,
      title: document.title,
      source: location.hostname,
      auto_fill: autoFill,
      premium,
      // The rendered text, for when the fetch sees a client-side shell or
      // an Apply that landed straight on the form.
      text: pageText(),
    });
    const data = await res.json();
    if (data.reason === "source_page") return { label: "Not a job page", id: null };
    return { label: data.created ? "Added" : "Already in autopilot", id: data.id || null, created: !!data.created };
  } catch (err) {
    return { label: `Failed: ${err.message}`, id: null };
  }
}

async function screen({ force = false, waited = 0 } = {}) {
  if (!isPosting()) return;
  // A filled form, or the confirmation it turned into, is not a posting
  // to screen: the verdict would paint over "submitted" and the screen
  // would run on a thank-you page.
  if (submissionWatched && !force && !notTheConfirmation()) return;
  const text = pageText();
  if (text.length < MIN_TEXT) {
    const url = location.href;
    if (waited < WAIT_MAX) {
      setTimeout(() => { if (location.href === url) screen({ force, waited: waited + 1 }); }, WAIT_MS);
      return;
    }
  }
  render({ summary: "" }, { pending: true });
  try {
    const res = await post("/screen", { url: location.href, title: document.title, text, force });
    const data = await res.json();
    if (!res.ok) {
      render({ verdict: "error", error: data.detail || `server ${res.status}` });
      return;
    }
    // A landing page from Apply may be a login wall or a redirect stub.
    // Say so rather than show nothing, so a missing verdict is never
    // mistaken for a clean one.
    render(data);
    // The server saw a confirmation page: this tab is done, never screen it again.
    if (data.verdict === "submitted") submissionWatched = true;
    if (data.seen && ["filled", "filling"].includes(data.seen.status)) watchSubmission(data.seen.id);
    // Jobright's copy of the posting outlives the page: the employer's
    // Apply may land on a bare form, and the tailor still needs a
    // description. Best effort, nothing waits on it.
    if (onJobright && postingId()) {
      post("/posting", { id: postingId(), url: location.href, title: document.title, text }).catch(() => {});
    }
  } catch (err) {
    render({ verdict: "error", error: `Is the server running? ${err.message}` });
  }
}

// ---- Add to autopilot, beside their own Apply ---------------------------
//
// Asked for 2026-10-30. With the countdown gone, adding a job is a click,
// and the click belongs where the decision is made: on the posting, next to
// the button the person already reaches for. The banner keeps its own Add
// for every other page; this is Jobright's posting page only, where Add
// cannot queue this URL (the server refuses a jobright.ai one) and so means
// "press their Apply, and add it from the page that opens".
//
// A sibling of their Apply, not something bolted on: it is inserted into
// Apply's own row and takes Apply's own height, radius, font and padding
// from the computed style, keeping only the assistant's purple so it is
// clear whose button it is. It never writes a width, a min-width or a
// margin onto one of their nodes.
const JR_ADD_ID = "job-autopilot-jr-add";
const JR_LABEL = "Add to autopilot";
// Jobright hard-codes a desktop minimum (min-width: 1200px on the main
// content) and the injector overrides it so the page reads at half a
// screen, which is where this button usually lives. There it has to give
// way rather than push: first the label shortens, and if the row still
// overflows the button leaves the row and floats under the badge. A button
// that scrolls their layout sideways is worse than one that floats.
const JR_LABEL_SHORT = "Add";
// Copied from their Apply so the two read as one row. Deliberately not
// colour: the colour is how you tell ours from theirs.
const JR_COPY = ["height", "minHeight", "borderRadius", "fontFamily", "fontSize",
                 "fontWeight", "paddingTop", "paddingBottom", "paddingLeft", "paddingRight"];

let jrAutoApprove = true;

function jrPrefs(fields) {
  const id = postingId();
  if (id) post("/prefs", { jr_id: id, ...fields }).catch(() => {});
}

// Returns the host and its root together: the root is closed, so Jobright's
// own stylesheet cannot reach in - and neither can we, from the host. Asking
// `host.shadowRoot` for a closed root answers null, which is how the first
// version of this mounted a button it could then never wire up.
function buildJobrightAdd() {
  const host = document.createElement("div");
  host.id = JR_ADD_ID;
  const shadow = host.attachShadow({ mode: "closed" });
  setHTML(shadow, `
    <style>
      :host { all: initial; }
      .wrap { display: inline-flex; align-items: center; gap: 6px; position: relative;
              font: 600 14px/1 "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace; }
      button { font: inherit; cursor: pointer; border: 2px solid #b48cff; box-sizing: border-box;
               background: #b48cff; color: #000; white-space: nowrap; }
      button.go { letter-spacing: .04em; }
      button.go:hover, button.go:focus-visible { background: #000; color: #b48cff; outline: none; }
      button.go:disabled { opacity: .6; cursor: default; }
      button.more { background: #000; color: #b48cff; width: 28px; height: 28px; padding: 0;
                    display: flex; align-items: center; justify-content: center; font-size: 13px; }
      button.more[aria-expanded="true"], button.more:hover { background: #b48cff; color: #000; }
      /* Absolutely positioned, so the decisions are reachable without the
         row growing a second line of controls. */
      .panel { display: none; position: fixed; z-index: 2147483647;
               background: #000; color: #fff; border: 2px solid #b48cff; padding: 8px 10px;
               font-size: 12px; min-width: 210px; }
      .panel.on { display: block; }
      .panel label { display: flex; align-items: center; gap: 6px; padding: 3px 0; cursor: pointer; }
      .panel input { accent-color: #b48cff; margin: 0; }
      .panel .why { color: #a0a0a0; font-size: 11px; display: block; margin-top: 4px; }
    </style>
    <span class="wrap">
      <button class="go" type="button" title="Screen and tailor this job. Nothing is submitted: the autopilot stops at the filled form.">${JR_LABEL}</button>
      <button class="more" type="button" aria-expanded="false" aria-label="How this job is tailored" title="How this job is tailored">⋯</button>
      <span class="panel" role="group">
        <label><input type="checkbox" class="appr" checked> auto-approve</label>
        <label><input type="checkbox" class="opus"> use Opus (costly)</label>
        <span class="why">Auto-approve starts the fill once the resume is tailored. It never submits.</span>
      </span>
    </span>`);
  return { host, root: shadow };
}

// Their Apply row, with the button in it and the overflow it caused undone.
// Measured rather than guessed: the row is read before and after, and a row
// that got wider than its own box hands the button back.
function fitJobrightAdd(host, apply, go) {
  const row = apply.parentElement;
  if (!row) return false;
  const style = getComputedStyle(apply);
  for (const name of JR_COPY) go.style[name] = style[name];
  // Never a width, a min-width or a margin on one of their nodes; only our
  // own host is told how to behave as a flex item.
  host.style.cssText = "display: inline-flex; flex: 0 0 auto; min-width: 0; max-width: 100%; margin-left: 8px;";
  apply.insertAdjacentElement("afterend", host);
  if (row.scrollWidth <= row.clientWidth + 1) return true;
  go.textContent = JR_LABEL_SHORT;
  if (row.scrollWidth <= row.clientWidth + 1) return true;
  // Even shortened it does not fit. Out of the row, under the badge, where
  // it costs their layout nothing.
  go.textContent = JR_LABEL;
  host.style.cssText = "position: fixed; top: 64px; left: 6px; z-index: 2147483646; display: inline-flex;";
  document.documentElement.appendChild(host);
  return true;
}

function mountJobrightAdd() {
  if (!onJobright || cold || !isPosting()) {
    document.getElementById(JR_ADD_ID)?.remove();
    return;
  }
  const apply = applyControl();
  const existing = document.getElementById(JR_ADD_ID);
  // Jobright replaces its posting without a navigation, so the button has
  // to be put back when their render takes it away - and moved when their
  // Apply is re-created somewhere else.
  if (existing) {
    // Already beside their Apply, or already floating because it would not
    // fit: either way it is placed. Re-measuring it every second would
    // flicker and would take the open panel with it. The cost is that a
    // window widened after it gave up the row keeps the floating button
    // until the posting changes, which is a worse-looking button rather
    // than a broken one.
    if (existing.style.position === "fixed") return;
    if (apply && existing.previousElementSibling === apply) return;
    existing.remove();
  }
  const { host, root } = buildJobrightAdd();
  const go = root.querySelector("button.go");
  if (apply) {
    if (!fitJobrightAdd(host, apply, go)) return;
  } else {
    // No Apply on the page (their render is not finished, or this posting
    // has none): floating rather than silently absent.
    host.style.cssText = "position: fixed; top: 64px; left: 6px; z-index: 2147483646; display: inline-flex;";
    document.documentElement.appendChild(host);
  }
  const more = root.querySelector("button.more");
  const panel = root.querySelector(".panel");
  const appr = root.querySelector("input.appr");
  const opus = root.querySelector("input.opus");
  appr.checked = jrAutoApprove;
  opus.checked = usePremium;
  more.addEventListener("click", () => {
    const on = panel.classList.toggle("on");
    more.setAttribute("aria-expanded", String(on));
    if (!on) return;
    // Fixed, and placed against the button's own box: an ancestor of theirs
    // with `overflow: hidden` would clip an absolutely positioned panel, and
    // their class names are hashed and rotate so there is nothing to except.
    const box = more.getBoundingClientRect();
    panel.style.top = `${Math.round(box.bottom) + 6}px`;
    panel.style.left = `${Math.max(6, Math.round(box.right) - 220)}px`;
  });
  appr.addEventListener("change", () => {
    jrAutoApprove = appr.checked;
    jrPrefs({ auto_fill: appr.checked });
  });
  opus.addEventListener("change", () => {
    usePremium = opus.checked;
    jrPrefs({ premium: opus.checked });
  });
  go.addEventListener("click", async () => {
    go.disabled = true;
    // The choices are sent before the press: the employer tab its Apply
    // opens is what queues the job, so they have to be on file by the time
    // that page calls /capture.
    // `asked` is the press itself, travelling to the tab that can act on
    // it: `/capture` refuses a jobright.ai URL, so the employer page is the
    // only one worth queueing, and without this one press here became two -
    // one here and one over there - which is worse than the countdown that
    // was removed. It is spent once and expires (`app.ASKED_TTL`).
    jrPrefs({ auto_fill: jrAutoApprove, premium: usePremium, asked: true });
    go.textContent = "Opening the job page…";
    if (!(await openJob())) { go.textContent = "No Apply button here"; return; }
    // Their Apply opens the employer's own page in a tab of its own, and
    // that page is where the job goes in: its URL is the one worth
    // queueing. The poll is only so this button can say when it landed.
    go.textContent = "Opened — adding it there…";
    const id = await waitQueued();
    if (id) {
      go.textContent = "Added ✓";
      openReview(id, { focus: false });
    }
  });
}

// ---- Jobright, decluttered ----------------------------------------------
//
// Asked for 2026-10-07. Jobright's window is now half the screen (the job
// Apply opens goes into the other half - `browser/inject.py`), and two of
// its three columns are not read while applying: the 72px nav rail on the
// left, and the right-hand third that holds saved filters, the Turbo advert
// and the copilot. Folding both away gives the job card - which is the part
// that is read - the whole width.
//
// This is CSS and nothing else. Nothing here queues, approves, fills or
// submits, and the page's own behaviour is untouched.
const JR_STYLE_ID = "job-autopilot-jobright-style";
// Jobright's class names are hashed and rotate with every build
// (`index_job-page-right__VlCL3`), so nothing may hang off a whole one; the
// stable half is the part before the hash.
const JR_HIDE = [
  '[class*="job-page-right"]',
  '[class*="recommend-right-content"]',
  '[class*="copilot-floating-root"]',
  '[class*="jobs-page-side-content"]',
  "aside.ant-layout-sider",
  // "< 25 applicants", which at half width is squeezed into a column one
  // letter wide down the side of the buttons. It is a stat, not a decision.
  '[class*="apply-time"]',
];
// Jobright hard-codes a desktop minimum - `min-width: 1200px` on the main
// content, 864px on the layout - so in a half-screen window the job card is
// simply cut off and the page scrolls sideways. Hiding the two columns is
// only half the job; the minimum has to go with them.
const JR_WIDEN = [
  '[class*="jobs-page-main-content"]',
  '[class*="jobs-page-main-left"]',
  '[class*="jobs-page-center"]',
  '[class*="jobs-list"]',
  "main.ant-layout-content",
  "div.ant-layout-has-sider",
];

function declutterJobright() {
  if (!onJobright || cold) return;
  const css = `
    ${JR_HIDE.join(", ")} { display: none !important; }
    ${JR_WIDEN.join(", ")} {
      min-width: 0 !important; max-width: 100% !important; width: auto !important;
      margin-left: 0 !important;
    }
  `;
  let style = document.getElementById(JR_STYLE_ID);
  // Compared rather than only checked for: a tab that was open when the
  // injector restarted keeps the node it already has, and would have kept
  // the old rules with it.
  if (style && style.textContent === css) return;
  if (!style) {
    style = document.createElement("style");
    style.id = JR_STYLE_ID;
    document.documentElement.appendChild(style);
  }
  style.textContent = css;
}

function schedule() {
  if (location.href === lastUrl) return;
  lastUrl = location.href;
  if (submissionWatched && !notTheConfirmation()) return;
  bannerCollapsed = false;
  // A cold page is not screened on arrival, so there is nothing to settle
  // for: the badge goes on and the person decides.
  if (cold) { askedByHand = false; renderCold(); return; }
  setTimeout(() => {
    if (location.href === lastUrl) screen();
  }, SETTLE_MS);
}

// A confirmation page reached from a filled form: the job id was kept.
try { const kept = sessionStorage.getItem(JOB_KEY); if (kept) watchSubmission(kept); } catch { /* storage blocked */ }

// Single-page ATSes (Ashby, Jobright) swap the posting without a navigation.
// Watch the URL and the title rather than the DOM, which churns constantly.
schedule();
declutterJobright();
mountJobrightAdd();
setInterval(() => {
  if (location.href !== lastUrl) schedule();
  // Jobright swaps its page without a navigation and can take the style
  // node with it; putting it back is cheaper than watching the DOM.
  declutterJobright();
  // Their Apply mounts late (their own extension draws over the posting) and
  // is re-created when the posting changes under the same URL, so the button
  // beside it is put back the same way.
  mountJobrightAdd();
}, 1000);
})();
