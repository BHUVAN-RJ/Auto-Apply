"""The parallel run: what our own table would have done with this page.

The live flow is untouched. Jobright presses its own autofill, `browser/`
fills the way it fills, the person works the form - and alongside all of it
this resolves every selector in `browser/forms/table.json` against the page
in front of them and writes down what it *would* have filled, what it could
not see, and what the page already held.

It is read-only on purpose and that is not timidity. Two writers on one
Workday slot is the 2026-10-11 defect exactly: the fill uploading at
10:23:37.086 and the banner's chip putting at 10:23:37.929, and whichever
landed second added a row rather than replacing one. A shadow that writes is
a second writer.

What it buys is a scorecard per page: `would_fill` 14 of 18 and the four it
cannot see, named. When that reads clean across a page class, switching the
real fill onto the table is an evidence-based change rather than a hope.
"""
from __future__ import annotations

from typing import Optional

from browser.forms import steps as form_steps
from browser.forms import table as form_table


async def score(page, url: str, session, tab: str) -> Optional[dict]:
    """Resolve the table against this page and record the result.

    Returns None when no system in the table claims the URL, which is most
    pages and is not a failure: the table is deliberately small and grows
    only where a page has been read.
    """
    try:
        rules = form_table.rules_for(url)
    except Exception as error:  # noqa: BLE001 - a malformed table is not the person's problem
        session.write({"kind": "shadow_error", "tab": tab, "why": str(error)[:160]})
        return None
    if rules is None:
        return None

    runner = form_steps.Runner(page, rules)
    found: dict[str, str] = {}
    missing: list[str] = []
    broken: dict[str, str] = {}
    for key, rule in rules.fields.items():
        hit = None
        for expr in rule.paths:
            try:
                hit = await runner.first(expr)
            except ValueError as error:          # the expression does not compile
                broken[key] = str(error)[:120]
                hit = None
                break
            if hit is not None:
                break
        if hit is None:
            missing.append(key)
        else:
            found[key] = hit.ref

    pages = {}
    for name in form_table.PAGE_KEYS:
        expr = rules.page(name)
        if not expr:
            continue
        try:
            pages[name] = await runner.count(expr) > 0
        except ValueError as error:
            broken[f"page:{name}"] = str(error)[:120]

    # What the page already holds where we would have written, read from the
    # live controls rather than assumed. This is the comparison that matters:
    # a field we can see and the person had to correct is a field our value
    # would have been wrong in.
    holding = {}
    for key, ref in found.items():
        try:
            holding[key] = bool(await page.evaluate(
                f"(() => {{ const el = document.querySelector('[data-autopilot-ref=\"{ref}\"]');"
                f" return !!(el && (el.value || el.textContent || '').trim()); }})()"))
        except Exception:  # noqa: BLE001
            holding[key] = None

    row = {
        "kind": "shadow", "tab": tab, "system": rules.name, "url": url.split("?")[0],
        "fields_in_table": len(rules.fields),
        "would_fill": sorted(found),
        "cannot_see": sorted(missing),
        "already_filled": sorted(k for k, v in holding.items() if v),
        "pages": pages,
        "broken": broken,
        # The headline number, so a session can be read without arithmetic.
        "score": f"{len(found)}/{len(rules.fields)}",
    }
    session.write(row)
    session.tally["shadow pages seen"] += 1
    session.tally["shadow fields resolved"] += len(found)
    session.tally["shadow fields missed"] += len(missing)
    if broken:
        session.tally["shadow broken selectors"] += len(broken)
    return row
