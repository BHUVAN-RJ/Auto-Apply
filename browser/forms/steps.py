"""Runs what `table.py` reads, over CDP.

The engine's own path searches the whole page for a control that looks
right. This one is told where to look: a field's `paths` resolve to one
element, and every step of that field is written against it (`%FIELD%`), so
a step cannot wander. That is the difference that matters - the clearing
control pressed on 2026-10-11 was a Work Experience "Delete" button reached
by climbing fourteen ancestors from a resume slot, and the same climb on
another form pressed a phone country selector. A step scoped to its own
slot has nowhere to climb to.

The elements it finds are tagged with the same `data-autopilot-ref` the
engine's scan uses, so every primitive already written - set the value
through the native setter, click, read a filename back, put the logo in
front of the label - works unchanged on them. Refs from here carry an `x`
prefix: `SCAN_JS` numbers its own from the highest number on the page, and
`Number("x3")` is not a number, so the two can never collide.

**This module cannot press Submit.** There is no method that takes the
table's `submit` expression, `advance()` refuses a control the guard reads
as a submit, and `tests/test_invariants.py` checks both halves.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Optional

from .. import guard
from ..autofill import Session
from .engine import CLICK_FN, FIND_FN, MARK_UPLOAD_FN, POLL, SET_TEXT_FN
from .table import FIELD_TOKEN, FieldRule, Rules, Step

# Every element an XPath selected, tagged so the ref-based primitives can
# reach it again. `visible` is by `offsetParent`, the same test the scan
# uses: a Workday page keeps the pages it is done with in the DOM, and a
# control on one of those is not the one in front of the person.
XPATH_FN = r"""
(function (expr, scope, tag) {
  const root = scope ? FIND(document, scope) : document;
  if (!root) return [];
  const out = [];
  let found;
  try {
    found = document.evaluate(expr, root, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
  } catch (e) {
    return [{ error: String(e && e.message || e) }];
  }
  for (let i = 0; i < found.snapshotLength; i++) {
    const el = found.snapshotItem(i);
    if (!el || el.nodeType !== 1) continue;
    let ref = el.getAttribute("data-autopilot-ref");
    if (!ref) { ref = tag + "-" + i; el.setAttribute("data-autopilot-ref", ref); }
    const label = (el.getAttribute("aria-label") || el.getAttribute("title") || "").trim();
    const text = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
    out.push({
      ref: ref,
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute("type") || "").toLowerCase(),
      label: label,
      text: text.slice(0, 200),
      disabled: !!(el.disabled || el.getAttribute("aria-disabled") === "true"),
      visible: !!(el.offsetParent || el.getClientRects().length),
    });
  }
  return out;
})
""".replace("FIND", FIND_FN.strip())


@dataclass
class Found:
    ref: str
    tag: str = ""
    type: str = ""
    label: str = ""
    text: str = ""
    disabled: bool = False
    visible: bool = True

    @property
    def spoken(self) -> str:
        """What a reader would call it. The guard is given this and never a
        coded name: a class of `remove` is enough to recognise a control and
        never enough to promise what it does (2026-10-11)."""
        return self.label or self.text


@dataclass
class Outcome:
    """What running one field did, in the words the review page shows."""
    key: str
    done: bool = False
    why: str = ""
    ref: str = ""
    notes: list[str] = dataclass_field(default_factory=list)


class Runner:
    def __init__(self, page: Session, rules: Rules,
                 documents: Optional[dict[str, Path]] = None):
        self.page = page
        self.rules = rules
        self.documents = documents or {}
        self._tag = 0

    # --- finding ------------------------------------------------------

    async def _evaluate(self, expr: str, scope: str = "") -> list[Found]:
        self._tag += 1
        raw = await self.page.evaluate(
            f"({XPATH_FN})({json.dumps(expr)}, {json.dumps(scope)}, "
            f"{json.dumps('x%d' % self._tag)})") or []
        out = []
        for r in raw:
            if not isinstance(r, dict):
                continue
            if r.get("error"):
                raise ValueError(f"{expr}: {r['error']}")
            out.append(Found(ref=str(r.get("ref", "")), tag=str(r.get("tag", "")),
                             type=str(r.get("type", "")), label=str(r.get("label", "")),
                             text=str(r.get("text", "")), disabled=bool(r.get("disabled")),
                             visible=bool(r.get("visible"))))
        return out

    async def all(self, expr: str, scope: str = "") -> list[Found]:
        """Every visible, enabled match. An expression that does not compile
        raises rather than answering "nothing here": a silent empty list is
        how a broken selector reads as a form with no fields on it."""
        return [f for f in await self._evaluate(expr, scope) if f.visible and not f.disabled]

    async def first(self, expr: str, scope: str = "") -> Optional[Found]:
        found = await self.all(expr, scope)
        return found[0] if found else None

    async def count(self, expr: str, scope: str = "") -> int:
        return len(await self.all(expr, scope))

    def _scoped(self, path: str, field_ref: str) -> tuple[str, str]:
        """A step's path and the ref it is relative to. `%FIELD%` becomes a
        scope rather than string-pasting the field's own expression, so the
        step is evaluated inside the element the field actually resolved to
        and not inside whatever that expression matches a second time."""
        if FIELD_TOKEN in path:
            return path.replace(FIELD_TOKEN, "."), field_ref
        return path, ""

    # --- pages --------------------------------------------------------

    async def at(self, key: str) -> bool:
        """Is this page the one `key` names? A page with no expression in
        the table is never claimed."""
        expr = self.rules.page(key)
        return bool(expr) and await self.count(expr) > 0

    async def wait_for(self, key: str, timeout: Optional[float] = None) -> bool:
        deadline = time.monotonic() + (timeout if timeout is not None
                                       else self.rules.param("render", 15.0))
        while time.monotonic() < deadline:
            if await self.at(key):
                return True
            await asyncio.sleep(self.rules.param("settle", 0.25))
        return False

    async def advance(self) -> Outcome:
        """Press this page's own "Save and Continue".

        Two things stop it being Submit, and they are independent. The
        table's `advance` excludes the submit wording, although the control
        carries the same `data-automation-id` either way; and the label of
        whatever that returns is read by `guard.describes_submit` before
        anything is pressed. The table is data and could be edited; the
        guard is the rule.
        """
        out = Outcome(key="advance")
        expr = self.rules.page("advance")
        if not expr:
            out.why = "this system has no continue button in the table"
            return out
        control = await self.first(expr)
        if control is None:
            out.why = "no continue button on this page"
            return out
        if guard.describes_submit(control.spoken):
            out.why = f"refused: {control.spoken!r} reads as a submit"
            return out
        await self.page.evaluate(f"({CLICK_FN})({json.dumps(control.ref)})")
        await asyncio.sleep(self.rules.param("settle", 0.25))
        out.done, out.ref = True, control.ref
        return out

    # --- slots --------------------------------------------------------

    async def slot_rows(self, field_ref: str) -> int:
        """How many files are on the slot. Workday's Resume/CV is a dropzone
        that keeps a row per file, so this is what says whether a clear
        actually cleared: a press that changes nothing must stop the loop
        rather than be reported as "replaced"."""
        expr = self.rules.slots.get("rows")
        if not expr:
            return 0
        path, scope = self._scoped(expr, field_ref)
        return await self.count(path, scope)

    async def clear_slot(self, field_ref: str, limit: int = 4) -> int:
        """Press the slot's own clearing control until no row goes. Returns
        how many went."""
        expr = self.rules.slots.get("clear")
        if not expr:
            return 0
        gone = 0
        for _ in range(limit):
            before = await self.slot_rows(field_ref)
            path, scope = self._scoped(expr, field_ref)
            control = await self.first(path, scope)
            if control is None:
                break
            if guard.describes_submit(control.spoken):
                break
            await self.page.evaluate(f"({CLICK_FN})({json.dumps(control.ref)})")
            await asyncio.sleep(self.rules.param("settle", 0.25) * 2)
            after = await self.slot_rows(field_ref)
            if before and after >= before:
                break
            gone += 1
        return gone

    # --- the steps ----------------------------------------------------

    async def _click(self, step: Step, field_ref: str) -> tuple[bool, str]:
        path, scope = self._scoped(step.path, field_ref)
        pressed = 0
        for _ in range(max(1, step.repeat)):
            control = await self.first(path, scope)
            if control is None:
                break
            if guard.describes_submit(control.spoken):
                return False, f"refused: {control.spoken!r} reads as a submit"
            await self.page.evaluate(f"({CLICK_FN})({json.dumps(control.ref)})")
            pressed += 1
            if step.settle:
                await asyncio.sleep(step.settle)
        if not pressed:
            return False, "nothing to press"
        return True, f"pressed {pressed}" if pressed > 1 else ""

    async def _upload(self, step: Step, field_ref: str) -> tuple[bool, str]:
        document = self.documents.get(step.document)
        if document is None:
            return False, f"no {step.document} to put on"
        path, scope = self._scoped(step.path, field_ref)
        control = await self.first(path, scope)
        if control is None:
            return False, "no file input on the slot"
        handle = await self.page.send("Runtime.evaluate", {
            "expression": f"({FIND_FN.strip()})(document, {json.dumps(control.ref)})"})
        object_id = (handle.get("result") or {}).get("objectId")
        if not object_id:
            return False, "the file input vanished"
        node = await self.page.send("DOM.describeNode", {"objectId": object_id})
        backend = (node.get("node") or {}).get("backendNodeId")
        await self.page.evaluate(f"({MARK_UPLOAD_FN})({json.dumps(control.ref)})")
        await self.page.send("DOM.setFileInputFiles",
                             {"files": [str(document.resolve())], "backendNodeId": backend})
        await asyncio.sleep(POLL * 4)
        return True, ""

    async def _expect_name(self, step: Step, field_ref: str) -> tuple[bool, str]:
        """The filename read back off the page, not the upload's own word for
        it. The model has claimed a resume was attached after every upload
        was refused; so has a form."""
        document = self.documents.get(step.document)
        if document is None:
            return False, f"no {step.document} to look for"
        path, scope = self._scoped(step.path, field_ref)
        deadline = time.monotonic() + self.rules.wait_for(step, 10.0)
        while True:
            for shown in await self.all(path, scope):
                if document.name in shown.text or document.name in shown.label:
                    return True, ""
            if time.monotonic() >= deadline:
                return False, f"{document.name} is not on the slot"
            await asyncio.sleep(self.rules.param("settle", 0.25))

    async def _expect_gone(self, step: Step, field_ref: str) -> tuple[bool, str]:
        path, scope = self._scoped(step.path, field_ref)
        deadline = time.monotonic() + self.rules.wait_for(step, 5.0)
        while True:
            if await self.count(path, scope) == 0:
                return True, ""
            if time.monotonic() >= deadline:
                return False, "still on the page"
            await asyncio.sleep(self.rules.param("settle", 0.25))

    async def _write(self, ref: str, value: str) -> tuple[bool, str]:
        result = await self.page.evaluate(
            f"({SET_TEXT_FN})({json.dumps(ref)}, {json.dumps(value)})")
        return (result == "ok"), ("" if result == "ok" else str(result))

    async def _set_text(self, step: Step, field_ref: str, value: str) -> tuple[bool, str]:
        """A step with no path of its own writes into the field itself. It
        must not re-resolve: `.` evaluated against the document is the
        document, which is not an element, so the field read as "no box to
        write in" on a page whose box was right there."""
        if not step.path or step.path in (".", FIELD_TOKEN):
            return await self._write(field_ref, value)
        path, scope = self._scoped(step.path, field_ref)
        control = await self.first(path, scope)
        if control is None:
            return False, "no box to write in"
        return await self._write(control.ref, value)

    async def _select_option(self, step: Step, field_ref: str, value: str) -> tuple[bool, str]:
        """Open the control, then click the option whose text is the value.
        Workday's dropdown is a button and a listbox in a popup, not a
        `<select>`, so there is nothing to set."""
        wanted = (step.value or value or "").strip().lower()
        if not wanted:
            return False, "no value to choose"
        path, scope = self._scoped(step.path, field_ref)
        control = await self.first(path, scope)
        if control is None:
            return False, "no dropdown here"
        await self.page.evaluate(f"({CLICK_FN})({json.dumps(control.ref)})")
        await asyncio.sleep(self.rules.param("settle", 0.25))
        option_expr = self.rules.options.get("option")
        if not option_expr:
            return False, "this system has no option path in the table"
        deadline = time.monotonic() + self.rules.param("options", 2.0)
        while True:
            for option in await self.all(option_expr):
                if option.text.strip().lower() == wanted:
                    await self.page.evaluate(f"({CLICK_FN})({json.dumps(option.ref)})")
                    return True, ""
            if time.monotonic() >= deadline:
                return False, f"no option reads {wanted!r}"
            await asyncio.sleep(self.rules.param("settle", 0.25))

    async def run_field(self, rule: FieldRule, value: str = "") -> Outcome:
        """One field: resolve it, then run its steps against it. A field with
        no steps is a text box and `value` is what goes in it."""
        out = Outcome(key=rule.key)
        target: Optional[Found] = None
        for expr in rule.paths:
            target = await self.first(expr)
            if target is not None:
                break
        if target is None:
            out.why = "not on this page"
            return out
        out.ref = target.ref
        if rule.is_text:
            if not value:
                out.why = "nothing to write"
                return out
            out.done, out.why = await self._write(target.ref, value)
            return out
        for i, step in enumerate(rule.steps, 1):
            if step.method == "click":
                ok, why = await self._click(step, target.ref)
            elif step.method == "upload":
                ok, why = await self._upload(step, target.ref)
            elif step.method == "expect_name":
                ok, why = await self._expect_name(step, target.ref)
            elif step.method == "expect_gone":
                ok, why = await self._expect_gone(step, target.ref)
            elif step.method == "set_text":
                ok, why = await self._set_text(step, target.ref, value)
            elif step.method == "select_option":
                ok, why = await self._select_option(step, target.ref, value)
            else:
                ok, why = False, f"'{step.method}' is not a method"
            if why:
                out.notes.append(f"step {i} ({step.method}): {why}")
            if not ok and not step.optional:
                out.why = why or f"step {i} ({step.method}) did not run"
                return out
            if step.settle:
                await asyncio.sleep(step.settle)
        out.done = True
        return out
