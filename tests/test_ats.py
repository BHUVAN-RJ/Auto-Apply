"""Per-system notes: picked by URL, read from the rules file, absent for
an unknown system."""
from browser import ats


def test_detects_the_systems_by_url():
    assert ats.detect("https://fa-evmr.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/31660") == "oracle"
    assert ats.detect("https://job-boards.greenhouse.io/acme/jobs/4001") == "greenhouse"
    assert ats.detect("https://acme.com/careers/apply?gh_jid=4001") == "greenhouse"
    assert ats.detect("https://jobs.ashbyhq.com/fieldguide/abc/application") == "ashby"
    assert ats.detect("https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/SWE_R123") == "workday"
    assert ats.detect("https://jobs.lever.co/acme/uuid") == "lever"
    assert ats.detect("https://acme.com/jobs/1") == ""


def test_every_system_has_notes_and_they_name_the_submit_control():
    for name, _ in ats.SYSTEMS:
        body = ats.notes(name)
        assert body, name
        assert "refused" in body, name


def test_task_block_is_empty_for_an_unknown_system():
    assert ats.task_block("https://acme.com/jobs/1") == ""
    block = ats.task_block("https://jobs.ashbyhq.com/x/y/application")
    assert block.startswith("\nNotes for this application system (ashby):")
