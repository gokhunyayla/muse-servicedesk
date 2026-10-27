from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.agents import handle_message
from app.db import Ticket, log, session
from app.voice import transcribe

app = FastAPI(title="Muse ServiceDesk")

from app.auth import current_user, needs_setup  # noqa: E402
from app.security import api_key, check_origin  # noqa: E402
from app.db import audit  # noqa: E402
from app.sla import timing  # noqa: E402
from app.dashboard import router as dashboard_router  # noqa: E402
from app.whatsapp import router as whatsapp_router    # noqa: E402
from app.auth import router as auth_router            # noqa: E402

app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(whatsapp_router)


@app.exception_handler(HTTPException)
async def _auth_redirect(request: Request, exc: HTTPException):
    """Browser page requests without a session go to the sign-in page; API calls get the JSON 401."""
    if exc.status_code == 401 and "text/html" in request.headers.get("accept", ""):
        return RedirectResponse("/setup" if needs_setup() else "/login", 303)
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))


class Incoming(BaseModel):
    requester: str
    text: str


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/messages", dependencies=[Depends(api_key)])
def post_message(msg: Incoming) -> dict:
    with session() as db:
        ticket, answer = handle_message(db, msg.requester, msg.text)
        return {"ticket_id": ticket.id, "reply": answer}


@app.post("/voice", dependencies=[Depends(api_key)])
async def post_voice(requester: str = Form(...), audio: UploadFile = File(...)) -> dict:
    """A voice note: transcribe it, then handle it like a text message."""
    text = transcribe(await audio.read(), audio.content_type or "audio/wav")
    if not text:
        raise HTTPException(422, "could not transcribe audio")
    with session() as db:
        ticket, answer = handle_message(db, requester, text)
        log(db, ticket.id, "user", "note", f"voice note transcribed: {text}")
        return {"ticket_id": ticket.id, "transcript": text, "reply": answer}


@app.post("/image", dependencies=[Depends(api_key)])
async def post_image(requester: str = Form(...), text: str = Form(""), image: UploadFile = File(...)) -> dict:
    """A photo or screenshot with an optional caption."""
    data = await image.read()
    with session() as db:
        ticket, answer = handle_message(db, requester, text or "(fotoğraf gönderdi)", data,
                                        image.content_type or "image/jpeg")
        return {"ticket_id": ticket.id, "reply": answer}


class Decision(BaseModel):
    decision: str          # approved | denied
    by: str                # approver's phone


class DeskClose(BaseModel):
    resolution: str = "closed by the service desk"
    by: str = "desk"


@app.post("/tickets/{ticket_id}/close")
def desk_close(ticket_id: int, d: DeskClose, request: Request, user=Depends(current_user)) -> dict:
    """A human on the desk closes a ticket (e.g. an escalated one); the employee is told over WhatsApp."""
    from app.db import Outbound
    with session() as db:
        t = db.get(Ticket, ticket_id)
        if t is None:
            raise HTTPException(404)
        if t.status == "resolved":
            return {"id": t.id, "status": t.status}
        check_origin(request)
        by = f"desk:{user.name}"
        from app.db import resolve
        resolve(t)
        t.resolution, t.owner = d.resolution, "desk"
        log(db, t.id, by, "note", f"closed by the desk: {d.resolution}")
        audit(db, user.email, "ticket.close", target=t.id, detail=d.resolution)
        text = f"Talebiniz (#{t.id}) servis masası tarafından kapatıldı: {d.resolution}"
        if t.requester.startswith("+"):
            db.add(Outbound(to=t.requester, ticket_id=t.id, text=text))
        log(db, t.id, "L1", "message", text)
        return {"id": t.id, "status": "resolved"}


class Problem(BaseModel):
    ticket_ids: list[int]
    title: str
    update: str = ""          # optional first status update sent to everyone affected
    owner: str = "L1"


@app.post("/problems")
def merge_into_problem(p: Problem, request: Request, user=Depends(current_user)) -> dict:
    """The desk merges several tickets into one problem record. The oldest selected ticket becomes the
    record; the others link to it and close with it. An optional update goes out to everyone."""
    from app.db import Outbound
    if len(p.ticket_ids) < 2:
        raise HTTPException(422, "select at least two tickets")
    with session() as db:
        ts = [db.get(Ticket, i) for i in sorted(set(p.ticket_ids))]
        if any(t is None for t in ts):
            raise HTTPException(404, "unknown ticket")
        head, rest = ts[0], ts[1:]
        head.title, head.owner = f"[INCIDENT] {p.title}", p.owner
        if head.status == "resolved":
            head.status = "open"
        check_origin(request)
        log(db, head.id, f"desk:{user.name}", "note", f"problem record created by the desk from tickets {[t.id for t in ts]}")
        audit(db, user.email, "problem.create", target=head.id, detail=f"{p.title} <- {[t.id for t in ts]}")
        for t in rest:
            t.incident_id = head.id
            log(db, t.id, "desk", "note", f"linked to problem #{head.id} by the desk")
        if p.update:
            for r in {t.requester for t in ts if t.requester.startswith("+")}:
                db.add(Outbound(to=r, ticket_id=head.id, text=f"[#{head.id}] {p.update}"))
            log(db, head.id, "desk", "note", f"update sent to {len(ts)} employees: {p.update}")
        return {"problem_id": head.id, "linked": [t.id for t in rest]}


class Assignment(BaseModel):
    owner: str                # L1 | L2 | L3 | L4 | human
    note: str = ""
    assignee: str = ""        # a person's name when owner == human


@app.post("/tickets/{ticket_id}/assign")
def assign_ticket(ticket_id: int, a: Assignment, request: Request, user=Depends(current_user)) -> dict:
    """The desk assigns a ticket. To an agent tier: that agent takes over right away with the desk's note.
    To a human: the ticket is escalated and waits for that person."""
    from app.agents import AGENTS
    with session() as db:
        t = db.get(Ticket, ticket_id)
        if t is None:
            raise HTTPException(404)
        check_origin(request)
        audit(db, user.email, "ticket.assign", target=t.id, detail=f"{a.owner} {a.assignee} {a.note}".strip())
        if a.owner == "human":
            t.status, t.owner = "escalated", "human"
            who = f" to {a.assignee}" if a.assignee else ""
            log(db, t.id, f"desk:{user.name}", "note", f"assigned{who} by the desk. {a.note}".strip())
            return {"id": t.id, "status": t.status, "owner": t.owner}
        if a.owner not in AGENTS:
            raise HTTPException(422, "owner must be L1, L2, L3, L4 or human")
        t.owner = a.owner
        if t.status in ("escalated", "resolved"):
            t.status = "open"
        log(db, t.id, f"desk:{user.name}", "note", f"assigned to {a.owner} by the desk. {a.note}".strip())
        answer = AGENTS[a.owner].run(db, t, f"[Assigned to you by the service desk] {a.note or 'Take over this ticket.'} "
                                            f"Review the trail, act with your tools, and report what you did.")
        log(db, t.id, a.owner, "message", answer)
        if a.owner == "L1" and t.requester.startswith("+"):
            from app.db import Outbound
            db.add(Outbound(to=t.requester, ticket_id=t.id, text=answer))
        return {"id": t.id, "status": t.status, "owner": t.owner, "reply": answer}


class DeskReply(BaseModel):
    text: str


@app.post("/tickets/{ticket_id}/reply")
def desk_reply(ticket_id: int, r: DeskReply, request: Request, user=Depends(current_user)) -> dict:
    """A desk user writes to the employee directly; it goes out over WhatsApp and into the trail."""
    from app.db import Outbound
    text = r.text.strip()
    if not text:
        raise HTTPException(422, "empty message")
    with session() as db:
        t = db.get(Ticket, ticket_id)
        if t is None:
            raise HTTPException(404)
        check_origin(request)
        log(db, t.id, f"desk:{user.name}", "message", text)
        if t.requester.startswith("+"):
            db.add(Outbound(to=t.requester, ticket_id=t.id, text=text))
        if t.status == "waiting_user":
            pass                                   # still waiting for the employee
        audit(db, user.email, "ticket.reply", target=t.id, detail=text)
        return {"id": t.id, "sent": t.requester.startswith("+")}


class StatusChange(BaseModel):
    status: str
    note: str = ""


@app.post("/tickets/{ticket_id}/status")
def desk_status(ticket_id: int, s: StatusChange, request: Request, user=Depends(current_user)) -> dict:
    """A desk user changes the ticket status by hand."""
    if s.status not in ("open", "waiting_user", "waiting_approval", "escalated", "resolved"):
        raise HTTPException(422, "unknown status")
    with session() as db:
        t = db.get(Ticket, ticket_id)
        if t is None:
            raise HTTPException(404)
        check_origin(request)
        old = t.status
        t.status = s.status
        if s.status == "resolved":
            from app.db import resolve
            resolve(t)
        elif t.resolved_at and s.status != "resolved":
            t.resolved_at = None                 # reopened
        if s.status == "escalated":
            t.owner = "human"
        if s.status == "resolved" and s.note:
            t.resolution = s.note
        log(db, t.id, f"desk:{user.name}", "note", f"status {old} → {s.status}" + (f": {s.note}" if s.note else ""))
        audit(db, user.email, "ticket.status", target=t.id, detail=f"{old} -> {s.status} {s.note}".strip())
        return {"id": t.id, "status": t.status, "owner": t.owner}


@app.post("/supervisor/run")
def supervisor_run_now(request: Request, user=Depends(current_user)) -> dict:
    from app import supervisor
    check_origin(request)
    with session() as db:
        audit(db, user.email, "supervisor.run")
    r = supervisor.run("manual")
    return {"id": r.id, "summary": r.summary, "actions": r.actions, "findings": r.findings, "error": r.error}


@app.get("/supervisor/stats")
def supervisor_stats(period: str | None = None, user=Depends(current_user)) -> dict:
    from app import supervisor
    from app.dashboard import period_start
    st = supervisor.stats(period_start(period))
    st["last_run"] = st["last_run"].isoformat() if st["last_run"] else None
    st["mode"] = supervisor.config().get("mode", "manual")
    return st


@app.get("/supervisor/runs")
def supervisor_runs(limit: int = 10, user=Depends(current_user)) -> list[dict]:
    from app.db import SupervisorRun
    with session() as db:
        return [{"id": r.id, "started_at": r.started_at.isoformat(), "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                 "trigger": r.trigger, "summary": r.summary, "findings": r.findings, "actions": r.actions, "error": r.error}
                for r in db.query(SupervisorRun).order_by(SupervisorRun.id.desc()).limit(min(limit, 100))]


@app.get("/sops/drafts")
def sop_drafts(status: str = "pending", user=Depends(current_user)) -> list[dict]:
    from app.db import SopDraft
    with session() as db:
        q = db.query(SopDraft).order_by(SopDraft.id.desc())
        if status != "all":
            q = q.filter(SopDraft.status == status)
        return [{"id": d.id, "tier": d.tier, "title": d.title, "content": d.content, "sources": d.sources, "reason": d.reason,
                 "ticket_ids": d.ticket_ids, "status": d.status, "created_at": d.created_at.isoformat(), "sop_id": d.sop_id}
                for d in q.limit(100)]


class DraftDecision(BaseModel):
    decision: str          # publish | reject


@app.post("/sops/drafts/{draft_id}")
def decide_draft(draft_id: int, d: DraftDecision, request: Request, user=Depends(current_user)) -> dict:
    from app.auth import admin_user
    from app.db import SopDraft
    from app.supervisor import publish_draft
    if user.role != "admin":
        raise HTTPException(403, "admin only")
    check_origin(request)
    with session() as db:
        dr = db.get(SopDraft, draft_id)
        if dr is None or dr.status != "pending":
            raise HTTPException(404, "no pending draft")
        if d.decision == "publish":
            sop_id = publish_draft(db, dr, by=user.name)
            audit(db, user.email, "sop.published", target=dr.id, detail=f"{dr.tier}: {dr.title}")
            return {"id": dr.id, "status": "published", "sop_id": sop_id}
        dr.status, dr.decided_by = "rejected", user.name
        audit(db, user.email, "sop.rejected", target=dr.id, detail=dr.title)
        return {"id": dr.id, "status": "rejected"}


@app.post("/approvals/{approval_id}", dependencies=[Depends(api_key)])
def decide_approval(approval_id: int, d: Decision) -> dict:
    from app.agents import handle_approval
    if d.decision not in ("approved", "denied"):
        raise HTTPException(422, "decision must be approved or denied")
    with session() as db:
        try:
            ticket, answer = handle_approval(db, approval_id, d.decision, d.by)
        except (ValueError, PermissionError) as e:
            raise HTTPException(403, str(e))
        return {"ticket_id": ticket.id, "reply": answer}


@app.post("/video", dependencies=[Depends(api_key)])
async def post_video(requester: str = Form(...), text: str = Form(""), video: UploadFile = File(...)) -> dict:
    """A short screen recording or phone clip: sample frames and let L1 look at them."""
    from app.llm import image_message
    from app.vision import sample_frames
    frames = sample_frames(await video.read())
    if not frames:
        raise HTTPException(422, "could not read video")
    with session() as db:
        # first frame rides through the normal image path; the rest are appended as extra parts
        caption = (text or "(video gönderdi)") + f" [video: {len(frames)} frames sampled, 2s apart]"
        from app.agents import AGENTS, handle_message
        ticket, answer = handle_message(db, requester, caption, frames[0], "image/jpeg",
                                        extra_images=frames[1:])
        return {"ticket_id": ticket.id, "frames": len(frames), "reply": answer}


@app.post("/camera/{camera_id}", dependencies=[Depends(api_key)])
async def post_camera_frame(camera_id: str, image: UploadFile = File(...)) -> dict:
    from app.camera import process_frame
    try:
        return {"findings": process_frame(camera_id, await image.read(), image.content_type or "image/jpeg")}
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/outbound", dependencies=[Depends(api_key)])
def list_outbound(unsent: bool = True) -> list[dict]:
    """Messages the desk wants to send on its own; a channel (WhatsApp) drains this."""
    from app.db import Outbound
    with session() as db:
        q = db.query(Outbound).order_by(Outbound.id)
        if unsent:
            q = q.filter(Outbound.sent.is_(False))
        return [{"id": o.id, "to": o.to, "ticket_id": o.ticket_id, "text": o.text} for o in q.limit(100)]


@app.post("/proactive/run", dependencies=[Depends(api_key)])
def run_proactive() -> dict:
    from app.proactive import run_proactive_checks
    return {"opened": run_proactive_checks()}


@app.post("/followups/run", dependencies=[Depends(api_key)])
def run_followups() -> dict:
    from app.scheduler import run_due_followups
    return {"ran": run_due_followups()}


@app.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: int, user=Depends(current_user)) -> dict:
    with session() as db:
        t = db.get(Ticket, ticket_id)
        if t is None:
            raise HTTPException(404)
        return {"id": t.id, "title": t.title, "status": t.status, "verified": t.verified, "owner": t.owner, "incident_id": t.incident_id,
                "requester": t.requester, "created_at": t.created_at.isoformat(), "resolution": t.resolution, "rating": t.rating,
                "linked": [c.id for c in db.query(Ticket).filter(Ticket.incident_id == t.id)],
                "resolved_at": t.resolved_at.isoformat() if t.resolved_at else None, "timing": timing(t, t.events),
                "events": [{"at": e.at.isoformat(), "actor": e.actor, "kind": e.kind,
                            "content": e.content, "data": e.data} for e in t.events]}
