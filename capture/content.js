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
  caution: { tone: "#ffb454", tag: "CAUTION", hint: "Something to weigh; nothing hard against you." },
  ok: { tone: "#3ddc84", tag: "OK", hint: "No auto-reject found in the posting." },
  pending: { tone: "#b48cff", tag: "SCREENING", hint: "" },
  error: { tone: "#a0a0a0", tag: "SCREEN FAILED", hint: "" },
  not_a_job: { tone: "#a0a0a0", tag: "NO POSTING", hint: "Could not find a job description on this page. If it is still loading, press Again." },
};

const CHECKS = "years of experience, visa and sponsorship, export control, clearance and citizenship, start date and graduation window, location, degree, seniority, PERM ads";

// An ok or caution verdict queues the job on its own after this long; the
// bar across the button is the countdown, and a click on it cancels. A
// reject never queues itself: the button waits for the person.
const AUTO_ADD_MS = 2000;
const AUTO_ADD = new Set(["ok", "caution"]);

// On Jobright itself only the posting pages carry a job; the recommend
// list, search, and the rest are shells around many. A Jobright posting
// page is screened but never queued on its own: its Apply button leads to
// the employer's page, which is the URL worth queueing, and queueing both
// would run the pipeline twice for one job.
const onJobright = /(^|\.)jobright\.ai$/.test(location.hostname);
function isPosting() {
  return !onJobright || location.pathname.startsWith("/jobs/info/");
}

let lastUrl = "";

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
  const c = COLOURS[verdict];
  const tag = c.tag;
  const facts = result.facts_source === "resume"
    ? "facts derived from your resume only, so visa and dates are unknown"
    : result.facts_source ? "facts from your profile" : "";
  const about = pending
    ? `Reading this posting against your profile. Checks: ${CHECKS}.`
    : verdict === "error" || verdict === "not_a_job" ? c.hint : `${c.hint} Checked ${CHECKS}${facts ? `; ${facts}` : ""}.${result.cached ? " Cached." : ""}`;

  const flags = (result.flags || [])
    .map(
      (f) =>
        `<li><b>${esc(LABELS[f.category] || f.category)}</b>` +
        (f.severity === "soft" ? ` <i>(soft)</i>` : "") +
        `: “${esc(f.quote)}”` +
        (f.reason ? ` — ${esc(f.reason)}` : "") +
        `</li>`
    )
    .join("");

  shadow.innerHTML = `
    <style>
      :host { all: initial; }
      .bar { position: fixed; top: 0; left: 0; right: 0; z-index: 2147483647;
             background: #000; color: #fff; border-bottom: 3px solid ${c.tone};
             font: 12px/1.45 "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
             padding: 8px 12px; display: flex; gap: 14px; align-items: flex-start; }
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
      button.add.counting::before { content: ""; position: absolute; inset: 0; background: ${c.tone};
                                    transform-origin: left; transform: scaleX(0);
                                    animation: fill ${AUTO_ADD_MS}ms linear forwards; z-index: 0; }
      button.add.counting span { position: relative; z-index: 1; }
      button.add.done { background: #000; color: ${c.tone}; border-color: ${c.tone}; cursor: default; }
      @keyframes fill { to { transform: scaleX(1); } }
      @media (prefers-reduced-motion: reduce) { button.add.counting::before { animation-duration: 0s; transform: scaleX(1); } }
      button:focus-visible { outline: 2px solid #8ab4ff; outline-offset: 2px; }
      .actions { display: flex; gap: 6px; white-space: nowrap; }
    </style>
    <div class="bar">
      <span class="tag${pending ? " live" : ""}">${tag}</span>
      <div class="body">
        <span class="summary">${esc(result.summary || result.error || "")}</span>
        ${about ? `<span class="about">${esc(about)}</span>` : ""}
        ${flags ? `<ul>${flags}</ul>` : ""}
      </div>
      <div class="actions">
        ${pending ? "" : `<button class="add" data-act="add"><span>Add to autopilot</span></button>`}
        <button data-act="close">✕</button>
      </div>
    </div>`;

  const add = shadow.querySelector("button.add");
  let timer = null;
  const label = (text) => { add.querySelector("span").textContent = text; };
  const queue = async () => {
    timer = null;
    add.classList.remove("counting");
    add.disabled = true;
    label("Adding…");
    label(await addToQueue());
    add.classList.add("done");
  };
  if (add && AUTO_ADD.has(verdict) && !onJobright) {
    add.classList.add("counting");
    label("Adding to autopilot");
    timer = setTimeout(queue, AUTO_ADD_MS);
  }

  shadow.addEventListener("click", (e) => {
    const act = e.target?.closest?.("[data-act]")?.dataset?.act;
    if (act === "close") { clearTimeout(timer); host.remove(); }
    if (act === "add" && !add.disabled) {
      if (timer) { clearTimeout(timer); timer = null; add.classList.remove("counting"); label("Add to autopilot"); }
      else queue();
    }
  });
  document.documentElement.appendChild(host);
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[ch]);
}

async function addToQueue() {
  try {
    const res = await fetch(`${SERVER}/capture`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: location.href,
        title: document.title,
        source: location.hostname,
      }),
    });
    const data = await res.json();
    return data.created ? "Added" : "Already queued";
  } catch (err) {
    return `Failed: ${err.message}`;
  }
}

async function screen({ force = false } = {}) {
  if (!isPosting()) return;
  const text = pageText();
  if (text.length < MIN_TEXT) return;
  render({ summary: "" }, { pending: true });
  try {
    const res = await fetch(`${SERVER}/screen`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: location.href, title: document.title, text, force }),
    });
    const data = await res.json();
    if (!res.ok) {
      render({ verdict: "error", error: data.detail || `server ${res.status}` });
      return;
    }
    // A landing page from Apply may be a login wall or a redirect stub.
    // Say so rather than show nothing, so a missing verdict is never
    // mistaken for a clean one.
    render(data);
  } catch (err) {
    render({ verdict: "error", error: `Is the server running? ${err.message}` });
  }
}

function schedule() {
  if (location.href === lastUrl) return;
  lastUrl = location.href;
  setTimeout(() => {
    if (location.href === lastUrl) screen();
  }, SETTLE_MS);
}

// Single-page ATSes (Ashby, Jobright) swap the posting without a navigation.
// Watch the URL and the title rather than the DOM, which churns constantly.
schedule();
setInterval(() => {
  if (location.href !== lastUrl) schedule();
}, 1000);
})();
