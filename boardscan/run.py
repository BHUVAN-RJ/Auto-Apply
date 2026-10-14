"""The daily read, on a thread, because a crawl nobody runs is a crawl that
does not exist.

The shape is `scout/run.py`'s: a tick every minute that looks at what is
due, so there is no scheduler to install and nothing outside the process to
go wrong. Two jobs are due on different clocks:

* **The crawl**, once a day (`RUNS_PER_DAY`). 138 seconds for the whole
  registry, and the roles it finds are the morning's news.
* **The harvest**, once a week (`HARVEST_EVERY_DAYS`). The lists on GitHub
  gain a handful of companies a week, not a thousand, and each harvest is
  28 MB of other people's files.

`AUTOPILOT_BOARDSCAN=0` turns the thread off; the buttons on the page keep
working either way. One job at a time, the same lock the page's buttons use,
so a scheduled crawl can never start on top of a running one.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from boardscan import crawl as crawler
from boardscan import registry, sources

ENV = "AUTOPILOT_BOARDSCAN"
TICK = 60.0                 # seconds between looks at what is due
RUNS_PER_DAY = 1
HARVEST_EVERY_DAYS = 7

_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age(when: Optional[str]) -> Optional[timedelta]:
    if not when:
        return None
    try:
        then = datetime.fromisoformat(when)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return _now() - then


def crawl_due(now: Optional[datetime] = None) -> bool:
    age = _age(registry.last_run("crawl"))
    return age is None or age >= timedelta(days=1) / max(1, RUNS_PER_DAY)


def harvest_due(now: Optional[datetime] = None) -> bool:
    age = _age(registry.last_run("harvest"))
    return age is None or age >= timedelta(days=HARVEST_EVERY_DAYS)


def round_once(force: bool = False) -> dict:
    """One tick's worth of work: the harvest if it is due, then the crawl.

    The harvest runs first so a crawl on the same tick reads the companies
    it just learned about.
    """
    done: dict = {}
    if force or harvest_due():
        done["harvest"] = sources.harvest()
        registry.record_run("harvest", {"sources": len(done["harvest"]["sources"]),
                                        "errors": len(done["harvest"]["errors"])})
    if force or crawl_due():
        result = crawler.crawl()
        done["crawl"] = result
        registry.record_run("crawl", {k: result[k] for k in
                                      ("companies", "seconds", "roles", "at_level", "new", "dead")})
    return done


def _loop() -> None:
    while not _stop.wait(TICK):
        try:
            round_once()
        except Exception:                   # noqa: BLE001 - a thread that dies stops the schedule
            # The failure is already on the company rows and in the run
            # record; swallowing it here keeps tomorrow's crawl alive.
            pass


def start() -> bool:
    """Started from the server. `AUTOPILOT_BOARDSCAN=0` leaves it off, and
    a test that builds the app without its startup event never runs it."""
    global _thread
    if os.environ.get(ENV, "1") == "0" or running():
        return False
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="boardscan-schedule", daemon=True)
    _thread.start()
    return True


def stop() -> None:
    _stop.set()


def running() -> bool:
    return _thread is not None and _thread.is_alive()


def status() -> dict:
    return {
        "running": running(),
        "enabled": os.environ.get(ENV, "1") != "0",
        "crawl": registry.last_run_record("crawl"),
        "harvest": registry.last_run_record("harvest"),
        "crawl_due": crawl_due(),
        "harvest_due": harvest_due(),
        "runs_per_day": RUNS_PER_DAY,
        "harvest_every_days": HARVEST_EVERY_DAYS,
    }


def wait_for_tick(seconds: float = TICK * 2) -> None:
    """For a test that wants the loop to have run at least once."""
    time.sleep(seconds)
