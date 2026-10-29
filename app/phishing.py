"""Phishing response on the real tenant (Microsoft Graph): look at the reported mail the way an analyst would, find
every copy in the company's mailboxes, move them to Deleted Items (never hard-deleted; can be moved back), tell
the people who received it and the admins, and stop the sender at the desk. Without Graph the simulated
mail gateway in systems answers instead."""
from __future__ import annotations

import difflib
import re

from contextlib import contextmanager

from app.db import Outbound, Setting, session
from app.sla import get_setting

LURE = ("urgent", "immediately", "verify your", "password", "suspended", "invoice", "payment", "login", "sign in", "24 hours",
        "acil", "şifre", "hesabınız", "askıya", "doğrulayın", "fatura", "ödeme", "giriş yapın", "24 saat")


@contextmanager
def _db(db):
    """The caller's session when a tool runs inside one (SQLite allows one writer), else a new one."""
    if db is not None:
        yield db
    else:
        with session() as s:
            yield s


def _put(db, key: str, value: dict) -> None:
    with _db(db) as s:
        row = s.get(Setting, key)
        if row is None:
            s.add(Setting(key=key, value=value))
        else:
            row.value = value


def _get(db, key: str) -> dict:
    with _db(db) as s:
        row = s.get(Setting, key)
        return dict(row.value or {}) if row else {}


def _graph():
    from app import systems
    return systems._graph()


def _domain(addr: str) -> str:
    return addr.rsplit("@", 1)[-1].lower().strip()


def _host(url: str) -> str:
    return re.sub(r"^[a-z]+://([^/:?#]+).*", r"\1", url.strip().lower(), flags=re.I)


def _lookalike(domain: str, ours: list[str]) -> str:
    """Our domain this one imitates, or '': cloudriurn.com, cloudrium-billing.com, cloudriurn-pay.net and
    login.cloudrium.secure-site.com all lean on the brand "cloudrium" without being ours."""
    domain = domain.lower().strip(".")
    if not domain or any(domain == d or domain.endswith("." + d) for d in ours):
        return ""
    words = [w for w in re.split(r"[.\-_]", domain) if len(w) >= 4]
    for d in ours:
        brand = d.split(".")[0]
        if len(brand) < 4:
            continue
        if difflib.SequenceMatcher(None, domain, d).ratio() >= 0.8:
            return d
        if any(w == brand or difflib.SequenceMatcher(None, w, brand).ratio() >= 0.8 for w in words):
            return d
    return ""


def signals(info: dict, ours: list[str]) -> list[str]:
    """Analyst findings from sender authentication, reply-to, links and wording."""
    out = []
    sender = info.get("from", "")
    dom = _domain(sender)
    external = dom not in ours
    auth = info.get("auth") or {}
    failed = [k.upper() for k, v in auth.items() if v in ("fail", "softfail", "permerror")]
    if failed:
        out.append(f"sender authentication failed: {', '.join(failed)}")
    if (fake := _lookalike(dom, ours)):
        out.append(f"sender domain {dom} imitates {fake}")
    for r in info.get("reply_to") or []:
        if _domain(r) != dom:
            out.append(f"replies go elsewhere: {r}")
    for link in info.get("links") or []:
        host = _host(link["href"])
        shown = _host(link["shown"]) if re.match(r"^(https?://)?[\w.-]+\.[a-z]{2,}", link.get("shown") or "", re.I) else ""
        if shown and host and shown != host:
            out.append(f"link shows {shown} but opens {host}")
        if (fake := _lookalike(host, ours)):
            out.append(f"link {host} imitates {fake}")
    text = (info.get("subject", "") + " " + info.get("text", "")).lower()
    lures = sorted({w for w in LURE if w in text})
    if lures and external:
        out.append(f"external sender uses pressure / credential words: {', '.join(lures[:5])}")
    return list(dict.fromkeys(out))


def verdict(sig: list[str]) -> str:
    strong = [s for s in sig if s.startswith(("sender authentication failed", "sender domain", "link shows", "link "))]
    return "phishing" if strong and len(sig) >= 2 else ("suspicious" if sig else "safe")


def analyze(sender: str, subject: str, body: str, reporter: str = "") -> dict:
    from app import systems
    g = _graph()
    if not g:
        return systems.analyze_email(sender, subject, body)
    ours = g.tenant_domains()
    hits = g.find_mail(sender, subject)
    if not hits:
        info = {"from": sender.lower(), "subject": subject, "text": body,
                "links": [{"href": u, "shown": ""} for u in re.findall(r"https?://[^\s)>\]]+", body)]}
        sig = signals(info, ours)
        return {"ok": True, "verdict": verdict(sig), "signals": sig, "found_in_mailboxes": 0,
                "note": "the mail is not in any company mailbox (last 30 days); judged from what the employee sent"}
    mine = [h for h in hits if reporter and reporter.lower() in (h["mailbox"], h["phone"])]
    first = (mine or hits)[0]
    info = {**g.inspect_mail(first["mailbox"], first["id"]), "subject": first["subject"]}
    sig = signals(info, ours)
    return {"ok": True, "verdict": verdict(sig), "signals": sig, "sender": info["from"], "auth": info["auth"],
            "links": info["links"], "found_in_mailboxes": len({h["mailbox"] for h in hits}),
            "opened_by": sorted({h["name"] for h in hits if h["read"]})}


def _lang() -> str:
    from app.reports import config as report_config
    return report_config().get("language") or "en"


def sweep(ticket_id: int, sender: str, subject: str, reporter: str = "", db=None) -> dict:
    """Move every copy to Deleted Items, remember where they went (for restore), tell recipients and admins."""
    from app import systems, vulnwatch
    from app.i18n import t as tr
    g = _graph()
    if not g:
        return systems.quarantine(subject, sender)
    hits = g.find_mail(sender, subject)
    moved = []
    for h in hits:
        moved.append({**h, "new_id": g.move_mail(h["mailbox"], h["id"], "deleteditems")})
    sweeps = _get(db, "phish_sweeps")
    sweeps[str(ticket_id)] = [{"mailbox": m["mailbox"], "id": m["new_id"]} for m in moved]
    _put(db, "phish_sweeps", sweeps)
    lang = _lang()
    people = {m["mailbox"]: m for m in moved}
    opened = sorted({m["name"] for m in moved if m["read"]})
    with _db(db) as s:
        for box, m in people.items():
            if reporter and reporter.lower() in (box, m["phone"]):
                continue
            s.add(Outbound(to=m["phone"] or box, ticket_id=ticket_id, text=tr("phish_user", lang, subject=m["subject"], sender=sender)))
        admin_text = tr("phish_admin", lang, subject=subject or (moved[0]["subject"] if moved else ""), sender=sender,
                        n=len(people), opened=", ".join(opened) or "-", id=ticket_id)
        for to in vulnwatch._admins():
            s.add(Outbound(to=to, ticket_id=ticket_id, text=admin_text))
    return {"ok": True, "tenant_wide": True, "mailboxes_affected": len(people), "messages_moved": len(moved),
            "moved_to": "Deleted Items (recoverable)", "opened_by": opened,
            "recipients": sorted(m["name"] for m in people.values()), "admins_told": True}


def restore(ticket_id: int, db=None) -> dict:
    g = _graph()
    items = _get(db, "phish_sweeps").get(str(ticket_id)) or []
    if not g or not items:
        return {"ok": False, "error": "nothing was removed for this ticket"}
    back = sum(1 for i in items if g.move_mail(i["mailbox"], i["id"], "inbox"))
    sweeps = _get(db, "phish_sweeps")
    sweeps.pop(str(ticket_id), None)
    _put(db, "phish_sweeps", sweeps)
    return {"ok": True, "restored": back}


def block(sender: str, db=None) -> dict:
    """Stop the sender at the desk: its mail no longer reaches the desk's channels. (A tenant-wide Exchange block
    needs Exchange admin tools, not Graph; the result says so.)"""
    from app import systems
    blocked = sorted(set(_get(db, "blocked_senders").get("list", [])) | {sender.strip().lower()})
    _put(db, "blocked_senders", {"list": blocked})
    systems.block_sender(sender)
    return {"ok": True, "blocked": blocked, "scope": "desk channels" if _graph() else "mail gateway (simulated)"}


def is_blocked(addr: str) -> bool:
    addr = (addr or "").lower()
    return any(addr == b or (b.startswith("@") and addr.endswith(b)) or _domain(addr) == b
               for b in get_setting("blocked_senders", {}).get("list", []))
