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
// Once the job is queued (or its page opened) the bar has done its work and
// closes itself after this long, counted down in red across the ✕.
const AUTO_CLOSE_MS = 6000;

// On Jobright itself only the posting pages carry a job; the recommend
// list, search, and the rest are shells around many. A Jobright posting
// page is screened but never queued: its Apply button leads to the
// employer's page, which is the URL worth queueing, and queueing both
// would run the pipeline twice for one job. There the button (and the
// countdown) presses Apply instead, and the employer's page queues itself.
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
  // Facts derived from the resume say nothing about visa status or dates,
  // so those checks did not happen. That is a line of its own under the
  // verdict, not a clause buried in the summary.
  const fromResume = result.facts_source === "resume";
  const facts = fromResume ? "" : result.facts_source ? "; facts from your profile" : "";
  const about = pending
    ? `Reading this posting against your profile. Checks: ${CHECKS}.`
    : verdict === "error" || verdict === "not_a_job" ? c.hint : `${c.hint} Checked ${CHECKS}${facts}.${result.cached ? " Cached." : ""}`;

  const unchecked = fromResume && !(verdict === "error" || verdict === "not_a_job")
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
      button.add .fill { position: absolute; top: 0; bottom: 0; left: 0; width: 0; background: ${c.tone}; }
      button.add span { position: relative; z-index: 1; }
      button.add.done { background: #000; color: ${c.tone}; border-color: ${c.tone}; cursor: default; }
      button.close { position: relative; overflow: hidden; }
      button.close .fill { position: absolute; top: 0; bottom: 0; left: 0; width: 0; background: #ff5c5c; }
      button.close span { position: relative; z-index: 1; }
      li.unchecked b { color: #a0a0a0; }
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
        ${pending ? "" : verdict === "error" || verdict === "not_a_job"
          ? `<button data-act="again">Again</button>`
          : `<button class="add" data-act="add"><i class="fill"></i><span>${onJobright ? "Open job page" : "Add to autopilot"}</span></button>`}
        <button class="close" data-act="close"><i class="fill"></i><span>✕</span></button>
      </div>
    </div>`;

  // On the employer's page the button queues the job; on Jobright's own
  // posting page it presses Jobright's Apply, which opens the employer's
  // page in a new tab, where this script runs again and queues from there.
  // An ok or caution verdict does either by itself after a countdown drawn
  // left to right across the button; a click during the countdown cancels.
  const add = shadow.querySelector("button.add");
  const bar = add?.querySelector(".fill");
  const label = (text) => { add.querySelector("span").textContent = text; };
  const idle = onJobright ? "Open job page" : "Add to autopilot";
  const closeBar = shadow.querySelector("button.close .fill");
  let timer = null;
  let started = 0;
  let frame = 0;
  const draw = () => {
    const done = Math.min(1, (performance.now() - started) / AUTO_ADD_MS);
    bar.style.width = `${done * 100}%`;
    if (done < 1 && timer) frame = requestAnimationFrame(draw);
  };
  // After the job is queued the bar counts itself out, red across the ✕.
  let closing = 0;
  const autoClose = () => {
    const from = performance.now();
    const tick = () => {
      const done = Math.min(1, (performance.now() - from) / AUTO_CLOSE_MS);
      closeBar.style.width = `${done * 100}%`;
      if (done < 1) closing = requestAnimationFrame(tick);
      else host.remove();
    };
    closing = requestAnimationFrame(tick);
  };
  const act = async () => {
    timer = null;
    cancelAnimationFrame(frame);
    add.classList.remove("counting");
    bar.style.width = "0";
    add.disabled = true;
    if (onJobright) {
      label("Opening job page…");
      label(openJob() ? "Opened" : "No Apply button found");
    } else {
      label("Adding…");
      label(await addToQueue());
    }
    add.classList.add("done");
    autoClose();
  };
  const cancel = () => {
    clearTimeout(timer); timer = null;
    cancelAnimationFrame(frame);
    add.classList.remove("counting");
    bar.style.width = "0";
    label(idle);
  };
  if (add && AUTO_ADD.has(verdict)) {
    add.classList.add("counting");
    label(onJobright ? "Opening job page" : "Adding to autopilot");
    started = performance.now();
    timer = setTimeout(act, AUTO_ADD_MS);
    frame = requestAnimationFrame(draw);
  }

  shadow.addEventListener("click", (e) => {
    const action = e.target?.closest?.("[data-act]")?.dataset?.act;
    if (action === "close") { if (timer) cancel(); cancelAnimationFrame(closing); host.remove(); }
    if (action === "again") screen({ force: true });
    if (action === "add" && !add.disabled) {
      if (timer) cancel();
      else act();
    }
  });
  document.documentElement.appendChild(host);
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[ch]);
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

async function addToQueue() {
  try {
    const res = await post("/capture", {
      url: location.href,
      title: document.title,
      source: location.hostname,
      // The rendered text, for when the fetch sees a client-side shell or
      // an Apply that landed straight on the form.
      text: pageText(),
    });
    const data = await res.json();
    return data.created ? "Added" : "Already queued";
  } catch (err) {
    return `Failed: ${err.message}`;
  }
}

async function screen({ force = false, waited = 0 } = {}) {
  if (!isPosting()) return;
  const text = pageText();
  if (text.length < MIN_TEXT) {
    const url = location.href;
    if (waited < WAIT_MAX) setTimeout(() => { if (location.href === url) screen({ force, waited: waited + 1 }); }, WAIT_MS);
    else render({ verdict: "not_a_job" });
    return;
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
