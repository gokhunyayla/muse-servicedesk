"""Tier agents. A tier is its tools plus its instructions; agents reach each other through
handoff_<tier> tools, so the model chooses who to call (L1 may go straight to L4)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.db import Ticket, log
from app.llm import ContentFiltered, get_llm
from app.tools import Ctx, run_tool, tools_for

MAX_STEPS = 10
MAX_DEPTH = 3

RULES = """Rules:
- Reply in the employee's language (Turkish by default), short and plain.
- Start with search_knowledge: follow the matching SOP. No SOP? Use your own IT expertise and the
  tools you have; ask at most two clarifying questions at a time; never leave the user with "I don't know".
- You are a service that finishes the job, not a guide: use your tools to fix the problem yourself.
- Verify identity before any account change. Ask for explicit consent before scripts that need it.
- If the problem is outside your tools, hand off to the right tier with a clear summary.
- If no tier can solve it (needs hands on site, a vendor or budget decision, something nobody is
  authorised to change), don't guess and don't loop: escalate_to_human with what was tried. Never leave
  the employee without a next step.
- A ticket closes only after the employee confirms: when you think it's fixed, request_confirmation;
  when the employee says it's solved, close_ticket right away (don't ask again).
- If the employee asks to close or cancel the ticket ("kapat", "iptal", "yanlış talep", "gerek kalmadı"),
  close_ticket immediately with resolution "closed on the employee's request" and confirm in one line;
  don't ask for a rating.
- When you solve something no SOP covered, write_sop so the next agent has it.
- Record important decisions with add_note.
- Messages may come from voice notes: numbers can be spelled out ("e bin bir" = E1001,
  "bin dokuz yüz doksan" = 1990). Interpret them; don't ask the user to retype.
- When the employee types back a password, code or ID you gave them, compare it character by character
  and point out any difference (trailing dot or space, O vs 0, l vs I, wrong case). Never say it is
  correct if it is not identical."""


@dataclass
class Agent:
    tier: str
    role: str
    capabilities: str
    heavy: bool = False   # use the heavy model (Muse Wonder): for production-impacting change planning

    def system_prompt(self, ticket: Ticket) -> str:
        from app import knowledge
        peers = "\n".join(f"- {a.tier}: {a.capabilities}" for a in AGENTS.values() if a.tier != self.tier)
        sops = "\n".join(f"- {t}" for t in knowledge.titles(self.tier)) or "(empty)"
        state = (f"Ticket #{ticket.id} | requester: {ticket.requester} | status: {ticket.status} | "
                 f"identity verified: {ticket.verified}")
        if ticket.status == "waiting_user":
            state += (f"\nThe employee was asked to confirm this fix: \"{ticket.resolution}\". If they confirm, "
                      f"close_ticket and ask for a 1-5 rating; if they report it still fails, keep working. "
                      f"If their message is a rating, record_rating.")
        elif ticket.status == "escalated":
            state += ("\nThis ticket is escalated to a human engineer. Acknowledge new information, add_note it, "
                      "and tell the employee it has been passed on; don't restart troubleshooting.")
        elif ticket.status == "resolved":
            state += (f"\nThis ticket is already resolved (\"{ticket.resolution}\") and the employee was asked for "
                      f"a 1-5 rating. If their message is a number, record_rating and thank them in one line. "
                      f"If it is just thanks, reply in one line. Do not ask what they need help with.")
        return (f"You are the {self.tier} agent of an autonomous IT service desk. {self.role}\n\n{RULES}\n\n"
                f"SOPs in your knowledge pool (search_knowledge to read one):\n{sops}\n\n"
                f"Other agents you can hand off to:\n{peers}\n\n{state}")

    def tool_specs(self) -> list[dict]:
        specs = [t.spec() for t in tools_for(self.tier)]
        for peer in AGENTS:
            if peer != self.tier:
                specs.append({"type": "function", "function": {
                    "name": f"handoff_{peer}",
                    "description": f"Hand the problem to {peer} ({AGENTS[peer].capabilities}). Returns its result.",
                    "parameters": {"type": "object", "properties": {"summary": {"type": "string"}},
                                   "required": ["summary"]}}})
        return specs

    def run(self, db, ticket: Ticket, message: str | list, history: list[dict] | None = None,
            depth: int = 0) -> str:
        llm = get_llm()
        ctx = Ctx(db=db, ticket=ticket, agent=self.tier)
        messages = [{"role": "system", "content": self.system_prompt(ticket)}, *(history or []),
                    {"role": "user", "content": message}]
        specs = self.tool_specs()
        for _ in range(MAX_STEPS):
            try:
                reply = llm.chat(messages, specs, heavy=self.heavy)
            except ContentFiltered:
                # the provider's safety filter is fuzzy; keep the ticket alive and ask for a rephrase
                log(db, ticket.id, self.tier, "note", "model request blocked by provider content filter")
                return ("Mesajınızı bu haliyle işleyemedim. Sorunu bir cümleyle, başka kelimelerle "
                        "yazar mısınız? (Örn. 'X sunucusunda Y hatası alıyorum')")
            if not reply.tool_calls:
                return reply.text.strip()
            messages.append({"role": "assistant", "content": reply.text or None, "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)}}
                for c in reply.tool_calls]})
            for call in reply.tool_calls:
                log(db, ticket.id, self.tier, "tool_call", call.name, call.arguments)
                if call.name.startswith("handoff_"):
                    result = self._handoff(db, ticket, call.name.removeprefix("handoff_"),
                                           call.arguments.get("summary", ""), depth)
                else:
                    result = run_tool(ctx, call.name, call.arguments)
                log(db, ticket.id, self.tier, "tool_result", call.name, result)
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": json.dumps(result, ensure_ascii=False, default=str)})
        # out of steps: park it for a human instead of leaving the employee hanging
        if ticket.status not in ("resolved", "escalated"):
            run_tool(ctx, "escalate_to_human", {"reason": "agent could not finish within its step budget",
                                                "tried": f"{MAX_STEPS} tool steps on {self.tier}"})
            log(db, ticket.id, self.tier, "tool_call", "escalate_to_human", {"reason": "step budget exhausted"})
        return "Bu adımda ilerleyemedim; talebiniz bir destek mühendisine aktarıldı, sizinle iletişime geçilecek."

    def _handoff(self, db, ticket: Ticket, target: str, summary: str, depth: int) -> dict:
        if target not in AGENTS:
            return {"ok": False, "error": f"unknown agent {target}"}
        if depth >= MAX_DEPTH:
            return {"ok": False, "error": "handoff chain too deep"}
        log(db, ticket.id, self.tier, "handoff", f"{self.tier} -> {target}: {summary}")
        ticket.owner = target
        answer = AGENTS[target].run(db, ticket, f"Handoff from {self.tier}: {summary}", depth=depth + 1)
        ticket.owner = self.tier
        return {"ok": True, "agent": target, "result": answer}


AGENTS: dict[str, Agent] = {a.tier: a for a in [
    Agent("L1", "You are the only agent that talks to the employee (over WhatsApp). Understand the request, "
                "solve standard problems yourself, and hand off the rest.",
          "first contact, identity checks, password resets, standard VPN fixes, reading photos/screenshots "
          "the employee sends (e.g. blue screen stop codes)"),
    Agent("L2", "You diagnose devices and networks. You never talk to the employee; return a clear result.",
          "device and network diagnosis, VPN deep analysis, performance problems"),
    Agent("L3", "You handle servers, business applications and security changes. Every production change "
                "follows this order: 1) find_vulnerable_servers to set the scope, 2) add_note with the change "
                "plan AND the rollback plan, 3) apply_patch host by host (a snapshot is taken for you), "
                "4) verify_patch each host; if verification fails, rollback_patch to its snapshot, "
                "5) add_note with the evidence (versions before/after, snapshot ids). Return a structured summary.",
          "servers, SAP and directory permissions, vulnerability (CVE) analysis and patching with rollback",
          heavy=True),
    Agent("L4", "You handle vendors and logistics: service cases, loaner devices, shipping.",
          "hardware repair cases, warranty, loaners, courier shipments"),
]}


def _belongs_to(ticket: Ticket, text: str) -> bool:
    """Does this message continue the open ticket, or is it a new problem? The model decides;
    very short messages (confirmations, ratings, approvals) always continue the ticket."""
    if len(text.split()) <= 3 or text.startswith("["):
        return True
    recent = [e.content for e in ticket.events if e.kind == "message"][-4:]
    prompt = (f"Open ticket #{ticket.id}: \"{ticket.title}\"\nRecent exchange:\n" +
              "\n".join(f"- {m[:200]}" for m in recent) +
              f"\n\nNew message from the same employee: \"{text[:400]}\"\n\n"
              "Is the new message about the SAME problem (a follow-up, more detail, confirmation, or an answer "
              "to our question), or a NEW, unrelated problem? Answer with one word: SAME or NEW.")
    try:
        reply = get_llm().chat([{"role": "user", "content": prompt}], [])
    except ContentFiltered:
        return True
    return "NEW" not in reply.text.upper()


RATING_WINDOW_HOURS = 24
RATING_RE = re.compile(r"^\s*[1-5]\s*(?:/\s*5)?\s*(?:puan)?\s*$", re.I)
# "kapat", "iptal", "yanlış talep, kapat", "bileti kapatın", "close", "cancel"
CLOSE_RE = re.compile(r"^\s*(?:(?:yanlış|hatalı)\s+talep[\s,.]*)?(?:bileti\s+|talebi\s+)?"
                      r"(?:kapat(?:ın|ınız)?|iptal(?:\s+et(?:in)?)?|close|cancel)[\s.!]*$", re.I)


def _closing_exchange(db, requester: str, text: str) -> Ticket | None:
    """A short message ("5", "teşekkürler") right after a ticket was closed belongs to that ticket's
    closing exchange (rating / thanks), not to a new one. Anything longer is a new request."""
    from datetime import timedelta
    from app.db import _now
    if len(text.split()) > 3:
        return None
    last = (db.query(Ticket).filter(Ticket.requester == requester, Ticket.status == "resolved",
                                    Ticket.rating.is_(None)).order_by(Ticket.id.desc()).first())
    if last is None or not last.events:
        return None
    if last.events[-1].at < _now() - timedelta(hours=RATING_WINDOW_HOURS):
        return None
    return last


def handle_message(db, requester: str, text: str, image: bytes | None = None,
                   mime: str = "image/jpeg", extra_images: list[bytes] | None = None) -> tuple[Ticket, str]:
    """Entry point for an employee message (optionally with a photo): reuse the open ticket
    or open a new one, then run L1."""
    from app.llm import image_message
    open_tickets = (db.query(Ticket).filter(Ticket.requester == requester, Ticket.status != "resolved")
                    .order_by(Ticket.id.desc()).all())
    ticket = open_tickets[0] if open_tickets else None
    short = len(text.split()) <= 3 and not text.startswith("[")
    if short and RATING_RE.match(text) and _closing_exchange(db, requester, text) is not None:
        ticket = _closing_exchange(db, requester, text)      # a bare "5" right after a close is the rating
    elif len(open_tickets) > 1 and short:
        # several tickets open and a short answer: it must name the ticket, otherwise ask
        named = [t for t in open_tickets if re.search(rf"(?<!\d)#?{t.id}(?!\d)", text)]
        if named:
            ticket = named[0]
            text = re.sub(rf"#?{ticket.id}", "", text, count=1).strip() or text
        else:
            choices = "\n".join(f"#{t.id}: {t.title[:60]}" for t in open_tickets)
            answer = f"Birden fazla açık talebiniz var, hangisi için?\n{choices}\n(Örn. \"#{open_tickets[0].id} çözüldü\")"
            log(db, open_tickets[0].id, "user", "message", text)
            log(db, open_tickets[0].id, "L1", "message", answer)
            db.refresh(open_tickets[0])
            return open_tickets[0], answer
    elif ticket is not None and not _belongs_to(ticket, text):
        log(db, ticket.id, "system", "note", f"new unrelated request from requester, opened a separate ticket: {text[:80]}")
        ticket = None
    if ticket is None:
        ticket = _closing_exchange(db, requester, text)
    if ticket is None:
        ticket = Ticket(requester=requester, title=text[:255])
        db.add(ticket)
        db.flush()
    history = []
    for e in ticket.events:
        if e.kind == "message":
            history.append({"role": "user" if e.actor == "user" else "assistant", "content": e.content})
    # a manager answering "ONAY 3" / "RET 3" is an approval decision, not a new problem
    m = re.match(r"^\s*(onay|ret|approve|deny)\s+#?(\d+)\s*$", text, re.I)
    if m:
        decision = "approved" if m.group(1).lower() in ("onay", "approve") else "denied"
        return handle_approval(db, int(m.group(2)), decision, requester)
    log(db, ticket.id, "user", "message", text, {"has_image": image is not None})
    # a plain "kapat" / "iptal" closes the ticket without a model round-trip
    if ticket.status != "resolved" and CLOSE_RE.match(text):
        run_tool(Ctx(db=db, ticket=ticket, agent="L1"), "close_ticket",
                 {"resolution": "closed on the employee's request"})
        log(db, ticket.id, "L1", "tool_call", "close_ticket", {"resolution": "closed on the employee's request"})
        answer = f"Talebiniz (#{ticket.id}) isteğiniz üzerine kapatıldı. Yeni bir sorun olursa yazmanız yeterli."
        log(db, ticket.id, "L1", "message", answer)
        db.refresh(ticket)
        return ticket, answer
    content = image_message(text, image, mime) if image else text
    for extra in extra_images or []:          # video frames: same message, more image parts
        content.append(image_message("", extra, mime)[1])
    answer = AGENTS["L1"].run(db, ticket, content, history=history)
    log(db, ticket.id, "L1", "message", answer)
    db.refresh(ticket)   # events were added by id; reload the relationship
    return ticket, answer


def handle_approval(db, approval_id: int, decision: str, by_phone: str) -> tuple[Ticket, str]:
    """A manager answered an approval request: record it and let L1 continue the ticket."""
    from app.db import Approval, _now
    a = db.get(Approval, approval_id)
    if a is None or a.decision is not None:
        raise ValueError("unknown or already decided approval")
    from app import systems
    approver = systems.USERS.get(a.approver, {})
    if approver.get("phone") != by_phone:
        raise PermissionError("only the assigned approver can decide")
    a.decision, a.decided_at = decision, _now()
    ticket = db.get(Ticket, a.ticket_id)
    ticket.status = "open"
    log(db, ticket.id, a.approver, "note", f"approval #{a.id} {decision}: {a.request}")
    answer = AGENTS["L1"].run(db, ticket, f"[Approval #{a.id} {decision} by {approver.get('name')}] "
                                          f"Request was: {a.request}. Continue the ticket accordingly and "
                                          f"tell the employee.")
    log(db, ticket.id, "L1", "message", answer)
    db.refresh(ticket)
    return ticket, answer
