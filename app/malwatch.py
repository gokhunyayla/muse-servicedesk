"""Malware watch: Microsoft Defender on every enrolled device reports its detections with each heartbeat. A detection
the desk has not seen yet opens an L3 ticket; the agent removes the threat, refreshes signatures and runs a quick
scan (``malware-clean``); the admins hear what was found and whether the device is clean again.

Unlike the vulnerability watch this is not daily: a detection is acted on within a heartbeat (about a minute)."""
from __future__ import annotations

import logging
import threading

from app.db import Outbound, Ticket, log, resolve, session
from app.sla import get_setting, put_setting

logger = logging.getLogger(__name__)
SEEN = "malware_seen"
_lock = threading.Lock()


def _key(t: dict) -> str:
    return f"{t.get('id')}|{t.get('detected')}"


def new_threats(hostname: str, threats: list) -> list[dict]:
    """Detections not handled before; remembered per device so each one is acted on once."""
    threats = [t for t in threats or [] if isinstance(t, dict) and t.get("id")]
    if not threats:
        return []
    with _lock:
        seen = get_setting(SEEN, {})
        have = set(seen.get(hostname, []))
        fresh = [t for t in threats if _key(t) not in have]
        if fresh:
            seen[hostname] = (list(have) + [_key(t) for t in fresh])[-200:]
            put_setting(SEEN, seen)
    return fresh


def on_heartbeat(hostname: str, metrics: dict) -> None:
    try:
        fresh = new_threats(hostname, (metrics or {}).get("threats") or [])
    except Exception as e:  # noqa: BLE001 — a heartbeat must never fail because of the watch
        logger.warning("malware watch: %s", e)
        return
    if fresh:
        threading.Thread(target=handle, args=(hostname, fresh), daemon=True, name="malwatch").start()


def _lang() -> str:
    from app.reports import config as report_config
    return report_config().get("language") or "en"


def handle(hostname: str, threats: list[dict]) -> dict:
    """Ticket, clean through the agent, tell the admins. Returns what happened."""
    from app import endpoints, systems, vulnwatch
    from app.i18n import t as tr
    names = ", ".join(dict.fromkeys(t.get("name") or t["id"] for t in threats))
    files = ", ".join(dict.fromkeys(r for t in threats for r in t.get("resources") or []))[:300]
    defender = ", ".join(dict.fromkeys(t.get("status") or "" for t in threats if t.get("status")))
    dev = endpoints.get(hostname)
    owner = dev.owner if dev is not None else ""
    admins = vulnwatch._admins()
    with session() as db:
        requester = ((systems.get_user(owner) or {}).get("phone") if owner else "") or (admins or ["desk"])[0]
        tk = Ticket(requester=requester, title=f"[Malware] {names} on {hostname}"[:200], owner="L3", status="open",
                    channel="whatsapp" if requester.startswith("+") else "email")
        db.add(tk)
        db.flush()
        log(db, tk.id, "system", "note", f"Microsoft Defender on {hostname} detected {names} ({files or 'no file path'}); "
                                         f"Defender: {defender or 'no action yet'}.", data={"threats": threats})
        tid = tk.id
    r = endpoints.run(hostname, "malware-clean", tid, "malwatch", wait=900) if vulnwatch.config()["auto_remediate"] else None
    out = (r or {}).get("output") or ""
    clean = bool(r and r.get("ok") and "Result: clean" in out)
    lang = _lang()
    with session() as db:
        tk = db.get(Ticket, tid)
        if clean:
            text = tr("malware_fixed", lang, names=names, host=hostname, files=files or "-", defender=defender or "-", id=tid)
            tk.resolution = f"{names} removed from {hostname}; quick scan clean."
            resolve(tk)
        else:
            why = (r or {}).get("error") or out or tr("auto_off", lang)
            text = tr("malware_open", lang, names=names, host=hostname, files=files or "-", why=why[:300], id=tid)
        log(db, tid, "system", "note", text)
        for to in admins:
            db.add(Outbound(to=to, ticket_id=tid, text=text))
    return {"ticket": tid, "status": "cleaned" if clean else "needs attention"}
