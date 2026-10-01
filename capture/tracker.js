// The tracker, drawn on the Simplify new-grad list's own GitHub page.
//
// github.com/SimplifyJobs/New-Grad-Positions is a README of HTML tables:
// company, role, location, an Apply link to the employer's own form, and an
// age in days. Every row carries the posting's UUID in the simplify.jobs
// link beside Apply, so a row is matched to our own queue exactly.
//
// What this adds to the page: a column of its own saying what happened with
// each posting (APPLIED, FILLED, IN AUTOPILOT, REJECTED, FAILED), an Add
// button on the rows nothing has been done about, and a bar that takes one
// day of the list at a time. Only rows that were queued get a badge; a row
// the screen rejected or the title filter passed over keeps its Add button
// and carries the reason in its tooltip, so the page stays the list and not
// a report.
//
// Nothing here acts by itself. There is no countdown and no automatic
// capture: the day button and the Add button are the whole decision, and
// what they start is the same pipeline a job added from its own page gets -
// screened, tailored, held at APPROVED by `settings.hold_fills`, filled one
// tab at a time, and never submitted.
(() => {
if (window.__jobAutopilotTracker) return;
window.__jobAutopilotTracker = true;

const SERVER = "http://127.0.0.1:8787";
const PAGE = /^\/[^/]+\/New-Grad-Positions(\/|$)/;
if (!/^github\.com$/.test(location.hostname) || !PAGE.test(location.pathname)) return;

// The section a table belongs to. A day's run covers the two that are this
// resume's; the others keep their badges and their Add buttons, because a
// decision about one job is always the person's.
const WORKED = /software engineering|data science|machine learning|\bai\b|\bml\b/i;
const POLL_MS = 2000;
const MARKS = ["🛂", "🇺🇸", "🎓", "🔥"];

// The same words the banner uses for the same states, so the two never
// describe one job differently.
const BADGE = {
  submitted: { text: "APPLIED", tone: "#1a7f37", promise: "Show me what I sent" },
  filled: { text: "FILLED", tone: "#9a6700", promise: "Open this job: the form is filled and waiting for your Submit" },
  filling: { text: "FILLING", tone: "#9a6700", promise: "Open this job: autopilot is filling the form now" },
  skipped: { text: "REJECTED", tone: "#cf222e", promise: "Show me why I rejected it" },
  failed: { text: "FAILED", tone: "#cf222e", promise: "Show me what happened" },
};
const IN_FLIGHT = { text: "IN AUTOPILOT", tone: "#8250df", promise: "Open this job in autopilot" };

const rows = [];            // every row of every table, in page order
const byUuid = new Map();

function text(cell) {
  // `<details><summary>6 locations</summary>Seattle…` would read as one
  // word, and `<br>` separates the places; neither survives textContent.
  const clone = cell.cloneNode(true);
  for (const el of clone.querySelectorAll("summary")) el.remove();
  for (const br of clone.querySelectorAll("br")) br.replaceWith("; ");
  return (clone.textContent || "").replace(/\s*;\s*/g, "; ").replace(/\s+/g, " ").trim();
}

function sectionOf(table) {
  let el = table;
  while ((el = el.previousElementSibling)) {
    if (/^H[1-4]$/.test(el.tagName)) return (el.textContent || "").trim();
  }
  return "";
}

// The Age column is a day for the first month ("0d", "22d") and then a
// month ("1mo", "3mo"), which is 267 of the 486 rows on the page. A month
// is not a day: parsing "1mo" as 1 would date a posting from August to
// yesterday, and one button over "1mo" would be 169 postings tailored at
// once. So a coarse age is its own bucket, kept as the list printed it, and
// only a real day can be run.
function dayOf(age) {
  const days = String(age).trim().match(/^(\d+)\s*d$/i);
  if (days) return new Date(Date.now() - Number(days[1]) * 86400000).toISOString().slice(0, 10);
  return String(age).trim();          // "1mo", "2mo": a bucket, not a date
}

function isDay(key) {
  return /^\d{4}-\d{2}-\d{2}$/.test(key);
}

function dayLabel(key) {
  if (!isDay(key)) {
    const months = (key.match(/^(\d+)\s*mo/i) || [])[1];
    return months ? `About ${months} month${months === "1" ? "" : "s"} old` : `Age ${key}`;
  }
  const when = new Date(`${key}T12:00:00Z`);
  if (isNaN(when)) return key;
  const today = new Date().toISOString().slice(0, 10);
  const name = when.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
  return key === today ? `Today · ${name}` : name;
}

// Reads the tables. One row per posting; a `↳` in the company cell means
// "same company as the row above", which is how the list groups several
// openings at one employer.
function read() {
  for (const table of document.querySelectorAll("table")) {
    const category = sectionOf(table);
    let company = "";
    for (const tr of table.querySelectorAll("tbody tr")) {
      const cells = [...tr.children].filter((c) => c.tagName === "TD");
      if (cells.length < 4) continue;
      const first = text(cells[0]);
      if (first && first !== "↳") company = first.replace(/[🔥🎓]/g, "").trim();
      const apply = [...cells[3].querySelectorAll("a[href]")]
        .map((a) => a.href)
        .filter((href) => !/simplify\.jobs/i.test(href));
      const tag = cells[3].querySelector('a[href*="simplify.jobs/p/"]');
      const uuid = tag ? (tag.href.match(/\/p\/([0-9a-f-]{36})/i) || [])[1] || "" : "";
      // A closed application (🔒) has no link and no id: it is history.
      if (!uuid || !apply.length) continue;
      const title = text(cells[1]);
      const row = {
        uuid,
        company,
        title: title.replace(/[🛂🇺🇸🎓🔥]/g, "").replace(/\s+/g, " ").trim(),
        url: apply[0],
        location: text(cells[2]),
        category,
        age: text(cells[cells.length - 1]),
        marks: MARKS.filter((m) => title.includes(m)).join(""),
        tr,
        cell: null,
      };
      row.day = dayOf(row.age);
      row.worked = WORKED.test(category);
      rows.push(row);
      byUuid.set(uuid, row);
    }
    head(table);
  }
}

// A column of its own, headed like the others.
function head(table) {
  const header = table.querySelector("thead tr");
  if (header && !header.querySelector(".autopilot-head")) {
    const th = document.createElement("th");
    th.className = "autopilot-head";
    th.textContent = "Autopilot";
    header.appendChild(th);
  }
}

function cellFor(row) {
  if (row.cell && row.cell.isConnected) return row.cell;
  const td = document.createElement("td");
  td.className = "autopilot-cell";
  row.tr.appendChild(td);
  row.cell = td;
  return td;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[ch]);
}

// What the row says now. A queued job gets a badge in the status's own
// colour, and the badge is the way back to it. Everything else gets the
// Add button and keeps whatever reason we have in its tooltip: the page is
// a list of jobs, not a report on them.
function paint(row) {
  const td = cellFor(row);
  const seen = row.state?.seen || null;
  const tracked = row.state?.tracked || null;
  // A row queued seconds ago by a day's run has no `seen` yet - that answer
  // comes from the queue on the next look - but it is in autopilot and the
  // badge should say so without waiting.
  const fresh = !seen && tracked?.job_id && tracked?.queued_at;
  if (seen || fresh) {
    const look = seen ? (BADGE[seen.status] || IN_FLIGHT) : IN_FLIGHT;
    const sure = seen && seen.level !== "high" ? "likely the same job · " : "";
    td.innerHTML = `<button class="autopilot-badge" data-uuid="${esc(row.uuid)}"
        style="--tone: ${look.tone}" title="${esc(sure + look.promise)}">${esc(look.text)}</button>`;
    return;
  }
  const why = tracked?.reason || "";
  const busy = row.busy ? "Adding…" : "+ Add";
  td.innerHTML = `<button class="autopilot-add" data-add="${esc(row.uuid)}" ${row.busy ? "disabled" : ""}
      title="${esc(why ? `Not queued: ${why}. Add it anyway — autopilot tailors a resume for it and stops at the filled form.`
                      : "Add to autopilot: tailor a resume and cover letter for this posting and fill the form, stopping before Submit.")}"
      >${esc(busy)}</button>`;
}

function paintAll() {
  for (const row of rows) paint(row);
}

// ------------------------------------------------------------------ the bar
// One day of the list at a time. The bar lists the days the page is
// showing, how many of each are already in autopilot, and starts a run.

const HOST_ID = "job-autopilot-tracker";
const BAR = { days: [], runs: {}, running: null, hold: true, note: "" };
let shadow = null;

function dayRows(day) {
  return rows.filter((r) => r.day === day && r.worked);
}

function countDays() {
  const seenDays = [];
  for (const row of rows) if (row.day && !seenDays.includes(row.day)) seenDays.push(row.day);
  // Newest day first; the month buckets after them, youngest first, since
  // they are the end of the list rather than a day of it.
  seenDays.sort((a, b) => {
    if (isDay(a) && isDay(b)) return a < b ? 1 : -1;
    if (isDay(a) !== isDay(b)) return isDay(a) ? -1 : 1;
    return (parseInt(a, 10) || 0) - (parseInt(b, 10) || 0);
  });
  BAR.days = seenDays.map((day) => {
    const mine = dayRows(day);
    return {
      day,
      total: mine.length,
      known: mine.filter((r) => r.state?.seen).length,
      runnable: isDay(day),
    };
  }).filter((d) => d.total);
}

function bar() {
  if (shadow) return shadow;
  const host = document.createElement("div");
  host.id = HOST_ID;
  shadow = host.attachShadow({ mode: "closed" });
  shadow.innerHTML = `
    <style>
      :host { all: initial; }
      .panel { position: fixed; right: 16px; bottom: 16px; z-index: 2147483647; width: 320px;
               max-height: 60vh; overflow-y: auto; background: #000; color: #fff;
               border: 2px solid #b48cff;
               font: 12px/1.45 "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace; }
      .head { display: flex; align-items: center; gap: 8px; padding: 8px 10px;
              border-bottom: 1px solid #333; }
      .head b { color: #b48cff; letter-spacing: .08em; font-size: 11px; text-transform: uppercase; }
      .head .grow { flex: 1; }
      .hold { color: #ffb74d; font-size: 11px; padding: 6px 10px; border-bottom: 1px solid #333; }
      .note { color: #a0a0a0; font-size: 11px; padding: 6px 10px; }
      .day { display: flex; align-items: baseline; gap: 8px; padding: 6px 10px;
             border-bottom: 1px solid #1a1a1a; }
      .day .when { color: #fff; font-weight: 600; flex: 1; min-width: 0; }
      .day .count { color: #a0a0a0; font-size: 11px; }
      .day.done .when { color: #3ddc84; }
      .day .sum { display: block; color: #a0a0a0; font-size: 11px; }
      button { background: #000; color: #fff; border: 2px solid #fff; padding: 3px 8px;
               font: inherit; font-size: 10px; font-weight: 600; letter-spacing: .08em;
               text-transform: uppercase; cursor: pointer; white-space: nowrap; }
      button:hover { background: #fff; color: #000; }
      button:disabled { opacity: .45; cursor: default; }
      button.go { border-color: #b48cff; color: #b48cff; }
      button.go:hover { background: #b48cff; color: #000; }
      button.close { border-color: #555; color: #a0a0a0; padding: 2px 6px; }
      .collapsed .body { display: none; }
    </style>
    <div class="panel"><div class="head"><b>Autopilot</b><span class="grow"></span>
      <button class="close" data-act="fold" title="Fold the bar away">−</button></div>
      <div class="body"></div></div>`;
  document.documentElement.appendChild(host);
  shadow.addEventListener("click", (event) => {
    const el = event.target?.closest?.("[data-act], [data-day]");
    if (!el) return;
    if (el.dataset.act === "fold") {
      shadow.querySelector(".panel").classList.toggle("collapsed");
      el.textContent = shadow.querySelector(".panel").classList.contains("collapsed") ? "+" : "−";
      return;
    }
    if (el.dataset.day) startDay(el.dataset.day);
  });
  return shadow;
}

function paintBar() {
  const body = bar().querySelector(".body");
  countDays();
  const hold = BAR.hold
    ? `<div class="hold">Jobs will be tailored and held at APPROVED — the hold on the review page is on, so no tab opens until you let go.</div>`
    : `<div class="hold">The hold is off: an approved job takes a browser tab as soon as the one before it is done.</div>`;
  const list = BAR.days.map((d) => {
    const run = BAR.runs[d.day];
    const going = run && run.running;
    const done = run && !run.running;
    const sum = run
      ? `<span class="sum">${going ? `${run.done}/${run.total} read` : `${run.queued.length} queued`}${
          run.rejected ? `, ${run.rejected} rejected` : ""}${run.skipped ? `, ${run.skipped} passed over` : ""}${
          run.failed ? `, ${run.failed} could not be read` : ""}</span>`
      : "";
    return `<div class="day${done ? " done" : ""}">
      <span class="when">${esc(dayLabel(d.day))}
        <span class="count">${d.total} role${d.total === 1 ? "" : "s"}${d.known ? ` · ${d.known} in autopilot` : ""}</span>
        ${sum}</span>
      ${d.runnable
        ? `<button class="go" data-day="${esc(d.day)}" ${BAR.running || going ? "disabled" : ""}>${
            going ? "Working…" : done ? "Again" : "Apply to this day"}</button>`
        : `<span class="count" title="The list groups everything past a month into one age, so this is not one day's worth. Add any of these from its own row.">by row only</span>`}
    </div>`;
  }).join("");
  body.innerHTML = hold + (list || `<div class="note">No rows found on this page.</div>`)
    + (BAR.note ? `<div class="note">${esc(BAR.note)}</div>` : "");
}

// ------------------------------------------------------------------ server

let seq = 0;
const waiting = new Map();
window.__autopilotReply = (id, ok, status, body) => {
  const settle = waiting.get(id);
  if (!settle) return;
  waiting.delete(id);
  ok ? settle.resolve({ ok: status < 400, status, json: async () => body })
     : settle.reject(new Error(body));
};
function post(path, body) {
  if (typeof window.__autopilotRequest === "function") {
    const id = ++seq;
    return new Promise((resolve, reject) => {
      waiting.set(id, { resolve, reject });
      window.__autopilotRequest(JSON.stringify({ id, path, body }));
    });
  }
  return fetch(`${SERVER}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function ask(path, body) {
  const res = await post(path, body);
  const data = await res.json();
  if (!res.ok) throw new Error(data?.detail || `server ${res.status}`);
  return data;
}

function payload(row) {
  const { uuid, company, title, url, location, category, age, day, marks } = row;
  return { uuid, company, title, url, location, category, age, day, marks };
}

async function loadStatus() {
  try {
    const data = await ask("/simplify/status", { rows: rows.map(payload) });
    for (const state of data.rows) {
      const row = byUuid.get(state.uuid);
      if (row) row.state = state;
    }
    BAR.runs = data.days || {};
    BAR.running = data.running || null;
    BAR.hold = !!data.hold;
    BAR.note = "";
  } catch (err) {
    BAR.note = `Could not reach autopilot: ${err.message}`;
  }
  paintAll();
  paintBar();
}

async function startDay(day) {
  if (!isDay(day)) { BAR.note = "That is a month of the list, not a day. Add those row by row."; paintBar(); return; }
  const mine = dayRows(day).filter((r) => !r.state?.seen);
  if (!mine.length) { BAR.note = `Everything on ${day} is already in autopilot.`; paintBar(); return; }
  BAR.note = "";
  try {
    const data = await ask("/simplify/day", { day, rows: mine.map(payload) });
    if (!data.started) { BAR.note = data.reason || "Could not start"; paintBar(); return; }
    BAR.running = day;
    paintBar();
    poll(day);
  } catch (err) {
    BAR.note = `Could not start: ${err.message}`;
    paintBar();
  }
}

let timer = null;
function poll(day) {
  clearTimeout(timer);
  const tick = async () => {
    let data;
    try {
      data = await ask("/simplify/day/status", { day });
    } catch (err) {
      BAR.note = `Lost the run: ${err.message}`;
      BAR.running = null;
      paintBar();
      return;
    }
    if (data.run) BAR.runs[day] = data.run;
    BAR.running = data.running || null;
    for (const [uuid, tracked] of Object.entries(data.rows || {})) {
      const row = byUuid.get(uuid);
      if (row) row.state = { ...(row.state || { uuid }), tracked };
    }
    paintAll();
    paintBar();
    if (data.run && data.run.running) { timer = setTimeout(tick, POLL_MS); return; }
    // The run has finished: the queue is what says where each job stands
    // now, so the badges come from there rather than from the run's own
    // counters.
    loadStatus();
  };
  timer = setTimeout(tick, POLL_MS);
}

async function addRow(uuid) {
  const row = byUuid.get(uuid);
  if (!row || row.busy) return;
  row.busy = true;
  paint(row);
  try {
    const state = await ask("/simplify/row", { ...payload(row), force: true });
    row.state = state;
    if (!state.queued) BAR.note = state.reason || "Not added";
  } catch (err) {
    BAR.note = `Could not add: ${err.message}`;
  } finally {
    row.busy = false;
    paint(row);
    paintBar();
  }
}

// The badge is the way back to the job, the way the banner's is.
async function openReview(id) {
  const url = `${SERVER}/#${id}`;
  if (typeof window.__autopilotRequest === "function") {
    try {
      const res = await post("/__open", { url, focus: true });
      if (res.ok) return;
    } catch { /* fall through */ }
  }
  if (typeof chrome !== "undefined" && chrome.runtime?.sendMessage) {
    try {
      const result = await chrome.runtime.sendMessage({ type: "open-autopilot", url, focus: true });
      if (result?.ok) return;
    } catch { /* extension background unavailable */ }
  }
  window.open(url, "_blank");
}

function style() {
  if (document.getElementById("autopilot-tracker-style")) return;
  const el = document.createElement("style");
  el.id = "autopilot-tracker-style";
  el.textContent = `
    th.autopilot-head { white-space: nowrap; font-size: 11px; letter-spacing: .06em;
      text-transform: uppercase; color: #8250df; text-align: center; }
    td.autopilot-cell { white-space: nowrap; text-align: center; vertical-align: middle; }
    button.autopilot-badge { background: var(--tone); color: #fff; border: 0; cursor: pointer;
      font: 600 10px/1 ui-monospace, SFMono-Regular, Menlo, monospace; letter-spacing: .08em;
      padding: 5px 7px; border-radius: 3px; }
    button.autopilot-badge:hover { filter: brightness(1.2); }
    button.autopilot-add { background: transparent; color: #8250df; border: 1px solid #8250df;
      cursor: pointer; font: 600 10px/1 ui-monospace, SFMono-Regular, Menlo, monospace;
      letter-spacing: .06em; padding: 4px 6px; border-radius: 3px; }
    button.autopilot-add:hover { background: #8250df; color: #fff; }
    button.autopilot-add:disabled { opacity: .5; cursor: default; }`;
  document.head.appendChild(el);
}

document.addEventListener("click", (event) => {
  const badge = event.target?.closest?.("button.autopilot-badge");
  if (badge) {
    event.preventDefault();
    const row = byUuid.get(badge.dataset.uuid);
    const id = row?.state?.seen?.id || row?.state?.tracked?.job_id;
    if (id) openReview(id);
    return;
  }
  const add = event.target?.closest?.("button.autopilot-add");
  if (add) {
    event.preventDefault();
    addRow(add.dataset.add);
  }
}, true);

// GitHub swaps the README in without a navigation (Turbo), and the tables
// go with it; the column is put back when they do.
let watching = null;
function start() {
  style();
  rows.length = 0;
  byUuid.clear();
  read();
  if (!rows.length) return;
  paintAll();
  paintBar();
  loadStatus();
}
start();
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && rows.length && !BAR.running) loadStatus();
});
watching = setInterval(() => {
  const missing = rows.filter((r) => !r.cell || !r.cell.isConnected);
  if (missing.length && document.querySelector("table")) start();
}, 3000);
})();
