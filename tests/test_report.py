from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from server import report
from server.app import app
from server.models import Job


def test_redact_takes_out_what_points_at_a_person():
    text = ("mail jane.doe@gmail.com or +1 (555) 010-0100, see "
            "https://jobs.ashbyhq.com/acme/123?utm=x in /Users/jane/Library")
    out = report.redact(text)
    assert "jane" not in out and "555" not in out and "/acme/123" not in out
    assert "<link jobs.ashbyhq.com>" in out and "<email>" in out


def test_the_issue_url_uses_the_template_and_fits(monkeypatch):
    url = report.issue_url("problem", "resume slot empty", "It stayed empty.", "x" * 20_000)
    assert len(url) <= report.MAX_URL
    query = parse_qs(urlparse(url).query)
    assert query["template"] == ["problem.yml"]
    assert query["title"] == ["Problem: resume slot empty"]
    assert query["what"] == ["It stayed empty."]


def test_diagnostics_and_open(monkeypatch):
    job = Job(url="https://boards.greenhouse.io/acme/jobs/9", company="Acme", title="SWE",
              error="TailorError: bad reply for jane@acme.com")
    monkeypatch.setattr(report.queue, "all_jobs", lambda: [job])
    opened = []
    monkeypatch.setattr(report.subprocess, "run",
                        lambda args, **kw: opened.append(args) or type("R", (), {"returncode": 0, "stdout": ""})())
    client = TestClient(app)
    diag = client.get("/report").json()["diagnostics"]
    assert "[boards.greenhouse.io] TailorError" in diag
    assert "jane@acme.com" not in diag and "Acme" not in diag
    assert client.post("/report/open", json={"kind": "feature", "text": ""}).status_code == 400
    reply = client.post("/report/open", json={"kind": "feature", "text": "dark mode",
                                              "diagnostics": "me@x.com"}).json()
    assert reply["opened"] and opened[-1][0] == "open"
    assert "me%40x.com" not in reply["url"] and "feature.yml" in reply["url"]
