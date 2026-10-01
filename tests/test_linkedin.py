"""LinkedIn's posting, which a fetch cannot see and a scrape cannot select.

A fetch of the job page comes back with 1778 words of LinkedIn around 194
words of posting, and the page's class names are generated (`bghmnt bghmns
bghr4 …`) and rotate, so nothing may hang off them. The guest endpoint
publishes the posting itself, without a login.
"""

from __future__ import annotations

from tailor import fetch, linkedin

GUEST = """
<html><body>
<h2 class="top-card-layout__title topcard__title">Ai Analytics Engineer</h2>
<a class="topcard__org-name-link" href="/company/zealtech">ZealTech</a>
<span class="topcard__flavor topcard__flavor--bullet">Fremont, CA</span>
<div class="show-more-less-html__markup relative">
<p><strong>AI/ML Analytics Engineer</strong></p><ul>
<li>Apply basic AI knowledge, including what an LLM and a RAG are.</li>
<li>Manipulate data using Python and the pandas library.</li></ul>
</div>
<ul class="description__job-criteria-list">
<h3 class="description__job-criteria-subheader">Seniority level</h3>
<span class="description__job-criteria-text">Entry level</span>
<h3 class="description__job-criteria-subheader">Employment type</h3>
<span class="description__job-criteria-text">Full-time</span>
</ul>
</body></html>
"""


def test_the_job_id_is_read_from_either_shape_of_url():
    assert linkedin.job_id("https://www.linkedin.com/jobs/view/4470028949/") == "4470028949"
    assert linkedin.job_id(
        "https://www.linkedin.com/jobs/collections/recommended/?currentJobId=4470028949"
    ) == "4470028949"
    assert linkedin.job_id("https://boards.greenhouse.io/acme/jobs/1") is None
    assert linkedin.job_id("") is None


def test_the_posting_is_the_posting_and_not_the_site():
    posting = linkedin.parse(GUEST, "https://www.linkedin.com/jobs/view/1/")
    assert posting is not None
    assert posting.title == "Ai Analytics Engineer"
    assert posting.company == "ZealTech"
    assert posting.location == "Fremont, CA"
    # The list survives as a list: the tailor reads bullets.
    assert "- Apply basic AI knowledge" in posting.text
    assert "pandas" in posting.text
    # The criteria the screen reads are kept, in their own words.
    assert "Seniority level: Entry level" in posting.text
    assert "Employment type: Full-time" in posting.text
    # And none of LinkedIn's own furniture.
    assert "Expand search" not in posting.text
    assert "Sign in" not in posting.text


def test_a_page_without_a_description_is_not_a_posting():
    """A guest endpoint that answers a sign-in wall, or an expired id."""
    assert linkedin.parse("<html><body>Join LinkedIn</body></html>", "u") is None


def test_the_fetch_asks_linkedin_first_and_falls_back(monkeypatch):
    calls = []
    monkeypatch.setattr(linkedin, "posting", lambda url: calls.append(url) or None)

    class Boom(Exception):
        pass

    def no_http(*args, **kwargs):
        raise Boom("the ordinary fetch ran")

    monkeypatch.setattr(fetch.httpx, "Client", no_http)
    try:
        fetch.fetch("https://www.linkedin.com/jobs/view/42/")
    except Boom:
        pass
    assert calls == ["https://www.linkedin.com/jobs/view/42/"], "LinkedIn is asked first"
    calls.clear()
    try:
        fetch.fetch("https://boards.greenhouse.io/acme/jobs/1")
    except Boom:
        pass
    assert calls == [], "and only for LinkedIn"
