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

const COLOURS = {
  reject: { bg: "#7f1d1d", fg: "#fecaca", tag: "NOT OK", hint: "Hard requirement you do not meet. Applying is likely wasted." },
  caution: { bg: "#78350f", fg: "#fde68a", tag: "CAUTION", hint: "Something to weigh; nothing hard against you." },
  ok: { bg: "#14532d", fg: "#bbf7d0", tag: "OK", hint: "No auto-reject found in the posting." },
  error: { bg: "#1f2937", fg: "#e5e7eb", tag: "SCREEN FAILED", hint: "" },
  not_a_job: { bg: "#1f2937", fg: "#9ca3af", tag: "NO POSTING", hint: "Could not find a job description on this page. If it is still loading, press Again." },
};

const CHECKS = "years of experience, visa and sponsorship, export control, clearance and citizenship, start date and graduation window, location, degree, seniority";

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

  const verdict = pending ? "ok" : result.verdict in COLOURS ? result.verdict : "error";
  const c = COLOURS[verdict];
  const tag = pending ? "SCREENING…" : c.tag;
  const facts = result.facts_source === "resume"
    ? "facts derived from your resume only, so visa and dates are unknown"
    : result.facts_source ? "facts from applicant.md" : "";
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
             background: ${c.bg}; color: ${c.fg}; font: 13px/1.4 -apple-system,
             system-ui, sans-serif; padding: 8px 12px; box-shadow: 0 2px 8px #0006;
             display: flex; gap: 12px; align-items: flex-start; }
      .tag { font-weight: 800; font-size: 15px; letter-spacing: .06em; white-space: nowrap; }
      .about { display: block; opacity: .7; font-size: 12px; margin-top: 2px; }
      .body { flex: 1; min-width: 0; }
      .summary { opacity: .85; }
      ul { margin: 4px 0 0; padding-left: 18px; }
      li { margin: 2px 0; }
      button { background: transparent; color: inherit; border: 1px solid currentColor;
               border-radius: 4px; padding: 2px 8px; font: inherit; cursor: pointer; }
      button:hover { background: #ffffff22; }
      .actions { display: flex; gap: 6px; white-space: nowrap; }
    </style>
    <div class="bar">
      <span class="tag">${tag}</span>
      <div class="body">
        <span class="summary">${esc(result.summary || result.error || "")}</span>
        ${about ? `<span class="about">${esc(about)}</span>` : ""}
        ${flags ? `<ul>${flags}</ul>` : ""}
      </div>
      <div class="actions">
        ${pending ? "" : `<button data-act="add">Add to autopilot</button>`}
        ${pending ? "" : `<button data-act="again">Again</button>`}
        <button data-act="close">✕</button>
      </div>
    </div>`;

  shadow.addEventListener("click", async (e) => {
    const act = e.target?.dataset?.act;
    if (act === "close") host.remove();
    if (act === "again") screen({ force: true });
    if (act === "add") {
      e.target.disabled = true;
      e.target.textContent = await addToQueue();
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
