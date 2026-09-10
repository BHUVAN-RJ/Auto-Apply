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
