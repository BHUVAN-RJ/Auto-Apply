"""Use Jobright's authenticated Find Any Email page as an explicit fallback.

The feature has no public API. This adapter only acts after the person presses
the lookup button in Autopilot, and only in an already-open Jobright tab. It
never sends an email and never touches an application form.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.request

from browser import autofill, chrome, guard

from . import store
from .models import Contact

EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
WAIT_SECONDS = 25


class JobrightError(RuntimeError):
    pass


FINDER_JS = r"""
(() => {
  const roots = [document];
  for (let i = 0; i < roots.length; i++) {
    for (const el of roots[i].querySelectorAll("*")) if (el.shadowRoot) roots.push(el.shadowRoot);
  }
  const all = (selector) => roots.flatMap(root => [...root.querySelectorAll(selector)]);
  const inputs = all("input, textarea");
  const input = inputs.find(el => /linkedin/i.test([
    el.placeholder, el.getAttribute("aria-label"), el.name, el.id
  ].filter(Boolean).join(" ")));
  if (!input) return {found:false, reason:"Open Jobright's Find Any Email page first."};
  const buttons = all("button, [role=button]");
  let near = input.parentElement;
  const nearby = [];
  for (let i = 0; near && i < 5; i++, near = near.parentElement) {
    nearby.push(...near.querySelectorAll("button, [role=button]"));
  }
  const button = [...new Set(nearby.concat(buttons))].find(el =>
    /(find|get|search|email)/i.test((el.innerText || el.getAttribute("aria-label") || "").trim())
    && !el.disabled);
  return {
    found:true,
    buttonText: (button?.innerText || button?.getAttribute("aria-label") || "").trim()
  };
})()
"""

SET_AND_CLICK_JS = r"""
((linkedin) => {
  const roots = [document];
  for (let i = 0; i < roots.length; i++) {
    for (const el of roots[i].querySelectorAll("*")) if (el.shadowRoot) roots.push(el.shadowRoot);
  }
  const all = (selector) => roots.flatMap(root => [...root.querySelectorAll(selector)]);
  const input = all("input, textarea").find(el => /linkedin/i.test([
    el.placeholder, el.getAttribute("aria-label"), el.name, el.id
  ].filter(Boolean).join(" ")));
  if (!input) return {ok:false, reason:"LinkedIn input disappeared"};
  const setter = Object.getOwnPropertyDescriptor(
    input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype,
    "value"
  )?.set;
  if (setter) setter.call(input, linkedin); else input.value = linkedin;
  input.dispatchEvent(new Event("input", {bubbles:true}));
  input.dispatchEvent(new Event("change", {bubbles:true}));
  let near = input.parentElement;
  const nearby = [];
  for (let i = 0; near && i < 5; i++, near = near.parentElement) {
    nearby.push(...near.querySelectorAll("button, [role=button]"));
  }
  const buttons = [...new Set(nearby.concat(all("button, [role=button]")))];
  const button = buttons.find(el =>
    /(find|get|search|email)/i.test((el.innerText || el.getAttribute("aria-label") || "").trim())
    && !el.disabled);
  if (!button) return {ok:false, reason:"Find Email button was not found"};
  button.click();
  return {ok:true, button:(button.innerText || button.getAttribute("aria-label") || "").trim()};
})(%s)
"""

EMAILS_JS = r"""
(() => {
  const roots = [document];
  for (let i = 0; i < roots.length; i++) {
    for (const el of roots[i].querySelectorAll("*")) if (el.shadowRoot) roots.push(el.shadowRoot);
  }
  const text = roots.map(root => root.innerText || root.textContent || "").join("\n");
  const values = roots.flatMap(root => [...root.querySelectorAll("input, textarea")]
    .map(el => el.value || "")).join("\n");
  return [...new Set((text + "\n" + values).match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi) || [])];
})()
"""


def _jobright_targets() -> list[dict]:
    try:
        with urllib.request.urlopen(f"{chrome.cdp_url(chrome.port())}/json", timeout=3) as response:
            rows = json.loads(response.read())
    except Exception as exc:  # noqa: BLE001 - one useful sentence for browser failures
        raise JobrightError(f"Autopilot Chrome is unavailable: {exc}") from exc
    return [row for row in rows if row.get("type") == "page"
            and "jobright.ai" in str(row.get("url") or "")]


async def lookup(contact: Contact) -> Contact:
    if not contact.linkedin_url:
        raise JobrightError("Paste the contact's LinkedIn URL first")
    cached = store.cache_get("jobright", contact.linkedin_url)
    if cached:
        return Contact.model_validate(cached)

    targets = _jobright_targets()
    if not targets:
        raise JobrightError("Open Jobright in Autopilot Chrome, then open Find Any Email")

    selected = None
    before: set[str] = set()
    for target in targets:
        async with autofill.attached(
            chrome.cdp_url(chrome.port()), target_id=target["id"],
        ) as (page, _):
            found = await page.evaluate(FINDER_JS) or {}
            if found.get("found"):
                selected = target
                before = {email.lower() for email in (await page.evaluate(EMAILS_JS) or [])}
                text = str(found.get("buttonText") or "")
                if guard.describes_submit(text):
                    raise JobrightError(f"refusing an unsafe Jobright control: {text!r}")
                result = await page.evaluate(SET_AND_CLICK_JS % json.dumps(contact.linkedin_url))
                if not (result or {}).get("ok"):
                    raise JobrightError((result or {}).get("reason") or "Jobright lookup did not start")
                break
    if selected is None:
        raise JobrightError("Open Jobright's Find Any Email page, then try again")

    found_email = ""
    async with autofill.attached(
        chrome.cdp_url(chrome.port()), target_id=selected["id"],
    ) as (page, _):
        for _ in range(WAIT_SECONDS * 2):
            await asyncio.sleep(0.5)
            emails = [email for email in (await page.evaluate(EMAILS_JS) or [])
                      if email.lower() not in before and not email.lower().endswith("@jobright.ai")]
            if emails:
                found_email = emails[-1]
                break
    if not found_email:
        raise JobrightError("Jobright did not return an email for this LinkedIn profile")

    result = contact.model_copy(update={
        "email": found_email,
        "source": "jobright",
        "verification": "guessed",
        "email_status": "Jobright guess",
    })
    store.cache_put("jobright", contact.linkedin_url, result.model_dump(mode="json"))
    return result
