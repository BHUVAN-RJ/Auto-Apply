"""Fill an application form in code, no model, over raw CDP.

The agent's job used to start with "press Jobright's Autofill and correct
what it got wrong". On the systems with a stable form (Ashby, Greenhouse,
Lever) that is more work than filling the form ourselves: the fields have
names, the labels are plain, and the values come from `base/form.json`.
So this does the whole first pass in code and hands the agent a list of
what is still empty.

One pass, four scripts:

1. SCAN: every control on the page (and in every open shadow root), with
   its label worked out the way a screen reader would, tagged with a
   `data-autopilot-ref` so the later scripts find it again.
2. Match, in Python: the adapter's selectors first (an `id` or `name` the
   system always uses), then the profile's `answers` by exact label, then
   the generic label patterns. Anything that reads as a visa or
   work-authorisation question is skipped before matching, whatever the
   profile holds.
3. SET, per control kind: text through the native value setter so React
   sees it, `<select>` by option text, radios by clicking the option, a
   combobox by typing through CDP and clicking the suggestion, and files
   with `DOM.setFileInputFiles` on the input's own node.
4. SCAN again: what is filled, what is not, what the resume input now
   holds. The report is what the agent is told, and `resume_uploaded` is
   read from the input's file list, never assumed.

Nothing here clicks a button that reads as submit (`guard.describes_submit`
is checked on every option and radio before the click), nothing closes a
tab, and every failure is a note in the report rather than an exception:
the agent is always the fallback.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .. import guard
from ..autofill import Session, attached, same_page, wait_for_load
from .profile import Profile

log = logging.getLogger("forms")

FIELDS_TIMEOUT = 20.0     # a React form renders after load
OPTIONS_TIMEOUT = 8.0     # a combobox's suggestions after typing
POLL = 0.25
# A label longer than this is a question ("Why are you considering leaving
# your most recent position?"), not a field name, and the generic patterns
# would match a word in it. Real Ashby run: that question got a job title.
GENERIC_LABEL_MAX = 60
# A short question that asks for a fact, not for prose. Jobright's autofill
# and `base/form.json` own these; the tailor model writing them produced
# "I use he/him pronouns. Happy to share this on the form, and I appreciate
# ..." in a one-line box, and re-wrote names the autofill already had right.
FACT_LABEL = re.compile(
    r"\b(?:legal\s+(?:first|last|middle|given|family)?\s*name|first name|last name|"
    r"middle name|given name|family name|surname|full name|preferred name|"
    r"name you go by|pronouns?|email|e-mail|phone|mobile|telephone|address|street|"
    r"city|town|state|province|zip|postal|country|linkedin|github|gitlab|portfolio|"
    r"personal (?:web)?site|website|web site|twitter|x profile|school|university|"
    r"college|degree|major|discipline|gpa|graduation|grad(?:uation)? (?:date|year)|"
    r"current (?:company|employer|title|role)|notice period|"
    r"(?:desired|expected|current) (?:salary|compensation|pay|rate)|salary|compensation|"
    r"start date|earliest start|date available|how did you hear|referred by|referral)\b",
    re.I,
)
# A label that asks to be told something is prose even when a fact word is
# in it ("Describe your current role"). Kept narrow: "What is your legal
# first name?" asks with a question word and is still a name.
PROSE_LABEL = re.compile(r"\b(?:why|describe|tell us|tell me|explain|elaborate)\b", re.I)


def describes_fact(label: str) -> bool:
    """Does this label ask for a fact rather than for prose?

    Read on the label alone, so it holds wherever the label came from. A
    prose word wins: "Why do you want to work at our company?" names a
    company and is still a question for the model.
    """
    text = " ".join((label or "").split())
    if not text or len(text) > GENERIC_LABEL_MAX:
        return False
    if PROSE_LABEL.search(text):
        return False
    return bool(FACT_LABEL.search(text))
TEXT_CAP = 20000          # of the page's visible text kept by a detailed snapshot
PAGE_TEXT_JS = "(document.body && document.body.innerText) || ''"

# Shared by every script: find the element carrying a ref, through shadow roots.
FIND_FN = r"""
(function find(root, ref) {
  const el = root.querySelector('[data-autopilot-ref="' + ref + '"]');
  if (el) return el;
  for (const host of root.querySelectorAll("*")) {
    if (host.shadowRoot) { const inner = find(host.shadowRoot, ref); if (inner) return inner; }
  }
  return null;
})
"""

# Every control, with a label. Labels are resolved in the order assistive
# technology uses: <label for>, aria-labelledby, aria-label, a wrapping
# <label>, then the nearest ancestor that holds exactly this one control and
# a label-like element; placeholder last. Radios and checkboxes also get
# their group's question (fieldset legend or labelled group).
SCAN_JS = r"""
(() => {
  const out = [];
  // Refs are page-wide and survive rescans: a control that appears later
  // (the resume input Greenhouse puts back after "Remove file") must not
  // get a number an earlier one already carries.
  let counter = 0;
  const highest = (root) => {
    for (const el of root.querySelectorAll("[data-autopilot-ref]")) counter = Math.max(counter, Number(el.getAttribute("data-autopilot-ref")) || 0);
    for (const host of root.querySelectorAll("*")) if (host.shadowRoot) highest(host.shadowRoot);
  };
  highest(document);
  const text = (el) => (el ? (el.innerText || el.textContent || "") : "").replace(/\s+/g, " ").trim();
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return (r.width > 0 || r.height > 0) && s.visibility !== "hidden" && s.display !== "none";
  };
  const byId = (root, id) => {
    try { return root.getElementById ? root.getElementById(id) : root.querySelector("#" + CSS.escape(id)); }
    catch (e) { return null; }
  };
  // [text, element]: the element is kept so a "required" marker on the
  // label (a class, an <abbr>, a styled asterisk) counts too.
  const labelOf = (root, el) => {
    if (el.id) {
      let l = null;
      try { l = root.querySelector('label[for="' + CSS.escape(el.id) + '"]'); } catch (e) {}
      if (l) return [text(l), l];
    }
    const ids = el.getAttribute("aria-labelledby");
    if (ids) {
      const els = ids.split(/\s+/).map((i) => byId(root, i)).filter(Boolean);
      const parts = els.map(text).filter(Boolean);
      if (parts.length) return [parts.join(" "), els[0]];
    }
    if (el.getAttribute("aria-label")) return [el.getAttribute("aria-label").trim(), null];
    const wrap = el.closest("label");
    // A label wrapping a <select> reads its options too; drop the control's own text.
    if (wrap) return [text(wrap).replace(text(el), "").trim(), wrap];
    let p = el.parentElement;
    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
      const controls = p.querySelectorAll("input:not([type=hidden]), select, textarea, [role=combobox]");
      if (controls.length > 1) break;
      const l = p.querySelector("label, legend, [class*='label' i], [id*='label' i]");
      if (l && !l.contains(el) && text(l)) return [text(l), l];
    }
    return ["", null];
  };
  const markedRequired = (l) => !!l && (/required/i.test(l.className || "") || !!l.querySelector("abbr, [class*='required' i], [aria-label*='required' i]"));
  const groupOf = (el) => {
    const fs = el.closest("fieldset");
    if (fs) { const lg = fs.querySelector("legend"); if (lg) return [text(lg), lg]; }
    const g = el.closest("[role=radiogroup], [role=group]");
    if (g) {
      const ids = g.getAttribute("aria-labelledby");
      if (ids) { const els = ids.split(/\s+/).map((i) => byId(g.getRootNode(), i)).filter(Boolean); const parts = els.map(text); if (parts.length) return [parts.join(" "), els[0]]; }
      if (g.getAttribute("aria-label")) return [g.getAttribute("aria-label").trim(), null];
      const l = g.querySelector("label, legend, [class*='label' i]");
      if (l) return [text(l), l];
    }
    // A run of radios under one question: the nearest ancestor that holds
    // more than one of them and starts with a label-like element.
    let p = el.parentElement;
    for (let i = 0; i < 5 && p; i++, p = p.parentElement) {
      const same = p.querySelectorAll('input[type=radio], input[type=checkbox], [role=radio], button[aria-pressed]');
      if (same.length > 1) {
        const l = p.querySelector("label, legend, [class*='label' i], p, span, div");
        if (l && !l.querySelector("input, button") && text(l).length < 300) return [text(l), l];
      }
    }
    return ["", null];
  };
  const walk = (root) => {
    // button[aria-pressed]: Ashby's Yes / No answers are toggle buttons.
    for (const el of root.querySelectorAll("input, select, textarea, [role=combobox], [role=radio], button[aria-pressed]")) {
      const tag = el.tagName.toLowerCase();
      const toggle = tag === "button";
      const type = toggle ? "toggle" : (el.getAttribute("type") || (tag === "input" ? "text" : tag)).toLowerCase();
      if (type === "hidden" || type === "submit" || type === "button" || type === "image" || type === "reset") continue;
      if (el.disabled || el.readOnly) continue;
      if (type !== "file" && !visible(el)) continue;
      let ref = el.getAttribute("data-autopilot-ref");
      if (!ref) { ref = String(++counter); el.setAttribute("data-autopilot-ref", ref); }
      const combo = !toggle && (el.getAttribute("role") === "combobox" || !!el.getAttribute("aria-autocomplete")
        || el.getAttribute("aria-haspopup") === "listbox" || el.hasAttribute("aria-expanded")
        || (el.closest("[class*='select__control'], [class*='select-shell'], [class*='combobox' i]") !== null && tag === "input"));
      const kind = type === "file" ? "file" : tag === "select" ? "select"
        : toggle || type === "radio" || el.getAttribute("role") === "radio" ? "radio"
        : type === "checkbox" ? "checkbox" : combo ? "combobox" : tag === "textarea" ? "textarea" : "text";
      const options = tag === "select"
        ? [...el.options].slice(0, 400).map((o) => ({ value: o.value, text: text(o) }))
        : [];
      const [label, labelEl] = toggle ? [text(el), null] : labelOf(root, el);
      const [group, groupEl] = kind === "radio" || kind === "checkbox" ? groupOf(el) : ["", null];
      const checked = toggle ? el.getAttribute("aria-pressed") === "true"
        : el.checked || el.getAttribute("aria-checked") === "true";
      // A react-select keeps the chosen value in a sibling div and empties
      // its input; the box's own "single value" is what the human sees.
      const chosen = () => {
        if (kind !== "combobox") return "";
        const box = el.closest("[class*='select__control'], [class*='control' i]");
        const sv = box && box.querySelector("[class*='single-value' i], [class*='selected-value' i], [class*='selectedValue']");
        return sv ? text(sv) : "";
      };
      out.push({
        ref, tag, type, kind,
        id: el.id || "", name: el.getAttribute("name") || "",
        autocomplete: el.getAttribute("autocomplete") || "",
        placeholder: el.getAttribute("placeholder") || "",
        label, group,
        required: !!el.required || el.getAttribute("aria-required") === "true" || /\*\s*$/.test(label)
          || /\*\s*$/.test(group) || markedRequired(labelEl) || markedRequired(groupEl),
        value: kind === "file" ? (el.files && el.files.length ? el.files[0].name : "")
             : kind === "radio" || kind === "checkbox" ? (checked ? "on" : "")
             : String(el.value || chosen() || ""),
        options,
        accept: el.getAttribute("accept") || "",
      });
    }
    for (const host of root.querySelectorAll("*")) if (host.shadowRoot) walk(host.shadowRoot);
  };
  walk(document);
  return out;
})()
"""

# A Greenhouse form embedded on a careers page lives in a cross-origin
# iframe our script cannot reach. Its src is a standalone form page.
IFRAME_JS = r"""
(() => {
  const f = [...document.querySelectorAll("iframe")].find((i) => /greenhouse\.io|lever\.co|ashbyhq\.com/i.test(i.src || ""));
  return f ? f.src : "";
})()
"""

SET_TEXT_FN = r"""
(function (ref, value) {
  const el = FIND(document, ref);
  if (!el) return "missing";
  const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const desc = Object.getOwnPropertyDescriptor(proto, "value");
  const set = desc && desc.set ? (v) => desc.set.call(el, v) : (v) => { el.value = v; };
  el.focus();
  set("");
  el.dispatchEvent(new Event("input", { bubbles: true }));
  set(value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  el.dispatchEvent(new Event("blur", { bubbles: true }));
  return el.value === value ? "ok" : "value:" + el.value;
})
""".replace("FIND", FIND_FN.strip())

SET_SELECT_FN = r"""
(function (ref, index) {
  const el = FIND(document, ref);
  if (!el) return "missing";
  const desc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value");
  const value = el.options[index].value;
  if (desc && desc.set) desc.set.call(el, value); else el.value = value;
  el.selectedIndex = index;
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  return el.selectedIndex === index ? "ok" : "index:" + el.selectedIndex;
})
""".replace("FIND", FIND_FN.strip())

CLICK_FN = r"""
(function (ref) {
  const el = FIND(document, ref);
  if (!el) return "missing";
  el.scrollIntoView({ block: "center", behavior: "instant" });
  el.click();
  return "ok";
})
""".replace("FIND", FIND_FN.strip())

# The suggestions a combobox shows, scoped to it: the listbox it names in
# aria-controls / aria-owns, else the widget's own wrapper (react-select
# renders its menu next to its control), else anything visible in the
# document (portals). Text and centre of each, for a real mouse click.
# Visibility is by offsetParent: a hidden phone-country list has a
# bounding box of zero and no offsetParent.
OPTIONS_FN = r"""
(function (ref) {
  const el = FIND(document, ref);
  const text = (n) => (n.innerText || n.textContent || "").replace(/\s+/g, " ").trim();
  const byId = (id) => { try { return document.getElementById(id); } catch (e) { return null; } };
  const collect = (scope) => {
    const out = [];
    for (const n of scope.querySelectorAll("[role=option], [role=listbox] li, [class*='option' i][id*='option' i], [class*='dropdown-results' i] > *, [class*='suggestion' i] li, [class*='autocomplete' i] li")) {
      const r = n.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0 || (n.offsetParent === null && getComputedStyle(n).position !== "fixed")) continue;
      out.push({ text: text(n), x: r.left + r.width / 2, y: r.top + r.height / 2 });
    }
    return out.slice(0, 200);
  };
  if (el) {
    const ids = ((el.getAttribute("aria-controls") || "") + " " + (el.getAttribute("aria-owns") || "")).split(/\s+/).filter(Boolean);
    for (const id of ids) { const box = byId(id); if (box) { const found = collect(box); if (found.length) return found; } }
    const control = el.closest("[class*='select__control'], [class*='control' i], [class*='combobox' i], [class*='autocomplete' i]");
    const wrapper = (control && control.parentElement) || el.parentElement;
    if (wrapper) { const found = collect(wrapper); if (found.length) return found; }
  }
  return collect(document);
})
""".replace("FIND", FIND_FN.strip())

# Whether the focus is somewhere keys will type text.
ACTIVE_EDITABLE_JS = r"""
(() => {
  let el = document.activeElement;
  while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || el.isContentEditable === true;
})()
"""

# Centre of a control, scrolled into view, for a real click. The scroll is
# instant: with `scroll-behavior: smooth` on the page, scrollIntoView
# returns before the scroll happens and the rectangle read is stale. A
# react-select input is a few pixels wide; its control box is the target.
CENTER_FN = r"""
(function (ref) {
  const el = FIND(document, ref);
  if (!el) return null;
  const box = el.closest("[class*='select__control'], [class*='control' i]") || el;
  box.scrollIntoView({ block: "center", behavior: "instant" });
  const r = box.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0 || r.bottom < 0 || r.top > innerHeight) return null;
  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
})
""".replace("FIND", FIND_FN.strip())

# Before an upload: mark the block the input sits in, because Greenhouse
# replaces the input with a filename row once a file is chosen, and the
# name then has to be read off the block instead.
MARK_UPLOAD_FN = r"""
(function (ref) {
  const el = FIND(document, ref);
  if (!el) return false;
  const block = el.closest("[role=group], fieldset, [class*='upload' i], [class*='file' i], [class*='field' i]") || el.parentElement;
  for (const old of document.querySelectorAll("[data-autopilot-upload]")) old.removeAttribute("data-autopilot-upload");
  block.setAttribute("data-autopilot-upload", ref);
  return true;
})
""".replace("FIND", FIND_FN.strip())

# The assistant's logo in front of a field the automation set, so the human
# can see at a glance which values are ours: the answer, the resume, the
# letter. It is the review page's orb at rest (`Bubble` in index.html): a
# flat ring in ink with a hard offset shadow and one purple core, drawn
# static and small. Goes before the label the person reads (the upload
# block for a file input); idempotent per ref; inline styles and fixed
# colours because the page's CSS is not ours. Advice only, no field is
# touched.
LOGO_SVG = (
    '<svg viewBox="-70 -70 140 140" width="18" height="18" aria-hidden="true" '
    'style="display:inline-block;vertical-align:middle;overflow:visible">'
    '<circle r="50" cx="7" cy="7" fill="#000"/>'
    '<circle r="50" fill="#fff" stroke="#000" stroke-width="7"/>'
    '<circle r="14" fill="#6a00ff"/></svg>'
)
# The marks also have to survive the page redrawing itself. Ashby's form is
# one tab of a React page: moving to Overview and back unmounts the form and
# every logo went with it, which reads as "did the resume come off too?" (it
# does not; the file and the answers were still there). So each mark is
# remembered in `window.__autopilotMarks` with its label's text, and one
# MutationObserver puts back any that goes missing. A remount builds fresh
# DOM without our `data-autopilot-ref`, so the label text is the anchor the
# second time round.
MARK_FN = r"""
(function (ref, note, logo) {
  const FIND = FINDFN;
  const state = window.__autopilotMarks = window.__autopilotMarks || {items: {}, watching: false};

  const clean = (el) => ((el && el.innerText) || "").replace(/\s+/g, " ").trim().slice(0, 120);

  function hostByRef(ref) {
    const el = FIND(document, ref);
    // Greenhouse drops the file input once a file is on the slot; the block
    // MARK_UPLOAD_FN tagged before the upload is still there and is the host.
    const block = document.querySelector("[data-autopilot-upload='" + CSS.escape(ref) + "']");
    if (!el && !block) return null;
    let host = null;
    if (!el || el.type === "file") host = block && (block.querySelector("label, legend, [class*='label' i]") || block);
    if (!host && el && el.labels && el.labels.length) host = el.labels[0];
    if (!host && el && el.getAttribute("aria-labelledby")) host = document.getElementById(el.getAttribute("aria-labelledby").split(/\s+/)[0]);
    if (!host && el) host = el.closest("label");
    if (!host && el) { const p = el.parentElement; host = p && p.querySelector("label") || p; }
    return host;
  }

  // The page was rebuilt and the ref is gone: the same question, written the
  // same way, is the only honest anchor left. Exact text, first match only.
  function hostByLabel(text) {
    if (!text) return null;
    for (const el of document.querySelectorAll("label, legend, [class*='label' i]")) {
      if (clean(el) === text) return el;
    }
    return null;
  }

  function place(entry) {
    const host = hostByRef(entry.ref) || hostByLabel(entry.label);
    if (!host) return false;
    if (!entry.label) entry.label = clean(host);
    const old = host.querySelector(":scope > [data-autopilot-mark='" + CSS.escape(entry.ref) + "']");
    if (old) { old.title = "Filled by Autopilot: " + entry.note; return true; }
    const dot = document.createElement("span");
    dot.setAttribute("data-autopilot-mark", entry.ref);
    dot.title = "Filled by Autopilot: " + entry.note;
    dot.setAttribute("aria-label", "Filled by Autopilot");
    dot.style.cssText = "display:inline-block;line-height:0;margin:0 8px 0 0;vertical-align:middle;flex:none;";
    dot.innerHTML = entry.logo;
    host.insertBefore(dot, host.firstChild);
    return true;
  }

  function restore() {
    for (const entry of Object.values(state.items)) {
      if (document.querySelector("[data-autopilot-mark='" + CSS.escape(entry.ref) + "']")) continue;
      place(entry);
    }
  }

  if (!state.watching) {
    state.watching = true;
    let timer = null;
    // Our own insert mutates the DOM too; the "already there" check above
    // ends the round, so the observer settles instead of looping.
    new MutationObserver(() => {
      if (timer) return;
      timer = setTimeout(() => { timer = null; try { restore(); } catch (e) {} }, 200);
    }).observe(document.documentElement, {childList: true, subtree: true});
  }

  const entry = state.items[ref] || (state.items[ref] = {ref: ref, note: note, logo: logo, label: ""});
  entry.note = note;
  entry.logo = logo;
  const ok = place(entry);
  if (!ok) delete state.items[ref];
  return ok;
})
""".replace("FINDFN", FIND_FN.strip())

# After: the name the input holds, else the name shown in its block.
FILE_NAME_FN = r"""
(function (ref, name) {
  const el = FIND(document, ref);
  if (el && el.files && el.files.length) return el.files[0].name;
  const block = document.querySelector('[data-autopilot-upload="' + ref + '"]');
  const shown = block ? (block.innerText || "") : "";
  return name && shown.includes(name) ? name : "";
})
""".replace("FIND", FIND_FN.strip())

# A slot that already holds a file has no input any more on Greenhouse:
# the block shows the filename and a "Remove file" button. Finds the block
# whose label matches `pattern` (a regex source) and does not read as
# `exclude`, and returns its remove button's label without clicking; the
# click is `CLICK_REMOVE_FN`, after the guard has read the label.
FIND_REMOVE_FN = r"""
(function (pattern, exclude) {
  const want = new RegExp(pattern, "i");
  const skip = exclude ? new RegExp(exclude, "i") : null;
  const text = (el) => (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
  const remove = /remove|delete|clear|replace|change|^[×✕x]$/i;
  for (const btn of document.querySelectorAll("button, [role=button], a")) {
    const label = (btn.getAttribute("aria-label") || btn.getAttribute("title") || text(btn)).trim();
    if (!remove.test(label) || label.length > 40) continue;
    let block = btn.parentElement;
    for (let i = 0; i < 6 && block; i++, block = block.parentElement) {
      if (block.querySelector("input[type=file]")) break;
      const heading = block.querySelector("label, legend, [class*='label' i], [id*='label' i]");
      const title = heading ? text(heading) : "";
      if (!title || title.length > 80) continue;
      if (want.test(title) && !(skip && skip.test(title))) {
        for (const old of document.querySelectorAll("[data-autopilot-remove]")) old.removeAttribute("data-autopilot-remove");
        btn.setAttribute("data-autopilot-remove", "1");
        return { label: label, title: title, file: text(block).replace(title, "").trim().slice(0, 120) };
      }
      break;
    }
  }
  return null;
})
"""

# The slot labels `remove_attached` looks for, as regex sources for the page.
RESUME_SLOT = r"\b(resume|r[ée]sum[ée]|cv|curriculum)\b"
COVER_SLOT = r"cover"

CLICK_REMOVE_FN = r"""
(function () {
  const btn = document.querySelector("[data-autopilot-remove]");
  if (!btn) return false;
  btn.click();
  btn.removeAttribute("data-autopilot-remove");
  return true;
})
"""

# Generic label patterns, tried after the adapter's selectors and the
# answers store. Order matters: the first match wins, so "last name" sits
# above "name". Kept literal and short; a wrong guess costs a field the
# human corrects, and the correction lands in `answers` for next time.
LABELS: list[tuple[str, str]] = [
    (r"^(first|given)\s*name", "first_name"),
    (r"^(last|family)\s*name|^surname", "last_name"),
    (r"^preferred\s*(first\s*)?name", "preferred_name"),
    (r"^(full\s*)?name$|^your\s*name$|^legal\s*name", "full_name"),
    (r"\be-?mail\b", "email"),
    (r"\bphone\b.*\b(country|code)\b|\bcountry\s*code\b|\bdial", "phone_country"),
    (r"\b(phone|mobile|telephone|cell)\b", "phone"),
    (r"\blinked\s*in\b", "linkedin"),
    (r"\bgit\s*hub\b", "github"),
    (r"\b(twitter|x\.com)\b", "twitter"),
    (r"^other\b", ""),   # "Other website", "Other links": not a field we know
    (r"\b(portfolio|website|personal\s*site|url|link)\b", "website"),
    (r"\b(street|address\s*line|address)\b", "address"),
    (r"\b(zip|postal)\b", "postal_code"),
    (r"^city\b|\bcity$", "city"),
    (r"\b(state|province|region)\b", "state"),
    (r"^country\b|\bcountry\s*(of\s*residence)?$", "country"),
    (r"\b(location|where\s*(are\s*you|do\s*you)\s*(based|located|live)|current\s*city)\b", "location"),
    (r"\b(current|most\s*recent|present)\s*(company|employer|organi[sz]ation)\b|^(company|employer|organi[sz]ation)$", "current_company"),
    (r"\b(current|most\s*recent|present|job)\s*(title|role|position)\b|^title$", "current_title"),
    (r"\b(school|university|college|institution|alma)\b", "school"),
    (r"\bdegree\b", "degree"),
    (r"\b(major|discipline|field\s*of\s*study|area\s*of\s*study|concentration)\b", "discipline"),
    (r"\b(graduation|grad)\s*(year|date)|\byear\s*of\s*graduation\b", "graduation_year"),
    (r"\bhow\s*did\s*you\s*(hear|find|learn)\b|\bsource\b|\breferr?al\s*source\b", "how_heard"),
    (r"\b(salary|compensation|pay)\s*(expectation|requirement|range)?s?\b|\bexpected\s*(salary|compensation)\b", "salary"),
    (r"\b(start\s*date|available\s*to\s*start|availability|earliest)\b", "start_date"),
    (r"\bgender\b", "gender"),
    (r"\b(race|ethnicity|ethnic)\b", "race"),
    (r"\bveteran\b", "veteran"),
    (r"\bdisabilit", "disability"),
]
_LABELS = [(re.compile(p, re.I), key) for p, key in LABELS]

# `<select>` and radio options to try, in order, per key. The first that
# matches an option's text wins; each falls back to the value itself.
OPTION_ALIASES: dict[str, list[str]] = {
    "phone_country": ["United States", "USA", "US", "+1"],
    "country": ["United States", "United States of America", "USA", "US"],
    # The source is never a real choice on a list built by the employer;
    # "Other" is the answer whenever the configured one is not offered.
    "how_heard": ["Other"],
}


@dataclass
class Field:
    ref: str
    kind: str
    label: str = ""
    group: str = ""
    id: str = ""
    name: str = ""
    autocomplete: str = ""
    placeholder: str = ""
    required: bool = False
    value: str = ""
    options: list[dict] = field(default_factory=list)
    tag: str = ""
    type: str = ""
    accept: str = ""

    @classmethod
    def from_scan(cls, raw: dict) -> "Field":
        known = {k: raw.get(k) for k in cls.__dataclass_fields__ if k in raw}
        return cls(**known)

    @property
    def question(self) -> str:
        """The text the human reads: the group's question for a radio,
        the label otherwise, the name or placeholder when there is none."""
        return self.group or self.label or self.placeholder or self.name or self.id

    def identifiers(self) -> tuple[str, ...]:
        return (self.label, self.group, self.id, self.name, self.autocomplete, self.placeholder)

    def protected(self) -> bool:
        return guard.describes_protected(*self.identifiers())


@dataclass
class Report:
    """What the code fill did. `summary()` and `task_lines()` are what the
    agent and the review page see; the rest is for the archive."""
    ats: str = ""
    target_id: str = ""
    attempted: bool = False
    filled: list[str] = field(default_factory=list)
    missing: list[dict] = field(default_factory=list)   # {"label", "kind", "required"}
    protected: list[str] = field(default_factory=list)
    answered: list[str] = field(default_factory=list)  # open questions written by the tailor model
    corrected: list[str] = field(default_factory=list)  # fields set from the correction store, over autofill
    resume_uploaded: bool = False
    resume_name: str = ""
    cover_letter_uploaded: bool = False
    errors: list[str] = field(default_factory=list)
    note: str = ""
    snapshot: dict = field(default_factory=dict)   # question -> value, after the pass

    @property
    def tab_id(self) -> str:
        return self.target_id[-4:]

    def summary(self) -> str:
        if not self.attempted:
            return f"form fill: not done by code ({self.note})"
        parts = [f"form fill ({self.ats}): {len(self.filled)} field(s) filled by code"]
        parts.append("resume attached" if self.resume_uploaded else "resume NOT attached")
        if self.cover_letter_uploaded:
            parts.append("cover letter attached")
        if self.answered:
            parts.append(f"{len(self.answered)} question(s) answered")
        if self.corrected:
            parts.append(f"{len(self.corrected)} field(s) corrected")
        required = [m for m in self.missing if m.get("required")]
        parts.append(f"{len(self.missing)} left ({len(required)} required)")
        if self.protected:
            parts.append(f"{len(self.protected)} visa/authorisation field(s) untouched")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        return "; ".join(parts)

    def task_lines(self) -> str:
        """The list the agent works from."""
        if not self.missing:
            return "   Nothing is left empty that code could see."
        lines = []
        for m in self.missing:
            mark = " (required)" if m.get("required") else ""
            lines.append(f"   - {m['label']}{mark} [{m['kind']}]")
        return "\n".join(lines)

    def to_json(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


class Adapter:
    """Per-system knowledge. Subclasses override the class attributes; the
    engine does the rest. `SELECTORS` maps a regex over `id|name|autocomplete`
    to a profile key; `RESUME` and `COVER_LETTER` are regexes over the same
    for the file inputs."""
    NAME = ""
    SELECTORS: list[tuple[str, str]] = []
    RESUME: str = ""
    COVER_LETTER: str = ""
    # Plain text inputs that are really autocompletes (Lever's location):
    # the scan cannot tell, the adapter can.
    COMBOBOX: str = ""

    def apply_url(self, url: str) -> str:
        """The page with the form on it, given the posting's URL."""
        return url

    def compiled(self) -> list[tuple[re.Pattern, str]]:
        return [(re.compile(p, re.I), key) for p, key in self.SELECTORS]

    def key_for(self, f: Field) -> str:
        haystack = f"{f.id}|{f.name}|{f.autocomplete}"
        for pattern, key in self.compiled():
            if pattern.search(haystack):
                return key
        return ""


def match_key(f: Field, adapter: Adapter) -> str:
    """Which profile key fills this field: the adapter's selectors, then the
    generic label patterns. Empty when nothing fits."""
    key = adapter.key_for(f)
    if key:
        return key
    if f.kind == "textarea":
        # A text box that size is an open question; only an answer on file fills it.
        return ""
    # A radio's own label is its option ("Other", "Yes"); the question is
    # the group's.
    texts = (f.group,) if f.kind in ("radio", "checkbox") else (f.label, f.group, f.placeholder, f.autocomplete)
    for text in texts:
        if not text or len(text) > GENERIC_LABEL_MAX:
            continue
        for pattern, key in _LABELS:
            if pattern.search(text):
                return key   # "" when the label is explicitly nobody's
    return ""


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9+]+", " ", (text or "").lower()).split())


def pick_option(options: list[dict], wanted: str, aliases: Optional[list[str]] = None) -> Optional[int]:
    """Index of the option whose text best matches: exact, then the option
    starting with the value, then containing it, each alias in turn. The
    empty "Select..." option never matches."""
    candidates = [wanted] + list(aliases or [])
    texts = [_norm(o.get("text", "")) for o in options]
    values = [_norm(o.get("value", "")) for o in options]
    for candidate in candidates:
        c = _norm(candidate)
        if not c:
            continue
        for i, (t, v) in enumerate(zip(texts, values)):
            if t and (t == c or v == c):
                return i
        for i, t in enumerate(texts):
            if t and t.startswith(c + " "):
                return i
        for i, t in enumerate(texts):
            if t and (f" {c} " in f" {t} " or (len(c) > 3 and c in t)):
                return i
    return None


def pick_suggestion(options: list[dict], wanted: str) -> Optional[dict]:
    """The combobox suggestion for a typed value: an exact match, else the
    one suggestion containing the whole value, else the one containing every
    comma-separated part, else the one starting with the first part. Each
    rule must single out one suggestion; two candidates is no answer, since
    "Austin" must never become Austin, Minnesota."""
    if not options:
        return None
    w = _norm(wanted)
    parts = [_norm(p) for p in wanted.split(",") if _norm(p)]
    if not w:
        return None
    exact = [o for o in options if _norm(o["text"]) == w]
    if exact:
        return exact[0]
    rules = [
        lambda t: w in t,
        lambda t: all(p in t for p in parts),
        lambda t: t.startswith(parts[0]),
    ]
    for rule in rules:
        found = [o for o in options if rule(_norm(o["text"]))]
        if len(found) == 1:
            return found[0]
    return None


class Engine:
    def __init__(self, page: Session, adapter: Adapter, profile: Profile,
                 resume: Optional[Path] = None, cover_letter: Optional[Path] = None):
        self.page = page
        self.adapter = adapter
        self.profile = profile
        self.resume = resume
        self.cover_letter = cover_letter
        self.report = Report(ats=adapter.NAME)

    async def call(self, fn: str, *args):
        expression = f"({fn.strip()})({', '.join(json.dumps(a) for a in args)})"
        return await self.page.evaluate(expression)

    async def scan(self) -> list[Field]:
        raw = await self.page.evaluate(SCAN_JS) or []
        fields = [Field.from_scan(r) for r in raw if isinstance(r, dict)]
        if self.adapter.COMBOBOX:
            for f in fields:
                if f.kind == "text" and re.search(self.adapter.COMBOBOX, f"{f.id}|{f.name}|", re.I):
                    f.kind = "combobox"
        return fields

    async def wait_for_fields(self, timeout: Optional[float] = None) -> list[Field]:
        """Fields once the form has rendered: some found, and the count the
        same on two polls in a row. The timeout is resolved here, not in
        the signature, so a test can shorten it."""
        deadline = time.monotonic() + (timeout or FIELDS_TIMEOUT)
        fields: list[Field] = []
        last = -1
        while time.monotonic() < deadline:
            fields = await self.scan()
            if fields and len(fields) == last:
                return fields
            last = len(fields)
            await asyncio.sleep(POLL * 2)
        return fields

    async def run(self) -> Report:
        report = self.report
        fields = await self.wait_for_fields()
        if not fields:
            src = await self.page.evaluate(IFRAME_JS)
            if src:
                # The form is in a cross-origin iframe; its src is the same
                # form as a page of its own.
                await self.page.send("Page.navigate", {"url": src})
                await wait_for_load(self.page)
                fields = await self.wait_for_fields()
        if not fields:
            report.note = "no form fields found on the page"
            return report
        report.attempted = True

        for f in fields:
            try:
                await self.fill_field(f)
            except Exception as error:  # noqa: BLE001 - one field never stops the pass
                report.errors.append(f"{f.question[:60]!r}: {error}")

        await self.upload_files(fields)
        await self.audit()
        return report

    # -- one field ---------------------------------------------------------

    async def fill_field(self, f: Field) -> None:
        if f.kind == "file":
            return
        if f.protected():
            if f.question and f.question not in self.report.protected:
                self.report.protected.append(f.question)
            return
        value = self.profile.answer(f.question) or self.profile.answer(f.label)
        key = ""
        if not value:
            key = match_key(f, self.adapter)
            value = self.profile.get(key) if key else ""
        if not value:
            return
        if await self.set_value(f, value, OPTION_ALIASES.get(key, [])):
            self.report.filled.append(f.question or key)
            await self.mark(f.ref, "from your profile")

    async def set_value(self, f: Field, value: str, aliases: Optional[list[str]] = None) -> bool:
        """Put `value` on the field the way its kind wants: the native
        setter with real typing as fallback for text, an option by text
        for a select, a click for a radio or a box, the menu for a
        combobox. No option or radio is clicked when its label reads as
        a submit. True when the value is on the field."""
        aliases = aliases or []
        if f.kind in ("text", "textarea"):
            if f.value.strip() == value:
                return True
            done = await self.call(SET_TEXT_FN, f.ref, value) == "ok"
            # Lever's location box drops a scripted value on blur and
            # keeps a typed one; the audit reads back what stuck.
            if not done:
                done = await self.type_into(f, value)
            return done
        if f.kind == "select":
            index = pick_option(f.options, value, aliases)
            if index is None:
                return False
            option = f.options[index]
            if guard.describes_submit(option.get("text", "")):
                return False
            return await self.call(SET_SELECT_FN, f.ref, index) == "ok"
        if f.kind == "radio":
            return await self.choose_radio(f, value, aliases)
        if f.kind == "checkbox":
            # Only an explicit answer ticks a box, and only "yes" does.
            if _norm(value) in ("yes", "true", "on", "checked") and not f.value:
                return await self.call(CLICK_FN, f.ref) == "ok"
            return False
        if f.kind == "combobox":
            return await self.fill_combobox(f, value, aliases)
        return False

    # -- the corrections -----------------------------------------------------

    def correctable(self, fields: list[Field]) -> list[tuple[Field, dict]]:
        """Fields the correction store has a value for, by exact label:
        the question or the label as the form shows it. A file slot is
        never one, a visa question is never one, and a textarea is never
        one either: free-form prose is written per job by the tailor
        model, not carried from the last form. Radios come once per
        group, so the group's question is what matches."""
        found = []
        seen: set[str] = set()
        for f in fields:
            if f.kind in ("file", "textarea") or f.protected():
                continue
            record = self.profile.correction(f.question) or self.profile.correction(f.label)
            if not record:
                continue
            if f.kind == "radio":
                if f.question in seen:
                    continue
                seen.add(f.question)
            found.append((f, record))
        return found

    async def apply_corrections(self, fields: list[Field]) -> None:
        """Every field the human corrected on an earlier form gets that
        value now, over whatever Jobright's autofill left there. Jobright
        puts the wrong city on the location field most of the time; once
        the human has fixed it on one form, it is fixed on every form
        that asks. A value already right is counted, not touched."""
        for f, record in self.correctable(fields):
            value = record["value"]
            q = f.question or f.label
            try:
                if f.kind == "radio":
                    done = await self.choose_radio_in_group(fields, f, value)
                else:
                    done = await self.set_value(f, value)
            except Exception as error:  # noqa: BLE001 - one field never stops the rest
                self.report.errors.append(f"correcting {q[:60]!r}: {error}")
                continue
            if done:
                self.report.corrected.append(q)
                was = str(record.get("was") or "")
                await self.mark(f.ref, f"corrected: {value}" + (f" (autofill had {was})" if was else ""))
            else:
                self.report.errors.append(f"could not put the corrected value on {q[:60]!r}")

    async def choose_radio_in_group(self, fields: list[Field], one: Field, value: str) -> bool:
        """The option of `one`'s group whose own label matches `value`."""
        for other in fields:
            if other.kind == "radio" and other.question == one.question:
                if await self.choose_radio(other, value, []):
                    return True
        return False

    async def mark(self, ref: str, note: str) -> None:
        """The purple dot in front of a field we set. Never fails a fill."""
        try:
            await self.call(MARK_FN, ref, note, LOGO_SVG)
        except Exception:  # noqa: BLE001 - the logo is advice
            pass

    # -- the open questions ------------------------------------------------

    def free_questions(self, fields: list[Field]) -> list[Field]:
        """Free-text fields whose label reads as a question: a textarea, or
        a text box with a question mark or a long label. Short labels are
        fields (name, phone), a visa question is never one. Full or empty:
        Jobright's autofill writes generic prose into these, and ours goes
        over it the way the tailored resume goes over its resume.

        A fact asked with a question mark is still a fact (`describes_fact`):
        "What is your legal first name?" and "Preferred pronouns?" belong to
        Jobright's autofill and `base/form.json`, not to the tailor model,
        which answered the second with three sentences of prose about being
        happy to share them.
        """
        found = []
        for f in fields:
            if f.kind not in ("text", "textarea") or f.protected():
                continue
            q = " ".join(f.question.split())
            if not q or describes_fact(q):
                continue
            if f.kind == "textarea" or "?" in q or len(q) > GENERIC_LABEL_MAX:
                found.append(f)
        return found

    @staticmethod
    def one_line(text: str, cap: int = 300) -> str:
        """An answer for a one-line box: one paragraph becomes one sentence.

        A single-line input shows about a dozen words. The model writes two to
        four sentences by the rules, which is right for a textarea and reads as
        a wall in a text box, so the first sentence is kept (up to `cap`).
        """
        body = " ".join((text or "").split())
        if len(body) <= cap:
            end = body.find(". ")
            return body[:end + 1] if 0 < end < len(body) - 2 else body
        cut = body.rfind(". ", 0, cap)
        return body[:cut + 1] if cut > 40 else body[:cap].rstrip() + "..."

    async def answer_questions(self, fields: list[Field], answerer) -> None:
        """Each free-form question answered by the tailor model (`answerer`,
        sync, in a thread) and written into its box, over whatever autofill
        put there. The model only writes prose; what it may say is
        `answer_rules.md`. A refusal or a failure leaves the box as it was
        and says so in the errors."""
        for f in self.free_questions(fields):
            q = " ".join(f.question.split())
            try:
                text = await asyncio.to_thread(answerer, q)
            except Exception as error:  # noqa: BLE001 - one question never stops the rest
                self.report.errors.append(f"answering {q[:60]!r}: {error}")
                continue
            if not text or not text.strip():
                continue
            if f.kind == "text":
                text = self.one_line(text)
            done = await self.call(SET_TEXT_FN, f.ref, text) == "ok"
            if not done:
                done = await self.type_into(f, text)
            if done:
                self.report.answered.append(q)
                await self.mark(f.ref, "answer written by the assistant")
            else:
                self.report.errors.append(f"could not write the answer into {q[:60]!r}")

    async def choose_radio(self, f: Field, value: str, aliases: list[str]) -> bool:
        """A radio is one option of a group; only the option whose own label
        matches the value is clicked, and only when it reads as an answer,
        not a submit."""
        own = f.label
        if pick_option([{"text": own, "value": ""}], value, aliases) is None:
            return False
        if guard.describes_submit(own):
            return False
        if f.value:
            return True
        return await self.call(CLICK_FN, f.ref) == "ok"

    async def type_into(self, f: Field, value: str) -> bool:
        """A real click on the control, then select-all, backspace and the
        value key by key, all as CDP input events. Only when the click put
        the focus in an editable element: a select-all on the page body
        selects the whole page, which is what happened on Lever's
        university picker."""
        centre = await self.call(CENTER_FN, f.ref)
        if not centre:
            return False
        await self.click_at(centre)
        await asyncio.sleep(POLL)
        if not await self.page.evaluate(ACTIVE_EDITABLE_JS):
            return False
        await self.page.send("Input.dispatchKeyEvent", {
            "type": "keyDown", "key": "a", "code": "KeyA", "modifiers": 4, "commands": ["selectAll"]})
        await self.page.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "a", "code": "KeyA", "modifiers": 4})
        for kind in ("keyDown", "keyUp"):
            await self.page.send("Input.dispatchKeyEvent", {
                "type": kind, "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8})
        for char in value:
            await self.page.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": char, "text": char})
            await self.page.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": char})
        return True

    async def escape(self) -> None:
        """Close whatever menu is open. Escape never submits a form."""
        for kind in ("keyDown", "keyUp"):
            await self.page.send("Input.dispatchKeyEvent", {
                "type": kind, "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27})

    async def fill_combobox(self, f: Field, value: str, aliases: list[str]) -> bool:
        """Type the value, wait for the widget's suggestions, click the best
        one with the mouse. react-select ignores a value set from script
        and opens its menu only for a pointer or a key, hence the input
        events. When no suggestion fits, the menu is closed and the field
        left for the agent."""
        if not await self.type_into(f, value):
            await self.escape()
            return False
        deadline = time.monotonic() + OPTIONS_TIMEOUT
        choice = None
        while time.monotonic() < deadline and not choice:
            await asyncio.sleep(POLL)
            options = await self.call(OPTIONS_FN, f.ref) or []
            for candidate in [value] + aliases:
                choice = pick_suggestion(options, candidate)
                if choice:
                    break
        if not choice or guard.describes_submit(choice.get("text", "")):
            await self.escape()
            return False
        await self.click_at(choice)
        await asyncio.sleep(POLL)
        return True

    async def click_at(self, point: dict) -> None:
        for kind in ("mousePressed", "mouseReleased"):
            await self.page.send("Input.dispatchMouseEvent", {
                "type": kind, "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1,
            })

    # -- files -------------------------------------------------------------

    def file_inputs(self, fields: list[Field]) -> tuple[Optional[Field], Optional[Field]]:
        """(resume input, cover letter input). The adapter's selector first,
        then whichever file input's label reads as one or the other. A slot
        that reads as a cover letter never takes the resume."""
        files = [f for f in fields if f.kind == "file"]
        resume = cover = None
        for f in files:
            hay = f"{f.id}|{f.name}|{f.label}"
            if self.adapter.RESUME and re.search(self.adapter.RESUME, hay, re.I):
                resume = resume or f
            if self.adapter.COVER_LETTER and re.search(self.adapter.COVER_LETTER, hay, re.I):
                cover = cover or f
        for f in files:
            is_cover = guard.describes_cover_letter(*f.identifiers())
            if resume is None and not is_cover and guard.describes_resume(*f.identifiers()):
                resume = f
            if cover is None and is_cover and f is not resume:
                cover = f
        if resume is None and len(files) == 1 and not guard.describes_cover_letter(*files[0].identifiers()):
            resume = files[0]
        return resume, cover

    async def upload(self, f: Field, path: Path) -> str:
        """`DOM.setFileInputFiles` on the input's own node; returns the name
        the input holds afterwards, read back rather than assumed."""
        handle = await self.page.send("Runtime.evaluate", {
            "expression": f"({FIND_FN.strip()})(document, {json.dumps(f.ref)})",
        })
        object_id = (handle.get("result") or {}).get("objectId")
        if not object_id:
            raise RuntimeError("file input vanished")
        node = await self.page.send("DOM.describeNode", {"objectId": object_id})
        backend = (node.get("node") or {}).get("backendNodeId")
        await self.call(MARK_UPLOAD_FN, f.ref)
        await self.page.send("DOM.setFileInputFiles", {
            "files": [str(path.resolve())], "backendNodeId": backend,
        })
        await asyncio.sleep(POLL * 4)
        return str(await self.call(FILE_NAME_FN, f.ref, path.name) or "")

    async def remove_attached(self, pattern: str, exclude: str = "") -> bool:
        """Click the remove button on the file slot whose label matches
        `pattern`, so its input reappears. False when there is none. The
        button's label goes through the submit guard first: the one click
        this makes on a form is never on anything that reads as submit."""
        found = await self.call(FIND_REMOVE_FN, pattern, exclude)
        if not found:
            return False
        label = str(found.get("label", ""))
        if guard.describes_submit(label, str(found.get("title", ""))):
            self.report.errors.append(f"refused to press {label!r} to clear the slot")
            return False
        if not await self.call(CLICK_REMOVE_FN):
            return False
        log.info("cleared %r (%s) with %r", found.get("title"), found.get("file"), label)
        await asyncio.sleep(POLL * 4)
        return True

    async def upload_files(self, fields: list[Field]) -> None:
        resume_input, cover_input = self.file_inputs(fields)
        report = self.report
        if self.resume:
            if resume_input is None:
                report.errors.append("no resume file input found")
            else:
                try:
                    name = await self.upload(resume_input, self.resume)
                    report.resume_uploaded = name == self.resume.name
                    report.resume_name = name
                    if report.resume_uploaded:
                        await self.mark(resume_input.ref, "the tailored resume")
                    if not report.resume_uploaded:
                        report.errors.append(f"resume upload did not take (input holds {name!r})")
                except Exception as error:  # noqa: BLE001
                    report.errors.append(f"resume upload: {error}")
        if self.cover_letter and cover_input is not None:
            try:
                name = await self.upload(cover_input, self.cover_letter)
                report.cover_letter_uploaded = name == self.cover_letter.name
                if report.cover_letter_uploaded:
                    await self.mark(cover_input.ref, "the cover letter")
            except Exception as error:  # noqa: BLE001
                report.errors.append(f"cover letter upload: {error}")

    # -- what is left ------------------------------------------------------

    async def audit(self) -> None:
        fields = await self.scan()
        self.report.snapshot = snapshot_of(fields)
        self._audit(fields)

    def _audit(self, fields: list[Field]) -> None:
        """List what is still empty, one entry per question (a radio group
        is one question, not five radios)."""
        seen: set[str] = set()
        groups: dict[str, bool] = {}
        for f in fields:
            if f.kind in ("radio", "checkbox"):
                q = f.question
                groups[q] = groups.get(q, False) or bool(f.value)
        for f in fields:
            if f.kind == "file":
                continue
            q = f.question
            if not q or q in seen:
                continue
            if f.kind in ("radio", "checkbox"):
                if groups.get(q):
                    continue
                if f.kind == "checkbox" and not f.required:
                    continue
            elif f.value.strip():
                continue
            seen.add(q)
            if f.protected():
                if q not in self.report.protected:
                    self.report.protected.append(q)
                continue
            self.report.missing.append({"label": q[:200], "kind": f.kind, "required": bool(f.required)})


def snapshot_of(fields: list[Field]) -> dict:
    """The form as a human reads it: question -> value. A radio group is
    its chosen option's label, a file its name, a visa question is left
    out entirely so nothing about it is ever recorded or learned."""
    out: dict = {}
    for f in fields:
        q = f.question
        if not q or f.protected():
            continue
        if f.kind in ("radio", "checkbox"):
            if f.value:
                out[q] = f.label if f.group else "yes"
            else:
                out.setdefault(q, "")
        elif f.kind == "select":
            chosen = next((o.get("text", "") for o in f.options if o.get("value", "") == f.value), f.value)
            out[q] = chosen if f.value else ""
        else:
            out[q] = f.value
    return out


def find_target(cdp_url: str, url: str, target_id: str = "") -> str:
    """The tab holding the form: the id when it is still open, else the
    page whose URL is the same form (`autofill.same_page`), else "".
    Read off the browser's /json list; nothing is attached."""
    import json as _json
    import urllib.request

    try:
        with urllib.request.urlopen(f"{cdp_url}/json", timeout=5) as response:
            pages = [p for p in _json.loads(response.read()) if p.get("type") == "page"]
    except Exception:  # noqa: BLE001 - no browser, no tab
        return ""
    ids = {p.get("id") for p in pages}
    if target_id and target_id in ids:
        return target_id
    for p in pages:
        if same_page(url, p.get("url") or ""):
            return p.get("id", "")
    return ""


def same_site(cdp_url: str, target_id: str, url: str) -> bool:
    """Whether the tab is still on the job's host. Read off /json."""
    import json as _json
    import urllib.request
    from urllib.parse import urlsplit

    try:
        with urllib.request.urlopen(f"{cdp_url}/json", timeout=5) as response:
            pages = _json.loads(response.read())
    except Exception:  # noqa: BLE001
        return False
    for p in pages:
        if p.get("id") == target_id:
            return urlsplit(p.get("url") or "").netloc == urlsplit(url or "").netloc
    return False


def identifiers_of(fields: list[Field]) -> dict:
    """What identifies each field beyond its label, keyed the way
    `snapshot_of` keys the values: the control's id and name, its kind,
    the tag. Kept next to a correction so the row on the page says which
    control on which system it was, not only the words over it."""
    out: dict = {}
    for f in fields:
        q = f.question
        if not q or f.protected() or q in out:
            continue
        out[q] = {k: v for k, v in (("id", f.id), ("name", f.name), ("kind", f.kind),
                                    ("tag", f.tag), ("type", f.type)) if v}
    return out


async def snapshot(cdp_url: str, target_id: str, detail: bool = False) -> dict:
    """The form in an existing tab, question -> value. Attaches to the
    tab, scans, detaches; nothing is touched. Empty when the tab is gone.
    With `detail`, `{"fields": question -> value, "meta": question ->
    identifiers, "text": the page's visible text, "url"}` instead, which
    is what the server's watch reads to see a confirmation page."""
    import json as _json
    import urllib.request
    from itertools import count

    from websockets.asyncio.client import connect

    from ..autofill import _send

    with urllib.request.urlopen(f"{cdp_url}/json/version", timeout=5) as response:
        ws_url = _json.loads(response.read())["webSocketDebuggerUrl"]
    async with connect(ws_url, max_size=None) as socket:
        ids = count(1)
        attached_ = await _send(socket, "Target.attachToTarget", {"targetId": target_id, "flatten": True}, None, ids)
        page = Session(socket, attached_["sessionId"], ids)
        await page.send("Runtime.enable")
        try:
            raw = await page.evaluate(SCAN_JS) or []
            fields = [Field.from_scan(r) for r in raw if isinstance(r, dict)]
            if detail:
                text = await page.evaluate(PAGE_TEXT_JS)
                url = await page.evaluate("location.href")
                return {"fields": snapshot_of(fields), "meta": identifiers_of(fields),
                        "text": str(text or "")[:TEXT_CAP], "url": str(url or "")}
            return snapshot_of(fields)
        finally:
            try:
                await _send(socket, "Target.detachFromTarget", {"sessionId": page.session_id}, None, ids)
            except Exception:  # noqa: BLE001
                pass


async def run(page: Session, adapter: Adapter, profile: Profile, target_id: str = "",
              resume: Optional[Path] = None, cover_letter: Optional[Path] = None) -> Report:
    engine = Engine(page, adapter, profile, resume, cover_letter)
    engine.report.target_id = target_id
    await wait_for_load(page)
    return await engine.run()


async def fill(cdp_url: str, url: str, adapter: Adapter, profile: Profile,
               resume: Optional[Path] = None, cover_letter: Optional[Path] = None) -> Report:
    """Open the form in a new tab on the browser at `cdp_url`, fill it,
    detach. The tab stays open for the agent."""
    async with attached(cdp_url, adapter.apply_url(url)) as (page, target_id):
        await page.send("DOM.enable")
        return await run(page, adapter, profile, target_id, resume, cover_letter)


async def run_documents(page: Session, adapter: Adapter, target_id: str = "",
                        resume: Optional[Path] = None, cover_letter: Optional[Path] = None,
                        answerer=None, corrections: Optional[Profile] = None) -> Report:
    """The files, the corrections and the open questions, on a form
    something else has filled: the tailored resume over whatever Jobright
    attached, the cover letter where there is a slot for one, every field
    the human corrected on an earlier form set to that value when a
    `corrections` profile is given, then every free-form question answered
    by the tailor model when an `answerer` is given. No other field is
    touched. `attempted` stays False so the agent's task is the Jobright
    one; the upload flags, `corrected` and `answered` are what change."""
    # `Profile.__bool__` is its contact details; a store of corrections alone is
    # falsy, so the test is on None, not truth.
    engine = Engine(page, adapter, corrections if corrections is not None else Profile({}), resume, cover_letter)
    report = engine.report
    report.target_id = target_id
    report.note = "documents only"
    fields = await engine.wait_for_fields()
    if not fields:
        report.errors.append("no form fields found on the page")
        return report
    # Greenhouse drops the resume input once a file (Jobright's) is on the
    # slot and shows a "Remove file" button instead. Clear the slot so the
    # input comes back; the button is read and guarded before the click.
    resume_input, cover_input = engine.file_inputs(fields)
    for wanted, have, pattern, exclude, what in (
        (resume, resume_input, RESUME_SLOT, COVER_SLOT, "resume"),
        (cover_letter, cover_input, COVER_SLOT, "", "cover letter"),
    ):
        if wanted and have is None:
            try:
                if await engine.remove_attached(pattern, exclude):
                    fields = await engine.wait_for_fields()
            except Exception as error:  # noqa: BLE001 - then the agent replaces it
                report.errors.append(f"clearing the {what} slot: {error}")
    await engine.upload_files(fields)
    # The uploads may have re-rendered the form; read it again.
    fields = await engine.scan() or fields
    if corrections is not None and corrections.corrections:
        await engine.apply_corrections(fields)
        fields = await engine.scan() or fields
    if answerer is not None:
        await engine.answer_questions(fields, answerer)
    return report


async def upload_documents(cdp_url: str, target_id: str, adapter: Adapter,
                           resume: Optional[Path] = None, cover_letter: Optional[Path] = None,
                           answerer=None, corrections: Optional[Profile] = None) -> Report:
    """`run_documents` in the tab `target_id`, attached over CDP. The tab
    stays open; the agent takes over in it afterwards."""
    async with attached(cdp_url, target_id=target_id) as (page, _):
        await page.send("DOM.enable")
        return await run_documents(page, adapter, target_id, resume, cover_letter, answerer, corrections)
