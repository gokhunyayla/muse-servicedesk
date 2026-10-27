"""Timing and SLA: how long each tier held a ticket, how long the desk waited for the employee, time to
first response and total elapsed time, all derived from the audit trail; SLA targets come from settings."""
from __future__ import annotations

import re

from app.db import Setting, _now, session

DEFAULT_SLA = {             # minutes; editable in Settings → SLA
    "first_response": 5,
    "L1": 30, "L2": 120, "L3": 480, "L4": 1440,
    "human": 480,
    "total": 480,
    "at_risk_pct": 80,          # warn the Supervisor when a ticket reaches this % of a target
}
_HANDOFF = re.compile(r"^(\w+)\s*->\s*(\w+):")


def get_setting(key: str, default: dict) -> dict:
    with session() as db:
        row = db.get(Setting, key)
        return {**default, **(row.value or {})} if row else dict(default)


def put_setting(key: str, value: dict) -> None:
    with session() as db:
        row = db.get(Setting, key)
        if row is None:
            db.add(Setting(key=key, value=value))
        else:
            row.value = value


def sla_targets() -> dict:
    return get_setting("sla", DEFAULT_SLA)


def _ends_wait(e) -> bool:
    """Waiting for the employee ends when they answer, the desk writes, an approver decides, or an agent
    starts real work again (a tool call or handoff). The agent's own closing message and system reminders
    do not end it."""
    if e.actor == "user" or e.actor.startswith("desk:"):
        return True
    if e.actor == "supervisor":
        return e.kind == "note" and "reassigned" in e.content
    if e.actor == "system":
        return False
    if e.kind in ("tool_call", "handoff"):
        return True
    return e.kind == "note" and not e.actor.startswith("L")      # e.g. an approver's decision


def timing(ticket, events) -> dict:
    """Walk the trail: the active tier changes on handoffs (and back when the handoff tool returns);
    time between a confirmation request and the employee's next message is 'waiting for employee'.
    Returns seconds per bucket plus first response and total."""
    start = ticket.created_at
    end = ticket.resolved_at or _now()
    per_tier: dict[str, float] = {}
    waiting = 0.0
    active = "L1"
    stack: list[str] = []
    cursor = start
    paused = False
    first_response = None

    def charge(until):
        nonlocal cursor, waiting
        delta = max(0.0, (until - cursor).total_seconds())
        if paused:
            waiting += delta
        else:
            per_tier[active] = per_tier.get(active, 0.0) + delta
        cursor = until

    for e in events:
        if first_response is None and e.kind == "message" and e.actor != "user":
            first_response = (e.at - start).total_seconds()
        if paused and _ends_wait(e):
            charge(e.at)
            paused = False
        if e.kind == "handoff":
            m = _HANDOFF.match(e.content)
            if m:
                charge(e.at)
                stack.append(active)
                active = m.group(2)
        elif e.kind == "tool_result" and e.content.startswith("handoff_") and stack:
            charge(e.at)
            active = stack.pop()
        elif (e.kind == "tool_result" and e.content == "request_confirmation") or \
             (e.kind == "message" and e.actor == "supervisor"):
            charge(e.at)
            paused = True
        elif e.kind == "note" and e.actor.startswith("desk:") and "assigned to" in e.content:
            m2 = re.search(r"assigned to (L\d|human)", e.content)
            if m2:
                charge(e.at)
                active = m2.group(1)
    charge(end)
    if ticket.status == "escalated" and active != "human":
        pass
    targets = sla_targets()
    tiers = []
    for tier, secs in per_tier.items():
        target = targets.get(tier)
        tiers.append({"tier": tier, "seconds": round(secs), "target_minutes": target,
                      "breached": bool(target) and secs > target * 60})
    total = (end - start).total_seconds()
    return {
        "first_response_seconds": None if first_response is None else round(first_response),
        "first_response_target_minutes": targets.get("first_response"),
        "first_response_breached": first_response is not None and first_response > targets.get("first_response", 0) * 60,
        "tiers": tiers,
        "waiting_for_employee_seconds": round(waiting),
        "total_seconds": round(total),
        "total_target_minutes": targets.get("total"),
        "total_breached": total - waiting > targets.get("total", 0) * 60,
        "resolved": ticket.resolved_at is not None,
    }


def risk(ticket, events, targets: dict | None = None) -> dict | None:
    """SLA position of an unresolved ticket: which target is closest to breaching and how far along.
    Returns None for resolved tickets or when nothing is above the at-risk threshold."""
    if ticket.status == "resolved":
        return None
    targets = targets or sla_targets()
    pct_limit = int(targets.get("at_risk_pct", 80))
    tm = timing(ticket, events)
    worst = None
    cands = []
    if tm["first_response_seconds"] is None and targets.get("first_response"):
        cands.append(("first_response", tm["total_seconds"], targets["first_response"] * 60))
    for x in tm["tiers"]:
        if x["tier"] == ticket.owner and x["target_minutes"]:
            cands.append((x["tier"], x["seconds"], x["target_minutes"] * 60))
    if targets.get("total"):
        cands.append(("total", tm["total_seconds"] - tm["waiting_for_employee_seconds"], targets["total"] * 60))
    for name, secs, limit in cands:
        pct = int(100 * secs / limit) if limit else 0
        if worst is None or pct > worst["pct"]:
            worst = {"metric": name, "seconds": round(secs), "limit_seconds": limit, "pct": pct}
    if worst is None or worst["pct"] < pct_limit:
        return None
    worst["breached"] = worst["pct"] >= 100
    worst["owner"] = ticket.owner
    worst["who"] = "human" if ticket.owner in ("human", "desk") else "agent"
    return worst


def at_risk(db) -> list[dict]:
    """All unresolved tickets at or over the at-risk threshold, worst first."""
    from app.db import Ticket
    out = []
    targets = sla_targets()
    for t in db.query(Ticket).filter(Ticket.status != "resolved").all():
        r = risk(t, t.events, targets)
        if r:
            out.append({"ticket_id": t.id, "title": t.title[:80], "status": t.status, **r})
    out.sort(key=lambda x: -x["pct"])
    return out
