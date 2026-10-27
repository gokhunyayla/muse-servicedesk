from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.agents import handle_message
from app.db import Ticket, log, session
from app.voice import transcribe

app = FastAPI(title="Muse ServiceDesk")

from app.auth import current_user, needs_setup  # noqa: E402
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


@app.post("/messages")
def post_message(msg: Incoming) -> dict:
    with session() as db:
        ticket, answer = handle_message(db, msg.requester, msg.text)
        return {"ticket_id": ticket.id, "reply": answer}


@app.post("/voice")
async def post_voice(requester: str = Form(...), audio: UploadFile = File(...)) -> dict:
    """A voice note: transcribe it, then handle it like a text message."""
    text = transcribe(await audio.read(), audio.content_type or "audio/wav")
    if not text:
        raise HTTPException(422, "could not transcribe audio")
    with session() as db:
        ticket, answer = handle_message(db, requester, text)
        log(db, ticket.id, "user", "note", f"voice note transcribed: {text}")
        return {"ticket_id": ticket.id, "transcript": text, "reply": answer}


@app.post("/image")
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
def desk_close(ticket_id: int, d: DeskClose, user=Depends(current_user)) -> dict:
    """A human on the desk closes a ticket (e.g. an escalated one); the employee is told over WhatsApp."""
    from app.db import Outbound
    with session() as db:
        t = db.get(Ticket, ticket_id)
        if t is None:
            raise HTTPException(404)
        if t.status == "resolved":
            return {"id": t.id, "status": t.status}
        by = f"desk:{user.name}"
        t.status, t.resolution, t.owner = "resolved", d.resolution, "desk"
        log(db, t.id, by, "note", f"closed by the desk: {d.resolution}")
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
def merge_into_problem(p: Problem, user=Depends(current_user)) -> dict:
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
        log(db, head.id, f"desk:{user.name}", "note", f"problem record created by the desk from tickets {[t.id for t in ts]}")
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
def assign_ticket(ticket_id: int, a: Assignment, user=Depends(current_user)) -> dict:
    """The desk assigns a ticket. To an agent tier: that agent takes over right away with the desk's note.
    To a human: the ticket is escalated and waits for that person."""
    from app.agents import AGENTS
    with session() as db:
        t = db.get(Ticket, ticket_id)
        if t is None:
            raise HTTPException(404)
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


@app.post("/approvals/{approval_id}")
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


@app.post("/video")
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


@app.post("/camera/{camera_id}")
async def post_camera_frame(camera_id: str, image: UploadFile = File(...)) -> dict:
    from app.camera import process_frame
    try:
        return {"findings": process_frame(camera_id, await image.read(), image.content_type or "image/jpeg")}
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/tickets")
def list_tickets(status: str | None = None, owner: str | None = None, period: str | None = None, user=Depends(current_user)) -> list[dict]:
    from app.dashboard import period_start
    with session() as db:
        q = db.query(Ticket).order_by(Ticket.id.desc())
        if status:
            q = q.filter(Ticket.status == status)
        if owner:
            q = q.filter(Ticket.owner == owner)
        since = period_start(period)
        if since:
            q = q.filter(Ticket.created_at >= since)
        return [{"id": t.id, "title": t.title, "status": t.status, "owner": t.owner, "rating": t.rating, "incident_id": t.incident_id,
                 "created_at": t.created_at.isoformat()} for t in q.limit(100)]


@app.get("/outbound")
def list_outbound(unsent: bool = True) -> list[dict]:
    """Messages the desk wants to send on its own; a channel (WhatsApp) drains this."""
    from app.db import Outbound
    with session() as db:
        q = db.query(Outbound).order_by(Outbound.id)
        if unsent:
            q = q.filter(Outbound.sent.is_(False))
        return [{"id": o.id, "to": o.to, "ticket_id": o.ticket_id, "text": o.text} for o in q.limit(100)]


@app.post("/proactive/run")
def run_proactive() -> dict:
    from app.proactive import run_proactive_checks
    return {"opened": run_proactive_checks()}


@app.post("/followups/run")
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
                "events": [{"at": e.at.isoformat(), "actor": e.actor, "kind": e.kind,
                            "content": e.content, "data": e.data} for e in t.events]}
