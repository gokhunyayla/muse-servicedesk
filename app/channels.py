"""Employee channels besides WhatsApp: the web chat (portal) and e-mail.

A requester address tells the channel apart: ``+90…`` is a WhatsApp number, ``name@…`` an e-mail address,
anything else an employee number from the integration API. Tickets remember where the employee last wrote
from (``Ticket.channel``), so replies and reminders go back the same way: WhatsApp sends, e-mail sends,
and the web chat picks its messages up by polling.
"""
from __future__ import annotations

import email
import email.utils
import imaplib
import logging
import re
import smtplib
import time
from email.message import EmailMessage

from app.db import Outbound, Ticket, session
from app.sla import get_setting

logger = logging.getLogger("channels")

DEFAULT_EMAIL = {"enabled": False, "imap_host": "", "imap_port": 993, "user": "", "folder": "INBOX", "poll_minutes": 2}
DEFAULT_PORTAL = {"enabled": True, "title": "IT Service Desk", "note": "", "country": "+90"}
_last_poll = 0.0


def kind(address: str) -> str:
    if "@" in address:
        return "email"
    if address.startswith("+"):
        return "whatsapp"
    return "api"


def reachable(address: str) -> bool:
    """Can the desk write to this address on its own (reminders, desk replies, supervisor nudges)?"""
    return kind(address) in ("email", "whatsapp")     # web chat users sign in with their phone number


def route(o: Outbound, db) -> str:
    """Where an outbound row goes: the employee's last channel for their own ticket, else by address."""
    if o.ticket_id:
        t = db.get(Ticket, o.ticket_id)
        if t is not None and t.requester == o.to and t.channel in ("web", "email", "whatsapp"):
            return t.channel
    return kind(o.to)


# ---- e-mail --------------------------------------------------------------------------------------

def email_config() -> dict:
    return {**DEFAULT_EMAIL, **get_setting("email", {})}


def portal_config() -> dict:
    return {**DEFAULT_PORTAL, **get_setting("portal", {})}


def send_email(to: str, subject: str, text: str) -> tuple[bool, str]:
    from app.reports import smtp_config
    c = smtp_config()
    if not c["host"]:
        return False, "SMTP not configured (Settings → Reports)"
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = c["from"] or c["user"], to, subject
    msg.set_content(text)
    try:
        with smtplib.SMTP(c["host"], int(c["port"]), timeout=25) as s:
            if c.get("tls", True):
                s.starttls()
            if c["user"]:
                s.login(c["user"], c["password"])
            s.send_message(msg)
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:255]


def _subject(ticket: Ticket) -> str:
    return f"[#{ticket.id}] {ticket.title[:70]}"


_AUTO = re.compile(r"auto-?reply|automatic reply|out of office|mailer-daemon|noreply|no-reply", re.I)


def parse_email(raw: bytes) -> dict | None:
    """Sender, text and the first image of an incoming mail; None for auto-replies and bounces."""
    msg = email.message_from_bytes(raw)
    name, addr = email.utils.parseaddr(msg.get("From", ""))
    addr = addr.lower().strip()
    if not addr or _AUTO.search(addr) or _AUTO.search(msg.get("Subject", "")) or msg.get("Auto-Submitted", "no") != "no":
        return None
    subject = str(email.header.make_header(email.header.decode_header(msg.get("Subject", "")))).strip()
    text, image, mime = "", None, "image/jpeg"
    for part in msg.walk():
        ctype = part.get_content_type()
        disp = str(part.get("Content-Disposition", ""))
        if ctype == "text/plain" and "attachment" not in disp and not text:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        elif ctype.startswith("image/") and image is None:
            image, mime = part.get_payload(decode=True), ctype
    if not text and msg.get_content_type() == "text/html":
        text = re.sub(r"<[^>]+>", " ", (msg.get_payload(decode=True) or b"").decode(msg.get_content_charset() or "utf-8", "replace"))
    # drop quoted history and signatures so the agent sees the new request only
    lines = []
    for line in text.splitlines():
        if line.startswith(">") or re.match(r"^(On .+ wrote:|-----Original Message-----|From: .+)$", line.strip()):
            break
        lines.append(line)
    body = "\n".join(lines).strip()
    m = re.search(r"\[#(\d+)\]", subject)
    return {"from": addr, "name": name, "subject": subject, "text": body, "image": image, "mime": mime,
            "ticket_id": int(m.group(1)) if m else None, "message_id": msg.get("Message-ID", "")}


def ingest_email(parsed: dict) -> tuple[Ticket, str] | None:
    """Run one parsed mail through the desk and answer by mail."""
    from app.agents import handle_message
    text = parsed["text"] or parsed["subject"]
    if parsed["subject"] and parsed["ticket_id"] is None and parsed["subject"].lower() not in text.lower():
        text = f"{parsed['subject']}\n{text}".strip()
    with session() as db:
        ticket, answer = handle_message(db, parsed["from"], text, parsed["image"], parsed["mime"], channel="email")
        subject, to = _subject(ticket), parsed["from"]
    ok, err = send_email(to, subject, answer)
    if not ok:
        logger.warning("e-mail reply to %s failed: %s", to, err)
    return ticket, answer


def poll_email(force: bool = False) -> int:
    """Fetch unseen mail from the configured mailbox and feed each one to the desk."""
    global _last_poll
    c = email_config()
    if not c["enabled"] or not c["imap_host"]:
        return 0
    if not force and time.time() - _last_poll < int(c["poll_minutes"]) * 60:
        return 0
    _last_poll = time.time()
    from app import vault
    handled = 0
    try:
        box = imaplib.IMAP4_SSL(c["imap_host"], int(c["imap_port"]), timeout=30)
        box.login(c["user"], vault.get("IMAP_PASSWORD") or "")
        box.select(c["folder"] or "INBOX")
        _, data = box.search(None, "UNSEEN")
        for num in (data[0].split() if data and data[0] else []):
            _, msg_data = box.fetch(num, "(RFC822)")
            raw = msg_data[0][1] if msg_data and isinstance(msg_data[0], tuple) else b""
            box.store(num, "+FLAGS", "\\Seen")
            parsed = parse_email(raw) if raw else None
            if parsed is None or parsed["from"] == (c["user"] or "").lower():
                continue
            try:
                ingest_email(parsed)
                handled += 1
            except Exception:  # noqa: BLE001
                logger.exception("e-mail from %s failed", parsed["from"])
        box.logout()
    except Exception as e:  # noqa: BLE001
        logger.warning("e-mail poll failed: %s", e)
    return handled


def test_email() -> tuple[bool, str]:
    c = email_config()
    if not c["imap_host"]:
        return False, "IMAP host missing"
    from app import vault
    try:
        box = imaplib.IMAP4_SSL(c["imap_host"], int(c["imap_port"]), timeout=20)
        box.login(c["user"], vault.get("IMAP_PASSWORD") or "")
        status, data = box.select(c["folder"] or "INBOX", readonly=True)
        box.logout()
        return True, f"connected; {data[0].decode() if data and data[0] else '?'} messages in {c['folder'] or 'INBOX'}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:200]


# ---- outbound ------------------------------------------------------------------------------------

def drain() -> int:
    """Deliver queued messages on each requester's channel. Web rows wait for the portal to poll them;
    permanent WhatsApp failures are marked once and never retried."""
    from app.whatsapp import SendError, send
    done = 0
    with session() as db:
        for o in db.query(Outbound).filter(Outbound.sent.is_(False), Outbound.error == "").limit(50):
            where = route(o, db)
            if where == "web":
                continue
            if where == "email":
                t = db.get(Ticket, o.ticket_id) if o.ticket_id else None
                ok, err = send_email(o.to, _subject(t) if t else "IT Service Desk", o.text)
                if ok:
                    o.sent, done = True, done + 1
                else:
                    o.error = err[:255]
                    logger.warning("outbound #%s to %s dropped (%s)", o.id, o.to, o.error)
                continue
            if where != "whatsapp":
                o.error = "no channel for this address"
                continue
            try:
                if send(o.to, o.text):
                    o.sent, done = True, done + 1
            except SendError as e:
                if e.permanent:
                    o.error = f"{e.code}: {e}"[:255]
                    logger.warning("outbound #%s to %s dropped (%s)", o.id, o.to, o.error)
                else:
                    logger.error("outbound #%s to %s failed, will retry: %s", o.id, o.to, e)
    return done
