"""Two tabs posting the same Jobright page must not take each other's file.

`POST /posting` answered 500 four times on 2026-10-30 with
`FileNotFoundError: .../<jr_id>.tmp -> .../<jr_id>.json`, each one followed
in `logs/server.log` by its sibling request's `200 OK`: the temp file was
named after the posting, so two concurrent writers for one id shared it and
whichever renamed second found it gone. Jobright's copy of three postings
was dropped, which is exactly the copy `pipeline.fetch_posting` needs for
the pages whose own fetch returns a shell.
"""

import json
import os
import threading

import pytest

from server import postings


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(postings.DIR_ENV, str(tmp_path / "postings"))


def _saved(text: str) -> postings.Saved:
    return postings.Saved(url="https://jobright.ai/jobs/info/abc", title="Role @ Co |",
                          text=text, company="Co")


def test_two_writers_for_one_posting_id_do_not_share_a_temp_file(monkeypatch):
    """The property the fix turns on: each writer's temp path is its own.

    The second writer is held inside `os.replace` until the first has also
    reached it, which is the interleaving that used to raise.
    """
    seen: list[str] = []
    both_here = threading.Barrier(2, timeout=10)
    real_replace = os.replace

    def replace(src, dst):
        seen.append(str(src))
        # Every writer has written its temp file and none has renamed yet:
        # the moment the shared name was fatal.
        assert os.path.exists(src), f"{src} was taken by another writer"
        both_here.wait()
        return real_replace(src, dst)

    monkeypatch.setattr(postings.os, "replace", replace)

    errors: list[BaseException] = []

    def write(text):
        try:
            assert postings.save_source("sameid", _saved(text)) is True
        except BaseException as error:  # noqa: BLE001 - reported, not swallowed
            errors.append(error)

    threads = [threading.Thread(target=write, args=(f"posting {n} " * 10,))
               for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert errors == []
    assert len(seen) == 2 and seen[0] != seen[1], seen
    kept = postings._read("source", "sameid")
    assert kept is not None and kept.text.startswith("posting ")


def test_concurrent_writers_always_leave_one_readable_posting():
    """Under real contention: nobody raises and the file always parses."""
    errors: list[BaseException] = []
    start = threading.Barrier(6, timeout=20)

    def hammer(n):
        try:
            start.wait()
            for round_ in range(25):
                assert postings.save_source("busy", _saved(f"writer {n} round {round_} " * 20))
                kept = postings._read("source", "busy")
                assert kept is not None, "a reader saw no file at all"
                assert kept.text.strip(), "a reader saw a half-written file"
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == [], errors
    path = postings._path("source", "busy")
    json.loads(path.read_text())
    # No writer's temp file is left lying about on a clean run.
    assert not list(path.parent.glob("*.tmp"))


def test_losing_jobrights_copy_does_not_fail_the_request(monkeypatch, caplog):
    """A 500 to a tab that was trying to help us is wrong on its own terms:
    the copy is a fallback, and the capture it was part of must carry on."""
    def no(_src, _dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(postings.os, "replace", no)
    assert postings.save_source("doomed", _saved("text")) is False
    assert postings.save_captured("doomed", _saved("text")) is False
    assert "could not keep" in caplog.text
    # And nothing half-written is left behind to be read as a posting.
    assert postings._read("source", "doomed") is None
    assert not list(postings._path("source", "doomed").parent.glob("*.tmp"))
