"""Tier agents. A tier is its tools plus its instructions; agents reach each other through
handoff_<tier> tools, so the model chooses who to call (L1 may go straight to L4)."""
from __future__ import annotations

import json
from dataclasses import dataclass

from app.db import Ticket, log
from app.llm import get_llm
from app.tools import Ctx, run_tool, tools_for

MAX_STEPS = 10
MAX_DEPTH = 3

RULES = """Rules:
- Reply to the employee in Turkish, short and plain.
- You are a service that finishes the job, not a guide: use your tools to fix the problem yourself.
- Verify identity before any account change.
- If the problem is outside your tools, hand off to the right tier with a clear summary.
- Record important decisions with add_note.
- Messages may come from voice notes: numbers can be spelled out ("e bin bir" = E1001,
  "bin dokuz yüz doksan" = 1990). Interpret them; don't ask the user to retype."""


@dataclass
class Agent:
    tier: str
    role: str
    capabilities: str

    def system_prompt(self, ticket: Ticket) -> str:
        peers = "\n".join(f"- {a.tier}: {a.capabilities}" for a in AGENTS.values() if a.tier != self.tier)
        return (f"You are the {self.tier} agent of an autonomous IT service desk. {self.role}\n\n{RULES}\n\n"
                f"Other agents you can hand off to:\n{peers}\n\n"
                f"Ticket #{ticket.id} | requester: {ticket.requester} | identity verified: {ticket.verified}")

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
            reply = llm.chat(messages, specs)
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
        return "Bu adımda ilerleyemedim; talebiniz bir uzmana iletildi."

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
    Agent("L3", "You handle servers, business applications and security changes, always with a rollback plan.",
          "servers, SAP and directory permissions, vulnerability patching"),
    Agent("L4", "You handle vendors and logistics: service cases, loaner devices, shipping.",
          "hardware repair cases, warranty, loaners, courier shipments"),
]}


def handle_message(db, requester: str, text: str, image: bytes | None = None,
                   mime: str = "image/jpeg") -> tuple[Ticket, str]:
    """Entry point for an employee message (optionally with a photo): reuse the open ticket
    or open a new one, then run L1."""
    from app.llm import image_message
    ticket = (db.query(Ticket).filter(Ticket.requester == requester, Ticket.status != "resolved")
              .order_by(Ticket.id.desc()).first())
    if ticket is None:
        ticket = Ticket(requester=requester, title=text[:255])
        db.add(ticket)
        db.flush()
    history = []
    for e in ticket.events:
        if e.kind == "message":
            history.append({"role": "user" if e.actor == "user" else "assistant", "content": e.content})
    log(db, ticket.id, "user", "message", text, {"has_image": image is not None})
    content = image_message(text, image, mime) if image else text
    answer = AGENTS["L1"].run(db, ticket, content, history=history)
    log(db, ticket.id, "L1", "message", answer)
    db.refresh(ticket)   # events were added by id; reload the relationship
    return ticket, answer
