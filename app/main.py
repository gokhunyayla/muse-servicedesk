from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.channels import reachable
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


@app.on_event("startup")
def _apply_console_secrets() -> None:
    from app import connectors, systems, vault
    try:
        systems.apply_demo_phone(vault.get("DEMO_MANAGER_PHONE"))
        connectors.register_all()
    except Exception:          # database may not exist yet on a very first run
        pass
app.include_router(dashboard_router)
app.include_router(whatsapp_router)
from app.support import router as support_router    # noqa: E402
app.include_router(support_router)
from app.agent_api import router as agent_router    # noqa: E402
app.include_router(agent_router)


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
        return {"ticket_id": ticket.id if ticket else None, "reply": answer}


@app.post("/voice", dependencies=[Depends(api_key)])
async def post_voice(requester: str = Form(...), audio: UploadFile = File(...)) -> dict:
    """A voice note: transcribe it, then handle it like a text message."""
    from starlette.concurrency import run_in_threadpool
    raw = await audio.read()
    return await run_in_threadpool(_voice_sync, requester, raw, audio.content_type or "audio/wav")


def _voice_sync(requester: str, raw: bytes, mime: str) -> dict:
    text = transcribe(raw, mime)
    if not text:
        raise HTTPException(422, "could not transcribe audio")
    with session() as db:
        ticket, answer = handle_message(db, requester, text, voice=True)
        if ticket is not None:
            log(db, ticket.id, "user", "note", f"voice note transcribed: {text}")
        return {"ticket_id": ticket.id if ticket else None, "transcript": text, "reply": answer}


@app.post("/image", dependencies=[Depends(api_key)])
async def post_image(requester: str = Form(...), text: str = Form(""), image: UploadFile = File(...)) -> dict:
    """A photo or screenshot with an optional caption."""
    from starlette.concurrency import run_in_threadpool
    data, mime = await image.read(), image.content_type or "image/jpeg"

    def go() -> dict:
        with session() as db:
            ticket, answer = handle_message(db, requester, text or "(sent a photo)", data, mime)
            return {"ticket_id": ticket.id if ticket else None, "reply": answer}
    return await run_in_threadpool(go)


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
        from app.i18n import t as tr_, ticket_lang
        text = tr_("closed_by_desk", ticket_lang(t), id=t.id, note=d.resolution)
        if reachable(t.requester):
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
            for r in {t.requester for t in ts if reachable(t.requester)}:
                db.add(Outbound(to=r, ticket_id=head.id, text=f"[#{head.id}] {p.update}"))
            log(db, head.id, "desk", "note", f"update sent to {len(ts)} employees: {p.update}")
        return {"problem_id": head.id, "linked": [t.id for t in rest]}


class NewTicket(BaseModel):
    requester: str            # phone, e-mail or employee number of the employee it is for
    text: str
    note: str = ""


@app.post("/tickets/new")
def desk_opens_ticket(p: NewTicket, request: Request, user=Depends(current_user)) -> dict:
    """An admin opens a ticket for an employee (the only way to raise a request for someone else).
    The agents handle it as if the employee had written; replies go to the employee's channel."""
    from app import systems
    from app.support import normalise
    if user.role != "admin":
        raise HTTPException(403, "admins only")
    check_origin(request)
    who = p.requester.strip()
    contact = normalise(who) if ("@" in who or who[:1] in "+0123456789") else ""
    if not contact:
        u = systems.get_user(who.upper())
        contact = (u or {}).get("phone") or systems.user_email(who.upper()) if u else ""
    if not contact:
        raise HTTPException(422, "unknown employee: give a phone, e-mail or employee number")
    text = p.text.strip()
    if not text:
        raise HTTPException(422, "empty request")
    with session() as db:
        t, answer = handle_message(db, contact, text, force_new=True, channel="whatsapp" if contact.startswith("+") else "email")
        log(db, t.id, f"desk:{user.name}", "note", f"opened by the desk on behalf of the employee" + (f": {p.note}" if p.note else ""))
        audit(db, user.email, "ticket.open_for", target=t.id, detail=f"{contact}: {text[:80]}")
        from app.channels import reachable
        if reachable(contact):
            from app.db import Outbound
            db.add(Outbound(to=contact, ticket_id=t.id, text=answer))
        return {"id": t.id, "requester": contact, "reply": answer}


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
            t.assignee = a.assignee or ""
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
        if a.owner == "L1" and reachable(t.requester):
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
        if reachable(t.requester):
            db.add(Outbound(to=t.requester, ticket_id=t.id, text=text))
        if t.status == "waiting_user":
            pass                                   # still waiting for the employee
        audit(db, user.email, "ticket.reply", target=t.id, detail=text)
        return {"id": t.id, "sent": reachable(t.requester)}


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
    # a pass can take minutes (web research, many tickets): run it beside the request, the page follows /supervisor/runs
    import threading
    threading.Thread(target=supervisor.run, args=("manual",), daemon=True, name="supervisor-manual").start()
    return {"status": "started"}


@app.get("/sla/at-risk")
def sla_at_risk(user=Depends(current_user)) -> list[dict]:
    from app.sla import at_risk
    with session() as db:
        return at_risk(db)


@app.get("/ai/usage")
def ai_usage(period: str | None = None, user=Depends(current_user)) -> dict:
    from app import usage
    from app.dashboard import period_start
    return usage.summary(period_start(period))


@app.get("/ai/calls")
def ai_calls(period: str | None = None, scope: str | None = None, model: str | None = None, limit: int = 500, user=Depends(current_user)) -> list[dict]:
    from app import usage
    from app.dashboard import period_start
    return usage.calls(period_start(period), limit=limit, scope_kind=scope, model=model)


@app.get("/ai/daily")
def ai_daily(period: str | None = None, user=Depends(current_user)) -> list[dict]:
    from app import usage
    from app.dashboard import period_start
    return usage.daily(period_start(period))


@app.get("/ai/tools")
def ai_tools(period: str | None = None, user=Depends(current_user)) -> list[dict]:
    from app import usage
    from app.dashboard import period_start
    return usage.tool_performance(period_start(period))


@app.get("/ai/calls.csv")
def ai_calls_csv(period: str | None = None, user=Depends(current_user)):
    import csv
    import io
    from fastapi.responses import PlainTextResponse
    from app import usage
    from app.dashboard import period_start
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["id", "at", "model", "scope", "scope_id", "actor", "prompt_tokens", "completion_tokens", "total_tokens", "latency_ms", "tool_calls", "ok"]
    w.writerow(cols)
    for r in usage.calls(period_start(period), limit=5000):
        w.writerow([r[c] for c in cols])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=ai-calls.csv"})


@app.get("/chat/threads/{thread_id}/usage")
def chat_thread_usage(thread_id: int, user=Depends(current_user)) -> dict:
    from app import usage
    return usage.for_thread(thread_id)


@app.get("/supervisor/stats")
def supervisor_stats(period: str | None = None, user=Depends(current_user)) -> dict:
    from app import supervisor
    from app.dashboard import period_start
    st = supervisor.stats(period_start(period))
    st["last_run"] = st["last_run"].isoformat() if st["last_run"] else None
    st["mode"] = supervisor.config().get("mode", "manual")
    return st


class ReportRequest(BaseModel):
    kind: str = "daily"           # daily | weekly | monthly
    to: str = ""                  # optional override, comma-separated


@app.post("/reports/send")
def send_report_now(r: ReportRequest, request: Request, user=Depends(current_user)) -> dict:
    from app import reports
    if r.kind not in ("daily", "weekly", "monthly"):
        raise HTTPException(422, "kind must be daily, weekly or monthly")
    check_origin(request)
    to = [x.strip() for x in r.to.split(",") if x.strip()] or None
    with session() as db:
        audit(db, user.email, "report.send", detail=f"{r.kind} {to or ''}".strip())
    rep = reports.build_and_send(r.kind, "manual", to)
    return {"id": rep.id, "sent": rep.sent, "error": rep.error, "subject": rep.subject, "narrative": rep.narrative}


@app.get("/reports")
def list_reports(limit: int = 20, user=Depends(current_user)) -> list[dict]:
    from app.db import Report
    with session() as db:
        return [{"id": r.id, "created_at": r.created_at.isoformat(), "kind": r.kind, "trigger": r.trigger, "recipients": r.recipients,
                 "subject": r.subject, "sent": r.sent, "error": r.error} for r in db.query(Report).order_by(Report.id.desc()).limit(min(limit, 100))]


@app.get("/reports/{report_id}", response_class=__import__("fastapi.responses", fromlist=["HTMLResponse"]).HTMLResponse)
def view_report(report_id: int, user=Depends(current_user)):
    from app.db import Report
    with session() as db:
        r = db.get(Report, report_id)
        if r is None:
            raise HTTPException(404)
        return r.html


@app.get("/supervisor/runs")
def supervisor_runs(limit: int = 10, user=Depends(current_user)) -> list[dict]:
    from app.db import SupervisorRun
    with session() as db:
        return [{"id": r.id, "started_at": r.started_at.isoformat(), "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                 "trigger": r.trigger, "summary": r.summary, "findings": r.findings, "actions": r.actions, "error": r.error}
                for r in db.query(SupervisorRun).order_by(SupervisorRun.id.desc()).limit(min(limit, 100))]


# ---- Connectors (MCP) ------------------------------------------------------------------------

class ConnectorIn(BaseModel):
    name: str
    label: str = ""
    kind: str = "http"            # stdio | http
    command: str = ""
    url: str = ""
    secrets: dict = {}            # env (stdio) or headers (http); values never echoed back
    tiers: list[str] = ["L1", "L2", "L3", "L4"]
    enabled: bool = True


def _admin(user):
    if user.role != "admin":
        raise HTTPException(403, "admin only")


@app.get("/connectors")
def list_connectors(user=Depends(current_user)) -> list[dict]:
    from app import connectors
    return connectors.listing()


@app.post("/connectors")
def create_connector(c: ConnectorIn, request: Request, user=Depends(current_user)) -> dict:
    import re
    from app import connectors
    from app.db import Connector
    _admin(user)
    check_origin(request)
    name = re.sub(r"[^a-z0-9_-]", "-", c.name.lower()).strip("-")[:64]
    if not name or c.kind not in ("stdio", "http"):
        raise HTTPException(422, "name and kind (stdio|http) are required")
    tiers = [t for t in c.tiers if t in ("L1", "L2", "L3", "L4", "SUP", "DESK")]
    with session() as db:
        if db.query(Connector).filter(Connector.name == name).first():
            raise HTTPException(409, "a connector with that name exists")
        row = Connector(name=name, label=c.label or c.name, kind=c.kind, command=c.command.strip(), url=c.url.strip(),
                        secrets=connectors.encrypt_secrets({k: v for k, v in c.secrets.items() if v}), tiers=tiers,
                        enabled=c.enabled, created_by=user.email)
        db.add(row)
        db.flush()
        audit(db, user.email, "connector.create", target=name, detail=f"{c.kind} {c.url or c.command} tiers={tiers}")
        cid = row.id
    return connectors.test(cid) | {"id": cid, "name": name}


@app.put("/connectors/{cid}")
def update_connector(cid: int, c: ConnectorIn, request: Request, user=Depends(current_user)) -> dict:
    from app import connectors
    from app.db import Connector
    _admin(user)
    check_origin(request)
    with session() as db:
        row = db.get(Connector, cid)
        if row is None:
            raise HTTPException(404)
        row.label, row.kind, row.command, row.url = c.label or row.label, c.kind, c.command.strip(), c.url.strip()
        row.tiers = [t for t in c.tiers if t in ("L1", "L2", "L3", "L4", "SUP", "DESK")]
        row.enabled = c.enabled
        if c.secrets:                                   # only replace when new values are given
            merged = connectors.decrypt_secrets(row.secrets) | {k: v for k, v in c.secrets.items() if v}
            row.secrets = connectors.encrypt_secrets(merged)
        db.flush()
        connectors.register(row)
        audit(db, user.email, "connector.update", target=row.name, detail=f"enabled={c.enabled} tiers={row.tiers}")
    return {"id": cid, "ok": True}


@app.post("/connectors/{cid}/test")
def test_connector(cid: int, request: Request, user=Depends(current_user)) -> dict:
    from app import connectors
    _admin(user)
    check_origin(request)
    with session() as db:
        audit(db, user.email, "connector.test", target=cid)
    return connectors.test(cid)


@app.delete("/connectors/{cid}")
def delete_connector(cid: int, request: Request, user=Depends(current_user)) -> dict:
    from app import connectors
    from app.db import Connector
    _admin(user)
    check_origin(request)
    with session() as db:
        row = db.get(Connector, cid)
        if row is None:
            raise HTTPException(404)
        connectors.unregister(row.name)
        audit(db, user.email, "connector.delete", target=row.name)
        db.delete(row)
    return {"id": cid, "deleted": True}


@app.get("/connectors/presets")
def connector_presets(user=Depends(current_user)) -> dict:
    from app.connectors import PRESETS
    return PRESETS


# ---- Desk Chat -------------------------------------------------------------------------------

@app.get("/chat/threads")
def chat_threads(user=Depends(current_user)) -> list[dict]:
    from app.db import ChatThread
    with session() as db:
        return [{"id": t.id, "title": t.title, "updated_at": t.updated_at.isoformat()}
                for t in db.query(ChatThread).filter(ChatThread.user_id == user.id).order_by(ChatThread.updated_at.desc()).limit(50)]


@app.post("/chat/threads")
def chat_new_thread(request: Request, user=Depends(current_user)) -> dict:
    from app.db import ChatThread
    check_origin(request)
    with session() as db:
        t = ChatThread(user_id=user.id)
        db.add(t)
        db.flush()
        return {"id": t.id, "title": t.title}


@app.get("/chat/threads/{thread_id}")
def chat_messages(thread_id: int, user=Depends(current_user)) -> list[dict]:
    from app.db import ChatMessage, ChatThread
    with session() as db:
        t = db.get(ChatThread, thread_id)
        if t is None or t.user_id != user.id:
            raise HTTPException(404)
        return [{"id": m.id, "at": m.at.isoformat(), "role": m.role, "content": m.content, "actions": m.actions, "usage": m.usage or {}}
                for m in db.query(ChatMessage).filter(ChatMessage.thread_id == thread_id).order_by(ChatMessage.id)]


class ChatIn(BaseModel):
    text: str


@app.post("/chat/threads/{thread_id}")
def chat_say(thread_id: int, m: ChatIn, request: Request, user=Depends(current_user)) -> dict:
    from app import chat
    check_origin(request)
    if not m.text.strip():
        raise HTTPException(422, "empty message")
    try:
        return chat.ask(user, thread_id, m.text.strip())
    except PermissionError:
        raise HTTPException(404)


@app.delete("/chat/threads/{thread_id}")
def chat_delete(thread_id: int, request: Request, user=Depends(current_user)) -> dict:
    from app.db import ChatMessage, ChatThread
    check_origin(request)
    with session() as db:
        t = db.get(ChatThread, thread_id)
        if t is None or t.user_id != user.id:
            raise HTTPException(404)
        db.query(ChatMessage).filter(ChatMessage.thread_id == thread_id).delete()
        db.delete(t)
    return {"id": thread_id, "deleted": True}


@app.post("/references")
async def upload_reference(request: Request, title: str = Form(""), scope: str = Form("shared"), description: str = Form(""),
                           kind: str = Form("policy"), enforcement: str = Form("strict"),
                           file: UploadFile = File(...), user=Depends(current_user)) -> dict:
    """A desk user uploads a reference policy/procedure (PDF, DOCX, Markdown, text)."""
    from app import references
    check_origin(request)
    data = await file.read()
    try:
        with session() as db:
            doc = references.add(title or file.filename, file.filename or "document", data, by=user.email, scope=scope,
                                 description=description, mime=file.content_type or "", kind=kind, enforcement=enforcement, db=db)
            audit(db, user.email, "reference.upload", target=doc.id, detail=f"{doc.title} ({doc.chunks} passages, {scope}, {kind}/{enforcement})")
            return {"id": doc.id, "title": doc.title, "chunks": doc.chunks, "scope": doc.scope}
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/references")
def list_references(user=Depends(current_user)) -> list[dict]:
    from app import references
    return references.listing()


@app.delete("/references/{doc_id}")
def delete_reference(doc_id: int, request: Request, user=Depends(current_user)) -> dict:
    from app import references
    if user.role != "admin":
        raise HTTPException(403, "admin only")
    check_origin(request)
    with session() as db:
        if not references.remove(doc_id, by=user.email, db=db):
            raise HTTPException(404)
        audit(db, user.email, "reference.remove", target=doc_id)
    return {"id": doc_id, "status": "removed"}


@app.get("/references/search")
def search_references_api(q: str, tier: str = "L1", user=Depends(current_user)) -> list[dict]:
    from app import references
    return references.search(tier, q)


@app.get("/sops")
def list_sops(status: str | None = None, user=Depends(current_user)) -> list[dict]:
    from app import knowledge
    return knowledge.listing(status)


class SopIn(BaseModel):
    tier: str
    title: str
    content: str
    publish: bool = True


@app.post("/sops")
def create_sop(s: SopIn, request: Request, user=Depends(current_user)) -> dict:
    """A desk user adds a SOP. Admins publish directly; agents' entries wait for an admin."""
    from app import knowledge
    check_origin(request)
    if s.tier not in knowledge.TIERS:
        raise HTTPException(422, "tier must be L1..L4 or shared")
    status = "published" if (s.publish and user.role == "admin") else "draft"
    key = knowledge.write(s.tier, s.title, s.content, author=user.email, source="human", status=status)
    with session() as db:
        audit(db, user.email, "sop.create", target=key, detail=f"{s.tier}: {s.title} [{status}]")
    return {"key": key, "status": status}


class SopEdit(BaseModel):
    title: str
    content: str
    tier: str | None = None


@app.put("/sops/{key:path}")
def edit_sop(key: str, s: SopEdit, request: Request, user=Depends(current_user)) -> dict:
    from app import knowledge
    check_origin(request)
    if not knowledge.update(key, s.title, s.content, by=user.email, tier=s.tier):
        raise HTTPException(404)
    with session() as db:
        audit(db, user.email, "sop.edit", target=key, detail=s.title)
    return {"key": key, "ok": True}


class SopStatus(BaseModel):
    status: str          # published | retired | draft


@app.post("/sops/{key:path}/status")
def sop_status(key: str, s: SopStatus, request: Request, user=Depends(current_user)) -> dict:
    """Publish / retire (admin only)."""
    from app import knowledge
    from app.supervisor import publish_draft
    if user.role != "admin":
        raise HTTPException(403, "admin only")
    check_origin(request)
    with session() as db:
        sop = knowledge.get(key, db=db)
        if sop is None:
            raise HTTPException(404)
        if s.status == "published":
            publish_draft(db, sop, by=user.name)
        elif not knowledge.set_status(key, s.status, user.name, db=db):
            raise HTTPException(422, "bad status")
        audit(db, user.email, f"sop.{s.status}", target=key, detail=sop.title)
    return {"key": key, "status": s.status}


class LessonDecision(BaseModel):
    status: str          # approved | rejected | suspended
    note: str = ""


@app.post("/lessons/{lesson_id}/status")
def lesson_status(lesson_id: int, d: LessonDecision, request: Request, user=Depends(current_user)) -> dict:
    """An admin approves, rejects or suspends a lesson the desk proposed (approval re-runs the safety check)."""
    from app import knowledge
    if user.role != "admin":
        raise HTTPException(403, "admin only")
    check_origin(request)
    with session() as db:
        r = knowledge.decide_lesson(lesson_id, d.status, by=user.email, note=d.note, db=db)
        if not r["ok"]:
            raise HTTPException(422, r["error"])
        audit(db, user.email, f"lesson.{d.status}", target=str(lesson_id), detail=d.note)
    return r


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
    from starlette.concurrency import run_in_threadpool
    raw = await video.read()
    return await run_in_threadpool(_video_sync, requester, text, raw)


def _video_sync(requester: str, text: str, raw: bytes) -> dict:
    from app.vision import sample_frames
    frames = sample_frames(raw)
    if not frames:
        raise HTTPException(422, "could not read video")
    with session() as db:
        # first frame rides through the normal image path; the rest are appended as extra parts
        caption = (text or "(sent a video)") + f" [video: {len(frames)} frames sampled, 2s apart]"
        from app.agents import AGENTS, handle_message
        ticket, answer = handle_message(db, requester, caption, frames[0], "image/jpeg",
                                        extra_images=frames[1:])
        return {"ticket_id": ticket.id if ticket else None, "frames": len(frames), "reply": answer}


@app.post("/camera/{camera_id}", dependencies=[Depends(api_key)])
async def post_camera_frame(camera_id: str, image: UploadFile = File(...)) -> dict:
    from starlette.concurrency import run_in_threadpool
    from app.camera import process_frame
    data, mime = await image.read(), image.content_type or "image/jpeg"
    try:
        return {"findings": await run_in_threadpool(process_frame, camera_id, data, mime)}
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
                "assignee": t.assignee,
                "channel": t.channel,
                "linked": [c.id for c in db.query(Ticket).filter(Ticket.incident_id == t.id)],
                "resolved_at": t.resolved_at.isoformat() if t.resolved_at else None, "timing": timing(t, t.events),
                "ai": __import__("app.usage", fromlist=["summary"]).summary(scope_kind="ticket", scope_id=t.id),
                "events": [{"at": e.at.isoformat(), "actor": e.actor, "kind": e.kind,
                            "content": e.content, "data": e.data} for e in t.events]}
