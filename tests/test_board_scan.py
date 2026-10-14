"""The board scan: slug harvesting, the registry, the role store, the crawl.

Nothing here touches the network. The lists on GitHub and the boards
themselves are stubbed, and the URL fixtures are the real shapes those
files carry - the embedded Greenhouse form with `?for=`, a hosted Lever
page, an Ashby application link - because all the harvester does is read
other people's links, and a fixture invented here would prove nothing
about them.

`tests/test_boards.py` is a different thing: it covers `tailor/boards.py`,
which reads one posting for one job.
"""
from __future__ import annotations

import json

import pytest

from boardscan import Role, crawl as crawler, known, registry, sources, store
from scout import Posting


@pytest.fixture(autouse=True)
def _own_store(tmp_path, monkeypatch):
    """A registry and a database of this test's own. Both resolve their
    path per call, so setting the variable is enough."""
    monkeypatch.setenv(registry.ENV, str(tmp_path / "boardscan.json"))
    monkeypatch.setenv(store.ENV, str(tmp_path / "boardscan.db"))


# --------------------------------------------------------------- harvesting

LIST_JSON = json.dumps([
    {"company_name": "Figma", "title": "Software Engineer, New Grad",
     "url": "https://boards.greenhouse.io/figma/jobs/4512345"},
    {"company_name": "Axon", "title": "SWE I",
     "url": "https://boards.greenhouse.io/embed/job_app?for=axon&token=7788"},
    {"company_name": "Palantir", "title": "Software Engineer",
     "url": "https://jobs.lever.co/palantir/0bb1-22?lever-origin=applied"},
    {"company_name": "Clay", "title": "New Grad Engineer",
     "url": "https://jobs.ashbyhq.com/clay/9f1e-aa/application"},
    # Not a board we read, and not an error: most links in these files are
    # LinkedIn, Workday, the repository's own pages, badges and images.
    {"company_name": "Big Co", "title": "SWE", "url": "https://www.linkedin.com/jobs/view/4119"},
    {"company_name": "Other", "title": "SWE",
     "url": "https://acme.wd1.myworkdayjobs.com/en-US/External/job/123"},
])

LIST_MARKDOWN = """
| Company | Role | Apply |
| --- | --- | --- |
| **Figma** | SWE New Grad | <a href="https://job-boards.greenhouse.io/figma/jobs/9"><img src="x.svg"></a> |
| **Ramp** | SWE I | [Apply](https://jobs.ashbyhq.com/ramp/1234-5678) |
| **Attentive** | New Grad | https://jobs.lever.co/attentive/abc |
"""


def test_slugs_out_of_a_json_list():
    rows, links = sources.slugs(LIST_JSON)
    assert links == 6
    assert sorted(rows) == [
        ("ashby", "clay", "Clay"),
        ("greenhouse", "axon", "Axon"),
        ("greenhouse", "figma", "Figma"),
        ("lever", "palantir", "Palantir"),
    ]


def test_the_greenhouse_embed_shape_is_the_company_not_the_path():
    """`/embed/job_app?for=axon` is one path for every job on every board;
    the company is in the query. Reading the path instead would make every
    embedded form the same company."""
    rows, _ = sources.slugs(json.dumps(
        [{"company_name": "Axon",
          "url": "https://boards.greenhouse.io/embed/job_app?for=axon&token=1"}]))
    assert rows == [("greenhouse", "axon", "Axon")]


def test_slugs_out_of_a_readme():
    rows, links = sources.slugs(LIST_MARKDOWN)
    # The three apply links; the badge image is a relative path, not a link.
    assert links == 3
    assert sorted(s for _, s, _ in rows) == ["attentive", "figma", "ramp"]


def test_a_slug_is_counted_once_per_file():
    doubled = json.dumps([
        {"company_name": "Figma", "url": "https://boards.greenhouse.io/figma/jobs/1"},
        {"company_name": "Figma", "url": "https://boards.greenhouse.io/figma/jobs/2"},
    ])
    rows, _ = sources.slugs(doubled)
    assert rows == [("greenhouse", "figma", "Figma")]


def test_harvest_one_reads_a_stubbed_list(monkeypatch):
    class Response:
        text = LIST_JSON

        def raise_for_status(self):
            return self

    class Client:
        def get(self, url):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(sources, "client", lambda: Client())
    result = sources.harvest_one(sources.SOURCES[0])
    assert result["new"] == 4
    assert registry.stats()["companies"] == 4
    assert registry.harvests()[0]["source"] == sources.SOURCES[0].name


# ----------------------------------------------------------------- registry


def test_a_slug_in_three_lists_is_one_company():
    registry.add("greenhouse", "figma", "Figma", source="one")
    _, fresh = registry.add("greenhouse", "Figma", "", source="two")
    assert fresh is False
    rows = registry.companies()
    assert len(rows) == 1
    assert rows[0].sources == ["one", "two"]


def test_a_board_we_do_not_read_is_refused():
    with pytest.raises(ValueError):
        registry.add("workday", "acme")
    assert registry.add_many([("workday", "acme", "", "x")])["refused"] == 1


def test_a_dead_board_is_kept_not_deleted():
    registry.add("lever", "gone")
    registry.update("lever:gone", dead=True, error="404")
    assert registry.companies() == []
    assert len(registry.companies(include_dead=True)) == 1
    # And the next harvest does not offer it back as news.
    assert registry.add_many([("lever", "gone", "", "list")])["new"] == 0


# -------------------------------------------------------------- the role store


def role(**fields) -> Role:
    base = dict(system="greenhouse", slug="figma", board_id="1", company="Figma",
                title="Software Engineer I", url="https://job-boards.greenhouse.io/figma/jobs/1")
    return Role(**{**base, **fields})


def test_the_same_role_read_twice_is_one_row():
    assert len(store.add_new([role()])) == 1
    assert store.add_new([role()]) == []
    assert store.counts()["roles"] == 1


def test_a_crawl_never_clears_a_decision():
    """The decline pile is the only data this service is calibrated on. An
    upsert here would wipe it every morning."""
    first = role()
    store.add_new([first])
    store.decide(first.id, "declined", "wants three years")
    store.add_new([role(title="Software Engineer I (updated)")])
    after = store.get(first.id)
    assert after.state == "declined"
    assert after.reason == "wants three years"
    assert store.declines()[0]["reason"] == "wants three years"


def test_the_listing_filters_and_hides_what_is_already_known():
    a, b = role(board_id="1"), role(board_id="2", title="Data Engineer II", verdict="reject")
    store.add_new([a, b])
    store.mark_seen(b.id, "high", "96f2")
    assert [r.board_id for r in store.listing(verdict="reject")] == ["2"]
    assert [r.board_id for r in store.listing(hide_seen=True)] == ["1"]
    assert store.counts()["already_known"] == 1


def test_the_listing_leaves_the_description_behind():
    store.add_new([role(text="x" * 5000)])
    assert store.listing()[0].text == ""
    assert len(store.listing(with_text=True)[0].text) == 5000


def test_a_state_that_is_not_a_state_is_refused():
    row = role()
    store.add_new([row])
    with pytest.raises(ValueError):
        store.decide(row.id, "applied")


# ------------------------------------------------------------------ the crawl


def posting(**fields) -> Posting:
    base = dict(id="1", title="Software Engineer I", url="https://example.com/1",
                location="San Francisco, CA", posted="2026-10-14", text="A real posting. " * 40)
    return Posting(**{**base, **fields})


def test_the_level_filter_keeps_new_grad_software_in_the_us():
    assert crawler.at_level(posting(), list(crawler.LEVEL_POSITIVE), list(crawler.LEVEL_NEGATIVE))


@pytest.mark.parametrize("title, location", [
    ("Senior Software Engineer", "San Francisco, CA"),
    ("Engineering Manager", "New York, NY"),
    # Measured on the first real crawl: `engineer ii` is in the positive
    # list because a new-grad software title often is, and these came with
    # it - other people's professions, on 40 boards.
    ("Manufacturing Engineer II, Primary Structure", "Long Beach, California"),
    ("Engineer II, Avionics Thermal Engineering", "Seattle, Washington"),
    ("Software Engineer I", "Bengaluru, India"),
])
def test_the_level_filter_drops_what_is_not_the_job(title, location):
    row = posting(title=title, location=location)
    assert not crawler.at_level(row, list(crawler.LEVEL_POSITIVE), list(crawler.LEVEL_NEGATIVE))


def test_a_board_that_is_gone_is_marked_dead(monkeypatch):
    company, _ = registry.add("lever", "gone")
    monkeypatch.setattr(crawler, "read_board",
                        lambda *a, **k: (_ for _ in ()).throw(crawler.BoardGone("404 from lever")))
    result = crawler.crawl_company(company, http=object())
    assert result["dead"] is True
    assert registry.get("lever:gone").dead is True


def test_a_board_that_breaks_is_recorded_and_the_crawl_goes_on(monkeypatch):
    company, _ = registry.add("ashby", "flaky")
    monkeypatch.setattr(crawler, "read_board",
                        lambda *a, **k: (_ for _ in ()).throw(TimeoutError("read timed out")))
    result = crawler.crawl_company(company, http=object())
    assert "TimeoutError" in result["error"]
    assert registry.get("ashby:flaky").error.startswith("TimeoutError")
    assert registry.get("ashby:flaky").dead is False


def test_a_crawl_stores_the_roles_at_level_with_their_verdict(monkeypatch):
    company, _ = registry.add("greenhouse", "figma", "Figma")
    rows = [posting(id="1"), posting(id="2", title="Senior Staff Engineer"),
            posting(id="3", title="Software Engineer, New Grad", text="")]
    monkeypatch.setattr(crawler, "read_board", lambda *a, **k: rows)
    monkeypatch.setattr(crawler, "judge", lambda p, facts: ("ok", []))
    result = crawler.crawl_company(company, http=object(), check_known=False)
    assert (result["roles"], result["at_level"], result["new"]) == (3, 2, 2)
    assert sorted(r.board_id for r in store.listing()) == ["1", "3"]
    assert registry.get("greenhouse:figma").ok_at is not None


def test_a_role_already_applied_for_is_never_offered(monkeypatch):
    """The whole reason the collection step exists: the same job arrives
    from four sources, and twice-applied is the failure."""
    company, _ = registry.add("greenhouse", "figma", "Figma")
    monkeypatch.setattr(crawler, "read_board", lambda *a, **k: [posting()])
    monkeypatch.setattr(crawler, "judge", lambda p, facts: ("ok", []))
    monkeypatch.setattr(crawler.known, "check", lambda *a, **k: known.Answer(
        level="high", job_id="96f2", status="submitted", block=True))
    crawler.crawl_company(company, http=object())
    row = store.listing(state="declined")[0]
    assert row.seen_verdict == "high" and row.seen_job == "96f2"
    assert "already applied" in row.reason


def test_a_likely_match_is_shown_but_not_decided(monkeypatch):
    company, _ = registry.add("greenhouse", "figma", "Figma")
    monkeypatch.setattr(crawler, "read_board", lambda *a, **k: [posting()])
    monkeypatch.setattr(crawler, "judge", lambda p, facts: ("ok", []))
    monkeypatch.setattr(crawler.known, "check", lambda *a, **k: known.Answer(
        level="confident", job_id="aa11", status="rejected", block=False))
    crawler.crawl_company(company, http=object())
    row = store.listing()[0]
    assert row.state == "new" and row.seen_verdict == "confident"


def test_the_screen_is_deterministic_and_needs_no_model():
    """`judge` runs the same twelve rules the posting banner runs. No model
    is stubbed here because none is called."""
    hard = posting(text="This role requires an active TS/SCI security clearance. " * 5)
    verdict, flags = crawler.judge(hard, facts="")
    assert verdict == "reject"
    assert any(f.startswith("clearance") for f in flags)


def test_a_posting_with_no_description_is_not_screened_silently():
    assert crawler.judge(posting(text=""), facts="") == ("", [])


# -------------------------------------------------------------------- the API


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    import server_boardscan

    return TestClient(server_boardscan.app)


def test_the_page_is_served_without_a_cache(client):
    res = client.get("/")
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-cache"


def test_the_list_and_one_decision_over_http(client):
    row = role()
    store.add_new([row])
    listed = client.get("/boards/roles?state=new").json()
    assert [r["id"] for r in listed["roles"]] == [row.id]
    assert "text" not in listed["roles"][0]

    answer = client.post(f"/boards/role/{row.id}/decide",
                         json={"state": "declined", "reason": "too far from home"})
    assert answer.status_code == 200
    assert store.get(row.id).reason == "too far from home"
    assert client.get("/boards/declines").json()["declines"][0]["reason"] == "too far from home"


def test_a_role_that_is_not_there_is_a_404_not_a_crash(client):
    assert client.get("/boards/role/deadbeef").status_code == 404
    assert client.post("/boards/role/deadbeef/decide",
                       json={"state": "added"}).status_code == 404


def test_a_company_is_added_from_a_pasted_board_url(client):
    res = client.post("/boards/company",
                      json={"url": "https://job-boards.greenhouse.io/figma/jobs/4512345"})
    assert res.json()["id"] == "greenhouse:figma"
    assert client.post("/boards/company",
                       json={"url": "https://example.com/careers"}).status_code == 400


def test_one_long_job_at_a_time(client, monkeypatch):
    """Two crawls at once would read every board twice."""
    import threading

    from boardscan import api

    gate = threading.Event()
    monkeypatch.setattr(api.crawler, "crawl", lambda **k: gate.wait(5) or {"companies": 0})
    assert client.post("/boards/crawl", json={"limit": 1}).json()["state"] == "running"
    assert client.post("/boards/crawl", json={"limit": 1}).status_code == 409
    gate.set()
