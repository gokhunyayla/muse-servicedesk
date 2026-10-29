"""Blue screens, noticed by the desk before anyone writes in: the endpoint agent reports the crashes Windows
recorded (stop code, likely cause) with each heartbeat; a new one becomes a proactive ticket for the device's
owner, and the agents take it from there (crash-report → system-repair or the hardware path)."""
from __future__ import annotations

import logging
import threading

from app.db import Ticket, session
from app.sla import get_setting, put_setting

logger = logging.getLogger(__name__)
SEEN = "crash_seen"
_lock = threading.Lock()


def new_crashes(hostname: str, crashes: list) -> list[dict]:
    crashes = [c for c in crashes or [] if isinstance(c, dict) and c.get("id")]
    with _lock:
        seen = get_setting(SEEN, {})
        if hostname not in seen:                     # first report from this device: history, not news
            seen[hostname] = [c["id"] for c in crashes]
            put_setting(SEEN, seen)
            return []
        if not crashes:
            return []
        have = set(seen[hostname])
        fresh = [c for c in crashes if c["id"] not in have]
        if fresh:
            seen[hostname] = (list(have) + [c["id"] for c in fresh])[-100:]
            put_setting(SEEN, seen)
    return fresh


def on_heartbeat(hostname: str, metrics: dict) -> None:
    try:
        fresh = new_crashes(hostname, (metrics or {}).get("crashes") or [])
    except Exception as e:  # noqa: BLE001 — a heartbeat must never fail because of the watch
        logger.warning("crash watch: %s", e)
        return
    if fresh:
        threading.Thread(target=handle, args=(hostname, fresh), daemon=True, name="crashwatch").start()


def finding(hostname: str, crashes: list[dict]) -> dict | None:
    from app import systems
    owner = systems.device_owner(hostname)
    if not owner:
        return None
    emp, u = owner
    contact = u.get("phone") or systems.user_email(emp)
    if not contact:
        return None
    c = crashes[-1]
    what = f"{c.get('code', '')} {c.get('name', '')}".strip() or "unknown stop code"
    return {"hostname": hostname, "employee_no": emp, "phone": contact, "kind": "crash",
            "text": f"[Proactive] {hostname} crashed with a blue screen at {c.get('at', '?')} ({what}; likely cause: "
                    f"{c.get('hint', 'unknown')}) and restarted. Run crash-report, then follow the blue screen SOP."}


def handle(hostname: str, crashes: list[dict]) -> int | None:
    """One open crash ticket per device: a further crash is added to it instead of opening another."""
    from app import proactive
    from app.db import log
    f = finding(hostname, crashes)
    if f is None:
        return None
    with session() as db:
        open_one = (db.query(Ticket).filter(Ticket.title == f"[Proactive:crash] {hostname}", Ticket.status != "resolved")
                    .order_by(Ticket.id.desc()).first())
        if open_one is not None:
            log(db, open_one.id, "system", "note", f["text"])
            return open_one.id
    return proactive.open_ticket(f)
