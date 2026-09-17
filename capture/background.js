const SERVER = "http://127.0.0.1:8787";

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "add-job",
    title: "Add this job to autopilot",
    contexts: ["page", "link", "selection"],
  });
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
const marked = new Set();
const lastUrl = new Map();

function fromSource(url) {
  try {
    const host = new URL(url).hostname;
    return SOURCE_HOSTS.some((h) => host === h || host.endsWith("." + h));
  } catch {
    return false;
  }
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
  if (info.status !== "complete" || !marked.has(tabId)) return;
  const url = tab.url || "";
  if (!/^https?:/.test(url) || fromSource(url)) return;
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
  } catch {
    // Restricted page (PDF viewer, chrome://), or the script is already there.
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  marked.delete(tabId);
  lastUrl.delete(tabId);
});
