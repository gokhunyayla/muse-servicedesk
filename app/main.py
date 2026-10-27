from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app.agents import handle_message
from app.db import Ticket, log, session
from app.voice import transcribe

app = FastAPI(title="Muse ServiceDesk")

from app.dashboard import router as dashboard_router  # noqa: E402
from app.whatsapp import router as whatsapp_router    # noqa: E402

app.include_router(dashboard_router)
app.include_router(whatsapp_router)


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
def desk_close(ticket_id: int, d: DeskClose) -> dict:
    """A human on the desk closes a ticket (e.g. an escalated one); the employee is told over WhatsApp."""
    from app.db import Outbound
    with session() as db:
        t = db.get(Ticket, ticket_id)
        if t is None:
            raise HTTPException(404)
        if t.status == "resolved":
            return {"id": t.id, "status": t.status}
        t.status, t.resolution, t.owner = "resolved", d.resolution, d.by
        log(db, t.id, d.by, "note", f"closed by the desk: {d.resolution}")
        text = f"Talebiniz (#{t.id}) servis masası tarafından kapatıldı: {d.resolution}"
        if t.requester.startswith("+"):
            db.add(Outbound(to=t.requester, ticket_id=t.id, text=text))
        log(db, t.id, "L1", "message", text)
        return {"id": t.id, "status": "resolved"}


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
def list_tickets(status: str | None = None) -> list[dict]:
    with session() as db:
        q = db.query(Ticket).order_by(Ticket.id.desc())
        if status:
            q = q.filter(Ticket.status == status)
        return [{"id": t.id, "title": t.title, "status": t.status, "owner": t.owner, "rating": t.rating,
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
def get_ticket(ticket_id: int) -> dict:
    with session() as db:
        t = db.get(Ticket, ticket_id)
        if t is None:
            raise HTTPException(404)
        return {"id": t.id, "title": t.title, "status": t.status, "verified": t.verified,
                "events": [{"at": e.at.isoformat(), "actor": e.actor, "kind": e.kind,
                            "content": e.content, "data": e.data} for e in t.events]}
