"""Desk Chat: a desk user talks to the desk's brain. It knows everything the console knows (tickets, who
holds what, SLA risk, approvals, drafts, Supervisor runs, knowledge, references) and can act with the
signed-in user's authority (assign, reply, close, approve, run the Supervisor, publish a draft). Every action
is audited as the user. Answers carry links the console renders (#12 → ticket, /knowledge, /settings…).

Reasoning: Muse Wonder (heavy)."""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from app.db import Approval, ChatMessage, ChatThread, Sop, SupervisorRun, Ticket, _now, audit, log, session
from app.llm import ContentFiltered, get_llm
from app.tools import STR, Ctx, run_tool, tool, tools_for

logger = logging.getLogger(__name__)
DESK = ("DESK",)
MAX_STEPS = 12
HISTORY = 16


# ---- read tools -------------------------------------------------------------------------------

@tool("Snapshot of the desk right now: open tickets by owner, who is working on what, waiting on whom, SLA at "
      "risk, pending approvals, SOP drafts waiting, last Supervisor run.", DESK)
def desk_overview(ctx: Ctx) -> dict:
    from app.sla import at_risk
    db = ctx.db
    open_ = db.query(Ticket).filter(Ticket.status != "resolved").order_by(Ticket.id.desc()).all()
    by_owner: dict = {}
    for t in open_:
        last = t.events[-1] if t.events else None
        by_owner.setdefault(t.owner, []).append({
            "id": t.id, "title": t.title[:80], "status": t.status, "requester": t.requester,
            "last_activity": last.at.isoformat(timespec="minutes") if last else None,
            "last": (last.content[:100] if last else "")})
    approvals = [{"id": a.id, "ticket": a.ticket_id, "approver": a.approver, "request": a.request[:120]}
                 for a in db.query(Approval).filter(Approval.decision.is_(None))]
    drafts = [{"key": s.key, "tier": s.tier, "title": s.title, "by": s.author} for s in db.query(Sop).filter(Sop.status == "draft")]
    run = db.query(SupervisorRun).order_by(SupervisorRun.id.desc()).first()
    since = _now() - timedelta(hours=24)
    resolved_24h = db.query(Ticket).filter(Ticket.resolved_at >= since).count()
    created_24h = db.query(Ticket).filter(Ticket.created_at >= since).count()
    me = ctx.user
    mine = [{"id": t.id, "title": t.title[:80], "status": t.status} for t in open_ if t.assignee and t.assignee in (me.name, me.email)]
    return {"ok": True, "you": {"name": me.name, "role": me.role, "your_open_tickets": mine},
            "open_by_owner": by_owner, "open_total": len(open_), "created_24h": created_24h, "resolved_24h": resolved_24h,
            "sla_at_risk": at_risk(db)[:10], "pending_approvals": approvals, "sop_drafts": drafts,
            "supervisor_last_run": {"at": run.started_at.isoformat(timespec="minutes"), "summary": run.summary[:400],
                                    "actions": len(run.actions or [])} if run else None,
            "links": {"ticket": "/tickets?id=<id>", "tickets": "/tickets", "knowledge": "/knowledge",
                      "settings_sla": "/settings?tab=sla", "settings_supervisor": "/settings?tab=supervisor", "reports": "/settings?tab=reports"}}


@tool("List tickets with filters.", DESK, status=STR, owner=STR, query=STR)
def find_tickets(ctx: Ctx, status: str = "", owner: str = "", query: str = "") -> dict:
    q = ctx.db.query(Ticket).order_by(Ticket.id.desc())
    if status:
        q = q.filter(Ticket.status == status)
    if owner:
        q = q.filter(Ticket.owner == owner)
    if query:
        q = q.filter(Ticket.title.contains(query) | Ticket.requester.contains(query))
    return {"ok": True, "tickets": [{"id": t.id, "title": t.title[:80], "status": t.status, "owner": t.owner,
                                     "requester": t.requester, "opened": t.created_at.isoformat(timespec="minutes")} for t in q.limit(30)]}


@tool("Read a ticket's trail (messages, tool calls, notes, handoffs) and its SLA timing.", DESK, ticket_id={"type": "integer"})
def read_ticket(ctx: Ctx, ticket_id: int) -> dict:
    from app.sla import timing
    t = ctx.db.get(Ticket, int(ticket_id))
    if t is None:
        return {"ok": False, "error": "no such ticket"}
    trail = [f"{e.at:%H:%M} {e.actor}/{e.kind}: {e.content[:200]}" for e in t.events
             if e.kind in ("message", "handoff", "note") or (e.kind == "tool_call")]
    return {"ok": True, "id": t.id, "title": t.title, "status": t.status, "owner": t.owner, "requester": t.requester,
            "verified": t.verified, "resolution": t.resolution, "rating": t.rating, "incident_id": t.incident_id,
            "timing": timing(t, t.events), "trail": trail[-40:], "link": f"/tickets?id={t.id}"}


@tool("Search the SOP knowledge base and the reference documents.", DESK, query=STR)
def search_desk_knowledge(ctx: Ctx, query: str) -> dict:
    from app import knowledge, references
    return {"ok": True, "sops": [{k: v for k, v in s.items() if k != "content"} | {"excerpt": s["content"][:300]}
                                 for s in knowledge.search("shared", query, limit=4, db=ctx.db)],
            "references": references.search("L1", query, limit=4, db=ctx.db)}


@tool("Desk users (people who can be assigned work).", DESK)
def desk_people(ctx: Ctx) -> dict:
    from app.db import User
    return {"ok": True, "people": [{"name": u.name, "email": u.email, "role": u.role}
                                   for u in ctx.db.query(User).filter(User.active.is_(True))]}


# ---- action tools (as the signed-in user) ---------------------------------------------------------

def _user(ctx: Ctx):
    return ctx.user  # set by the chat loop


@tool("Assign a ticket to an agent tier (L1-L4), to 'me' (the person chatting) or to a named desk user, with a note. "
      "An agent takes over at once; a person gets it as escalated.", DESK, ticket_id={"type": "integer"}, to=STR, note=STR)
def assign(ctx: Ctx, ticket_id: int, to: str, note: str = "") -> dict:
    from app.agents import AGENTS
    from app.db import Outbound
    t = ctx.db.get(Ticket, int(ticket_id))
    if t is None:
        return {"ok": False, "error": "no such ticket"}
    user = _user(ctx)
    who = user.name if to.lower() in ("me", "myself") else to
    if to in AGENTS:
        t.owner = to
        if t.status in ("escalated", "resolved"):
            t.status = "open"
        log(ctx.db, t.id, f"desk:{user.name}", "note", f"assigned to {to} via Desk Chat. {note}".strip())
        answer = AGENTS[to].run(ctx.db, t, f"[Assigned to you by {user.name} from the desk] {note or 'Take over this ticket.'} "
                                           f"Review the trail, act with your tools, and report what you did.")
        log(ctx.db, t.id, to, "message", answer)
        if to == "L1" and t.requester.startswith("+"):
            ctx.db.add(Outbound(to=t.requester, ticket_id=t.id, text=answer))
        audit(ctx.db, user.email, "ticket.assign", target=t.id, detail=f"chat → {to} {note}".strip())
        return {"ok": True, "owner": to, "agent_reply": answer[:300], "link": f"/tickets?id={t.id}"}
    t.status, t.owner, t.assignee = "escalated", "human", who
    log(ctx.db, t.id, f"desk:{user.name}", "note", f"assigned to {who} via Desk Chat. {note}".strip())
    audit(ctx.db, user.email, "ticket.assign", target=t.id, detail=f"chat → human:{who} {note}".strip())
    return {"ok": True, "owner": "human", "assignee": who, "link": f"/tickets?id={t.id}"}


@tool("Write to the employee on a ticket (sent over WhatsApp, logged on the trail).", DESK, ticket_id={"type": "integer"}, text=STR)
def message_employee(ctx: Ctx, ticket_id: int, text: str) -> dict:
    from app.db import Outbound
    t = ctx.db.get(Ticket, int(ticket_id))
    if t is None:
        return {"ok": False, "error": "no such ticket"}
    user = _user(ctx)
    log(ctx.db, t.id, f"desk:{user.name}", "message", text)
    if t.requester.startswith("+"):
        ctx.db.add(Outbound(to=t.requester, ticket_id=t.id, text=text))
    audit(ctx.db, user.email, "ticket.reply", target=t.id, detail=text)
    return {"ok": True, "sent": t.requester.startswith("+")}


@tool("Change a ticket's status (open, waiting_user, escalated, resolved) with a note; resolved needs a resolution.",
      DESK, ticket_id={"type": "integer"}, status=STR, note=STR)
def set_ticket_status(ctx: Ctx, ticket_id: int, status: str, note: str = "") -> dict:
    from app.db import Outbound, resolve
    from app.i18n import t as tr_, ticket_lang
    t = ctx.db.get(Ticket, int(ticket_id))
    if t is None or status not in ("open", "waiting_user", "waiting_approval", "escalated", "resolved"):
        return {"ok": False, "error": "no such ticket or bad status"}
    user = _user(ctx)
    old = t.status
    if status == "resolved":
        resolve(t)
        t.resolution = note or "closed by the service desk"
        if t.requester.startswith("+"):
            ctx.db.add(Outbound(to=t.requester, ticket_id=t.id, text=tr_("closed_by_desk", ticket_lang(t), id=t.id, note=t.resolution)))
    else:
        t.status = status
        if status == "escalated":
            t.owner = "human"
    log(ctx.db, t.id, f"desk:{user.name}", "note", f"status {old} → {status} via Desk Chat" + (f": {note}" if note else ""))
    audit(ctx.db, user.email, "ticket.status", target=t.id, detail=f"chat {old} -> {status} {note}".strip())
    return {"ok": True, "status": status, "link": f"/tickets?id={t.id}"}


@tool("Decide a pending approval on behalf of the desk (approved or denied). The employee's ticket continues.",
      DESK, approval_id={"type": "integer"}, decision=STR)
def decide_approval(ctx: Ctx, approval_id: int, decision: str) -> dict:
    from app.agents import AGENTS
    from app.db import Outbound
    a = ctx.db.get(Approval, int(approval_id))
    if a is None or a.decision is not None or decision not in ("approved", "denied"):
        return {"ok": False, "error": "no pending approval with that id, or bad decision"}
    user = _user(ctx)
    a.decision, a.decided_at = decision, _now()
    t = ctx.db.get(Ticket, a.ticket_id)
    t.status = "open"
    log(ctx.db, t.id, f"desk:{user.name}", "note", f"approval #{a.id} {decision} by the desk via Desk Chat: {a.request}")
    answer = AGENTS["L1"].run(ctx.db, t, f"[Approval #{a.id} {decision} by {user.name} (service desk)] Request was: {a.request}. "
                                         f"Continue the ticket accordingly and tell the employee.")
    log(ctx.db, t.id, "L1", "message", answer)
    if t.requester.startswith("+"):
        ctx.db.add(Outbound(to=t.requester, ticket_id=t.id, text=answer))
    audit(ctx.db, user.email, "approval.decide", target=a.id, detail=f"chat {decision}")
    return {"ok": True, "ticket": t.id, "agent_reply": answer[:300], "link": f"/tickets?id={t.id}"}


@tool("Publish a SOP draft (admins only).", DESK, key=STR)
def publish_sop(ctx: Ctx, key: str) -> dict:
    from app import knowledge
    from app.supervisor import publish_draft
    user = _user(ctx)
    if user.role != "admin":
        return {"ok": False, "error": "only admins publish SOPs"}
    s = knowledge.get(key, db=ctx.db)
    if s is None:
        return {"ok": False, "error": "no such SOP"}
    publish_draft(ctx.db, s, by=user.name)
    audit(ctx.db, user.email, "sop.published", target=key, detail="via Desk Chat")
    return {"ok": True, "key": key, "link": "/knowledge"}


@tool("Run the Supervisor now (full desk review). Takes up to a minute; returns its summary.", DESK)
def run_supervisor_now(ctx: Ctx) -> dict:
    from app import supervisor
    user = _user(ctx)
    audit(ctx.db, user.email, "supervisor.run", detail="via Desk Chat")
    ctx.db.flush()
    r = supervisor.run("manual")
    return {"ok": not r.error, "summary": r.summary, "actions": [a["tool"] for a in r.actions], "error": r.error}


# ---- the conversation ------------------------------------------------------------------------------

SYSTEM = """You are the brain of Muse ServiceDesk, talking to {name} ({role}) from the service desk console. You know who
they are (below): address them by name when natural, relate answers to their tickets, their role and what they
did recently, and when they ask "what should I do" start from what is theirs.
You know the whole desk through your tools and you can act with this person's authority. Be concise, factual
and specific: numbers, ticket ids, owners, times. Never invent a ticket, a person or a rule; call a tool.
When the person should look at something, give a link: tickets as [#12](/tickets?id=12), the queue as
[Tickets](/tickets), the knowledge base as [Knowledge](/knowledge), SLA targets as [SLA settings](/settings?tab=sla),
the Supervisor as [Supervisor settings](/settings?tab=supervisor), reports as [Reports](/settings?tab=reports).
If the person wants to get involved, offer the concrete step (assign to me, approve #3, reply to the employee,
escalate) and do it when they say so. Confirm before destructive or irreversible actions (closing tickets,
denying approvals). Answer in the language the person writes in. Use Markdown: short paragraphs, lists, bold ids."""


def person_context(db, user) -> str:
    """What the brain knows about the person it is talking to: role, their tickets, their recent actions,
    their drafts, what waits for them. Rebuilt every turn so it is current."""
    from app.db import Audit, User
    since = _now() - timedelta(hours=24)
    u = db.get(User, user.id)
    mine = db.query(Ticket).filter(Ticket.status != "resolved", Ticket.assignee.in_([user.name, user.email])).all()
    recent = db.query(Audit).filter(Audit.user == user.email, Audit.at >= since).order_by(Audit.id.desc()).limit(8).all()
    drafts = db.query(Sop).filter(Sop.status == "draft").all()
    my_drafts = [s for s in drafts if s.author == user.email]
    approvals = db.query(Approval).filter(Approval.decision.is_(None)).count()
    lines = [f"Name: {user.name} · e-mail: {user.email} · role: {user.role} · desk user since {u.created_at:%d %b %Y}" if u else f"Name: {user.name} · role: {user.role}"]
    lines.append("Their open tickets (assigned to them): " + (", ".join(f"#{t.id} {t.title[:50]} [{t.status}]" for t in mine) if mine else "none"))
    lines.append("Their actions in the last 24h: " + (", ".join(f"{a.action}{' ' + str(a.target) if a.target else ''}" for a in recent) if recent else "none"))
    if my_drafts:
        lines.append("SOP drafts they wrote: " + ", ".join(s.key for s in my_drafts))
    if user.role == "admin":
        lines.append(f"Waiting for an admin: {len(drafts)} SOP draft(s), {approvals} pending approval(s) desk-wide.")
    return "\n".join(lines)


def _history(db, thread_id: int) -> list[dict]:
    rows = db.query(ChatMessage).filter(ChatMessage.thread_id == thread_id).order_by(ChatMessage.id.desc()).limit(HISTORY).all()
    return [{"role": m.role, "content": m.content} for m in reversed(rows)]


def ask(user, thread_id: int, text: str) -> dict:
    """One turn: store the question, run the brain with tools, store and return the answer."""
    llm = get_llm()
    actions: list[dict] = []
    with session() as db:
        th = db.get(ChatThread, thread_id)
        if th is None or th.user_id != user.id:
            raise PermissionError("not your conversation")
        if th.title == "New conversation":
            th.title = text.strip()[:80]
        db.add(ChatMessage(thread_id=th.id, role="user", content=text))
        db.flush()
        ctx = Ctx(db=db, ticket=None, agent="DESK")
        ctx.user = user
        specs = [t.spec() for t in tools_for("DESK")]
        messages = [{"role": "system", "content": SYSTEM.format(name=user.name, role=user.role) + "\n\nAbout the person you are talking to (current):\n" + person_context(db, user)},
                    *_history(db, th.id)]
        answer = ""
        from app import usage
        _tok = usage._scope.set(("chat", th.id, "DESK"))
        try:
            for _ in range(MAX_STEPS):
                reply = llm.chat(messages, specs, heavy=True)
                if not reply.tool_calls:
                    answer = reply.text.strip()
                    break
                messages.append({"role": "assistant", "content": reply.text or None, "tool_calls": [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)}}
                    for c in reply.tool_calls]})
                for call in reply.tool_calls:
                    result = run_tool(ctx, call.name, call.arguments)
                    actions.append({"tool": call.name, "args": call.arguments, "ok": result.get("ok", True)})
                    messages.append({"role": "tool", "tool_call_id": call.id,
                                     "content": json.dumps(result, ensure_ascii=False, default=str)[:14000]})
            else:
                answer = "I ran out of steps while working on that; here is what I did: " + ", ".join(a["tool"] for a in actions)
        except ContentFiltered:
            answer = "The model declined that phrasing; could you say it differently?"
        finally:
            usage._scope.reset(_tok)
        turn = usage.summary(scope_kind="chat", scope_id=th.id)
        last = getattr(llm, "last_usage", None) or {}
        msg_usage = {"calls": len(actions) + 1, "prompt_tokens": last.get("prompt_tokens", 0), "completion_tokens": last.get("completion_tokens", 0),
                     "latency_ms": last.get("latency_ms", 0), "context_pct": last.get("context_pct"), "context_limit": last.get("context_limit"),
                     "model": last.get("model", "")}
        db.add(ChatMessage(thread_id=th.id, role="assistant", content=answer, actions=actions, usage=msg_usage))
        th.updated_at = _now()
        db.flush()
        return {"thread_id": th.id, "answer": answer, "actions": actions, "usage": msg_usage,
                "thread_usage": {"calls": turn["calls"], "total_tokens": turn["total_tokens"]}}
