"""Where the code lives and where the person's data lives.

They are two different places once the app is installed. The code is a
git clone that `git pull` updates; the data (the master resume, the
profile, the stories, the queue, the applications, the edited prompts, the
OpenRouter key) must never be inside it, or an update would collide with
it and a push of the clone would publish a resume.

`AUTOPILOT_HOME` names the data directory. The installer sets it to
`~/Library/Application Support/Autopilot`; unset, it is the repository
itself, which is how a development checkout has always been laid out.
Read once at import: the launcher sets it before anything starts.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
HOME = Path(os.environ.get("AUTOPILOT_HOME") or ROOT).expanduser()

BASE = HOME / "base"
DATA = HOME / "data"
APPLICATIONS = HOME / "applications"
# Prompts the person edited, one file per prompt; the stock text is in the
# code. See tailor/prompts.py.
PROMPTS = DATA / "prompts"
ENV_FILE = HOME / ".env"


def load_env() -> None:
    """The person's .env first, then the clone's, never overriding the shell.

    load_dotenv leaves a variable alone once it is set, so the first file
    read wins; in a development checkout both are the same file.
    """
    load_dotenv(ENV_FILE)
    if ENV_FILE != ROOT / ".env":
        load_dotenv(ROOT / ".env")
