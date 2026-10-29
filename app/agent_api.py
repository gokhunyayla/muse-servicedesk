"""HTTP side of the endpoint agent: what the agent calls (heartbeat, jobs, results) and what admins use to
enrol a machine. Agents authenticate with their own bearer token; admins with the console session."""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from app import endpoints
from app.auth import _esc, _ip, _page, admin_user
from app.config import auth_settings
from app.db import Device, User, audit, session
from app.security import check_origin

router = APIRouter()
AGENT_FILE = Path(__file__).resolve().parent.parent / "agent" / "muse-agent.ps1"


def _device(request: Request, db) -> Device:
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    d = endpoints.authenticate(db, token)
    if d is None:
        raise HTTPException(401, "unknown or disabled device token")
    return d


def agent_version() -> str:
    """The version of the agent this desk ships; agents that report another one update themselves."""
    import re
    m = re.search(r'\$Version = "([^"]+)"', AGENT_FILE.read_text(encoding="utf-8"))
    return m.group(1) if m else ""


@router.get("/agent/muse-agent.ps1", response_class=PlainTextResponse)
def agent_script() -> str:
    """The agent itself (no secrets in it): installers download the version that matches this desk."""
    return AGENT_FILE.read_text(encoding="utf-8")


@router.post("/agent/heartbeat")
async def agent_heartbeat(request: Request) -> dict:
    payload = await request.json()
    with session() as db:
        d = _device(request, db)
        endpoints.heartbeat(db, d, payload if isinstance(payload, dict) else {})
        return {"ok": True, "hostname": d.hostname, "agent_version": agent_version()}


@router.get("/agent/jobs")
def agent_jobs(request: Request, wait: int = 20) -> dict:
    """Long poll: returns as soon as a job is queued, or empty after `wait` seconds."""
    deadline = time.time() + max(0, min(int(wait), 25))
    while True:
        with session() as db:
            d = _device(request, db)
            jobs = endpoints.take_jobs(db, d)
        if jobs or time.time() >= deadline:
            return {"jobs": jobs}
        time.sleep(1)


@router.post("/agent/jobs/{job_id}/result")
async def agent_result(job_id: int, request: Request) -> dict:
    payload = await request.json()
    with session() as db:
        d = _device(request, db)
        ok = endpoints.finish(db, d, job_id, bool(payload.get("ok")), str(payload.get("output", "")))
    if not ok:
        raise HTTPException(404, "no such running job for this device")
    return {"ok": True}


# ---- console: enrol a device ------------------------------------------------------------------------

@router.post("/devices", response_class=HTMLResponse)
async def enroll_device(request: Request, admin: User = Depends(admin_user)) -> str:
    check_origin(request)
    form = await request.form()
    hostname = str(form.get("hostname", "")).strip().upper()
    owner = str(form.get("owner", "")).strip()
    if not hostname or len(hostname) > 64 or not all(c.isalnum() or c in "-_." for c in hostname):
        raise HTTPException(422, "hostname: letters, digits, - _ . only")
    token = endpoints.enroll(hostname, owner, admin.email)
    with session() as db:
        audit(db, admin.email, "device.enrolled", target=hostname, detail=f"owner={owner}", ip=_ip(request))
    desk = auth_settings.public_url.rstrip("/")
    install = (f"$d='{desk}'; $t='{token}'; iwr \"$d/agent/muse-agent.ps1\" -OutFile $env:TEMP\\muse-agent.ps1; "
               f"powershell -ExecutionPolicy Bypass -File $env:TEMP\\muse-agent.ps1 -Install -Desk $d -Token $t")
    body = f"""<p class="muted">Device <b>{_esc(hostname)}</b> enrolled{(' for <b>' + _esc(owner) + '</b>') if owner else ''}. The token below is shown
only now; it is stored hashed. Run the command in an elevated PowerShell on that machine: it installs the agent as a
start-up task that only calls out to this desk (no inbound ports).</p>
<label>Install command</label><textarea rows="5" readonly onclick="this.select()" style="font-family:ui-monospace,Menlo,monospace;font-size:12px">{_esc(install)}</textarea>
<div class="row end"><a class="btn primary slim" href="/settings?tab=connectors">Done</a></div>"""
    return _page("Device enrolled", body, user=admin, active="settings")


@router.post("/devices/{device_id}/toggle")
def toggle_device(device_id: int, request: Request, admin: User = Depends(admin_user)):
    check_origin(request)
    with session() as db:
        d = db.get(Device, device_id)
        if d is None:
            raise HTTPException(404)
        d.enabled = not d.enabled
        audit(db, admin.email, "device.toggled", target=d.hostname, detail=f"enabled={d.enabled}", ip=_ip(request))
    return RedirectResponse("/settings?tab=connectors", 303)


@router.post("/devices/{device_id}/run")
async def run_on_device(device_id: int, request: Request, admin: User = Depends(admin_user)) -> dict:
    """An admin runs one of the agent's allow-listed scripts (the same list the agents use), e.g. app-rollback."""
    from fastapi.concurrency import run_in_threadpool
    check_origin(request)
    body = await request.json()
    with session() as db:
        d = db.get(Device, device_id)
        if d is None:
            raise HTTPException(404)
        host = d.hostname
        audit(db, admin.email, "device.run", target=host, detail=str(body)[:200], ip=_ip(request))
    return await run_in_threadpool(endpoints.run, host, str(body.get("script", "")), None, f"desk:{admin.name}",
                                   wait=min(int(body.get("wait", 120)), 600), args=body.get("args") or None)


@router.post("/people/{process_id}/cancel")
def cancel_people_process(process_id: int, request: Request, admin: User = Depends(admin_user)) -> dict:
    """Stop an onboarding/offboarding (a duplicate, a hire that fell through): its tickets close without messages."""
    from app.db import PeopleProcess, Ticket, log, resolve
    check_origin(request)
    with session() as db:
        p = db.get(PeopleProcess, process_id)
        if p is None:
            raise HTTPException(404)
        p.status = "cancelled"
        for tid in (p.hr_ticket_id, p.manager_ticket_id):
            t = db.get(Ticket, tid) if tid else None
            if t is not None and t.status != "resolved":
                t.resolution = f"{p.kind} #{p.id} cancelled by {admin.email}"
                resolve(t)
                log(db, t.id, "system", "note", t.resolution)
        audit(db, admin.email, "people.cancelled", target=str(process_id), ip=_ip(request))
    return {"ok": True, "status": "cancelled"}


@router.post("/vulnwatch/run")
def vulnwatch_run(request: Request) -> dict:
    """Run the vulnerability watch now (console admins, or an integration with the API key). It runs in the background:
    findings arrive as tickets and messages to the admins."""
    import threading
    from app import vulnwatch
    from app.auth import current_user
    from app.security import api_key
    user = None
    try:
        user = current_user(request)
    except HTTPException:
        pass
    if user is not None:
        if user.role != "admin":
            raise HTTPException(403, "admins only")
        check_origin(request)
    else:
        api_key(request)
    threading.Thread(target=vulnwatch.run_watch, kwargs={"force": True}, daemon=True, name="vulnwatch-now").start()
    return {"ok": True, "status": "started"}


@router.get("/vulnwatch/status")
def vulnwatch_status(request: Request) -> dict:
    """What the last vulnerability watch saw and did (console admins, or the API key)."""
    from app import vulnwatch
    from app.auth import current_user
    from app.security import api_key
    try:
        if current_user(request).role != "admin":
            raise HTTPException(403, "admins only")
    except HTTPException as e:
        if e.status_code == 403:
            raise
        api_key(request)
    c = vulnwatch.config()
    return {"enabled": c["enabled"], "auto_remediate": c["auto_remediate"], "last_run": c.get("last_run"),
            "last_result": c.get("last_result") or {}}


@router.post("/settings/vulnwatch")
async def settings_vulnwatch(request: Request, admin: User = Depends(admin_user)):
    from app.sla import get_setting, put_setting
    check_origin(request)
    form = await request.form()
    put_setting("vulnwatch", {**get_setting("vulnwatch", {}), "enabled": form.get("enabled") == "1",
                              "auto_remediate": form.get("auto_remediate") == "1"})
    with session() as db:
        audit(db, admin.email, "settings.vulnwatch", detail=f"enabled={form.get('enabled') == '1'} auto={form.get('auto_remediate') == '1'}", ip=_ip(request))
    return RedirectResponse("/settings?tab=connectors&info=Vulnerability+watch+saved", 303)


@router.get("/devices.json")
def devices_json(admin: User = Depends(admin_user)) -> list[dict]:
    return endpoints.listing()
