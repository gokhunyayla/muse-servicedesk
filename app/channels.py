"""Employee channels besides WhatsApp: the web chat (portal) and e-mail.

A requester address tells the channel apart: ``+<country>…`` is a WhatsApp number, ``name@…`` an e-mail address,
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

DEFAULT_EMAIL = {"enabled": False, "provider": "imap",   # "imap" = own mailbox (IMAP/SMTP); "graph" = Microsoft 365 mailbox
                 "outgoing": "",     # how the desk sends: "" = same as provider, "smtp" = SMTP above even when intake is Graph
                 "imap_host": "", "imap_port": 993, "user": "", "folder": "INBOX", "poll_minutes": 2,
                 "allowed": "",      # optional: comma-separated sender domains/addresses; empty = anyone
                 "last_uid": 0}      # mailbox position: only mail that arrives after the channel is enabled is read
DEFAULT_PORTAL = {"enabled": True, "title": "IT Service Desk", "note": "", "country": ""}   # e.g. +44: added to local numbers; empty = numbers must include the country code
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
    """Outgoing mail: the Microsoft 365 mailbox (Graph) when the e-mail channel uses it, otherwise SMTP. A Graph
    failure is logged and, when SMTP is configured, the message still goes out over SMTP — a sign-in code or an
    invitation must not be lost to a tenant restriction; the error stays visible in the log and the audit trail."""
    from app.reports import smtp_config
    c = smtp_config()
    graph_err = ""
    ec = email_config()
    if ec.get("provider") == "graph" and ec.get("outgoing") != "smtp":
        from app import graph
        try:
            graph.send_mail(to, subject, text)
            return True, ""
        except Exception as e:  # noqa: BLE001
            graph_err = f"Microsoft 365: {str(e)[:220]}"
            logger.warning("Graph sendMail to %s failed: %s%s", to, graph_err, " — falling back to SMTP" if c["host"] else "")
            if not c["host"]:
                return False, graph_err
    if not c["host"]:
        return False, "SMTP not configured (Settings → E-mail)"
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
        return False, (f"{graph_err}; SMTP: " if graph_err else "") + str(e)[:255]


def _subject(ticket: Ticket, incoming: str = "") -> str:
    """Reply subject: the employee's own subject when we have it (so their mail client threads it), tagged with the ticket."""
    base = re.sub(r"\s+", " ", incoming or ticket.title).strip()
    base = re.sub(r"\[#\d+\]\s*", "", base)
    if not re.match(r"(?i)^re:", base):
        base = "Re: " + base
    return f"[#{ticket.id}] {base[:70]}"


_AUTO = re.compile(r"auto-?reply|automatic reply|out of office|mailer-daemon|noreply|no-reply", re.I)


def parse_email(raw: bytes) -> dict | None:
    """Sender, text and the first image of an incoming mail; None for auto-replies and bounces."""
    msg = email.message_from_bytes(raw)
    name, addr = email.utils.parseaddr(msg.get("From", ""))
    addr = addr.lower().strip()
    if not addr or _AUTO.search(addr) or _AUTO.search(msg.get("Subject", "")) or msg.get("Auto-Submitted", "no") != "no":
        return None
    subject = re.sub(r"\s+", " ", str(email.header.make_header(email.header.decode_header(msg.get("Subject", ""))))).strip()[:200]
    from app.attachments import is_document
    text, image, mime, document = "", None, "image/jpeg", None
    for part in msg.walk():
        ctype = part.get_content_type()
        disp = str(part.get("Content-Disposition", ""))
        fname = part.get_filename() or ""
        if ctype == "text/plain" and "attachment" not in disp and not text:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        elif ctype.startswith("image/") and image is None:
            image, mime = part.get_payload(decode=True), ctype
        elif document is None and fname and is_document(fname, ctype):
            document = (fname, ctype, part.get_payload(decode=True) or b"")
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
    return {"from": addr, "name": name, "subject": subject, "text": body, "image": image, "mime": mime, "document": document,
            "ticket_id": int(m.group(1)) if m else None, "message_id": msg.get("Message-ID", "")}


def sender_allowed(addr: str, cfg: dict | None = None) -> bool:
    from app import phishing
    if phishing.is_blocked(addr):
        return False
    allowed = [a.strip().lower().lstrip("@") for a in str((cfg or email_config()).get("allowed", "")).split(",") if a.strip()]
    if not allowed:
        return True
    addr = addr.lower()
    return any(addr == a or addr.endswith("@" + a) or addr.endswith("." + a) for a in allowed)


def ingest_email(parsed: dict) -> tuple[Ticket | None, str] | None:
    """Run one parsed mail through the desk and answer by mail."""
    from app.agents import handle_message
    text = parsed["text"] or parsed["subject"]
    if parsed["subject"] and parsed["ticket_id"] is None and parsed["subject"].lower() not in text.lower():
        text = f"{parsed['subject']}\n{text}".strip()
    if parsed.get("document"):
        from app.attachments import with_document
        text = with_document(text, *parsed["document"])
    with session() as db:
        # a reply to one of our mails carries [#id] and continues that ticket; any other mail is a request of its own
        tagged = db.get(Ticket, parsed["ticket_id"]) if parsed["ticket_id"] else None
        continues = tagged is not None and tagged.requester == parsed["from"] and tagged.status != "resolved"
        ticket, answer = handle_message(db, parsed["from"], text, parsed["image"], parsed["mime"], channel="email",
                                        force_new=not continues, ticket=tagged if continues else None)
        subject, to = (_subject(ticket, parsed["subject"]) if ticket else "Re: " + (parsed["subject"] or "IT Service Desk")), parsed["from"]
    ok, err = send_email(to, subject, answer)
    if not ok:
        logger.warning("e-mail reply to %s failed: %s", to, err)
    return ticket, answer


def _box(c: dict):
    from app import vault
    box = imaplib.IMAP4_SSL(c["imap_host"], int(c["imap_port"]), timeout=30)
    box.login(c["user"], vault.get("IMAP_PASSWORD") or "")
    return box


def _max_uid(box) -> int:
    _, d = box.uid("search", None, "ALL")
    ids = d[0].split() if d and d[0] else []
    return int(ids[-1]) if ids else 0


def mark_baseline() -> int:
    """Remember the newest mail now in the box, so enabling the channel never replays old mail."""
    from app.sla import put_setting
    c = email_config()
    if c.get("provider") == "graph":
        from app import graph
        put_setting("graph", {**get_setting("graph", {}), "last_received": graph.now_iso()})
        return 1
    if not c["imap_host"]:
        return 0
    try:
        box = _box(c)
        box.select(c["folder"] or "INBOX", readonly=True)
        uid = _max_uid(box)
        box.logout()
    except Exception as e:  # noqa: BLE001   (unreachable now: the first poll sets the baseline instead)
        logger.warning("e-mail baseline failed: %s", e)
        return 0
    put_setting("email", {**get_setting("email", {}), "last_uid": uid})
    return uid


def poll_email(force: bool = False) -> int:
    """Read mail that arrived after the channel was enabled (by IMAP UID) and feed each one to the desk."""
    global _last_poll
    from app.sla import put_setting
    c = email_config()
    if not c["enabled"] or (c.get("provider") != "graph" and not c["imap_host"]):
        return 0
    if not force and time.time() - _last_poll < int(c["poll_minutes"]) * 60:
        return 0
    _last_poll = time.time()
    if c.get("provider") == "graph":
        return _poll_graph(c)
    handled = 0
    try:
        box = _box(c)
        box.select(c["folder"] or "INBOX")
        last = int(c.get("last_uid") or 0)
        if not last:                                   # never enabled properly: start from now, read nothing old
            last = _max_uid(box)
            put_setting("email", {**get_setting("email", {}), "last_uid": last})
        _, data = box.uid("search", None, f"UID {last + 1}:*")
        uids = [int(u) for u in (data[0].split() if data and data[0] else []) if int(u) > last]
        for uid in uids[:20]:                          # a burst never floods the desk; the rest comes next tick
            _, msg_data = box.uid("fetch", str(uid), "(RFC822)")
            raw = msg_data[0][1] if msg_data and isinstance(msg_data[0], tuple) else b""
            last = uid
            put_setting("email", {**get_setting("email", {}), "last_uid": last})
            parsed = parse_email(raw) if raw else None
            if parsed is None or parsed["from"] == (c["user"] or "").lower() or not sender_allowed(parsed["from"], c):
                continue
            if not email_config()["enabled"]:          # switched off meanwhile: stop at once
                break
            try:
                ingest_email(parsed)
                handled += 1
            except Exception:  # noqa: BLE001
                logger.exception("e-mail from %s failed", parsed["from"])
        box.logout()
    except Exception as e:  # noqa: BLE001
        logger.warning("e-mail poll failed: %s", e)
    return handled


def _poll_graph(c: dict) -> int:
    """Same contract as the IMAP path, through the Microsoft 365 mailbox: only mail received after the last position."""
    from app import graph
    from app.sla import put_setting
    gc = graph.config()
    since = gc.get("last_received") or ""
    if not since:                                        # first run: start from now, never the backlog
        put_setting("graph", {**get_setting("graph", {}), "last_received": graph.now_iso()})
        return 0
    handled = 0
    from datetime import datetime, timedelta
    seen = list(get_setting("email_seen", {}).get("ids", []))
    try:
        for m in graph.new_messages(since):
            # Graph shows receivedDateTime to the second but compares with milliseconds: "gt <that second>" returns
            # the same message again. Move one second past it, and remember the message id as a second lock.
            nxt = (datetime.fromisoformat(m["received"].replace("Z", "+00:00")) + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ") if m.get("received") else since
            put_setting("graph", {**get_setting("graph", {}), "last_received": max(nxt, since)})
            mid = m.get("message_id") or m.get("received", "")
            if mid in seen:
                continue
            seen = (seen + [mid])[-300:]
            put_setting("email_seen", {"ids": seen})
            parsed = _parse_graph(m)
            if parsed is None or parsed["from"] == gc["mailbox"].lower() or not sender_allowed(parsed["from"], c):
                continue
            if not email_config()["enabled"]:
                break
            try:
                ingest_email(parsed)
                handled += 1
            except Exception:  # noqa: BLE001
                logger.exception("e-mail from %s failed", parsed["from"])
    except Exception as e:  # noqa: BLE001
        logger.warning("Microsoft 365 poll failed: %s", e)
    return handled


def _parse_graph(m: dict) -> dict | None:
    """A Graph message in the shape parse_email() produces (quotes and signatures dropped, [#id] read)."""
    addr = m["from"]
    if not addr or _AUTO.search(addr) or _AUTO.search(m["subject"]):
        return None
    lines = []
    for line in (m["raw_text"] or "").splitlines():
        if line.strip().startswith(">") or re.match(r"^(On .+ wrote:|-----Original Message-----|From: .+)$", line.strip()):
            break
        lines.append(line)
    body = "\n".join(l.rstrip() for l in lines).strip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    t = re.search(r"\[#(\d+)\]", m["subject"])
    return {"from": addr, "name": m.get("name", ""), "subject": m["subject"], "text": body, "image": m.get("image"),
            "mime": m.get("mime", "image/jpeg"), "document": m.get("document"),
            "ticket_id": int(t.group(1)) if t else None, "message_id": m.get("message_id", "")}


def test_email() -> tuple[bool, str]:
    c = email_config()
    if c.get("provider") == "graph":
        from app import graph
        try:
            r = graph.test()
            return r.get("mailbox") == "ok", f"{r.get('tenant')}: mailbox {r.get('mailbox', 'not set')}"
        except Exception as e:  # noqa: BLE001
            return False, str(e)[:200]
    if not c["imap_host"]:
        return False, "IMAP host missing"
    try:
        box = _box(c)
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
