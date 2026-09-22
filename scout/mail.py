"""The digest mail, over plain SMTP with a personal account.

Gmail: 2-step verification on, an App Password from
myaccount.google.com/apppasswords, and four lines in `.env`:
`AUTOPILOT_SMTP_USER`, `AUTOPILOT_SMTP_PASS`, `AUTOPILOT_MAIL_TO` (the
user itself when unset) and, for another provider, `AUTOPILOT_SMTP_HOST`
/ `AUTOPILOT_SMTP_PORT` (587 STARTTLS, 465 SSL). Nothing else; the
standard library sends it. Not configured = no mail, the page says so,
the hits still land on the page.
"""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from html import escape

from scout import Hit, Watch

HOST = "AUTOPILOT_SMTP_HOST"
PORT = "AUTOPILOT_SMTP_PORT"
USER = "AUTOPILOT_SMTP_USER"
PASS = "AUTOPILOT_SMTP_PASS"
TO = "AUTOPILOT_MAIL_TO"
FROM = "AUTOPILOT_MAIL_FROM"


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
    c = config()
    return bool(c["user"] and c["password"] and c["to"])


def status() -> dict:
    c = config()
    return {"configured": configured(), "to": c["to"], "host": c["host"]}


def send(subject: str, text: str, html: str | None = None) -> None:
    c = config()
    if not configured():
        raise MailError("mail is not set up: AUTOPILOT_SMTP_USER, AUTOPILOT_SMTP_PASS in .env")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = c["sender"]
    msg["To"] = ", ".join(c["to"])
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    try:
        if c["port"] == 465:
            with smtplib.SMTP_SSL(c["host"], c["port"], timeout=30) as smtp:
                smtp.login(c["user"], c["password"])
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(c["host"], c["port"], timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.login(c["user"], c["password"])
                smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError("SMTP login refused: for Gmail use an App Password, not the account password") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"SMTP failed: {exc}") from exc


def referral_note(watch: Watch, hit: Hit) -> str:
    """The message to forward to the person who can refer. A template, not
    a model: it goes to a friend, and it should read like the person."""
    p = hit.posting
    who = watch.referrer.split(",")[0].split("(")[0].strip() or "there"
    places = [x.strip() for x in (p.get("location") or "").split(";") if x.strip()]
    where = f" ({'; '.join(places[:2])}{'; …' if len(places) > 2 else ''})" if places else ""
    return (f"Hi {who}, {hit.company} just posted {p['title']}{where}: {p['url']}\n"
            f"It is at my level and I would love to apply with a referral. Could you put me forward?")


def digest(rows: list[tuple[Watch, Hit]]) -> tuple[str, str, str]:
    """(subject, text, html) for one round's new hits."""
    companies = sorted({h.company for _, h in rows})
    subject = f"Scout: {len(rows)} new at {', '.join(companies[:3])}{'…' if len(companies) > 3 else ''}"
    text_lines, html_rows = [], []
    for watch, hit in rows:
        p = hit.posting
        verdict = hit.verdict or "not screened"
        line = f"{hit.company}: {p['title']}"
        if p.get("location"):
            line += f" — {p['location']}"
        if p.get("posted"):
            line += f" (posted {p['posted']})"
        text_lines += [line, f"  {p['url']}", f"  screen: {verdict}" + (f" — {hit.summary}" if hit.summary else "")]
        if watch.referrer:
            text_lines += [f"  ask: {watch.referrer}", "  " + referral_note(watch, hit).replace("\n", "\n  ")]
        text_lines.append("")
        colour = {"ok": "#2a7", "caution": "#c90", "reject": "#c33"}.get(verdict, "#888")
        html_rows.append(
            f"<p style='margin:0 0 14px'><b>{escape(hit.company)}</b>: "
            f"<a href='{escape(p['url'])}'>{escape(p['title'])}</a>"
            + (f" — {escape(p['location'])}" if p.get("location") else "")
            + (f" <span style='color:#888'>(posted {escape(p['posted'])})</span>" if p.get("posted") else "")
            + f"<br><span style='color:{colour}'>screen: {escape(verdict)}</span>"
            + (f" — {escape(hit.summary)}" if hit.summary else "")
            + (f"<br>ask: {escape(watch.referrer)}<br><i style='color:#555'>{escape(referral_note(watch, hit)).replace(chr(10), '<br>')}</i>" if watch.referrer else "")
            + "</p>")
    html = "<div style='font-family:sans-serif;font-size:14px'>" + "".join(html_rows) + "</div>"
    return subject, "\n".join(text_lines).rstrip() + "\n", html


def broken_notice(watch: Watch) -> tuple[str, str]:
    """(subject, text) for a page that stopped answering. Sent once per
    breakage, not every round."""
    subject = f"Scout: {watch.company} DOES NOT WORK"
    text = (f"THIS COMPANY DOES NOT WORK: {watch.company}\n\n"
            f"The careers page could not be read:\n  {watch.url}\n\nReason: {watch.error}\n\n"
            f"Nothing from {watch.company} is being watched until it reads again. Open the Scout tab, "
            f"press Verify; if the site changed, paste the page's new URL.\n")
    return subject, text
