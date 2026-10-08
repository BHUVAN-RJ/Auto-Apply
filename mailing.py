"""Shared SMTP delivery for Scout digests and human-approved outreach."""

from __future__ import annotations

import mimetypes
import os
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Iterable

HOST = "AUTOPILOT_SMTP_HOST"
PORT = "AUTOPILOT_SMTP_PORT"
USER = "AUTOPILOT_SMTP_USER"
PASS = "AUTOPILOT_SMTP_PASS"
TO = "AUTOPILOT_MAIL_TO"
FROM = "AUTOPILOT_MAIL_FROM"
MAX_ATTACHMENT = 10 * 1024 * 1024


class MailError(RuntimeError):
    pass


def config() -> dict:
    user = os.environ.get(USER, "").strip()
    return {
        "host": os.environ.get(HOST, "smtp.gmail.com").strip(),
        "port": int(os.environ.get(PORT, "587") or 587),
        "user": user,
        "password": os.environ.get(PASS, ""),
        "to": [a.strip() for a in os.environ.get(TO, user).split(",") if a.strip()],
        "sender": os.environ.get(FROM, user).strip() or user,
    }


def configured() -> bool:
    settings = config()
    return bool(settings["user"] and settings["password"])


def _addresses(to: str | Iterable[str]) -> list[str]:
    rows = [to] if isinstance(to, str) else list(to)
    clean = [str(row).strip() for row in rows if str(row).strip()]
    if not clean or any("\n" in row or "\r" in row or "@" not in row for row in clean):
        raise MailError("a valid recipient email is required")
    return clean


def message(to: str | Iterable[str], subject: str, text: str, html: str | None = None,
            attachments: Iterable[Path] = ()) -> EmailMessage:
    settings = config()
    if not configured():
        raise MailError("mail is not set up: AUTOPILOT_SMTP_USER, AUTOPILOT_SMTP_PASS in .env")
    if not subject.strip() or "\n" in subject or "\r" in subject:
        raise MailError("a one-line subject is required")
    msg = EmailMessage()
    msg["Subject"] = subject.strip()
    msg["From"] = settings["sender"]
    msg["To"] = ", ".join(_addresses(to))
    msg["Message-ID"] = make_msgid(domain=settings["sender"].split("@")[-1])
    msg.set_content(text.strip() + "\n")
    if html:
        msg.add_alternative(html, subtype="html")
    for item in attachments:
        path = Path(item)
        if not path.is_file():
            raise MailError(f"attachment is missing: {path.name}")
        if path.stat().st_size > MAX_ATTACHMENT:
            raise MailError(f"attachment is over 10 MB: {path.name}")
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        main, sub = content_type.split("/", 1)
        msg.add_attachment(path.read_bytes(), maintype=main, subtype=sub, filename=path.name)
    return msg


def send_message(msg: EmailMessage) -> str:
    settings = config()
    try:
        if settings["port"] == 465:
            with smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=30) as smtp:
                smtp.login(settings["user"], settings["password"])
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(settings["host"], settings["port"], timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.login(settings["user"], settings["password"])
                smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError(
            "SMTP login refused: for Gmail use an App Password, not the account password",
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"SMTP failed: {exc}") from exc
    return str(msg["Message-ID"])


def send(to: str | Iterable[str], subject: str, text: str, html: str | None = None,
         attachments: Iterable[Path] = ()) -> str:
    return send_message(message(to, subject, text, html, attachments))
