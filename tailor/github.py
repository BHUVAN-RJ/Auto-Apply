"""Projects from GitHub: scan a handle, read each public repo, rank them,
and hand the chosen ones to the interviewer as experiences.

The scan runs in a thread and writes base/stories/_github.json as it goes,
so the Projects tab can draw itself from disk at any point and a restart
picks up where it stopped. Per repo, the reading is two requests, neither
needing a token: one API call for the candidate's commit count (the
listing already carries description, language, stars, and dates), and the
tarball from codeload, which is not rate limited. No git binary, no clone.
Fifty repos fit under the unauthenticated hour; AUTOPILOT_GITHUB_TOKEN
raises the limit when someone has more.

What GitHub can tell is written into a scaffold by the cheap model: what
the code does, the stack, what the commit log says about the candidate's
part, and the one to three questions only the candidate can answer. Those
questions are the whole interview for a project that came from here; the
scaffold is the rest of its main document.

Ranking is arithmetic on the listing (recency, stars, commits, a README),
never a model call. The top ten are ticked for the candidate to confirm;
the rest stay here as candidates until ticked. Forks and repos the handle
has no commits on are skipped and listed as issues, not judged.
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import tarfile
import threading
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

import httpx

from . import llm, profile

STATE_NAME = "_github.json"
SCAFFOLD_DIR = "_github"
RULES = Path(__file__).resolve().parent / "github_rules.md"

API = "https://api.github.com"
CODELOAD = "https://codeload.github.com"
TIMEOUT = 60.0

# How many the scan ticks by itself. The best ten are what a resume uses.
AUTO_PICK = 10
MAX_QUESTIONS = 3

# The tarball is read in memory; a repo full of binaries stays unread past
# this, and its README alone has to do.
MAX_TARBALL_BYTES = 40 * 1024 * 1024
MAX_TREE_ENTRIES = 400
MAX_README_CHARS = 12_000
MAX_MANIFEST_CHARS = 6_000
MAX_SOURCE_FILES = 5
MAX_SOURCE_CHARS = 5_000
MAX_CONTEXT_CHARS = 45_000

SKIP_DIRS = {"node_modules", "vendor", "dist", "build", ".git", "__pycache__", ".venv", "venv",
             "target", ".next", "coverage", "assets", "static", "public", "images", "img", "fonts"}
MANIFESTS = ("package.json", "pyproject.toml", "requirements.txt", "setup.py", "go.mod",
             "Cargo.toml", "pom.xml", "build.gradle", "Gemfile", "composer.json", "Makefile",
             "Dockerfile", "docker-compose.yml", "manifest.json", "Package.swift", "pubspec.yaml")
SOURCE_SUFFIXES = (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".swift",
                   ".c", ".cc", ".cpp", ".h", ".rb", ".php", ".cs", ".scala", ".sh", ".sql", ".m")
ENTRY_NAMES = ("main", "index", "app", "server", "cli", "__main__", "mod", "lib")


class GitHubError(RuntimeError):
    pass


def github_model() -> str:
    return os.environ.get("OPENROUTER_GITHUB_MODEL",
                          os.environ.get("OPENROUTER_SCREEN_MODEL", "deepseek/deepseek-v4-flash"))


def _headers() -> dict:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "job-autopilot"}
    token = os.environ.get("AUTOPILOT_GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def state_path() -> Path:
    return profile.STORIES / STATE_NAME


def scaffold_dir(name: str) -> Path:
    return profile.STORIES / SCAFFOLD_DIR / name


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------- state --


@dataclass
class Repo:
    name: str               # repo name, also the scaffold folder
    full_name: str
    url: str
    description: str = ""
    language: str = ""
    stars: int = 0
    pushed_at: str = ""
    created_at: str = ""
    size_kb: int = 0
    archived: bool = False
    commits: int = -1       # the handle's own commits; -1 = not counted
    has_readme: bool = False
    score: float = 0.0
    rank: int = 0
    state: str = "listed"   # listed | reading | ready | error
    error: str = ""
    picked: bool = False    # ticked, by the scan or the candidate
    on_resume: str = ""     # the resume project this repo is, when the names say so
    slug: str = ""          # the experience it became, once confirmed
    questions: list[str] = field(default_factory=list)
    summary: str = ""       # one line from the scaffold, for the list
    # A project can live at more than one address: the repository, a store
    # listing, a deployed site. `links` holds them all (the repository is
    # always the first), `link` is the one the resume hyperlinks. Empty
    # `link` = the repository.
    links: list[str] = field(default_factory=list)
    link: str = ""

    def all_links(self) -> list[str]:
        out = [self.url] if self.url else []
        for url in self.links:
            if url and url not in out:
                out.append(url)
        return out

    def chosen(self) -> str:
        return self.link if self.link in self.all_links() else self.url


@dataclass
class Scan:
    handle: str = ""
    phase: str = "new"      # new | scanning | ranked | confirmed | error
    repos: list[dict] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    error: str = ""
    started: str = ""

    @classmethod
    def load(cls) -> "Scan":
        path = state_path()
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def save(self) -> None:
        _write_atomic(state_path(), json.dumps(asdict(self), indent=2) + "\n")

    def repo(self, name: str) -> Repo:
        for entry in self.repos:
            if entry["name"] == name:
                return _repo_of(entry)
        raise GitHubError(f"no repo {name!r} in the scan")

    def put(self, repo: Repo) -> None:
        for i, entry in enumerate(self.repos):
            if entry["name"] == repo.name:
                self.repos[i] = asdict(repo)
                return
        self.repos.append(asdict(repo))


_lock = threading.Lock()


def _update(fn) -> Scan:
    """Read-modify-write under one lock: the thread and the API both write."""
    with _lock:
        scan = Scan.load()
        fn(scan)
        scan.save()
        return scan


def _repo_of(entry: dict) -> Repo:
    return Repo(**{k: v for k, v in entry.items() if k in Repo.__dataclass_fields__})


def status() -> dict:
    scan = Scan.load()
    repos = sorted(scan.repos, key=lambda r: (r.get("rank") or 10**6, r["name"]))
    return {
        "handle": scan.handle, "phase": scan.phase, "error": scan.error, "issues": scan.issues,
        "repos": [{k: r.get(k) for k in ("name", "full_name", "url", "description", "language",
                                          "stars", "pushed_at", "commits", "has_readme", "rank",
                                          "state", "error", "picked", "slug", "questions", "summary",
                                          "on_resume")}
                 | {"links": _repo_of(r).all_links(), "link": _repo_of(r).chosen()}
                  for r in repos],
        "ready": sum(1 for r in repos if r["state"] == "ready"),
        "auto_pick": AUTO_PICK,
    }


# --------------------------------------------------------------- listing --


def _get(client: httpx.Client, url: str, **params) -> httpx.Response:
    response = client.get(url, params=params or None, headers=_headers(), timeout=TIMEOUT)
    _check(response, url)
    return response


def _check(response: httpx.Response, url: str) -> None:
    if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
        reset = response.headers.get("x-ratelimit-reset", "")
        raise GitHubError("GitHub's rate limit is spent; it resets at "
                          + (datetime.fromtimestamp(int(reset), timezone.utc).strftime("%H:%M UTC")
                             if reset.isdigit() else "the top of the hour")
                          + ". Set AUTOPILOT_GITHUB_TOKEN to raise it.")
    if response.status_code == 404:
        raise GitHubError(f"not found: {url}")
    if response.status_code >= 400:
        raise GitHubError(f"GitHub answered {response.status_code} for {url.split('/repos/')[-1]}")


def list_repos(handle: str, client: Optional[httpx.Client] = None) -> tuple[list[Repo], list[str]]:
    """Every public repo the handle owns, forks set aside as issues."""
    client = client or httpx.Client(follow_redirects=True)
    repos: list[Repo] = []
    issues: list[str] = []
    page = 1
    while True:
        data = _get(client, f"{API}/users/{handle}/repos", per_page=100, page=page,
                    type="owner", sort="pushed").json()
        if not isinstance(data, list):
            raise GitHubError("unexpected reply from GitHub")
        for item in data:
            if item.get("fork"):
                issues.append(f"{item['name']}: a fork, skipped. Add it by hand if the work is yours.")
                continue
            repos.append(Repo(
                name=item["name"], full_name=item["full_name"], url=item["html_url"],
                description=item.get("description") or "", language=item.get("language") or "",
                stars=int(item.get("stargazers_count") or 0), pushed_at=item.get("pushed_at") or "",
                created_at=item.get("created_at") or "", size_kb=int(item.get("size") or 0),
                archived=bool(item.get("archived")),
            ))
        if len(data) < 100:
            break
        page += 1
    if not repos and not issues:
        raise GitHubError(f"{handle} has no public repositories")
    return repos, issues


def count_commits(full_name: str, handle: str, client: httpx.Client) -> tuple[int, str]:
    """The handle's commits on the repo, off the Link header's last page.
    Commits made under an email GitHub does not link to the account carry
    no author login, so an owned repo with none attributed is counted
    whole and noted rather than skipped. Returns (count, note)."""
    def count(**params) -> int:
        response = client.get(f"{API}/repos/{full_name}/commits", params={"per_page": 1, **params},
                              headers=_headers(), timeout=TIMEOUT)
        if response.status_code == 409:
            return 0  # an empty repository
        _check(response, f"{API}/repos/{full_name}/commits")
        if not response.json():
            return 0
        match = re.search(r'[?&]page=(\d+)>;\s*rel="last"', response.headers.get("link", ""))
        return int(match.group(1)) if match else 1

    own = count(author=handle)
    if own:
        return own, ""
    total = count()
    if total:
        return total, "commits not attributed to the handle (another email); all counted"
    return 0, ""


# --------------------------------------------------------------- reading --


@dataclass
class Snapshot:
    tree: list[str] = field(default_factory=list)
    readme: str = ""
    manifests: dict = field(default_factory=dict)
    sources: dict = field(default_factory=dict)
    truncated: bool = False

    def text(self) -> str:
        parts = []
        if self.readme:
            parts.append(f"## README\n\n{self.readme[:MAX_README_CHARS]}")
        if self.tree:
            listing = "\n".join(self.tree[:MAX_TREE_ENTRIES])
            if len(self.tree) > MAX_TREE_ENTRIES:
                listing += f"\n… {len(self.tree) - MAX_TREE_ENTRIES} more"
            parts.append(f"## Files\n\n{listing}")
        for name, body in self.manifests.items():
            parts.append(f"## {name}\n\n```\n{body[:MAX_MANIFEST_CHARS]}\n```")
        for name, body in self.sources.items():
            parts.append(f"## {name} (head)\n\n```\n{body[:MAX_SOURCE_CHARS]}\n```")
        text = "\n\n".join(parts)
        return text[:MAX_CONTEXT_CHARS]


def _is_text(data: bytes) -> bool:
    return b"\0" not in data[:1024]


def read_tarball(data: bytes) -> Snapshot:
    """Tree, README, manifests, and the heads of a few entry-point files."""
    snap = Snapshot()
    candidates: list[tuple[int, str, tarfile.TarInfo]] = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar:
            if not member.isfile():
                continue
            parts = member.name.split("/")[1:]  # the tarball's top folder is <repo>-<sha>
            if not parts or any(p in SKIP_DIRS or p.startswith(".") for p in parts[:-1]):
                continue
            rel = "/".join(parts)
            base = parts[-1]
            snap.tree.append(rel)
            if base.lower().startswith("readme") and not snap.readme and member.size < 200_000:
                body = tar.extractfile(member).read()
                if _is_text(body):
                    snap.readme = body.decode("utf-8", errors="replace")
            elif base in MANIFESTS and len(parts) <= 2 and member.size < 100_000:
                body = tar.extractfile(member).read()
                if _is_text(body):
                    snap.manifests[rel] = body.decode("utf-8", errors="replace")
            elif base.endswith(SOURCE_SUFFIXES) and 200 < member.size < 200_000:
                stem = base.rsplit(".", 1)[0].lower()
                depth = len(parts)
                # Entry points first, then shallow files, then big ones.
                weight = (0 if stem in ENTRY_NAMES else 1, depth, -member.size)
                candidates.append((weight, rel, member))
        candidates.sort(key=lambda c: c[0])
        for _, rel, member in candidates[:MAX_SOURCE_FILES]:
            body = tar.extractfile(member).read(MAX_SOURCE_CHARS * 2)
            if _is_text(body):
                snap.sources[rel] = body.decode("utf-8", errors="replace")
    snap.tree.sort()
    return snap


def fetch_tarball(full_name: str, client: httpx.Client) -> Optional[bytes]:
    """codeload's tarball of HEAD, or None past the size cap."""
    with client.stream("GET", f"{CODELOAD}/{full_name}/tar.gz/HEAD", headers=_headers(),
                       timeout=TIMEOUT, follow_redirects=True) as response:
        if response.status_code == 404:
            return None
        response.raise_for_status()
        buffer = io.BytesIO()
        for chunk in response.iter_bytes():
            buffer.write(chunk)
            if buffer.tell() > MAX_TARBALL_BYTES:
                return None
    return buffer.getvalue()


# ------------------------------------------------------------- scaffold --


def _rules() -> str:
    return RULES.read_text()


def _parse_json_block(reply: str) -> dict:
    match = re.search(r"```(?:json)?\s*(.*?)```", reply, re.S)
    text = match.group(1) if match else reply
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GitHubError(f"model reply was not json: {exc}") from exc
    if not isinstance(data, dict):
        raise GitHubError("model reply was not a json object")
    return data


def write_scaffold(repo: Repo, snap: Optional[Snapshot], handle: str) -> tuple[str, list[str], str]:
    """The cheap model reads what GitHub has and writes scaffold.md plus
    the questions only the candidate can answer. Returns (markdown,
    questions, one-line summary)."""
    meta = [f"Repository: {repo.full_name} ({repo.url})",
            f"Description: {repo.description or 'none'}",
            f"Primary language: {repo.language or 'unknown'}",
            f"Stars: {repo.stars}", f"Created: {repo.created_at[:10]}", f"Last push: {repo.pushed_at[:10]}",
            f"Commits by {handle}: {repo.commits if repo.commits >= 0 else 'not counted'}",
            f"Archived: {'yes' if repo.archived else 'no'}"]
    user = "## Repository\n\n" + "\n".join(meta)
    if snap is not None:
        user += "\n\n" + snap.text()
    else:
        user += "\n\n(The code could not be read; only the listing above is known.)"
    reply = llm.complete(_rules(), user, model=github_model(), temperature=0.1, max_tokens=4000,
                         reasoning=llm.minimal_reasoning(github_model()))
    data = _parse_json_block(reply)
    summary = str(data.get("summary") or "").strip()
    questions = [str(q).strip() for q in (data.get("questions") or []) if str(q).strip()][:MAX_QUESTIONS]
    stack = data.get("stack") or []
    if isinstance(stack, str):
        stack = [s.strip() for s in stack.split(",") if s.strip()]
    unknowns = data.get("unknowns") or []
    if isinstance(unknowns, str):
        unknowns = [unknowns]
    lines = [f"# {repo.name}", "", f"GitHub: {repo.full_name}", f"Link: {repo.chosen()}", "",
             f"Summary: {summary}", f"Stack: {', '.join(str(s) for s in stack) or 'not stated'}",
             f"Dates: {repo.created_at[:10] or 'unknown'} to {repo.pushed_at[:10] or 'unknown'}",
             f"Commits by the candidate: {repo.commits if repo.commits >= 0 else 'not counted'}", "",
             "## What it does", "", str(data.get("what_it_does") or "").strip() or "not stated", "",
             "## How it is built", "", str(data.get("how_it_is_built") or "").strip() or "not stated", "",
             "## Own contribution, from the commit log", "",
             str(data.get("own_contribution") or "").strip() or "not stated", "",
             "## Not on GitHub", ""] + [f"- {u}" for u in unknowns]
    return "\n".join(lines).rstrip() + "\n", questions, summary


# --------------------------------------------------------------- ranking --


def _days_since(iso: str) -> float:
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 3650.0
    return max(0.0, (datetime.now(timezone.utc) - then).total_seconds() / 86400)


def score(repo: Repo) -> float:
    """Newer, starred, worked-on, documented. Each term is bounded so no
    one of them decides alone; an old project with real work still ranks."""
    recency = max(0.0, 1.0 - _days_since(repo.pushed_at) / 1095)  # three years to zero
    stars = min(1.0, math.log1p(repo.stars) / math.log1p(50))
    commits = min(1.0, math.log1p(max(repo.commits, 0)) / math.log1p(100))
    readme = 1.0 if repo.has_readme else 0.0
    described = 0.5 if repo.description else 0.0
    size = min(1.0, math.log1p(repo.size_kb) / math.log1p(5000))
    return round(2.5 * recency + 1.5 * stars + 2.0 * commits + 1.0 * readme + described + 0.5 * size, 3)


def rank(repos: list[Repo]) -> list[Repo]:
    ordered = sorted(repos, key=lambda r: (-r.score, r.name))
    for i, repo in enumerate(ordered, 1):
        repo.rank = i
    return ordered


# ------------------------------------------------------------------ scan --


def start_scan(handle: str) -> dict:
    handle = handle.strip().lstrip("@")
    match = re.fullmatch(r"(?:https?://github\.com/)?([A-Za-z0-9-]+)/?", handle)
    if not match:
        raise GitHubError("that is not a GitHub handle")
    handle = match.group(1)
    scan = Scan.load()
    if scan.phase == "scanning":
        raise GitHubError("a scan is already running")
    scan = Scan(handle=handle, phase="scanning", started=datetime.now(timezone.utc).isoformat())
    with _lock:
        scan.save()
    threading.Thread(target=_scan_safely, args=(handle,), daemon=True).start()
    return status()


def _scan_safely(handle: str) -> None:
    try:
        run_scan(handle)
    except Exception as exc:  # noqa: BLE001 - thread; record, never raise
        traceback.print_exc()

        def fail(scan: Scan) -> None:
            scan.phase = "error"
            scan.error = f"{type(exc).__name__}: {exc}"
        _update(fail)


def run_scan(handle: str, client: Optional[httpx.Client] = None) -> None:
    client = client or httpx.Client(follow_redirects=True)
    repos, issues = list_repos(handle, client)

    # Commit counts first: one call each, and the ranking wants them. A spent
    # rate limit here leaves the count unknown rather than stopping the scan.
    for repo in repos:
        try:
            repo.commits, note = count_commits(repo.full_name, handle, client)
            if note:
                issues.append(f"{repo.name}: {note}.")
        except GitHubError as exc:
            issues.append(f"{repo.name}: commits not counted ({exc})")
            if "rate limit" in str(exc):
                break
        except httpx.HTTPError as exc:
            issues.append(f"{repo.name}: commits not counted ({type(exc).__name__})")
    own = [r for r in repos if r.commits != 0]
    for repo in repos:
        if repo.commits == 0:
            issues.append(f"{repo.name}: empty, skipped.")
    # A repo that is a project on the resume is always in: the resume
    # already uses it, and the scaffold only enriches that story.
    from . import interview
    experiences = interview.State.load().experiences
    for repo in own:
        repo.has_readme = repo.size_kb > 0  # refined once the tarball is read
        repo.score = score(repo)
    ordered = rank(own)
    for i, repo in enumerate(ordered):
        match = match_resume_project(repo, experiences)
        repo.on_resume = match["title"] if match else ""
        repo.picked = i < AUTO_PICK or bool(repo.on_resume)

    def listed(scan: Scan) -> None:
        scan.issues = issues
        scan.repos = [asdict(r) for r in ordered]
    _update(listed)

    # Read in rank order so the ten that matter are ready first.
    for repo in ordered:
        _read_one(repo, handle, client)

    def finish(scan: Scan) -> None:
        # Re-rank now that READMEs are known; picks follow the final order.
        fresh = rank([scan.repo(r["name"]) for r in scan.repos])
        for i, repo in enumerate(fresh):
            repo.picked = i < AUTO_PICK or bool(repo.on_resume)
            scan.put(repo)
        if scan.phase == "scanning":
            scan.phase = "ranked"
    _update(finish)


def _read_one(repo: Repo, handle: str, client: httpx.Client) -> None:
    def mark(state: str, **fields) -> None:
        def apply(scan: Scan) -> None:
            current = scan.repo(repo.name)
            current.state = state
            for key, value in fields.items():
                setattr(current, key, value)
            scan.put(current)
        _update(apply)

    mark("reading")
    try:
        snap: Optional[Snapshot] = None
        try:
            data = fetch_tarball(repo.full_name, client)
            if data is not None:
                snap = read_tarball(data)
        except (httpx.HTTPError, tarfile.TarError, OSError) as exc:
            snap = None
            print(f"  {repo.name}: tarball not read ({exc})")
        repo.has_readme = bool(snap is not None and snap.readme.strip())
        markdown, questions, summary = write_scaffold(repo, snap, handle)
        _write_atomic(scaffold_dir(repo.name) / "scaffold.md", markdown)
        mark("ready", questions=questions, summary=summary, has_readme=repo.has_readme,
             score=score(repo))
    except Exception as exc:  # noqa: BLE001 - one bad repo never stops the scan
        mark("error", error=f"{type(exc).__name__}: {exc}")


def scaffold(name: str) -> str:
    path = scaffold_dir(name) / "scaffold.md"
    if not path.exists():
        raise GitHubError(f"{name} has not been read yet")
    return path.read_text()


# ---------------------------------------------------------------- picks --


def set_picked(name: str, picked: bool) -> dict:
    def apply(scan: Scan) -> None:
        repo = scan.repo(name)
        if repo.slug:
            raise GitHubError(f"{name} is already in the interview")
        repo.picked = picked
        scan.put(repo)
    _update(apply)
    return status()


def _valid_link(link: str) -> str:
    link = link.strip()
    if not re.match(r"https?://", link):
        raise GitHubError("a link starts with http:// or https://")
    return link


def set_link(name: str, link: str) -> dict:
    """The URL the resume hyperlinks for this project: the repository by
    default, a store listing or a live site when the candidate says so.
    A link the project does not have yet is added to its list. Written to
    the scan, and through to the experience's documents when one exists."""
    link = _valid_link(link)

    def apply(scan: Scan) -> None:
        repo = scan.repo(name)
        if link not in repo.all_links():
            repo.links.append(link)
        repo.link = link
        scan.put(repo)
    return _push_links(_update(apply), name)


def add_link(name: str, link: str) -> dict:
    """Another address for the same project, without changing the choice."""
    link = _valid_link(link)

    def apply(scan: Scan) -> None:
        repo = scan.repo(name)
        if link not in repo.all_links():
            repo.links.append(link)
            scan.put(repo)
    return _push_links(_update(apply), name)


def remove_link(name: str, link: str) -> dict:
    """Drop one address. The repository's own URL stays; the choice falls
    back to it when the chosen address is the one removed."""
    link = link.strip()

    def apply(scan: Scan) -> None:
        repo = scan.repo(name)
        if link == repo.url:
            raise GitHubError("the repository's own link stays")
        repo.links = [u for u in repo.links if u != link]
        if repo.link == link:
            repo.link = ""
        scan.put(repo)
    return _push_links(_update(apply), name)


def _push_links(scan: Scan, name: str) -> dict:
    """The experience carries the same list and the same choice."""
    repo = scan.repo(name)
    if repo.slug:
        from . import interview
        interview.set_links(repo.slug, repo.all_links(), repo.chosen())
    return status()


def similar(a: str, b: str) -> float:
    def norm(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def match_resume_project(repo: Repo, experiences: list[dict]) -> Optional[dict]:
    """The resume project this repo is, when the names say so. The resume
    name is often longer ("Project Hydra (Distributed Systems Platform)"):
    the repo's name inside the title counts, so does a close ratio."""
    best, best_score = None, 0.0
    for entry in experiences:
        if entry.get("kind") != "project" or entry.get("github"):
            continue
        title = entry["title"]
        head = re.split(r"[(:\-–—,]", title, maxsplit=1)[0]
        ratio = max(similar(repo.name, title), similar(repo.name, head),
                    similar(repo.name.replace("-", " "), head))
        if re.sub(r"[^a-z0-9]", "", repo.name.lower()) in re.sub(r"[^a-z0-9]", "", title.lower()):
            ratio = max(ratio, 0.9)
        if ratio > best_score:
            best, best_score = entry, ratio
    return best if best_score >= 0.75 else None


def confirm() -> dict:
    """Ticked repos become experiences: a match on the resume folds the
    scaffold into that project, the rest queue for their short interview."""
    from . import interview
    scan = Scan.load()
    if scan.phase not in ("ranked", "confirmed"):
        raise GitHubError("the scan has not finished")
    picked = [scan.repo(r["name"]) for r in scan.repos if r.get("picked") and not r.get("slug")]
    if not picked:
        raise GitHubError("nothing ticked")
    for repo in picked:
        if repo.state != "ready":
            raise GitHubError(f"{repo.name} is not read yet")
    added = interview.add_github_projects(picked)

    def apply(current: Scan) -> None:
        for name, slug in added.items():
            repo = current.repo(name)
            repo.slug = slug
            current.put(repo)
        current.phase = "confirmed"
    _update(apply)
    return status()
