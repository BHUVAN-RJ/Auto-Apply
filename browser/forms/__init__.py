"""Deterministic form filling for the systems whose forms are stable enough
to fill without a model. `adapter_for(url)` picks one by the URL through
`browser.ats.detect`; a system with no adapter here keeps the Jobright
path. Adding a system is one module with an `Adapter` subclass and a line
in `ADAPTERS`.
"""
from __future__ import annotations

from typing import Optional

from .. import ats
from .ashby import Ashby
from .engine import Adapter, Report, fill, find_target, same_site, snapshot, upload_documents
from .greenhouse import Greenhouse
from .lever import Lever
from .profile import Profile, load, load_corrections

ADAPTERS: dict[str, type[Adapter]] = {
    "ashby": Ashby,
    "greenhouse": Greenhouse,
    "lever": Lever,
}


def adapter_for(url: str) -> Optional[Adapter]:
    cls = ADAPTERS.get(ats.detect(url))
    return cls() if cls else None


__all__ = ["Adapter", "Report", "Profile", "adapter_for", "fill", "find_target", "load", "load_corrections", "same_site", "snapshot", "upload_documents"]
