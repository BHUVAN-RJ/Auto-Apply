const SERVER = "http://127.0.0.1:8787";

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "add-job",
    title: "Add this job to autopilot",
    contexts: ["page", "link", "selection"],
  });
});

function isAutopilotTab(url) {
  return url === `${SERVER}/` || url.startsWith(`${SERVER}/#`);
}

// `focus` false: the review tab is pointed at the job but the active tab
// stays; a queued job is not a reason to leave the posting.
async function openAutopilot(url, focus = true) {
  if (!url.startsWith(`${SERVER}/`)) throw new Error("invalid Autopilot URL");
  const tabs = await chrome.tabs.query({});
  const existing = tabs.find((tab) => isAutopilotTab(tab.url || tab.pendingUrl || ""));
  if (existing?.id != null) {
    await chrome.tabs.update(existing.id, focus ? { url, active: true } : { url });
    if (focus && existing.windowId != null) await chrome.windows.update(existing.windowId, { focused: true });
    return { reused: true };
  }
  await chrome.tabs.create({ url, active: focus });
  return { reused: false };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "close-me") {
    // The employer tab, queued, asks to go; only the sender, only a tab.
    const tabId = sender?.tab?.id;
    if (tabId == null) { sendResponse({ ok: false }); return undefined; }
    chrome.tabs.remove(tabId)
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }
  if (message?.type !== "open-autopilot") return undefined;
  openAutopilot(String(message.url || ""), message.focus !== false)
    .then((result) => sendResponse({ ok: true, ...result }))
    .catch((error) => sendResponse({ ok: false, error: error.message }));
  return true;
});

// Runs in the page to guess the company name. Best effort only -- the tailor
// step re-derives company and title from the scraped posting anyway.
function scrapeMeta() {
  const meta = (name) =>
    document.querySelector(`meta[property="${name}"], meta[name="${name}"]`)
      ?.content || "";
  const ld = [...document.querySelectorAll('script[type="application/ld+json"]')]
    .map((s) => {
      try {
        return JSON.parse(s.textContent);
      } catch {
        return null;
      }
    })
    .flatMap((v) => (Array.isArray(v) ? v : [v]))
    .find((v) => v && v["@type"] === "JobPosting");
  return {
    title: ld?.title || meta("og:title") || document.title || "",
    company: ld?.hiringOrganization?.name || meta("og:site_name") || "",
  };
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== "add-job") return;

  // A right-click on a link captures the link; otherwise the page itself.
  const url = info.linkUrl || info.pageUrl || tab.url;

  let meta = { title: tab.title || "", company: "" };
  try {
    const [result] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: scrapeMeta,
    });
    if (result?.result) meta = result.result;
  } catch {
    // Restricted page (chrome://, PDF viewer). Fall back to the tab title.
  }

  try {
    const res = await fetch(`${SERVER}/capture`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url,
        title: meta.title,
        company: meta.company,
        source: new URL(url).hostname,
      }),
    });
    if (!res.ok) throw new Error(`server ${res.status}`);
    const data = await res.json();
    notify(
      data.created ? "Added to queue" : "Already queued",
      `${data.queued} job${data.queued === 1 ? "" : "s"} pending`
    );
    if (data.id) await openAutopilot(`${SERVER}/#${data.id}`);
  } catch (err) {
    notify("Capture failed", `Is the server running? ${err.message}`);
  }
});

function notify(title, message) {
  chrome.notifications.create({
    type: "basic",
    iconUrl: "icon128.png",
    title,
    message,
  });
}

// Clicking Apply on Jobright lands on whatever site the employer uses,
// which cannot be enumerated in the manifest. Any tab that was opened from
// Jobright, or navigated away from it, is screened wherever it ends up.
// Nothing else is screened: the screen is for the Jobright flow, and the
// context menu still queues any page by hand.
const SOURCE_HOSTS = ["jobright.ai"];
// An application form opened by hand is still a form. These hosts are
// screened wherever they came from, but never marked: the banner offers,
// and the countdown that queues a job by itself stays off (the content
// script reads `__autopilotHandOpened`).
const ATS_HOSTS = [
  "myworkdayjobs.com", "myworkdaysite.com", "greenhouse.io", "ashbyhq.com",
  "lever.co", "smartrecruiters.com", "bamboohr.com", "workable.com",
  "icims.com", "jobvite.com", "taleo.net", "successfactors.com",
  "oraclecloud.com", "eightfold.ai", "dover.com",
];
const marked = new Set();
const lastUrl = new Map();

function onHosts(url, hosts) {
  try {
    const host = new URL(url).hostname;
    return hosts.some((h) => host === h || host.endsWith("." + h));
  } catch {
    return false;
  }
}

function fromSource(url) {
  return onHosts(url, SOURCE_HOSTS);
}

function isAts(url) {
  return onHosts(url, ATS_HOSTS);
}

chrome.tabs.onCreated.addListener(async (tab) => {
  if (!tab.openerTabId) return;
  try {
    const opener = await chrome.tabs.get(tab.openerTabId);
    if (fromSource(opener.url || opener.pendingUrl || "")) marked.add(tab.id);
  } catch {
    // Opener already gone.
  }
});

chrome.tabs.onUpdated.addListener(async (tabId, info, tab) => {
  if (info.url) {
    // Same-tab navigation off Jobright: the Apply button that does not
    // open a new tab.
    const previous = lastUrl.get(tabId) || "";
    if (fromSource(previous) && !fromSource(info.url)) marked.add(tabId);
    lastUrl.set(tabId, info.url);
  }
  const url = tab.url || "";
  if (info.status !== "complete" || !(marked.has(tabId) || isAts(url))) return;
  if (!/^https?:/.test(url) || fromSource(url)) return;
  const hand = !marked.has(tabId);
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (value) => { window.__autopilotHandOpened = value; },
      args: [hand],
    });
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
  } catch {
    // Restricted page (PDF viewer, chrome://), or the script is already there.
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  marked.delete(tabId);
  lastUrl.delete(tabId);
});
