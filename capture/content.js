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

// An ok or caution verdict queues the job on its own after this long; the
// bar across the button is the countdown, and a click on it cancels. A
// reject never queues itself: the button waits for the person.
// 3s, not 2 (2026-09-24): the "Use Opus" button lives inside this window,
// and two seconds was not enough to read the verdict and press it.
const AUTO_ADD_MS = 3000;
const AUTO_ADD = new Set(["ok", "caution"]);
// Once the job is queued the bar has done its work and closes itself after
// this long, counted down in red across the ✕. Not before: on Jobright's
// page the job is queued by the employer tab its Apply opened, so the bar
// asks the server whether that has happened, this often, for this long,
// and only then starts the countdown. A job that never lands leaves the
// bar open, saying so.
const AUTO_COLLAPSE_MS = 5000;
const QUEUED_POLL_MS = 2000;
const QUEUED_WAIT_MS = 90000;

// On Jobright itself only the posting pages carry a job; the recommend
// list, search, and the rest are shells around many. A Jobright posting
// page is screened but never queued: its Apply button leads to the
// employer's page, which is the URL worth queueing, and queueing both
// would run the pipeline twice for one job. There the button (and the
// countdown) presses Apply instead, and the employer's page queues itself.
const onJobright = /(^|\.)jobright\.ai$/.test(location.hostname);

// "Use Opus": tailor this one job with the expensive model. Armed by the
// button during the countdown and read when the job is queued, so pressing
// it does not cut the countdown short — the job goes in as it would have,
// with the model changed. Per tab, per job; the default is the cheap model.
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
function seenLine(seen) {
  const when = seen.at ? ` ${seen.at.slice(0, 10)}` : "";
  const state = `${SEEN_STATUS[seen.status] || seen.status}${["submitted", "skipped"].includes(seen.status) ? when : ""}`;
  return seen.level === "high"
    ? `Already in autopilot · ${state}`
    : `Looks like a job already in autopilot: ${seen.company || ""}${seen.company && seen.title ? " — " : ""}${seen.title || ""} · ${state}`;
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

function render(result, { pending = false } = {}) {
  document.getElementById(HOST_ID)?.remove();
  const host = document.createElement("div");
  host.id = HOST_ID;
  const shadow = host.attachShadow({ mode: "closed" });

  const verdict = pending ? "pending" : result.verdict in COLOURS ? result.verdict : "error";
  const seen = pending ? null : result.seen || null;
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
  const canAdd = !pending && !(seen && seen.level === "high") && verdict !== "submitted";
  const canRetry = !pending && (verdict === "error" || verdict === "not_a_job");

  shadow.innerHTML = `
    <style>
      :host { all: initial; }
      .bar { position: fixed; top: 0; left: 0; right: 0; z-index: 2147483647;
             background: #000; color: #fff; border-bottom: 3px solid ${c.tone};
             font: 12px/1.45 "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
             padding: 8px 12px; display: flex; gap: 14px; align-items: flex-start; }
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
      .shell.error .assistant-core { fill: #ff5c5c; }
      @keyframes flow { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.7); } }
      @media (prefers-reduced-motion: reduce) { .shell.working .assistant-core { animation: none; transform: scale(1.4); } }
      .auto { display: none; color: #b48cff; font-weight: 600; margin-top: 3px; }
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
      .body { flex: 1; min-width: 0; padding-top: 4px; }
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
      button.add { background: ${c.tone}; color: #000; border-color: ${c.tone}; position: relative; overflow: hidden; }
      button.add:hover { background: #fff; border-color: #fff; }
      button.add.counting { background: #fff; color: #000; border-color: #fff; }
      button.add .fill { position: absolute; top: 0; bottom: 0; left: 0; width: 0; background: ${c.tone}; }
      button.add span { position: relative; z-index: 1; }
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
      .files { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 5px; align-items: center; }
      .files .hint { color: #a0a0a0; font-size: 11px; }
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
      .actions { display: flex; gap: 6px; white-space: nowrap; align-items: center; }
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
      <div class="bar">
        <span class="tag${pending ? " live" : ""}">${tag}</span>
        <div class="body">
          <span class="summary">${esc(result.summary || result.error || "")}</span>
          ${about ? `<span class="about">${esc(about)}</span>` : ""}
          <span class="auto"></span>
          ${seen ? `<span class="seen">${esc(seenLine(seen))}</span>` : ""}
          ${seen && seen.level === "high" ? `<div class="files" id="files"></div>` : ""}
          <div class="changes" id="changes"></div>
          ${flags ? `<ul>${flags}</ul>` : ""}
        </div>
        <div class="actions">
          ${seen ? `<button class="open" data-act="open"><span>Open in autopilot</span></button>` : ""}
          ${canRetry ? `<button data-act="again">Again</button>` : ""}
          ${canAdd ? `<label class="approve" title="Checked: once the resume is tailored the fill starts by itself. Unchecked: it waits for your approval on the review page."><input type="checkbox" checked> auto-approve</label>` : ""}
          ${canAdd ? `<button class="opus" data-act="opus" aria-pressed="${usePremium}" title="Tailor this job's resume and cover letter with the expensive model. About eight times the cost of a normal job, and it rewrites far more of the resume. The countdown keeps running.">${usePremium ? "Opus on" : "Use Opus"}</button>` : ""}
          ${canAdd ? `<button class="add" data-act="add"><i class="fill"></i><span>${seen ? "Add anyway" : onJobright ? "Open job page" : "Add to autopilot"}</span></button>` : ""}
          <button class="close" data-act="close" aria-label="Minimize Job Autopilot"><i class="fill"></i><span>−</span></button>
        </div>
      </div>
    </div>`;

  // On the employer's page the button queues the job; on Jobright's own
  // posting page it presses Jobright's Apply, which opens the employer's
  // page in a new tab, where this script runs again and queues from there.
  // An ok or caution verdict does either by itself after a countdown drawn
  // left to right across the button; a click during the countdown cancels.
  const add = shadow.querySelector("button.add");
  const bar = add?.querySelector(".fill");
  // Auto-approve, per job, decided here during the countdown. On the
  // employer's page it travels with the capture; on Jobright's page the
  // employer tab queues the job, so the choice goes ahead by posting id.
  const approve = shadow.querySelector("label.approve input");
  const autoFill = () => (approve ? approve.checked : null);
  approve?.addEventListener("change", () => {
    if (onJobright && postingId()) post("/prefs", { jr_id: postingId(), auto_fill: approve.checked }).catch(() => {});
  });
  const label = (text) => { add.querySelector("span").textContent = text; };
  const idle = seen ? "Add anyway" : onJobright ? "Open job page" : "Add to autopilot";
  const closeBar = shadow.querySelector("button.close .fill");
  let timer = null;
  let started = 0;
  let frame = 0;
  const draw = () => {
    const done = Math.min(1, (performance.now() - started) / AUTO_ADD_MS);
    bar.style.width = `${done * 100}%`;
    if (done < 1 && timer) frame = requestAnimationFrame(draw);
  };
  const shell = shadow.querySelector(".shell");
  const badge = shadow.querySelector("button.badge");
  const auto = shadow.querySelector(".auto");
  expandBanner = () => { if (bannerCollapsed) expand(); };
  paintChanges = () => {
    const box = shadow.getElementById("changes");
    if (!box) return;
    const rows = CHANGES.rows;
    if (!rows.length && !CHANGES.note) { box.classList.remove("on"); box.innerHTML = ""; return; }
    box.classList.add("on");
    const open = rows.filter((r) => !r.remembered);
    box.innerHTML = `<span class="hint">${open.length
        ? "Changed since autofill. Tick what Jobright got wrong and every next form gets your value:"
        : rows.length ? "Everything you changed here is on file." : ""}</span>`
      + rows.map((r) => `<label class="row${r.remembered ? " kept" : ""}" title="${esc(r.label)}">
          <input type="checkbox" data-label="${esc(r.label)}" ${r.remembered ? "checked disabled" : CHANGES.picked.has(r.label) ? "checked" : ""}>
          <span class="l">${esc(r.label.length > 48 ? r.label.slice(0, 47) + "…" : r.label)}</span>
          ${r.was ? `<span class="was">${esc(r.was)}</span>` : ""}
          <span class="now">${esc(r.now)}</span>${r.remembered ? " ✓" : ""}</label>`).join("")
      + `<div class="row-actions">${open.length ? `<button class="remember" data-act="remember" ${CHANGES.busy || !CHANGES.picked.size ? "disabled" : ""}>Remember ${CHANGES.picked.size || ""}</button>` : ""}
          ${CHANGES.note ? `<span class="note">${esc(CHANGES.note)}</span>` : ""}</div>`;
    for (const input of box.querySelectorAll("input[type=checkbox]:not(:disabled)")) {
      input.addEventListener("change", () => {
        if (input.checked) CHANGES.picked.add(input.dataset.label); else CHANGES.picked.delete(input.dataset.label);
        const button = box.querySelector("button.remember");
        if (button) { button.disabled = CHANGES.busy || !CHANGES.picked.size; button.textContent = `Remember ${CHANGES.picked.size || ""}`; }
      });
    }
  };
  paintChanges();

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
  };
  const expand = () => {
    bannerCollapsed = false;
    host.dataset.collapsed = "false";
    shell.classList.remove("collapsed");
  };
  // Clicks crossing a closed shadow root are retargeted to its host on some
  // ATS pages.  Let the host restore the badge as well as its inner button.
  host.addEventListener("click", () => { if (bannerCollapsed) expand(); });
  // After the job is queued the bar counts down, then becomes the persistent
  // corner badge. Restoring it only expands this DOM; it never screens again.
  let closing = 0;
  let collapseTimer = null;
  const autoCollapse = () => {
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
  const act = async () => {
    timer = null;
    cancelAnimationFrame(frame);
    add.classList.remove("counting");
    bar.style.width = "0";
    add.disabled = true;
    let queuedId = null;
    let result = null;
    if (onJobright) {
      label("Opening job page…");
      if (openJob()) {
        label("Opened, waiting for the job page…");
        queuedId = await waitQueued();
        label(queuedId ? "Added" : "Job page did not add it");
      } else {
        label("No Apply button found");
      }
    } else {
      label("Adding…");
      result = await addToQueue(autoFill(), usePremium);
      label(result.label);
      queuedId = result.id;
      // The pipeline is now tailoring; keep the core purple until the
      // browser agent (or a failure) says otherwise, so the badge never
      // reads "done" while something is still running for this job.
      if (result.created) {
        AUTOMATION.pipeline = true;
        window.__autopilotAutomation("working", "Added; tailoring the resume");
      }
    }
    add.classList.add("done");
    if (queuedId) {
      // The review tab is pointed at the job but stays where it is: the
      // person is reading the posting, and the fill opens its own tab
      // when the tailoring is done. Only "Open in autopilot" focuses.
      await openReview(queuedId, { focus: false });
      autoCollapse();
    }
    // This tab has done its job once the pipeline has it: the fill opens
    // its own tab on approve. Only a job created just now; an "already in
    // autopilot" tab may be the one a fill is working on.
    if (!onJobright && result?.created) closeSelf();
  };
  const cancel = () => {
    clearTimeout(timer); timer = null;
    cancelAnimationFrame(frame);
    add.classList.remove("counting");
    bar.style.width = "0";
    label(idle);
  };
  if (add && AUTO_ADD.has(verdict) && !seen) {
    add.classList.add("counting");
    label(onJobright ? "Opening job page" : "Adding to autopilot");
    started = performance.now();
    timer = setTimeout(act, AUTO_ADD_MS);
    frame = requestAnimationFrame(draw);
  }
  if (!pending && (seen || verdict === "submitted")) {
    autoCollapse();
  }

  shadow.addEventListener("click", (e) => {
    const action = e.target?.closest?.("[data-act]")?.dataset?.act;
    if (action === "close") {
      // The host's own listener re-expands a collapsed bar on any click
      // that reaches it; this one must not.
      e.stopPropagation();
      if (timer) cancel();
      clearTimeout(collapseTimer); collapseTimer = null;
      cancelAnimationFrame(closing);
      collapse();
    }
    if (action === "expand") expand();
    if (action === "again") screen({ force: true });
    if (action === "open" && seen) openReview(seen.id);
    if (action === "remember") { e.stopPropagation(); rememberPicked(); }
    if (action === "opus") {
      // Armed, not pressed: the countdown is deliberately left running, so
      // one click is the whole decision and the job still adds itself.
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
    if (action === "add" && !add.disabled) {
      if (timer) cancel();
      else act();
    }
  });
  document.documentElement.appendChild(host);
  if (seen && seen.level === "high") loadFiles(shadow, seen.id);
}

// The tailored PDFs for this job, as chips. Drag one onto the form's own
// file slot (the drag carries the File, so a drop zone or an <input
// type=file> takes it like a file from the desktop); or click it to save
// the file to Downloads for the picker. Nothing here touches the form by
// itself: the person chooses the slot.
async function loadFiles(shadow, jobId) {
  const row = shadow.getElementById("files");
  if (!row) return;
  let data;
  try {
    const res = await post(`/review/${jobId}/files`, {});
    data = await res.json();
    if (!res.ok) throw new Error(data.detail || `server ${res.status}`);
  } catch (err) {
    row.innerHTML = `<span class="hint">Files: ${esc(err.message)}</span>`;
    return;
  }
  const entries = [["resume", "Resume"], ["cover_letter", "Cover letter"]].filter(([k]) => data[k]);
  if (!entries.length) { row.innerHTML = `<span class="hint">No tailored files yet.</span>`; return; }
  row.innerHTML = `<span class="hint">Drag onto a file slot, click to download, or “put” it in the form's slot:</span>` + entries.map(([k, label]) =>
    `<span class="chip" draggable="true" data-key="${k}" title="${esc(data[k].name)}"><b>⇣</b> ${label} · ${esc(data[k].name)}</span>` +
    `<span class="chip put" data-put="${k}" title="Set the form's ${label.toLowerCase()} file input to this file"><b>→</b> put</span>`).join("");
  const files = {};
  for (const [k] of entries) {
    const bytes = Uint8Array.from(atob(data[k].b64), (c) => c.charCodeAt(0));
    files[k] = new File([bytes], data[k].name, { type: data[k].type });
  }
  for (const chip of row.querySelectorAll(".chip")) {
    const file = files[chip.dataset.key];
    chip.addEventListener("dragstart", (e) => {
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
    button.addEventListener("click", () => {
      const key = button.dataset.put;
      const result = putFile(files[key], key);
      button.innerHTML = `<b>${result.ok ? "✓" : "✕"}</b> ${esc(result.note)}`;
    });
  }
}

// Sets a file input on the page to the tailored file, the way a drop
// would. Which input: the one whose name, id, or label reads as the file's
// kind; else the only one; else the person picks from a numbered list.
// Fires the events a real pick fires so the form notices.
function putFile(file, key) {
  const inputs = [];
  const walk = (root) => {
    for (const el of root.querySelectorAll("input[type=file]")) inputs.push(el);
    for (const host of root.querySelectorAll("*")) if (host.shadowRoot) walk(host.shadowRoot);
  };
  walk(document);
  if (!inputs.length) return { ok: false, note: "no file input on this page" };
  const describe = (el) => {
    const label = el.labels && el.labels.length ? [...el.labels].map((l) => l.innerText).join(" ") : "";
    const near = el.closest("label, fieldset, section, div")?.innerText?.slice(0, 80) || "";
    return `${el.name || ""} ${el.id || ""} ${el.getAttribute("aria-label") || ""} ${label} ${near}`.replace(/\s+/g, " ").trim();
  };
  const want = key === "resume" ? /resume|\bcv\b|curriculum/i : /cover\s*letter/i;
  const other = key === "resume" ? /cover\s*letter/i : /resume|\bcv\b|curriculum/i;
  const described = inputs.map((el) => ({ el, text: describe(el) }));
  let target = described.find((d) => want.test(d.text) && !other.test(d.text))
    || (described.length === 1 && !other.test(described[0].text) ? described[0] : null);
  if (!target) {
    const menu = described.map((d, i) => `${i + 1}. ${d.text.slice(0, 60) || "(unlabelled file input)"}`).join("\n");
    const pick = parseInt(prompt(`Which slot for ${file.name}?\n\n${menu}`, "1"), 10);
    if (!(pick >= 1 && pick <= described.length)) return { ok: false, note: "cancelled" };
    target = described[pick - 1];
  }
  try {
    const dt = new DataTransfer();
    dt.items.add(file);
    target.el.files = dt.files;
    target.el.dispatchEvent(new Event("input", { bubbles: true }));
    target.el.dispatchEvent(new Event("change", { bubbles: true }));
    return { ok: true, note: `put in “${target.text.slice(0, 30) || "file input"}”` };
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
function openJob() {
  const button = [...document.querySelectorAll("button, a")]
    .find((el) => /^\s*apply\b/i.test(el.innerText || ""));
  if (!button) return false;
  button.click();
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
  if (submissionWatched && !force) return;
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

function schedule() {
  if (location.href === lastUrl) return;
  lastUrl = location.href;
  if (submissionWatched) return;
  bannerCollapsed = false;
  setTimeout(() => {
    if (location.href === lastUrl) screen();
  }, SETTLE_MS);
}

// A confirmation page reached from a filled form: the job id was kept.
try { const kept = sessionStorage.getItem(JOB_KEY); if (kept) watchSubmission(kept); } catch { /* storage blocked */ }

// Single-page ATSes (Ashby, Jobright) swap the posting without a navigation.
// Watch the URL and the title rather than the DOM, which churns constantly.
schedule();
setInterval(() => {
  if (location.href !== lastUrl) schedule();
}, 1000);
})();
