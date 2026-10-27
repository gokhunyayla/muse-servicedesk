"""The Supervisor: a desk-manager agent that periodically reviews every open ticket, merges emerging
problems, spots where the tier agents are failing, researches the gap on the web and writes a draft SOP
with Muse Wonder, and — when only a person can help — opens a ticket for a human with a precise ask.

All reasoning is Meta Model API (Muse Wonder for the pass, Muse Spark for quick checks); the web is just
a data source the model reads."""
from __future__ import annotations

import html
import json
import logging
import os
import re
from collections import Counter
from datetime import timedelta

import httpx

from app.db import Event, SopDraft, SupervisorRun, Ticket, _now, log, session
from app.llm import ContentFiltered, get_llm
from app.sla import DEFAULT_SLA, get_setting, put_setting, timing
from app.tools import STR, Ctx, run_tool, tool, tools_for

logger = logging.getLogger(__name__)
SUP = ("SUP",)
DEFAULT_SUPERVISOR = {
    "mode": "manual",           # manual (Run now only) | auto (on the cadence below)
    "every_minutes": 10,        # cadence when mode is auto (Settings → Supervisor)
    "window_hours": 24,         # how far back to look for emerging problems
    "cluster_min": 3,           # similar open tickets that make a problem
    "stuck_minutes": 60,        # open ticket with no activity for this long = stuck
    "auto_publish_sops": False, # otherwise drafts wait for an admin at /knowledge
    "search_provider": "ddg",   # ddg (no key) | tavily (TAVILY_API_KEY)
    "max_research": 2,          # research tasks per run
}
MAX_STEPS = 16


def config() -> dict:
    return get_setting("supervisor", DEFAULT_SUPERVISOR)


# ---- deterministic pre-analysis (cheap, no model) --------------------------------------------

def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"\w+", text.lower()) if len(t) > 3}


def emerging_clusters(db, cfg: dict) -> list[dict]:
    """Groups of open, unlinked tickets with overlapping titles inside the window."""
    since = _now() - timedelta(hours=cfg["window_hours"])
    open_ = [t for t in db.query(Ticket).filter(Ticket.status != "resolved", Ticket.created_at >= since,
                                                 Ticket.incident_id.is_(None)).all()
             if not t.title.startswith(("[INCIDENT]", "[Proactive", "[Compliance", "[Supervisor"))]
    groups: list[list[Ticket]] = []
    for t in open_:
        tk = _tokens(t.title)
        for g in groups:
            if any(len(tk & _tokens(o.title)) >= 2 for o in g):
                g.append(t)
                break
        else:
            groups.append([t])
    out = []
    for g in groups:
        if len(g) >= cfg["cluster_min"] and len({t.requester for t in g}) >= 2:
            common = Counter(w for t in g for w in _tokens(t.title)).most_common(4)
            out.append({"ticket_ids": [t.id for t in g], "requesters": len({t.requester for t in g}),
                        "keywords": [w for w, _ in common], "titles": [t.title[:80] for t in g[:5]]})
    return out


def failure_signals(db, cfg: dict) -> list[dict]:
    """Where the tier agents are not coping: stuck, escalated, SLA breaches, tool errors, low ratings."""
    out = []
    since = _now() - timedelta(hours=cfg["window_hours"] * 7)
    for t in db.query(Ticket).filter(Ticket.created_at >= since).all():
        ev = t.events
        last = ev[-1].at if ev else t.created_at
        sig = []
        if t.status in ("open", "waiting_approval") and (_now() - last) > timedelta(minutes=cfg["stuck_minutes"]):
            sig.append(f"stuck: no activity for {int((_now() - last).total_seconds() // 60)} min")
        if t.status == "escalated":
            sig.append("escalated to a human: " + (t.resolution or "")[:160])
        errs = [e.content for e in ev if e.kind == "tool_result" and isinstance(e.data, dict) and e.data.get("ok") is False]
        if len(errs) >= 2:
            sig.append("tool errors: " + ", ".join(sorted(set(errs))[:5]))
        if t.rating is not None and t.rating <= 2:
            sig.append(f"low rating {t.rating}/5")
        from app.sla import risk
        r = risk(t, ev)
        if r:
            sig.append(f"SLA {'breached' if r['breached'] else 'at risk'}: {r['metric']} at {r['pct']}% of target "
                       f"({'human' if r['who'] == 'human' else 'agent ' + str(r['owner'])})")
        if any(e.kind == "note" and "content filter" in e.content for e in ev):
            sig.append("model request blocked by content filter")
        if sig:
            out.append({"ticket_id": t.id, "title": t.title[:100], "status": t.status, "owner": t.owner,
                        "signals": sig})
    return out[:12]


# ---- tools the Supervisor may call -----------------------------------------------------------

@tool("Read a ticket's trail: messages, tool calls, notes (compact).", SUP, ticket_id={"type": "integer"})
def read_ticket(ctx: Ctx, ticket_id: int) -> dict:
    t = ctx.db.get(Ticket, int(ticket_id))
    if t is None:
        return {"ok": False, "error": "no such ticket"}
    trail = [f"{e.actor}/{e.kind}: {e.content[:160]}" for e in t.events if e.kind in ("message", "handoff", "note")
             or (e.kind == "tool_result" and isinstance(e.data, dict) and e.data.get("ok") is False)]
    return {"ok": True, "id": t.id, "title": t.title, "status": t.status, "owner": t.owner,
            "requester": t.requester, "trail": trail[-30:]}


@tool("Merge tickets into one problem record (the oldest becomes the record) and send one update to "
      "everyone affected.", SUP, ticket_ids={"type": "array", "items": {"type": "integer"}}, title=STR, update=STR)
def create_problem(ctx: Ctx, ticket_ids: list[int], title: str, update: str) -> dict:
    from app.db import Outbound
    ts = [ctx.db.get(Ticket, int(i)) for i in sorted(set(ticket_ids))]
    ts = [t for t in ts if t is not None]
    if len(ts) < 2:
        return {"ok": False, "error": "need at least two tickets"}
    head, rest = ts[0], ts[1:]
    head.title = f"[INCIDENT] {title}"
    if head.status == "resolved":
        head.status = "open"
    log(ctx.db, head.id, "supervisor", "note", f"problem record created by the Supervisor from {[t.id for t in ts]}")
    for t in rest:
        t.incident_id = head.id
        log(ctx.db, t.id, "supervisor", "note", f"linked to problem #{head.id} by the Supervisor")
    for r in {t.requester for t in ts if t.requester.startswith("+")}:
        ctx.db.add(Outbound(to=r, ticket_id=head.id, text=f"[#{head.id}] {update}"))
    ctx.db.flush()
    return {"ok": True, "problem_id": head.id, "linked": [t.id for t in rest]}


@tool("Search the web (results: title, url, snippet). Use precise, vendor-specific queries.", SUP, query=STR)
def web_search(ctx: Ctx, query: str) -> dict:
    return {"ok": True, "query": query, "results": search_web(query)}


@tool("Fetch a web page and return its readable text (truncated).", SUP, url=STR)
def fetch_page(ctx: Ctx, url: str) -> dict:
    try:
        r = httpx.get(url, timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 MuseServiceDesk"})
        text = _readable(r.text)
        return {"ok": True, "url": url, "text": text[:6000]}
    except Exception as e:   # the model should move on to the next source
        return {"ok": False, "error": str(e)[:200]}


@tool("Propose a SOP for a tier, written from what you researched: symptom, checks, fix steps with the "
      "exact tools/scripts, when to hand off. Cite sources. An admin publishes it (unless auto-publish is on).",
      SUP, tier=STR, title=STR, content=STR, sources={"type": "array", "items": STR}, reason=STR,
      ticket_ids={"type": "array", "items": {"type": "integer"}})
def propose_sop(ctx: Ctx, tier: str, title: str, content: str, sources: list[str], reason: str,
                ticket_ids: list[int] | None = None) -> dict:
    if tier not in ("L1", "L2", "L3", "L4"):
        return {"ok": False, "error": "tier must be L1..L4"}
    d = SopDraft(tier=tier, title=title[:255], content=content, sources=list(sources or []), reason=reason,
                 ticket_ids=list(ticket_ids or []))
    ctx.db.add(d)
    ctx.db.flush()
    if config().get("auto_publish_sops"):
        publish_draft(ctx.db, d, by="supervisor")
    return {"ok": True, "draft_id": d.id, "status": d.status}


@tool("Only a person can unblock this (access, purchase, policy, on-site work, vendor contact): open a "
      "ticket for the desk stating exactly what is needed and why, with the affected tickets.",
      SUP, need=STR, why=STR, ticket_ids={"type": "array", "items": {"type": "integer"}})
def open_human_ticket(ctx: Ctx, need: str, why: str, ticket_ids: list[int] | None = None) -> dict:
    t = Ticket(requester="supervisor", title=f"[Supervisor] {need[:230]}", status="escalated", owner="human")
    ctx.db.add(t)
    ctx.db.flush()
    log(ctx.db, t.id, "supervisor", "message", f"Need: {need}\n\nWhy: {why}\n\nAffected tickets: {ticket_ids or []}")
    for i in ticket_ids or []:
        o = ctx.db.get(Ticket, int(i))
        if o:
            log(ctx.db, o.id, "supervisor", "note", f"Supervisor asked a human for help in #{t.id}: {need[:120]}")
    return {"ok": True, "ticket_id": t.id}


@tool("Hand a ticket to another tier (L1-L4) with instructions; that agent takes over immediately. Use when the "
      "current owner is stuck or the wrong tier holds it.", SUP, ticket_id={"type": "integer"}, tier=STR, instructions=STR)
def reassign_ticket(ctx: Ctx, ticket_id: int, tier: str, instructions: str) -> dict:
    from app.agents import AGENTS
    t = ctx.db.get(Ticket, int(ticket_id))
    if t is None:
        return {"ok": False, "error": "no such ticket"}
    if tier not in AGENTS:
        return {"ok": False, "error": "tier must be L1..L4"}
    old = t.owner
    t.owner = tier
    if t.status in ("escalated", "resolved"):
        t.status = "open"
    log(ctx.db, t.id, "supervisor", "note", f"reassigned {old} → {tier} by the Supervisor: {instructions}")
    answer = AGENTS[tier].run(ctx.db, t, f"[Reassigned to you by the Supervisor] {instructions} Review the trail, act with "
                                         f"your tools, and report what you did.")
    log(ctx.db, t.id, tier, "message", answer)
    if tier == "L1" and t.requester.startswith("+"):
        from app.db import Outbound
        ctx.db.add(Outbound(to=t.requester, ticket_id=t.id, text=answer))
    return {"ok": True, "owner": tier, "result": answer[:400]}


@tool("Message the employee on a ticket that is waiting for them (confirmation, information, consent) so the "
      "clock moves. Short and polite, in their language.", SUP, ticket_id={"type": "integer"}, text=STR)
def nudge_employee(ctx: Ctx, ticket_id: int, text: str) -> dict:
    from app.db import Outbound
    t = ctx.db.get(Ticket, int(ticket_id))
    if t is None:
        return {"ok": False, "error": "no such ticket"}
    if not t.requester.startswith("+"):
        return {"ok": False, "error": "requester has no phone"}
    ctx.db.add(Outbound(to=t.requester, ticket_id=t.id, text=text))
    log(ctx.db, t.id, "supervisor", "message", text)
    return {"ok": True}


@tool("Remind the approver of a pending approval on this ticket (sends the ONAY/RET prompt again).", SUP,
      ticket_id={"type": "integer"})
def remind_approver(ctx: Ctx, ticket_id: int) -> dict:
    from app import systems
    from app.db import Approval, Outbound
    a = (ctx.db.query(Approval).filter(Approval.ticket_id == int(ticket_id), Approval.decision.is_(None))
         .order_by(Approval.id.desc()).first())
    if a is None:
        return {"ok": False, "error": "no pending approval on this ticket"}
    phone = systems.USERS.get(a.approver, {}).get("phone")
    if not phone:
        return {"ok": False, "error": "approver has no phone"}
    ctx.db.add(Outbound(to=phone, ticket_id=a.ticket_id,
                        text=f"Hatırlatma: onayınız bekleniyor (#{a.id}): {a.request[:200]} — \"ONAY {a.id}\" / \"RET {a.id}\""))
    log(ctx.db, a.ticket_id, "supervisor", "note", f"approver {a.approver} reminded about approval #{a.id}")
    return {"ok": True, "approval_id": a.id, "approver": a.approver}


@tool("Escalate a ticket to a human engineer now (SLA about to break or agents cannot progress), with a summary "
      "of what is needed.", SUP, ticket_id={"type": "integer"}, summary=STR)
def bump_to_human(ctx: Ctx, ticket_id: int, summary: str) -> dict:
    t = ctx.db.get(Ticket, int(ticket_id))
    if t is None:
        return {"ok": False, "error": "no such ticket"}
    t.status, t.owner = "escalated", "human"
    t.resolution = f"escalated by the Supervisor: {summary}"
    log(ctx.db, t.id, "supervisor", "note", f"escalated to a human by the Supervisor: {summary}")
    return {"ok": True, "status": "escalated"}


@tool("Leave a note on a ticket (what you noticed, what you did).", SUP, ticket_id={"type": "integer"}, text=STR)
def note_ticket(ctx: Ctx, ticket_id: int, text: str) -> dict:
    t = ctx.db.get(Ticket, int(ticket_id))
    if t is None:
        return {"ok": False, "error": "no such ticket"}
    log(ctx.db, t.id, "supervisor", "note", text)
    return {"ok": True}


# ---- web -------------------------------------------------------------------------------------

def _readable(page: str) -> str:
    page = re.sub(r"(?is)<(script|style|nav|footer|header|noscript).*?</\1>", " ", page)
    text = re.sub(r"(?s)<[^>]+>", " ", page)
    text = html.unescape(re.sub(r"\s+", " ", text))
    return text.strip()


def search_web(query: str, limit: int = 6) -> list[dict]:
    provider = config().get("search_provider", "ddg")
    try:
        from app import vault
        tavily = vault.get("TAVILY_API_KEY")
        if provider == "tavily" and tavily:
            r = httpx.post("https://api.tavily.com/search", timeout=20,
                           json={"api_key": tavily, "query": query, "max_results": limit})
            return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")[:300]}
                    for x in r.json().get("results", [])]
        r = httpx.get("https://html.duckduckgo.com/html/", params={"q": query}, timeout=20,
                      headers={"User-Agent": "Mozilla/5.0 MuseServiceDesk"})
        out = []
        for m in re.finditer(r'<a rel="nofollow" class="result__a" href="([^"]+)">(.*?)</a>.*?'
                             r'class="result__snippet"[^>]*>(.*?)</a>', r.text, re.S):
            url = html.unescape(m.group(1))
            if "uddg=" in url:
                from urllib.parse import parse_qs, unquote, urlparse
                url = unquote(parse_qs(urlparse(url).query).get("uddg", [url])[0])
            out.append({"title": _readable(m.group(2))[:120], "url": url, "snippet": _readable(m.group(3))[:300]})
            if len(out) >= limit:
                break
        return out
    except Exception as e:
        logger.warning("web search failed: %s", e)
        return []


# ---- drafts ----------------------------------------------------------------------------------

def publish_draft(db, d: SopDraft, by: str) -> str:
    from app import knowledge
    body = d.content.strip()
    if d.sources:
        body += "\n\nSources:\n" + "\n".join(f"- {s}" for s in d.sources)
    body += f"\n\n_Written by the Supervisor from research; published by {by}._"
    d.sop_id = knowledge.write(d.tier, d.title, body)
    d.status, d.decided_by = "published", by
    for i in d.ticket_ids or []:
        t = db.get(Ticket, int(i))
        if t:
            log(db, t.id, "supervisor", "note", f"SOP '{d.title}' published to {d.tier} ({d.sop_id})")
    return d.sop_id


# ---- the pass --------------------------------------------------------------------------------

PROMPT = """You are the Supervisor of an autonomous IT service desk (tiers L1-L4 are AI agents with tools;
humans only step in when asked). Review the desk and act. You have tools; use them, don't just describe.

Your duties, in order:
1. EMERGING PROBLEMS: when several employees report the same thing, create ONE problem record
   (create_problem) with a clear title and a first update to everyone. Don't merge unrelated tickets.
2. FAILURES: for each ticket where agents are failing (stuck, escalated, repeated tool errors, SLA breach,
   low rating), read_ticket and decide WHY: (a) a knowledge gap — the agents lacked a procedure;
   (b) a capability gap — no tool/access/person can do it; (c) just waiting on the employee (do nothing).
3. LEARN: for (a), research the real fix on the web (web_search, then fetch_page on 1-2 good sources,
   prefer vendor docs) and propose_sop for the right tier: concrete symptom → checks → fix steps naming the
   desk's own tools/scripts where they exist → when to hand off. At most {max_research} research tasks per run.
4. SLA: for tickets "at risk" or breached, act so the clock moves, with the least drastic step that works:
   waiting on the employee → nudge_employee; waiting on an approval → remind_approver; the owning tier is
   stuck or wrong → reassign_ticket with precise instructions; nothing an agent can do → bump_to_human.
   Human-owned tickets at risk → open_human_ticket (or note_ticket if one is already open for it).
5. ASK A HUMAN: for (b), open_human_ticket with exactly what is needed (access, purchase, policy decision,
   on-site action, vendor contact) and why, referencing the affected tickets. Don't open duplicates: if a
   [Supervisor] ticket for the same need is already open, note_ticket instead.
6. Finish with a short summary in English, PLAIN TEXT (no markdown, no bullets, no backticks): first one
   sentence with the outcome, then 2-5 short sentences on what you noticed, did and deliberately skipped.

Desk's own tools agents can use: {tools}
SOP pools already present: {sops}

Findings from the pre-analysis (facts, not conclusions):
Emerging clusters: {clusters}
Failure signals: {signals}
SLA at risk now (threshold {risk_pct}% of target): {at_risk}
Open [Supervisor] tickets: {open_sup}
"""


def run(trigger: str = "schedule") -> SupervisorRun:
    cfg = config()
    with session() as db:
        r = SupervisorRun(trigger=trigger)
        db.add(r)
        db.flush()
        run_id = r.id
    actions: list[dict] = []
    findings: dict = {}
    summary, error = "", ""
    try:
        with session() as db:
            clusters = emerging_clusters(db, cfg)
            signals = failure_signals(db, cfg)
            open_sup = [t.title for t in db.query(Ticket).filter(Ticket.title.like("[Supervisor]%"),
                                                                   Ticket.status != "resolved")]
            from app.sla import at_risk as _at_risk, sla_targets
            risks = _at_risk(db)
            findings = {"clusters": clusters, "signals": signals, "at_risk": risks}
            if not clusters and not signals and not risks:
                summary = "Nothing to do: no emerging problems and no failing tickets."
            else:
                from app import knowledge
                from app.tools import REGISTRY
                tool_names = sorted(n for n, t in REGISTRY.items() if any(x in t.tiers for x in ("L1", "L2", "L3", "L4")))
                sops = {tier: knowledge.titles(tier) for tier in ("L1", "L2", "L3", "L4")}
                prompt = PROMPT.format(max_research=cfg["max_research"], tools=", ".join(tool_names),
                                       sops=json.dumps(sops, ensure_ascii=False), clusters=json.dumps(clusters, ensure_ascii=False),
                                       signals=json.dumps(signals, ensure_ascii=False), open_sup=open_sup,
                                       at_risk=json.dumps(risks, ensure_ascii=False), risk_pct=sla_targets().get("at_risk_pct", 80))
                summary = _agent_loop(db, prompt, actions)
    except ContentFiltered:
        error = "model request blocked by content filter"
    except Exception as e:                      # a failing pass must never break the scheduler
        logger.exception("supervisor run failed")
        error = str(e)[:500]
    with session() as db:
        r = db.get(SupervisorRun, run_id)
        r.finished_at, r.summary, r.findings, r.actions, r.error = _now(), summary, findings, actions, error
        db.flush()
        db.refresh(r)
        return r


def _agent_loop(db, prompt: str, actions: list[dict]) -> str:
    llm = get_llm()
    ctx = Ctx(db=db, ticket=None, agent="SUP")
    specs = [t.spec() for t in tools_for("SUP")]
    messages = [{"role": "system", "content": "You act through tools and answer briefly."},
                {"role": "user", "content": prompt}]
    for _ in range(MAX_STEPS):
        reply = llm.chat(messages, specs, heavy=True)
        if not reply.tool_calls:
            return reply.text.strip()
        messages.append({"role": "assistant", "content": reply.text or None, "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)}}
            for c in reply.tool_calls]})
        for call in reply.tool_calls:
            result = run_tool(ctx, call.name, call.arguments)
            actions.append({"tool": call.name, "args": call.arguments, "ok": result.get("ok", True),
                            "result": {k: v for k, v in result.items() if k not in ("text", "results", "trail")}})
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": json.dumps(result, ensure_ascii=False, default=str)[:12000]})
    return "Stopped after the step budget; see actions."


def due(last: SupervisorRun | None, cfg: dict | None = None) -> bool:
    cfg = cfg or config()
    if cfg.get("mode", "manual") != "auto":
        return False
    if last is None or last.finished_at is None:
        return last is None
    return _now() - last.finished_at >= timedelta(minutes=int(cfg["every_minutes"]))


def stats(since=None) -> dict:
    """What the Supervisor did in a period: runs, problems merged, SOPs drafted/published, human asks, notes."""
    with session() as db:
        q = db.query(SupervisorRun)
        if since:
            q = q.filter(SupervisorRun.started_at >= since)
        runs = q.all()
        c = Counter()
        for r in runs:
            for a in r.actions or []:
                if a.get("ok"):
                    c[a["tool"]] += 1
            c["errors"] += bool(r.error)
        dq = db.query(SopDraft)
        if since:
            dq = dq.filter(SopDraft.created_at >= since)
        drafts = dq.all()
        return {"runs": len(runs), "problems": c["create_problem"], "sops_drafted": len(drafts),
                "sops_published": sum(d.status == "published" for d in drafts),
                "sops_pending": sum(d.status == "pending" for d in drafts),
                "human_asks": c["open_human_ticket"], "notes": c["note_ticket"],
                "sla_actions": c["reassign_ticket"] + c["nudge_employee"] + c["remind_approver"] + c["bump_to_human"],
                "research": c["web_search"] + c["fetch_page"], "errors": c["errors"],
                "last_run": max((r.started_at for r in runs), default=None)}
