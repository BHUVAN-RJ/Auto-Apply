"""Deterministic form filling for the systems whose forms are stable enough
to fill without a model. `adapter_for(url)` picks one by the URL through
`browser.ats.detect`; a system with no adapter here keeps the Jobright
path. Adding a system is one module with an `Adapter` subclass and a line
in `ADAPTERS`.

There are two ways in, and they answer different questions. An `Adapter`
tells the engine which of a page's controls is which, by identifier and then
by the look of the label - the only thing that can work on a form nobody has
read. `table.json` is the other: where a system's own anchors have been seen
on a real page, they are written down with the timings they need, and
`steps.Runner` runs them scoped to the element they belong to. `rules_for`
returns None for a system that is not in the table, which is the engine's
own matching, unchanged.
"""
from __future__ import annotations

from typing import Optional

from .. import ats
from .ashby import Ashby
from .engine import (NOT_A_FORM, Adapter, Report, fill, find_target, mark_put_now, same_site,
                     snapshot, upload_documents)
from .greenhouse import Greenhouse
from .lever import Lever
from .profile import Profile, load, load_corrections
from .steps import Found, Outcome, Runner
from .table import FieldRule, Rules, Step, rules_for

ADAPTERS: dict[str, type[Adapter]] = {
    "ashby": Ashby,
    "greenhouse": Greenhouse,
    "lever": Lever,
}


def adapter_for(url: str) -> Optional[Adapter]:
    cls = ADAPTERS.get(ats.detect(url))
    return cls() if cls else None


__all__ = ["Adapter", "FieldRule", "Found", "Outcome", "Profile", "Report", "Rules", "Runner",
           "Step", "adapter_for", "fill", "find_target", "load", "load_corrections",
           "mark_put_now", "rules_for", "same_site", "snapshot", "upload_documents"]
