"""The Settings tab: every key and account the app needs, in one place.

Until now a key was typed wherever it happened to be wanted - the
OpenRouter key in the onboarding wizard, the Apollo key and the Gmail App
Password on one job's Outreach card - so there was nowhere to go to see
what the app holds, nowhere to replace a key that had been rotated, and no
way to add the Apollo key before the first job existed to add it from.

What this module promises:

- **A key is written, never read back.** `GET /keys` says whether each one
  is set and shows its last four characters, because that is enough to
  tell two keys apart and not enough to use one. Nothing here returns a
  secret, so the page cannot leak one and neither can a screenshot of it.
- **It goes to the data directory's `.env`**, `0600`, through the same
  `write_env` the wizard uses, and into `os.environ`, so the server and
  every script it starts from then on see it without a restart.
- **The shape is checked before the network is.** A paste with a newline
  in it, or an OpenRouter key that does not start with `sk-or-`, is a
  typing mistake and is refused as one.
- **Checking a key is the person's click, not a side effect of typing
  one.** `POST /keys/{name}/check` is separate, because a check costs a
  request to somebody else's service (and, for SMTP, a login attempt that
  a provider may count), while saving costs nothing.

The switches in `.env` are not here. A key is a credential the person
holds and must be able to replace; `AUTOPILOT_SPLIT=0` is a decision about
how the app behaves, and the ones worth a control of their own already
have one on the page that owns them.
"""

from __future__ import annotations

import os
import re
import smtplib
from dataclasses import dataclass
from typing import Callable, Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import mailing
import paths
from outreach import apollo

from .prompts import KEY_CHECK_URL, KEY_SHAPE, write_env

router = APIRouter()

FISH_KEY = "AUTOPILOT_FISH_API_KEY"  # `voice/fish.py` reads it; it has no constant.

SHOWN = 4  # Characters of a saved key the page may see: enough to tell two apart.
TIMEOUT = 15


class KeyValue(BaseModel):
    value: str


@dataclass(frozen=True)
class Secret:
    """One credential: where it lives, what it is for, and how to check it."""

    name: str  # The `.env` variable, which is also the id the page sends.
    label: str
    group: str  # Several variables held by one account share a group.
    what: str  # What stops working without it, in the person's words.
    where: str  # Where to get one.
    required: bool = False
    password: bool = True  # False for the ones that are not secret (an address).
    shape: Optional[re.Pattern] = None
    shape_said: str = ""
    min_length: int = 8
    check: Optional[str] = None  # The group whose checker covers this one.


def _openrouter_check(values: dict[str, str]) -> str:
    key = values.get("OPENROUTER_API_KEY", "")
    try:
        answer = httpx.get(KEY_CHECK_URL, headers={"Authorization": f"Bearer {key}"},
                           timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"could not reach OpenRouter: {exc}") from None
    if answer.status_code in (401, 403):
        raise HTTPException(400, "OpenRouter refused this key. Copy it again from "
                                 "openrouter.ai/settings/keys.")
    if answer.status_code >= 400:
        raise HTTPException(502, f"OpenRouter answered {answer.status_code}")
    data = answer.json().get("data", {}) if answer.headers.get(
        "content-type", "").startswith("application/json") else {}
    limit, usage = data.get("limit"), data.get("usage")
    if limit is None:
        return "OpenRouter accepts this key."
    return f"OpenRouter accepts this key. {usage or 0} of {limit} credits used."


def _apollo_check(values: dict[str, str]) -> str:
    """Apollo's own health route, which costs no credit."""
    key = values.get(apollo.API_KEY, "")
    try:
        answer = httpx.get(f"{apollo.BASE_URL}/auth/health",
                           headers={"X-Api-Key": key, "accept": "application/json"},
                           timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"could not reach Apollo: {exc}") from None
    if answer.status_code in (401, 403):
        raise HTTPException(400, "Apollo refused this key. Copy it again from "
                                 "the API keys page in Apollo's settings.")
    if answer.status_code >= 400:
        raise HTTPException(502, f"Apollo answered {answer.status_code}")
    return "Apollo accepts this key."


def _github_check(values: dict[str, str]) -> str:
    token = values.get("AUTOPILOT_GITHUB_TOKEN", "")
    try:
        answer = httpx.get("https://api.github.com/user",
                           headers={"Authorization": f"Bearer {token}",
                                    "Accept": "application/vnd.github+json"},
                           timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"could not reach GitHub: {exc}") from None
    if answer.status_code in (401, 403):
        raise HTTPException(400, "GitHub refused this token. A new one: "
                                 "github.com/settings/tokens, no scopes needed.")
    if answer.status_code >= 400:
        raise HTTPException(502, f"GitHub answered {answer.status_code}")
    who = answer.json().get("login", "") if answer.content else ""
    remaining = answer.headers.get("x-ratelimit-remaining", "")
    said = f"GitHub accepts this token" + (f", signed in as {who}" if who else "")
    return said + (f". {remaining} requests left this hour." if remaining else ".")


def _mail_check(values: dict[str, str]) -> str:
    """A login, nothing sent. The one check that touches the person's own
    account, which is why it is a click and not a consequence of saving."""
    user = values.get(mailing.USER, "")
    password = values.get(mailing.PASS, "")
    if not user or not password:
        raise HTTPException(400, "enter the address and its App Password first")
    host = os.environ.get(mailing.HOST, "smtp.gmail.com").strip() or "smtp.gmail.com"
    port = int(os.environ.get(mailing.PORT, "587") or 587)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
                smtp.login(user, password)
        else:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.login(user, password)
    except smtplib.SMTPAuthenticationError:
        raise HTTPException(400, "That login was refused. For Gmail this must be an App "
                                 "Password from myaccount.google.com/apppasswords, with "
                                 "2-step verification on - never the account password.") from None
    except (smtplib.SMTPException, OSError) as exc:
        raise HTTPException(502, f"could not reach {host}: {exc}") from None
    return f"{host} accepted this login. Nothing was sent."


def _fish_check(values: dict[str, str]) -> str:
    key = values.get(FISH_KEY, "")
    try:
        answer = httpx.get("https://api.fish.audio/wallet/self/api-credit",
                           headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"could not reach Fish Audio: {exc}") from None
    if answer.status_code in (401, 403):
        raise HTTPException(400, "Fish Audio refused this key.")
    if answer.status_code >= 400:
        raise HTTPException(502, f"Fish Audio answered {answer.status_code}")
    return "Fish Audio accepts this key."


CHECKS: dict[str, Callable[[dict[str, str]], str]] = {
    "openrouter": _openrouter_check,
    "apollo": _apollo_check,
    "github": _github_check,
    "mail": _mail_check,
    "fish": _fish_check,
}

ONE_LINE = re.compile(r"^[^\s]+$")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

CATALOG: tuple[Secret, ...] = (
    Secret(
        name="OPENROUTER_API_KEY", label="OpenRouter key", group="openrouter",
        what="Every model call: the screen, the tailored resume, the cover letter, the "
             "form answers, the interviews. Nothing works without it.",
        where="openrouter.ai/settings/keys",
        required=True, shape=KEY_SHAPE, shape_said="OpenRouter keys start with sk-or-.",
        min_length=26, check="openrouter",
    ),
    Secret(
        name=apollo.API_KEY, label="Apollo key", group="apollo",
        what="Finding a recruiter and a hiring manager for the Outreach card. Without "
             "it, outreach falls back to a guessed address you have to verify.",
        where="Apollo's settings, under API keys",
        min_length=20, check="apollo",
    ),
    Secret(
        name=mailing.USER, label="Gmail address", group="mail",
        what="Who the Scout digests and any approved outreach are sent from.",
        where="your own address",
        password=False, shape=EMAIL, shape_said="That does not look like an email address.",
        min_length=6, check="mail",
    ),
    Secret(
        name=mailing.PASS, label="Gmail App Password", group="mail",
        what="Lets the app log in to send. Gmail refuses the account password over "
             "SMTP; only an App Password works, and 2-step verification has to be on.",
        where="myaccount.google.com/apppasswords",
        min_length=12, check="mail",
    ),
    Secret(
        name=mailing.TO, label="Send digests to", group="mail",
        what="Where Scout's digest of new roles arrives. Blank, it goes to the Gmail "
             "address above; commas for several.",
        where="your own address",
        password=False, min_length=6,
    ),
    Secret(
        name="AUTOPILOT_GITHUB_TOKEN", label="GitHub token", group="github",
        what="Raises the rate limit on the Projects scan, which reads your public "
             "repositories. The scan works without one, just slower and sooner capped.",
        where="github.com/settings/tokens - no scopes needed for public repositories",
        min_length=20, check="github",
    ),
    Secret(
        name=FISH_KEY, label="Fish Audio key", group="fish",
        what="The interview's spoken voice. Without it the voice falls back to Kokoro "
             "or macOS `say`, which still work.",
        where="fish.audio, under API keys",
        min_length=20, check="fish",
    ),
)

BY_NAME = {secret.name: secret for secret in CATALOG}


def _held(name: str) -> str:
    return os.environ.get(name, "").strip()


def _tail(secret: Secret, value: str) -> str:
    """What the page may show. An address is not a secret and is shown
    whole; a key is four characters, which tells two keys apart."""
    if not value:
        return ""
    if not secret.password:
        return value
    return value[-SHOWN:] if len(value) > SHOWN else "·" * len(value)


def _row(secret: Secret) -> dict:
    value = _held(secret.name)
    return {
        "name": secret.name,
        "label": secret.label,
        "group": secret.group,
        "what": secret.what,
        "where": secret.where,
        "required": secret.required,
        "password": secret.password,
        "set": bool(value),
        "tail": _tail(secret, value),
        "checkable": bool(secret.check),
    }


def status() -> dict:
    rows = [_row(secret) for secret in CATALOG]
    return {
        "env_file": str(paths.ENV_FILE),
        "home": str(paths.HOME),
        "keys": rows,
        "missing": [row["label"] for row in rows if row["required"] and not row["set"]],
    }


@router.get("/keys")
def list_keys() -> dict:
    """What the app holds. Never a value: `set` and the last four characters."""
    return status()


def _secret_or_404(name: str) -> Secret:
    secret = BY_NAME.get(name)
    if secret is None:
        raise HTTPException(404, f"no such key: {name}")
    return secret


def _refuse_bad_shape(secret: Secret, value: str) -> None:
    if not value:
        raise HTTPException(400, f"{secret.label} is empty")
    if "\n" in value or "\r" in value:
        raise HTTPException(400, "that paste has a line break in it; copy the value alone")
    if len(value.replace(" ", "")) < secret.min_length:
        raise HTTPException(400, f"that is too short for {secret.label.lower()}")
    if secret.shape and not secret.shape.match(value):
        raise HTTPException(400, secret.shape_said or f"that is not a {secret.label.lower()}")


@router.post("/keys/{name}")
def save_key(name: str, body: KeyValue) -> dict:
    """Write one value to the data directory's `.env` and this process's
    environment. An App Password is pasted with spaces in it, which Gmail
    does not want back, so only that one keeps them stripped."""
    secret = _secret_or_404(name)
    value = body.value.strip()
    _refuse_bad_shape(secret, value)
    if secret.name == mailing.PASS:
        value = value.replace(" ", "")
    write_env(paths.ENV_FILE, secret.name, value)
    os.environ[secret.name] = value
    return status()


@router.delete("/keys/{name}")
def forget_key(name: str) -> dict:
    """Take a key out of the `.env` and this process. A required key can be
    replaced but not removed, since the app cannot run without it."""
    secret = _secret_or_404(name)
    if secret.required:
        raise HTTPException(409, f"{secret.label} is what every model call uses; "
                                 "replace it rather than removing it")
    write_env(paths.ENV_FILE, secret.name, "")
    os.environ.pop(secret.name, None)
    return status()


@router.post("/keys/{name}/check")
def check_key(name: str) -> dict:
    """Ask the service whether what we hold works. A request to somebody
    else's service, so it is the person's click and never automatic."""
    secret = _secret_or_404(name)
    if not secret.check:
        raise HTTPException(409, f"{secret.label} cannot be checked from here")
    group = [row for row in CATALOG if row.group == secret.group]
    values = {row.name: _held(row.name) for row in group}
    if not values.get(secret.name):
        raise HTTPException(409, f"{secret.label} is not set")
    said = CHECKS[secret.check](values)
    return {"ok": True, "said": said, **status()}
