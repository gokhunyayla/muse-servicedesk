"""Proactive maintenance: periodic checks over the inventory that open tickets before employees
notice. Each finding is handed to L1 as a normal message from the affected employee's number, so
the same SOPs, tools and confirmation flow apply."""
from __future__ import annotations

from datetime import date, timedelta

from app import systems
from app.agents import handle_message
from app.db import Ticket, session


def findings() -> list[dict]:
    out = []
    for host, d in systems.DEVICES.items():
        owner = systems.device_owner(host)
        if not owner:
            continue
        emp, u = owner
        if d.get("disk_used_pct", 0) >= 90:
            out.append({"hostname": host, "employee_no": emp, "phone": u["phone"], "kind": "disk",
                        "text": f"[Proactive] {host}: disk {d['disk_used_pct']}% full; will start failing updates."})
        if d.get("os_patch_days", 0) >= 45:
            out.append({"hostname": host, "employee_no": emp, "phone": u["phone"], "kind": "patch",
                        "text": f"[Proactive] {host}: no OS updates for {d['os_patch_days']} days."})
    soon = (date.today() + timedelta(days=30)).isoformat()
    for host, w in systems.WARRANTY.items():
        owner = systems.device_owner(host)
        if owner and date.today().isoformat() <= w["valid_until"] <= soon:
            emp, u = owner
            out.append({"hostname": host, "employee_no": emp, "phone": u["phone"], "kind": "warranty",
                        "text": f"[Proactive] {host}: {w['vendor']} warranty ends {w['valid_until']}."})
    return out


def run_proactive_checks() -> list[int]:
    """Open one ticket per finding, unless an open ticket already mentions that host and kind."""
    opened = []
    for f in findings():
        with session() as db:
            dup = db.query(Ticket).filter(Ticket.status != "resolved",
                                          Ticket.title.contains(f["hostname"]),
                                          Ticket.title.contains(f["kind"])).count()
            if dup:
                continue
            title = f"[Proactive:{f['kind']}] {f['hostname']}"
            ticket, _ = handle_message(db, f["phone"], f"{f['text']} Owner: {f['employee_no']}. "
                                                       f"Fix what you can remotely, then tell the employee.", force_new=True)
            ticket.title = title
            opened.append(ticket.id)
    return opened
