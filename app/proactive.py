"""Proactive maintenance: periodic checks over the inventory that open tickets before employees
notice. Each finding is handed to L1 as a normal message from the affected employee's number, so
the same SOPs, tools and confirmation flow apply."""
from __future__ import annotations

from datetime import date, timedelta

from app import systems
from app.agents import handle_message
from app.db import Ticket, session


def findings() -> list[dict]:
    """With a real directory (Entra) only real machines count: the simulated inventory belongs to the simulated
    directory and its owners are not real people."""
    from app import endpoints
    real_directory = systems._graph() is not None
    devices = endpoints.all_states() if real_directory else systems.all_devices()
    out = []
    for host, d in devices.items():
        owner = systems.device_owner(host)
        if not owner:
            continue
        emp, u = owner
        contact = u.get("phone") or systems.user_email(emp)
        if not contact:
            continue
        def add(kind, text):
            out.append({"hostname": host, "employee_no": emp, "phone": contact, "kind": kind, "text": text})
        if d.get("disk_used_pct", 0) >= 90:
            add("disk", f"[Proactive] {host}: disk {d['disk_used_pct']}% full; will start failing updates.")
        if d.get("os_patch_days", 0) >= 45:
            add("patch", f"[Proactive] {host}: no OS updates for {d['os_patch_days']} days.")
        if d.get("real"):                                    # alerts only a live endpoint agent can raise
            m = d.get("metrics") or {}
            if str(m.get("spooler", "")).lower() == "stopped":
                add("service", f"[Proactive] {host}: the print spooler service has stopped.")
            if int(m.get("critical_events_24h", 0) or 0) >= 5:
                add("events", f"[Proactive] {host}: {m['critical_events_24h']} critical/error system events in 24 hours.")
    soon = (date.today() + timedelta(days=30)).isoformat()
    for host, w in ({} if real_directory else systems.WARRANTY).items():
        owner = systems.device_owner(host)
        if owner and date.today().isoformat() <= w["valid_until"] <= soon:
            emp, u = owner
            out.append({"hostname": host, "employee_no": emp, "phone": u.get("phone") or systems.user_email(emp), "kind": "warranty",
                        "text": f"[Proactive] {host}: {w['vendor']} warranty ends {w['valid_until']}."})
    return out


COOLDOWN_DAYS = 7


def run_proactive_checks() -> list[int]:
    """Open one ticket per finding — unless one is already open for that host and kind, or one was opened
    (even if since closed) within the last COOLDOWN_DAYS: a closed finding is not nagged again every run."""
    from datetime import timedelta
    from app.db import _now
    opened = []
    for f in findings():
        with session() as db:
            dup = db.query(Ticket).filter(Ticket.title.contains(f["hostname"]), Ticket.title.contains(f["kind"]),
                                          (Ticket.status != "resolved") | (Ticket.created_at >= _now() - timedelta(days=COOLDOWN_DAYS))).count()
            if dup:
                continue
            title = f"[Proactive:{f['kind']}] {f['hostname']}"
            ticket, _ = handle_message(db, f["phone"], f"{f['text']} Owner: {f['employee_no']}. "
                                                       f"Fix what you can remotely, then tell the employee.", force_new=True)
            ticket.title = title
            opened.append(ticket.id)
    return opened
