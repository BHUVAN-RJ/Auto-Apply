"""Projects from GitHub: the scan (listing, tarball, ranking, picks), the
hand-over to the interviewer, the link the resume hyperlinks. GitHub and
the model are stubs; nothing here touches the network."""

import io
import json
import tarfile
import types

import httpx
import pytest
from fastapi.testclient import TestClient

from server import settings
from server.app import app
from tailor import github, interview, llm, profile, tailor
from tests.test_interview import EXTRACTION, RESUME, Script, events


def make_tarball(files: dict[str, bytes], top: str = "repo-abc123") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


LISTING = [
    {"name": "hydra", "full_name": "me/hydra", "html_url": "https://github.com/me/hydra",
     "description": "A distributed cache", "language": "Go", "stargazers_count": 12,
     "pushed_at": "2026-09-01T00:00:00Z", "created_at": "2025-01-01T00:00:00Z", "size": 900, "fork": False},
    {"name": "auditex", "full_name": "me/auditex", "html_url": "https://github.com/me/auditex",
     "description": "On-device TTS extension", "language": "TypeScript", "stargazers_count": 3,
     "pushed_at": "2026-06-01T00:00:00Z", "created_at": "2026-01-01T00:00:00Z", "size": 300, "fork": False},
    {"name": "dotfiles", "full_name": "me/dotfiles", "html_url": "https://github.com/me/dotfiles",
     "description": "", "language": "Shell", "stargazers_count": 0,
     "pushed_at": "2021-01-01T00:00:00Z", "created_at": "2020-01-01T00:00:00Z", "size": 10, "fork": False},
    {"name": "linux", "full_name": "me/linux", "html_url": "https://github.com/me/linux",
     "description": "", "language": "C", "stargazers_count": 0,
     "pushed_at": "2026-01-01T00:00:00Z", "created_at": "2026-01-01T00:00:00Z", "size": 10, "fork": True},
    {"name": "teamwork", "full_name": "me/teamwork", "html_url": "https://github.com/me/teamwork",
     "description": "", "language": "C", "stargazers_count": 0,
     "pushed_at": "2026-01-01T00:00:00Z", "created_at": "2026-01-01T00:00:00Z", "size": 10, "fork": False},
    {"name": "oldmail", "full_name": "me/oldmail", "html_url": "https://github.com/me/oldmail",
     "description": "committed under another email", "language": "C", "stargazers_count": 0,
     "pushed_at": "2020-01-01T00:00:00Z", "created_at": "2019-01-01T00:00:00Z", "size": 10, "fork": False},
    {"name": "empty", "full_name": "me/empty", "html_url": "https://github.com/me/empty",
     "description": "", "language": "", "stargazers_count": 0,
     "pushed_at": "2026-01-01T00:00:00Z", "created_at": "2026-01-01T00:00:00Z", "size": 0, "fork": False},
]
COMMITS = {"me/hydra": 40, "me/auditex": 12, "me/dotfiles": 3, "me/teamwork": 0, "me/oldmail": 0, "me/empty": 0}
UNATTRIBUTED = {"me/oldmail": 6}


def scaffold_reply(name: str) -> str:
    return "```json\n" + json.dumps({
        "summary": f"{name} does a thing.", "stack": ["Go"], "what_it_does": "Things.",
        "how_it_is_built": "Modules.", "own_contribution": "Sole author.",
        "unknowns": ["outcome"], "questions": [f"Who used {name}?", "What was hardest?"],
    }) + "\n```"


class FakeGitHub:
    """Answers the two requests the scan makes per repo, plus the listing.
    Stands in for the httpx client as well as for `_get`."""

    def __init__(self):
        self.calls: list[str] = []

    def get(self, url, params=None, **kw):
        # The client: the commit count calls it directly.
        self.calls.append(url)
        params = params or {}
        if url.endswith("/commits"):
            full = url.split("/repos/", 1)[1].rsplit("/commits", 1)[0]
            if full == "me/empty":
                return httpx.Response(409, request=httpx.Request("GET", url))
            n = COMMITS[full] if params.get("author") else COMMITS[full] or UNATTRIBUTED.get(full, 0)
            if n == 0:
                return httpx.Response(200, json=[])
            headers = {"link": f'<{url}?page=2>; rel="next", <{url}?page={n}>; rel="last"'} if n > 1 else {}
            return httpx.Response(200, json=[{"sha": "x"}], headers=headers)
        raise AssertionError(url)

    def api(self, client, url, **params):
        # `_get`: the listing.
        self.calls.append(url)
        if url.endswith("/repos"):
            return httpx.Response(200, json=LISTING if params.get("page", 1) == 1 else [])
        raise AssertionError(url)

    def tarball(self, full_name, client):
        if full_name == "me/dotfiles":
            return None
        return make_tarball({"README.md": b"# Hello\nA cache.", "go.mod": b"module x",
                             "main.go": b"package main\n" * 30, "node_modules/x.js": b"junk"})


@pytest.fixture
def scan(tmp_path, monkeypatch):
    stories = tmp_path / "stories"
    monkeypatch.setattr(profile, "STORIES", stories)
    monkeypatch.setattr(profile, "BASE_RESUME", tmp_path / "resume.tex")
    monkeypatch.setattr(profile, "APPLICANT", tmp_path / "applicant.md")
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    (tmp_path / "resume.tex").write_text(RESUME)
    fake = FakeGitHub()
    monkeypatch.setattr(github, "_get", fake.api)
    monkeypatch.setattr(github.httpx, "Client", lambda **kw: fake)
    monkeypatch.setattr(github, "fetch_tarball", fake.tarball)
    # tailor.llm is one module shared with the interviewer's stub, so the
    # scan gets a client of its own.
    monkeypatch.setattr(github, "llm", types.SimpleNamespace(
        complete=lambda system, user, **kw: scaffold_reply(user.split("Repository: me/", 1)[1].split(" ", 1)[0]),
        minimal_reasoning=lambda model: {}, LLMError=llm.LLMError))
    # The scan thread is run inline; a test wants the result, not the race.
    monkeypatch.setattr(github.threading, "Thread", lambda target, args=(), daemon=True: type(
        "T", (), {"start": lambda self: target(*args)})())
    return fake


def test_tarball_is_read_into_a_snapshot():
    snap = github.read_tarball(make_tarball({
        "README.md": b"# Title\nText", "package.json": b'{"name":"x"}',
        "src/index.ts": b"x" * 400, "src/util.ts": b"y" * 300, "node_modules/a/b.js": b"z" * 500,
        "dist/bundle.js": b"q" * 900, "logo.png": b"\x89PNG\0\0" * 100,
    }))
    assert snap.readme.startswith("# Title")
    assert list(snap.manifests) == ["package.json"]
    assert "node_modules/a/b.js" not in snap.tree and "dist/bundle.js" not in snap.tree
    assert list(snap.sources) == ["src/index.ts", "src/util.ts"]  # entry point first
    text = snap.text()
    assert "## README" in text and "## Files" in text and "## src/index.ts (head)" in text


def test_score_prefers_recent_worked_on_documented_repos():
    fresh = github.Repo(name="a", full_name="m/a", url="", pushed_at="2026-09-01T00:00:00Z",
                        commits=50, has_readme=True, description="x", stars=10)
    stale = github.Repo(name="b", full_name="m/b", url="", pushed_at="2019-01-01T00:00:00Z",
                        commits=2, has_readme=False)
    assert github.score(fresh) > github.score(stale)
    ordered = github.rank([stale, fresh])
    assert [r.name for r in ordered] == ["a", "b"] and ordered[0].rank == 1


def test_scan_lists_ranks_reads_and_ticks_the_best(scan, monkeypatch):
    monkeypatch.setattr(github, "AUTO_PICK", 2)
    github.start_scan("@me")
    status = github.status()
    assert status["phase"] == "ranked" and status["handle"] == "me"
    names = [r["name"] for r in status["repos"]]
    assert names == ["hydra", "auditex", "oldmail", "dotfiles"]
    assert [r["picked"] for r in status["repos"]] == [True, True, False, False]
    assert all(r["state"] == "ready" for r in status["repos"])
    assert status["repos"][0]["commits"] == 40 and status["repos"][0]["has_readme"]
    assert status["repos"][3]["has_readme"] is False  # tarball past the cap: README unknown
    assert status["repos"][0]["questions"] == ["Who used hydra?", "What was hardest?"]
    # A fork and a repo with none of the handle's commits are issues, not rows.
    assert any("linux: a fork" in i for i in status["issues"])
    assert any("teamwork: empty" in i for i in status["issues"])
    assert any("empty: empty" in i for i in status["issues"])
    # Commits under an unlinked email: counted whole and noted, not skipped.
    assert status["repos"][2]["commits"] == 6
    assert any("oldmail: commits not attributed" in i for i in status["issues"])
    scaffold = github.scaffold("hydra")
    assert scaffold.startswith("# hydra\n\nGitHub: me/hydra\nLink: https://github.com/me/hydra\n")
    # One API call per repo for the commit count, a second only when none
    # is attributed to the handle.
    assert sum(1 for c in scan.calls if c.endswith("/commits")) == 6 + 3


def test_scan_refuses_a_bad_handle_and_a_second_run(scan):
    with pytest.raises(github.GitHubError):
        github.start_scan("not a handle")
    github.start_scan("me")
    github._update(lambda s: setattr(s, "phase", "scanning"))
    with pytest.raises(github.GitHubError, match="already running"):
        github.start_scan("me")


def test_a_bad_repo_is_marked_not_fatal(scan, monkeypatch):
    calls = {"n": 0}

    def flaky(system, user, **kw):
        calls["n"] += 1
        if "me/auditex" in user:
            raise github.llm.LLMError("boom")
        return scaffold_reply("hydra")
    monkeypatch.setattr(github.llm, "complete", flaky)
    github.start_scan("me")
    status = github.status()
    assert status["phase"] == "ranked"
    bad = next(r for r in status["repos"] if r["name"] == "auditex")
    assert bad["state"] == "error" and "boom" in bad["error"]
    assert next(r for r in status["repos"] if r["name"] == "hydra")["state"] == "ready"


def test_rate_limit_is_a_readable_error():
    response = httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1800000000"},
                              request=httpx.Request("GET", "https://api.github.com/x"))
    client = type("C", (), {"get": lambda self, *a, **k: response})()
    with pytest.raises(github.GitHubError, match="rate limit is spent"):
        github._get(client, "https://api.github.com/x")


@pytest.fixture
def interviewed(scan, monkeypatch):
    """A profile interview past setup, so projects can join it."""
    s = Script()
    monkeypatch.setattr(interview.llm, "stream", s.stream)
    monkeypatch.setattr(interview.llm, "complete", s.complete)
    monkeypatch.setattr(interview, "start_children", lambda slug: s.started.append(slug))
    s.started = []
    monkeypatch.setattr(interview.facts_module, "derived_lines", lambda: {})
    monkeypatch.setattr(interview.facts_module, "contacts", lambda: {})
    s.replies.append("```json\n" + EXTRACTION + "\n```")
    interview.start("")
    interview.skip_facts()
    s.replies.append("EXPERIENCES: []\nSTART: yes\n---\nGood.")
    s.replies.append("COVERED:\nDONE: no\n---\nTell me about the billing service.")
    events("Looks right.")
    return s


def test_confirm_merges_a_resume_project_and_queues_the_rest(interviewed, scan, monkeypatch):
    monkeypatch.setattr(github, "AUTO_PICK", 2)
    github.start_scan("me")
    status = github.confirm()
    assert status["phase"] == "confirmed"
    by_name = {r["name"]: r for r in status["repos"]}
    # hydra is on the resume: same experience, the repo's name in brackets.
    assert by_name["hydra"]["slug"] == "hydra"
    state = interview.State.load()
    hydra = next(e for e in state.experiences if e["slug"] == "hydra")
    assert hydra["title"] == "Hydra (hydra)" and hydra["link"] == "https://github.com/me/hydra"
    assert (profile.STORIES / "hydra" / "scaffold.md").exists()
    # auditex is new: a project with the scaffold's questions as its checklist.
    assert by_name["auditex"]["slug"] == "auditex"
    auditex = next(e for e in state.experiences if e["slug"] == "auditex")
    assert auditex["questions"] == ["Who used auditex?", "What was hardest?"]
    assert by_name["dotfiles"]["slug"] == ""  # not ticked, still a candidate
    assert state.queue() == ["backend-intern-acme-corp", "hydra", "auditex"]
    # The resume's project was ticked by the scan whatever its rank.
    assert by_name["hydra"]["on_resume"] == "Hydra"
    with pytest.raises(github.GitHubError, match="nothing ticked"):
        github.confirm()


def test_a_github_project_is_asked_only_its_questions_then_closed(interviewed, scan, monkeypatch):
    monkeypatch.setattr(github, "AUTO_PICK", 2)
    github.start_scan("me")
    github.set_picked("hydra", False)
    github.confirm()
    state = interview.State.load()
    state.current = None
    state.save()
    # Skip the resume's own experiences: close them by hand.
    for slug in ("backend-intern-acme-corp", "hydra"):
        exp = interview.Experience(slug=slug, title=slug, kind="project", closed=True)
        exp.save()
    interviewed.replies.append("COVERED:\nDONE: no\n---\nask: Who used auditex?")
    evs = events("go on")
    assert interview.State.load().current == "auditex"
    system = interviewed.calls[-1][0]["content"]
    assert "This project came from GitHub" in system and "g1. Who used auditex?" in system
    assert "## From GitHub" in interviewed.calls[-1][1]["content"]
    exp = interview.Experience.load("auditex")
    assert exp.lines(False) == ["g1", "g2"] and exp.asked == 1
    interviewed.replies.append("COVERED: g1\nDONE: no\n---\nask: What was hardest?")
    events("My whole class used it.")
    assert interview.Experience.load("auditex").coverage == {"g1": "covered"}
    # The second answer is the last: the code closes, whatever the header says.
    interviewed.replies.append("COVERED: g2\nDONE: no\n---\nThanks.")
    interviewed.replies.append("# auditex\nKind: project\n\n## Who used auditex?\n\nMy class.\n")
    interviewed.replies.append("COVERED:\nDONE: no\n---\nnext")
    events("The audio pipeline.")
    exp = interview.Experience.load("auditex")
    assert exp.closed and exp.asked == 2
    main = (profile.STORIES / "auditex" / "main.md").read_text()
    assert main.startswith("# auditex\nKind: project\nGitHub: me/auditex\nLink: https://github.com/me/auditex\n")
    # The scaffold went into the document prompt, so what GitHub knows is kept.
    assert "## From GitHub" in interviewed.completes[-1][1]


def test_projects_join_a_closed_interview_and_reopen_it(interviewed, scan):
    state = interview.State.load()
    state.phase, state.current = "open", None
    state.save()
    github.start_scan("me")
    github.set_picked("hydra", False)
    github.confirm()
    state = interview.State.load()
    assert state.phase == "interviewing" and state.current is None
    assert "joined the queue" in state.transcript[-1]["content"]


def test_link_change_reaches_the_documents(interviewed, scan):
    github.start_scan("me")
    github.confirm()
    folder = profile.STORIES / "auditex"
    (folder / "main.md").write_text("# auditex\nKind: project\nLink: https://github.com/me/auditex\n\n## x\n")
    (folder / "tailor.md").write_text("Summary: TTS.\nStack: TS\n")
    interview.Experience(slug="auditex", title="auditex", kind="project", link="https://github.com/me/auditex").save()
    status = github.set_link("auditex", "https://chromewebstore.google.com/detail/auditex")
    assert next(r for r in status["repos"] if r["name"] == "auditex")["url"].startswith("https://chromewebstore")
    assert "Link: https://chromewebstore.google.com/detail/auditex" in (folder / "main.md").read_text()
    assert (folder / "tailor.md").read_text().startswith("Summary: TTS.\nLink: https://chromewebstore")
    assert interview.Experience.load("auditex").link.startswith("https://chromewebstore")
    with pytest.raises(github.GitHubError):
        github.set_link("auditex", "chromewebstore.google.com")


def test_confirm_needs_the_interview_started(scan):
    github.start_scan("me")
    with pytest.raises(interview.InterviewError, match="start the profile interview"):
        github.confirm()


def test_tailored_resume_may_link_only_the_master_and_story_links():
    original = r"\href{https://github.com/me/hydra}{Hydra}"
    stories = "### auditex\n\nSummary: TTS.\nLink: https://chromewebstore.google.com/detail/auditex\nStack: TS"
    tailor._check_links(original, r"\href{https://chromewebstore.google.com/detail/auditex}{Auditex}", stories)
    with pytest.raises(tailor.TailorError, match="links to a URL"):
        tailor._check_links(original, r"\href{https://example.com/made-up}{X}", stories)


def test_projects_over_http(interviewed, scan, monkeypatch):
    monkeypatch.setattr(github, "AUTO_PICK", 1)
    client = TestClient(app)
    assert client.post("/projects/scan", json={"handle": "??"}).status_code == 422
    assert client.post("/projects/scan", json={"handle": "me"}).status_code == 200
    body = client.get("/projects").json()
    assert body["phase"] == "ranked" and body["repos"][0]["name"] == "hydra"
    assert client.get("/projects/hydra/scaffold").text.startswith("# hydra")
    assert client.get("/projects/nope/scaffold").status_code == 404
    assert client.post("/projects/auditex/pick", json={"picked": True}).json()["repos"][1]["picked"] is True
    assert client.post("/projects/auditex/link", json={"link": "nope"}).status_code == 422
    assert client.post("/projects/confirm", json={}).json()["phase"] == "confirmed"
    assert client.post("/projects/hydra/pick", json={"picked": False}).status_code == 409
