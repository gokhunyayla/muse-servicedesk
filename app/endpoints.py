"""Real endpoints: machines running the Muse endpoint agent (agent/muse-agent.ps1).

The agent only calls out to the desk: a heartbeat with health metrics every minute and a long-poll for jobs.
The desk never connects in, so a laptop behind NAT, a home network or a cloud VM with no open ports works the
same. When an agent tool targets a hostname that has an enrolled agent, the job runs on that machine and the
real output comes back; any other hostname keeps the simulated inventory in ``app.systems``.

Only the allow-listed scripts in ``systems.SCRIPTS`` ever run, on both sides: the desk refuses anything else and
the agent carries its own copy of the list. Every job is tied to its ticket and lands on the ticket trail.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from datetime import timedelta

from app.db import Device, DeviceJob, Event, _now, session

ONLINE_SEC = 180          # a device is online if it sent a heartbeat within this window
JOB_WAIT_SEC = 45         # how long an agent tool waits for the result before reporting "queued"
JOB_EXPIRE_MIN = 30       # queued jobs older than this are never handed out


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---- enrolment (console, admins) ------------------------------------------------------------------

def enroll(hostname: str, owner: str, by: str) -> str:
    """Create or re-key a device. Returns the one-time token (shown once, stored hashed)."""
    hostname = hostname.strip().upper()
    token = "mda_" + secrets.token_urlsafe(32)
    with session() as db:
        d = db.query(Device).filter(Device.hostname == hostname).first()
        if d is None:
            d = Device(hostname=hostname, created_by=by)
            db.add(d)
        d.owner, d.token_hash, d.enabled = owner.strip(), _hash(token), True
    return token


def listing() -> list[dict]:
    with session() as db:
        return [_public(d) for d in db.query(Device).order_by(Device.hostname)]


def _online(d: Device) -> bool:
    return bool(d.enabled and d.last_seen and d.last_seen >= _now() - timedelta(seconds=ONLINE_SEC))


def _public(d: Device) -> dict:
    return {"id": d.id, "hostname": d.hostname, "owner": d.owner, "enabled": d.enabled, "online": _online(d),
            "os": d.os, "agent_version": d.agent_version, "metrics": d.metrics or {},
            "last_seen": d.last_seen.isoformat() if d.last_seen else None}


# ---- the agent side ------------------------------------------------------------------------------

def authenticate(db, token: str) -> Device | None:
    if not token:
        return None
    d = db.query(Device).filter(Device.token_hash == _hash(token)).first()
    return d if d is not None and d.enabled else None


def heartbeat(db, d: Device, payload: dict) -> None:
    d.last_seen = _now()
    d.os = str(payload.get("os", d.os))[:255]
    d.agent_version = str(payload.get("version", d.agent_version))[:32]
    m = payload.get("metrics")
    if isinstance(m, dict):
        d.metrics = {k: v for k, v in m.items() if isinstance(k, str) and len(k) < 40}


def take_jobs(db, d: Device) -> list[dict]:
    d.last_seen = _now()
    cutoff = _now() - timedelta(minutes=JOB_EXPIRE_MIN)
    jobs = (db.query(DeviceJob).filter(DeviceJob.device_id == d.id, DeviceJob.status == "queued")
            .order_by(DeviceJob.id).all())
    out = []
    for j in jobs:
        if j.created_at < cutoff:
            j.status = "expired"
            continue
        j.status, j.started_at = "running", _now()
        out.append({"id": j.id, "script": j.script})
    return out


def finish(db, d: Device, job_id: int, ok: bool, output: str) -> bool:
    j = db.get(DeviceJob, job_id)
    if j is None or j.device_id != d.id or j.status not in ("running", "queued"):
        return False
    j.status, j.ok, j.output, j.finished_at = ("done" if ok else "failed"), bool(ok), str(output)[:8000], _now()
    if j.ticket_id:
        db.add(Event(ticket_id=j.ticket_id, actor="system", kind="note",
                     content=f"[{d.hostname}] {j.script}: {'ok' if ok else 'failed'}", data={"output": j.output[:2000]}))
    return True


# ---- the desk side (agent tools, proactive checks) ------------------------------------------------

def get(hostname: str) -> Device | None:
    with session() as db:
        d = db.query(Device).filter(Device.hostname == (hostname or "").strip().upper(), Device.enabled.is_(True)).first()
        if d is not None:
            db.expunge(d)
        return d


def state(hostname: str) -> dict | None:
    """The device in the shape of the simulated inventory, plus the raw metrics."""
    d = get(hostname)
    if d is None:
        return None
    m = d.metrics or {}
    return {"real": True, "online": _online(d), "dns_ok": bool(m.get("dns_ok", True)),
            "disk_used_pct": m.get("disk_used_pct", 0), "os_patch_days": m.get("last_patch_days", 0),
            "vpn_profile": "no managed VPN client", "last_seen": d.last_seen.isoformat() if d.last_seen else None,
            "owner": d.owner, "metrics": m, "os": d.os}


def all_states() -> dict[str, dict]:
    with session() as db:
        names = [d.hostname for d in db.query(Device).filter(Device.enabled.is_(True))]
    return {n: state(n) for n in names}


def owner_of(hostname: str) -> str | None:
    d = get(hostname)
    return d.owner if d is not None and d.owner else None


def device_for_owner(employee_no: str) -> str | None:
    if not employee_no:
        return None
    with session() as db:
        d = db.query(Device).filter(Device.owner == employee_no, Device.enabled.is_(True)).first()
        return d.hostname if d is not None else None


def run(hostname: str, script: str, ticket_id: int | None, requested_by: str, wait: int = JOB_WAIT_SEC, db=None) -> dict:
    """Queue an allow-listed script on a real device and wait for its result. With `db` (the ticket's own session)
    the job is written and committed there, so the ticket it points to is visible to the device's session too."""
    from app import systems
    if script not in systems.SCRIPTS:
        return {"ok": False, "error": f"script not allow-listed; available: {list(systems.SCRIPTS)}"}
    d = get(hostname)
    if d is None:
        return {"ok": False, "error": "no endpoint agent on this device"}
    if not _online(d):
        return {"ok": False, "hostname": d.hostname, "error": "device offline (no heartbeat in the last few minutes)",
                "last_seen": d.last_seen.isoformat() if d.last_seen else None}
    j = DeviceJob(device_id=d.id, script=script, ticket_id=ticket_id, requested_by=requested_by)
    if db is not None:
        db.add(j)
        db.commit()                      # the device polls from its own session: it must see the job (and ticket) now
        job_id = j.id
    else:
        with session() as own:
            own.add(j)
            own.flush()
            job_id = j.id
    deadline = time.time() + wait
    while time.time() < deadline:
        with session() as db:
            j = db.get(DeviceJob, job_id)
            if j.status in ("done", "failed"):
                return {"ok": bool(j.ok), "hostname": d.hostname, "script": script, "output": j.output, "job": job_id,
                        "real_device": True}
        time.sleep(1)
    return {"ok": True, "hostname": d.hostname, "script": script, "job": job_id, "status": "queued",
            "note": "the device has not answered yet; its result will be added to this ticket when it does",
            "real_device": True}
