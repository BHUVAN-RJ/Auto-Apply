"""The scout: a careers URL becomes a watch, a check records only what is
new at the level, the mail says so. A notify watch (a referral is
possible) never queues; every other watch sends what the screen does not
reject into autopilot, and nothing is ever applied to by itself."""
import json

import pytest
from fastapi.testclient import TestClient

from scout import DetectError, Hit, Posting, Watch, detect, mail, providers, run, store
from scout import filter as level
from scout.filter import at_level
from scout import DEFAULT_NEGATIVE, DEFAULT_POSITIVE
from server import queue, runner, settings
from server.app import app

REAL_SCREEN = run._screen      # kept before the autouse fixture replaces it


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(store.ENV, str(tmp_path / "scout.json"))
    monkeypatch.setenv(run.ENV, "0")
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    qpath = tmp_path / "queue.json"
    monkeypatch.setattr(queue, "QUEUE_PATH", qpath)
    monkeypatch.setattr(queue, "LOCK_PATH", qpath.with_suffix(".lock"))
    monkeypatch.setattr(runner, "start_pipeline", lambda job_id, log_dir=None: 4242)
    from server import postings
    monkeypatch.setenv(postings.DIR_ENV, str(tmp_path / "postings"))
    # No model, no network, no SMTP from a test.
    monkeypatch.setattr(run, "_screen", lambda posting, company: ("ok", "fits"))
    monkeypatch.delenv(mail.USER, raising=False)
    monkeypatch.delenv(mail.PASS, raising=False)


@pytest.fixture
def client():
    return TestClient(app)


def listing(monkeypatch, rows):
    """The provider answers these rows, whatever the watch."""
    monkeypatch.setattr(providers, "fetch", lambda watch, http=None: list(rows))


def posting(i, title, **kw):
    return Posting(id=str(i), title=title, url=f"https://x/{i}", **kw)


# --------------------------------------------------------------- detect --

def test_detect_names_the_provider_from_the_url():
    assert detect("https://boards.greenhouse.io/stripe")[:2] == ("greenhouse", {"slug": "stripe"})
    assert detect("https://boards.greenhouse.io/embed/job_board?for=acme")[1] == {"slug": "acme"}
    assert detect("https://jobs.ashbyhq.com/ramp/")[:2] == ("ashby", {"slug": "ramp"})
    assert detect("https://jobs.lever.co/palantir")[:2] == ("lever", {"slug": "palantir"})
    provider, args, company = detect("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite")
    assert (provider, company) == ("workday", "nvidia")
    assert args == {"host": "nvidia.wd5.myworkdayjobs.com", "tenant": "nvidia", "site": "NVIDIAExternalCareerSite"}
    provider, args, _ = detect("https://www.google.com/about/careers/applications/jobs/results?q=swe&location=United+States")
    assert provider == "google" and args == {"query": "swe", "location": "United States"}
    assert detect("https://jobs.careers.microsoft.com/global/en/search")[1] == {
        "host": "apply.careers.microsoft.com", "domain": "microsoft.com"}
    assert detect("https://www.amazon.jobs/en/search?base_query=sde")[1] == {"query": "sde"}
    assert detect("https://jobs.apple.com/en-us/search")[0] == "apple"


def test_detect_refuses_what_it_cannot_read():
    with pytest.raises(DetectError):
        detect("https://www.metacareers.com/jobs")
    with pytest.raises(DetectError):
        detect("https://example.com/careers")
    with pytest.raises(DetectError):
        detect("https://nvidia.wd5.myworkdayjobs.com/en-US")   # no site


# --------------------------------------------------------------- filter --

def test_level_filter_is_word_bounded_and_case_blind():
    ok = lambda t: at_level(t, DEFAULT_POSITIVE, DEFAULT_NEGATIVE)  # noqa: E731
    assert ok("Software Engineer II, YouTube")
    assert ok("SDE I")
    assert ok("Driver Software Engineer")            # "iv" inside a word is not a level
    assert ok("Software Engineer III, Google Cloud")  # III is Google's new-grad level
    assert not ok("Senior Software Engineer")
    assert not ok("Sr. Software Engineer")
    assert not ok("Software Engineering Intern")
    assert not ok("Product Manager")
    assert at_level("Anything", [], ["senior"])       # no positives = every title


# ------------------------------------------------------------ providers --

def test_google_rows_come_out_of_the_data_block():
    row = [123, "Software Engineer II", "https://g/apply/123", [None, "<p>do things</p>"], [None, "quals"],
           None, None, None, None, [["Austin, TX, USA"], ["Remote"]], [None, "<b>desc</b>"], None,
           [1789657674], None, None, [None, "team"], None, None, [None, "pref"], [None, "min"]]
    html = "x AF_initDataCallback({key: 'ds:1', hash: '2', data:" + json.dumps([[row]]) + ", sideChannel: {}});"
    rows = providers._google_rows(html)
    assert len(rows) == 1
    p = rows[0]
    assert p.id == "123" and p.title == "Software Engineer II" and p.url.endswith("/jobs/results/123")
    assert p.location == "Austin, TX, USA; Remote" and p.posted == "2026-09-17"
    assert "desc" in p.text and "Minimum qualifications:\nmin" in p.text and "pref" in p.text
    with pytest.raises(RuntimeError):
        providers._google_rows("<html>no block</html>")


def test_dates_read_epochs_and_amazon_words():
    assert providers._date(1789657674) == "2026-09-17"
    assert providers._date(1789657674000) == "2026-09-17"
    assert providers._date("September 18, 2026") == "2026-09-18"
    assert providers._date("2026-09-10T13:11:58-04:00") == "2026-09-10"
    assert providers._date("") == ""


# ---------------------------------------------------------------- check --

def test_first_check_seeds_seen_without_hits_or_mail(monkeypatch):
    listing(monkeypatch, [posting(1, "Software Engineer"), posting(2, "Senior Engineer")])
    watch, _ = store.add_watch(Watch(url="https://boards.greenhouse.io/acme", company="Acme", provider="greenhouse", args={"slug": "acme"}))
    result = run.check(watch)
    assert result["first_run"] and result["new"] == 0 and result["matching"] == 1
    assert store.hits() == []
    saved = store.get_watch(watch.id)
    assert saved.seen_ids == ["1"] and saved.error is None and saved.last_ok


def test_a_new_role_at_the_level_is_a_hit_once(monkeypatch):
    listing(monkeypatch, [posting(1, "Software Engineer")])
    watch, _ = store.add_watch(Watch(url="https://boards.greenhouse.io/acme", company="Acme", provider="greenhouse", args={"slug": "acme"}))
    run.check(watch)
    listing(monkeypatch, [posting(1, "Software Engineer"), posting(2, "Software Engineer II, Payments", location="Austin", text="desc"),
                          posting(3, "Staff Engineer")])
    result = run.check(store.get_watch(watch.id))
    assert result["new"] == 1 and result["matching"] == 2
    hits = store.hits()
    assert len(hits) == 1 and hits[0].posting["title"] == "Software Engineer II, Payments" and hits[0].verdict == "ok"
    # Again: nothing new, nothing duplicated.
    assert run.check(store.get_watch(watch.id))["new"] == 0 and len(store.hits()) == 1
    # A role that leaves the page is forgotten; back again, it is news again.
    listing(monkeypatch, [posting(1, "Software Engineer")])
    run.check(store.get_watch(watch.id))
    assert store.get_watch(watch.id).seen_ids == ["1"]


def test_a_page_that_cannot_be_read_is_broken_not_empty(monkeypatch):
    def boom(watch, http=None):
        raise RuntimeError("HTTP 500")
    monkeypatch.setattr(providers, "fetch", boom)
    watch, _ = store.add_watch(Watch(url="https://jobs.lever.co/acme", company="Acme", provider="lever", args={"slug": "acme"}))
    result = run.check(watch)
    assert not result["ok"] and "HTTP 500" in result["error"]
    assert store.get_watch(watch.id).broken
    listing(monkeypatch, [])
    result = run.check(store.get_watch(watch.id))
    assert not result["ok"] and "lists no roles" in result["error"]
    # Reads again: the banner goes.
    listing(monkeypatch, [posting(1, "Software Engineer")])
    run.check(store.get_watch(watch.id))
    assert store.get_watch(watch.id).error is None


def test_verify_reads_without_writing(monkeypatch):
    listing(monkeypatch, [posting(1, "Software Engineer"), posting(2, "Senior Engineer")])
    watch = Watch(url="https://jobs.lever.co/acme", company="Acme", provider="lever", args={"slug": "acme"})
    result = run.verify(watch)
    assert result["ok"] and result["total"] == 2 and result["matching"] == 1 and result["sample"] == ["Software Engineer"]
    assert store.watches() == []


def test_due_follows_checks_per_day(monkeypatch):
    watch = Watch(url="https://jobs.lever.co/acme", provider="lever", args={"slug": "acme"})
    assert run.due(watch)
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    watch.last_checked = (now - timedelta(hours=5)).isoformat(timespec="seconds")
    assert not run.due(watch, now)                 # 4 a day = every 6 h
    watch.checks_per_day = 6
    assert run.due(watch, now)                     # every 4 h
    settings.save(scout_checks_per_day=24)
    watch.checks_per_day = None
    assert run.due(watch, now)
    watch.enabled = False
    assert not run.due(watch, now)


# ----------------------------------------------------------------- mail --

def test_mail_digest_names_the_role_the_verdict_and_the_referrer():
    watch = Watch(url="https://x", company="Acme", provider="lever", args={}, referrer="Priya (WhatsApp)")
    hit = Hit(watch_id=watch.id, company="Acme", posting=posting(7, "Software Engineer II", location="Austin", posted="2026-09-20").to_dict(),
              verdict="ok", summary="fits")
    subject, text, html = mail.digest([(watch, hit)])
    assert subject == "Scout: 1 to ask about at Acme"
    assert "Acme: Software Engineer II — Austin (posted 2026-09-20)" in text and "https://x/7" in text
    assert "screen: ok — fits" in text and "ask: Priya (WhatsApp)" in text
    assert "Hi Priya, Acme just posted Software Engineer II (Austin)" in mail.referral_note(watch, hit)
    assert "<a href='https://x/7'>Software Engineer II</a>" in html


def test_mail_new_sends_one_digest_and_marks_them(monkeypatch):
    sent = []
    monkeypatch.setenv(mail.USER, "me@gmail.com")
    monkeypatch.setenv(mail.PASS, "app-password")
    monkeypatch.setattr(mail, "send", lambda subject, text, html=None: sent.append(subject))
    watch, _ = store.add_watch(Watch(url="https://x", company="Acme", provider="lever", args={}))
    store.add_hits([
        Hit(watch_id=watch.id, company="Acme", posting=posting(1, "SWE").to_dict(), verdict="ok"),
        Hit(watch_id=watch.id, company="Acme", posting=posting(2, "SWE II").to_dict(), verdict="caution"),
        Hit(watch_id=watch.id, company="Acme", posting=posting(3, "SWE III").to_dict(), verdict="reject"),
    ])
    assert run.mail_new() == {"sent": True, "count": 2, "subject": "Scout: 2 to ask about at Acme"}
    assert sent == ["Scout: 2 to ask about at Acme"]
    assert all(h.mailed for h in store.hits())           # the reject is marked too, never sent
    assert run.mail_new() == {"sent": False, "reason": "nothing new"}


def test_mail_not_set_up_is_said_not_raised():
    assert run.mail_new() == {"sent": False, "reason": "mail not set up"}
    assert not mail.configured()
    with pytest.raises(mail.MailError):
        mail.send("x", "y")


# ------------------------------------------------------------------ API --

def test_api_adds_verifies_checks_and_queues(client, monkeypatch):
    listing(monkeypatch, [posting(1, "Software Engineer", text="first")])
    res = client.post("/scout/watch", json={"url": "https://boards.greenhouse.io/acme", "notify": True, "referrer": "Sam"})
    assert res.status_code == 200
    body = res.json()
    assert body["created"] and body["verify"]["ok"] and body["watch"]["company"] == "acme"
    wid = body["watch"]["id"]
    assert body["watch"]["seen"] == 1 and not body["watch"]["broken"]

    listing(monkeypatch, [posting(1, "Software Engineer"), posting(2, "Software Engineer, New Grad", text="the posting")])
    res = client.post(f"/scout/watch/{wid}/check").json()
    assert res["new"] == 1 and res["mail"] == {"sent": False, "reason": "mail not set up"}

    state = client.get("/scout").json()
    assert len(state["hits"]) == 1
    hit = state["hits"][0]
    assert hit["status"] == "new" and hit["notify"] and hit["referrer"] == "Sam" and "Hi Sam, acme just posted" in hit["note"]
    assert "text" not in hit["posting"]

    res = client.post(f"/scout/hits/{hit['id']}/queue").json()
    assert res["ok"] and res["created"] and res["processing"]
    job = queue.get(res["id"])
    assert job.url == "https://x/2" and job.source == "scout" and job.status.value == "queued"
    assert client.get("/scout").json()["hits"][0]["status"] == "queued"

    res = client.patch(f"/scout/watch/{wid}", json={"checks_per_day": 12, "referrer": "Sam (Slack)", "negative": ["senior"]}).json()
    assert res["checks_per_day"] == 12 and res["negative"] == ["senior"]
    assert client.patch(f"/scout/watch/{wid}", json={"checks_per_day": 0}).status_code == 400
    assert client.post("/scout/frequency", json={"checks_per_day": 8}).json() == {"checks_per_day": 8}
    assert client.get("/scout").json()["checks_per_day"] == 8
    assert client.delete(f"/scout/watch/{wid}").json() == {"removed": True}
    assert client.get("/scout").json() == {**client.get("/scout").json(), "watches": [], "hits": []}


def test_api_refuses_unknown_hosts_and_shows_broken(client, monkeypatch):
    assert client.post("/scout/watch", json={"url": "https://example.com/jobs"}).status_code == 400

    def boom(watch, http=None):
        raise RuntimeError("HTTP 403")
    monkeypatch.setattr(providers, "fetch", boom)
    body = client.post("/scout/watch", json={"url": "https://jobs.ashbyhq.com/acme"}).json()
    assert body["created"] and not body["verify"]["ok"] and body["watch"]["broken"] and "HTTP 403" in body["watch"]["error"]
    assert client.post("/scout/verify").json()["broken"] == 1


def test_settings_post_changes_only_what_is_sent(client):
    settings.save(scout_checks_per_day=9)
    client.post("/settings", json={"use_profile": True})
    assert settings.load()["scout_checks_per_day"] == 9


def test_verifier_script_exits_with_the_broken_count(monkeypatch, capsys):
    from tools import scout_verify
    listing(monkeypatch, [posting(1, "Software Engineer")])
    code = scout_verify.main(["https://boards.greenhouse.io/acme", "https://example.com/jobs", "--no-color"])
    out = capsys.readouterr().out
    assert code == 1 and "OK" in out and "BROKEN" in out and "No provider" in out


def test_a_broken_page_is_mailed_once_until_it_reads_again(monkeypatch):
    sent = []
    monkeypatch.setenv(mail.USER, "me@gmail.com")
    monkeypatch.setenv(mail.PASS, "app-password")
    monkeypatch.setattr(mail, "send", lambda subject, text, html=None: sent.append(subject))
    watch, _ = store.add_watch(Watch(url="https://jobs.lever.co/acme", company="Acme", provider="lever", args={"slug": "acme"}))

    def boom(watch, http=None):
        raise RuntimeError("HTTP 500")
    monkeypatch.setattr(providers, "fetch", boom)
    assert run.check(watch)["broken_mailed"]
    assert sent == ["Scout: Acme DOES NOT WORK"]
    assert not run.check(store.get_watch(watch.id))["broken_mailed"]   # still broken, not nagged
    assert sent == ["Scout: Acme DOES NOT WORK"]
    listing(monkeypatch, [posting(1, "Software Engineer")])
    run.check(store.get_watch(watch.id))
    assert not store.get_watch(watch.id).broken_mailed
    monkeypatch.setattr(providers, "fetch", boom)
    run.check(store.get_watch(watch.id))
    assert sent == ["Scout: Acme DOES NOT WORK"] * 2                   # broke again: said again


def test_status_names_the_broken_companies(client, monkeypatch):
    def boom(watch, http=None):
        raise RuntimeError("HTTP 403")
    monkeypatch.setattr(providers, "fetch", boom)
    client.post("/scout/watch", json={"url": "https://jobs.ashbyhq.com/acme"})
    assert client.get("/scout").json()["broken"] == ["acme"]


# ------------------------------------------------- notify vs autopilot --

def test_a_watch_without_notify_queues_what_the_screen_lets_through(monkeypatch):
    verdicts = {"2": ("ok", "fits"), "3": ("reject", "senior"), "4": ("", "screen failed: boom"), "5": ("caution", "stretch")}
    monkeypatch.setattr(run, "_screen", lambda posting, company: verdicts[posting.id])
    listing(monkeypatch, [posting(1, "Software Engineer")])
    watch, _ = store.add_watch(Watch(url="https://boards.greenhouse.io/acme", company="Acme", provider="greenhouse", args={"slug": "acme"}))
    run.check(watch)
    listing(monkeypatch, [posting(i, f"Software Engineer {i}", text="desc") for i in range(1, 6)])
    result = run.check(store.get_watch(watch.id))
    assert result["new"] == 4 and result["queued"] == 2
    by_title = {h.posting["title"]: h for h in store.hits()}
    assert by_title["Software Engineer 2"].status == "queued" and by_title["Software Engineer 5"].status == "queued"
    # The reject and the failed screen wait for the person, never queued by the scout.
    assert by_title["Software Engineer 3"].status == "new" and by_title["Software Engineer 4"].status == "new"
    jobs = {j.url for j in queue.all_jobs()}
    assert jobs == {"https://x/2", "https://x/5"}
    for j in queue.all_jobs():
        assert j.status.value == "queued" and j.source == "scout"


def test_a_notify_watch_never_queues(monkeypatch):
    listing(monkeypatch, [posting(1, "Software Engineer")])
    watch, _ = store.add_watch(Watch(url="https://boards.greenhouse.io/acme", company="Acme", provider="greenhouse",
                                     args={"slug": "acme"}, notify=True, referrer="Sam"))
    run.check(watch)
    listing(monkeypatch, [posting(1, "Software Engineer"), posting(2, "Software Engineer II", text="desc")])
    result = run.check(store.get_watch(watch.id))
    assert result["new"] == 1 and result["queued"] == 0
    assert [h.status for h in store.hits()] == ["new"] and queue.all_jobs() == []


def test_the_digest_separates_to_ask_from_in_autopilot():
    ask = Watch(url="https://x", company="Acme", provider="lever", args={}, notify=True, referrer="Priya")
    auto = Watch(url="https://y", company="Beta", provider="lever", args={})
    rows = [
        (auto, Hit(watch_id=auto.id, company="Beta", posting=posting(1, "SWE").to_dict(), verdict="ok", status="queued", job_id="ab12")),
        (ask, Hit(watch_id=ask.id, company="Acme", posting=posting(2, "SWE II").to_dict(), verdict="ok")),
    ]
    subject, text, html = mail.digest(rows)
    assert subject == "Scout: 1 to ask about and 1 in autopilot at Acme, Beta"
    # The one to ask about comes first, with the note; the queued one says so.
    assert text.index("Acme: SWE II") < text.index("Beta: SWE")
    assert "Hi Priya, Acme just posted" in text and "screen: ok, in autopilot" in text
    assert "ask:" not in text.split("Beta: SWE")[1]


def test_prescreen_rejects_the_hard_cases_in_code():
    assert level.prescreen("Requirements: 5+ years of experience in Java") == "asks for 5+ years of experience"
    assert level.prescreen("Minimum 4 years of professional software experience") == "asks for 4+ years of experience"
    assert level.prescreen("0-2 years of experience; new grads welcome") is None
    assert level.prescreen("2+ years of industry experience") is None
    assert level.prescreen("1-3 years of relevant experience and 5+ years of Python") is None
    assert level.prescreen("Great team, 12 years in business") is None
    assert level.prescreen("Must hold an active Secret clearance") == "needs a security clearance"
    assert level.prescreen("US citizenship is required for this role") == "US citizenship required"
    assert level.prescreen("") is None


def test_prescreen_runs_before_the_model(monkeypatch):
    from server import screen as screen_api

    def model(*a, **k):
        raise AssertionError("the model was called")
    monkeypatch.setattr(screen_api, "screen_url", model)
    monkeypatch.setattr(run, "_screen", REAL_SCREEN)   # the autouse fixture stubs it
    verdict, summary = run._screen(posting(9, "Software Engineer", text="Requires 6+ years of experience"), "Acme")
    assert (verdict, summary) == ("reject", "asks for 6+ years of experience")


# ------------------------------------------- bamboohr and board discovery --

class _Http:
    """An httpx stand-in answering fixed bodies by URL substring."""
    def __init__(self, pages): self.pages = pages
    def get(self, url, **kw):
        for key, body in self.pages.items():
            if key in url:
                return _Resp(body)
        raise RuntimeError(f"no page for {url}")


class _Resp:
    def __init__(self, body): self.body = body
    def raise_for_status(self): return self
    def json(self): return self.body
    @property
    def text(self): return self.body


def test_bamboohr_lists_the_board_and_reads_detail_once():
    http = _Http({
        "/careers/list": {"result": [
            {"id": "112", "jobOpeningName": "Software Developer - Grippers", "location": {"city": "Suwanee", "state": "Georgia"}},
            {"id": "7", "jobOpeningName": "Old one", "location": {}, "isRemote": True},
        ]},
        "/careers/112/detail": {"result": {"jobOpening": {"description": "<p>Build <b>grippers</b></p>", "datePosted": "2026-02-05"}}},
    })
    watch = Watch(url="https://mujin.bamboohr.com/careers", company="Mujin", provider="bamboohr", args={"slug": "mujin"}, seen_ids=["7"])
    rows = providers.bamboohr(watch, http)
    assert [p.title for p in rows] == ["Software Developer - Grippers", "Old one"]
    assert rows[0].url == "https://mujin.bamboohr.com/careers/112" and rows[0].location == "Suwanee, Georgia"
    assert rows[0].text.split() == ["Build", "grippers"] and rows[0].posted == "2026-02-05"
    assert rows[1].text == "" and rows[1].location == "Remote"     # seen: no detail call
    assert detect("https://mujin.bamboohr.com/careers")[:2] == ("bamboohr", {"slug": "mujin"})
    with pytest.raises(DetectError):
        detect("https://www.bamboohr.com/careers")


def test_discover_finds_every_board_a_company_page_embeds():
    html = ('<a href="https://jobs.lever.co/mujininc?">Japan</a> <script src="https://mujin.bamboohr.com/js/embed.js&quot;">'
            '<iframe src="https://boards.greenhouse.io/embed/job_board?for=acme&b=x"> https://jobs.lever.co/mujininc/again')
    assert providers.discover("https://mujin-corp.com/careers", _Http({"mujin-corp.com": html})) == [
        "https://boards.greenhouse.io/acme", "https://jobs.lever.co/mujininc", "https://mujin.bamboohr.com/careers"]
    assert providers.discover("https://x.com/careers", _Http({"x.com": "<p>nothing</p>"})) == []


def test_api_turns_a_company_page_into_one_watch_per_board(client, monkeypatch):
    listing(monkeypatch, [posting(1, "Software Engineer")])
    monkeypatch.setattr(providers, "discover", lambda url, http=None: ["https://jobs.lever.co/mujininc", "https://mujin.bamboohr.com/careers"])
    res = client.post("/scout/watch", json={"url": "https://mujin-corp.com/careers", "company": "Mujin"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["found"] == ["https://jobs.lever.co/mujininc", "https://mujin.bamboohr.com/careers"]
    assert [w["company"] for w in body["watches"]] == ["Mujin (lever)", "Mujin (bamboohr)"]
    assert body["watch"]["provider"] == "lever" and body["created"]
    assert [w["provider"] for w in client.get("/scout").json()["watches"]] == ["lever", "bamboohr"]

    monkeypatch.setattr(providers, "discover", lambda url, http=None: [])
    res = client.post("/scout/watch", json={"url": "https://nothing.example/jobs"})
    assert res.status_code == 400 and "names no board" in res.json()["detail"]


# ------------------------------------------------ us only and the list --

def test_us_only_drops_roles_abroad_before_anything_else():
    assert level.outside_us("Tokyo, Japan (MJHQ)") and level.outside_us("Best, Netherlands")
    assert level.outside_us("London or Berlin") and level.outside_us("Bengaluru, India")
    assert not level.outside_us("Suwanee, Georgia") and not level.outside_us("") and not level.outside_us("Remote")
    assert not level.outside_us("Austin, TX; Toronto, ON, Canada")     # one of them is here
    assert not level.outside_us("Remote - US") and not level.outside_us("Mountain View, CA, USA")
    rows = [posting(1, "Software Engineer", location="Tokyo, Japan"), posting(2, "Software Engineer", location="Austin, TX")]
    us = Watch(url="https://x", company="A", provider="lever", args={})
    assert [p.id for p in level.matching(us, rows)] == ["2"]
    assert [p.id for p in level.matching(us.model_copy(update={"us_only": False}), rows)] == ["1", "2"]


def test_the_watch_keeps_every_role_at_the_level_and_one_can_be_added_by_hand(monkeypatch):
    listing(monkeypatch, [posting(1, "Software Engineer", location="Austin, TX"), posting(2, "Software Engineer", location="Tokyo, Japan"),
                          posting(3, "Staff Engineer")])
    watch, _ = store.add_watch(Watch(url="https://boards.greenhouse.io/acme", company="Acme", provider="greenhouse", args={"slug": "acme"}))
    run.check(watch)
    saved = store.get_watch(watch.id)
    assert [r["title"] for r in saved.listed] == ["Software Engineer"] and saved.listed[0]["id"] == "1"
    assert saved.seen_ids == ["1"]
    result = run.add_listed(watch.id, "1")
    assert result["ok"] and result["created"]
    assert [h.status for h in store.hits()] == ["queued"] and queue.all_jobs()[0].url == "https://x/1"
    assert run.add_listed(watch.id, "9")["ok"] is False


def test_api_add_from_the_list_and_us_only_patch(client, monkeypatch):
    listing(monkeypatch, [posting(1, "Software Engineer", location="Austin, TX")])
    wid = client.post("/scout/watch", json={"url": "https://boards.greenhouse.io/acme"}).json()["watch"]["id"]
    state = client.get("/scout").json()["watches"][0]
    assert state["us_only"] and [r["id"] for r in state["listed"]] == ["1"]
    assert client.post(f"/scout/watch/{wid}/add", json={"posting_id": "1"}).json()["ok"]
    assert client.post(f"/scout/watch/{wid}/add", json={"posting_id": "1"}).json()["created"] is False   # same job, not twice
    assert client.post(f"/scout/watch/{wid}/add", json={"posting_id": "x"}).status_code == 404
    assert client.patch(f"/scout/watch/{wid}", json={"us_only": False}).json()["us_only"] is False
