"""Tier agents. A tier is its tools plus its instructions; agents reach each other through
handoff_<tier> tools, so the model chooses who to call (L1 may go straight to L4)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.db import Ticket, log
from app.i18n import DEFAULT_LANG, detect, t as tr_, ticket_lang
from app.llm import ContentFiltered, get_llm
from app.tools import Ctx, run_tool, tools_for

MAX_STEPS = 10
MAX_DEPTH = 3
MAX_REFUSALS = 3     # the same tool refused this many times on one ticket: stop and hand off to a person

RULES = """Rules:
- Reply in this ticket's language (stated below), short and plain — not the language of the employee's earlier tickets.
- When the employee's device is known and a safe check or fix fits the symptom (network_diagnostics, or the
  non-disruptive scripts dns-reset, perf-report, patch-status, printer-fix), run it FIRST and tell them what it
  found; ask only what the result does not answer. Don't ask an employee for facts the device can report.
- Tell the employee only what your tools actually did. A hand-off to a human engineer is "I passed it to an
  engineer", never "I opened a request in <system>" for a system you have no tool for (Intune, a vendor portal).
- Work that continues on the device after a script returns (OS updates) reports back to the desk by itself and
  the ticket resumes with the outcome: tell the employee you will let them know, don't ask whether to check.
- On a new problem, search_knowledge first. If a SOP matches, follow exactly what its text says and cite it
  as [SOP:key] in your notes; never add steps, values, commands or policies the SOP does not contain. If
  nothing matches, say ONCE, in one short clause, that the desk has no procedure and you are helping from
  general IT knowledge — then never repeat that sentence in this ticket, and do not search again for the same
  topic on follow-ups: the conversation so far is in front of you, build on it. Ask at most two clarifying
  questions at a time, never ones the employee already answered; never leave the user with "I don't know".
- Reference documents are rules you live by, not just reading. Before any action with consequences
  (account, access, license, approval, scripts, data) and whenever a request touches a rule, entitlement or
  deadline, search_references and quote the passage as [REF:doc#n]. A STRICT passage binds you even if the
  employee insists: decline that part, quote the rule, and offer the compliant path (or hand off / escalate
  to a human if an exception needs a decision). A FLEXIBLE passage is the default; you may deviate when the
  situation calls for it, and you add_note the reason. If no reference covers it, say so; never invent a
  policy, a limit, a deadline or a rule.
- You are a service that finishes the job, not a guide: use your tools to fix the problem yourself.
- Verify identity before any account change. Ask for explicit consent before scripts that need it. If the
  prompt says identity is already verified for this employee, do not ask again.
- Requests are for the employee writing, never for someone else: if they ask for a colleague's password,
  account, licence or data, decline in one line and say the colleague must write to the desk themselves
  (the desk team can open a ticket for them from the console). Reporting a shared problem (a printer, an
  outage) is fine.
- A password or credential reset needs more than identity: after verify_identity, call send_verification and
  tell the employee a code was sent to their other channel (say which, masked) and to type it back here. If
  the result also carries phrase_to_say, tell them that exact phrase must be said in a VOICE note as well.
  When the code arrives (typed), call check_verification(code). Only after it succeeds call reset_password.
  If something is missing or wrong, say what and ask again; never skip a step.
- If the problem is outside your tools, hand off to the right tier with a clear summary.
- If no tier can solve it (needs hands on site, a vendor or budget decision, something nobody is
  authorised to change), don't guess and don't loop: escalate_to_human with what was tried. Never leave
  the employee without a next step.
- A ticket closes only after the employee confirms: when you think it's fixed, request_confirmation;
  when the employee says it's solved, close_ticket right away (don't ask again).
- If the employee asks to close or cancel the ticket ("kapat", "iptal", "yanlış talep", "gerek kalmadı"),
  close_ticket immediately with resolution "closed on the employee's request" and confirm in one line;
  don't ask for a rating.
- When you solve something no SOP covered, write_sop (it becomes a draft for an admin) so the next agent has it.
- Record important decisions with add_note.
- Employee history is in your prompt. If the same problem recurred (two or more similar tickets in the last
  30 days), say so to the employee in one line and go for a lasting fix (hand off the device to L2, or L4 for
  replacement) instead of repeating the quick fix; add_note the pattern.
- If the employee is clearly frustrated, angry, or asks for a person, acknowledge it in one calm sentence and
  escalate_to_human right away with what was tried; do not argue and do not keep troubleshooting.
- Messages may come from voice notes: numbers can be spelled out ("e bin bir" = E1001,
  "bin dokuz yüz doksan" = 1990). Interpret them; don't ask the user to retype.
- When the employee types back a password, code or ID you gave them, compare it character by character
  and point out any difference (trailing dot or space, O vs 0, l vs I, wrong case). Never say it is
  correct if it is not identical."""


def _reopen_if_same(db, requester: str, text: str, channel: str) -> Ticket | None:
    """A ticket resolved less than REOPEN_HOURS ago and the employee describes the same problem again:
    reopen it with a note (ITSM practice) instead of a duplicate. Older ones stay closed; a new ticket is opened."""
    from datetime import timedelta
    from app.db import _now
    recent = (db.query(Ticket).filter(Ticket.requester == requester, Ticket.status == "resolved",
                                      Ticket.resolved_at >= _now() - timedelta(hours=REOPEN_HOURS))
              .order_by(Ticket.id.desc()).first())
    if recent is None or recent.title.startswith("["):
        return None
    if _route(db, recent, text) != "same":
        return None
    recent.status, recent.resolved_at, recent.rating, recent.channel = "open", None, recent.rating, channel
    log(db, recent.id, "system", "note", "reopened: the employee reports the same problem again within 48 h")
    return recent


def requester_history(db, ticket: Ticket, days: int = 60, limit: int = 6) -> str:
    """The requester's other tickets, so the agent notices repeats ("third VPN problem this month")."""
    from datetime import timedelta
    from app.db import _now
    rows = (db.query(Ticket).filter(Ticket.requester == ticket.requester, Ticket.id != ticket.id,
                                    Ticket.created_at >= _now() - timedelta(days=days))
            .order_by(Ticket.id.desc()).limit(limit).all())
    out = []
    for t in rows:
        when = t.created_at.strftime("%d %b")
        res = (t.resolution or "").split("\n")[0][:80]
        out.append(f"- #{t.id} {when} · {t.title.split(chr(10))[0][:60]} · {t.status}" + (f" · {res}" if res else ""))
    return "\n".join(out)


RESUME_MINUTES = 60


class _ResumeStore:
    """Pending "continue where we left off?" questions, kept in the settings table (inside the caller's
    session) so a restart does not lose them and SQLite never waits on a second connection."""
    @staticmethod
    def _key(requester: str) -> str:
        return f"resume:{requester}"

    def get(self, db, requester: str):
        from app.db import Setting
        row = db.get(Setting, self._key(requester))
        return row.value if row and row.value else None

    def set(self, db, requester: str, value: dict) -> None:
        from app.db import Setting
        row = db.get(Setting, self._key(requester))
        if row is None:
            db.add(Setting(key=self._key(requester), value=value))
        else:
            row.value = value
        db.flush()

    def drop(self, db, requester: str) -> None:
        from app.db import Setting
        row = db.get(Setting, self._key(requester))
        if row is not None:
            row.value = None
            db.flush()

    def clear(self) -> None:
        from app.db import Setting, session
        with session() as db:
            db.query(Setting).filter(Setting.key.like("resume:%")).delete(synchronize_session=False)


_RESUME = _ResumeStore()     # requester -> {"ticket": id, "pending": first message on the new channel, "until": epoch}
YES_RE = re.compile(r"^\s*(evet|devam|oradan|aynen|tabii|tamam|ok(?:ey)?|yes|continue|sure|yep|same)\b", re.I)
NO_RE = re.compile(r"^\s*(hay[ıi]r|yeni|ba[şs]ka|farkl[ıi]|no|new|different|other)\b", re.I)
CHANNEL_NAMES = {"tr": {"whatsapp": "WhatsApp", "web": "destek sayfası", "email": "e-posta", "api": "API"},
                 "en": {"whatsapp": "WhatsApp", "web": "the support page", "email": "e-mail", "api": "the API"}}


def _most_recent_conversation(tickets: list, minutes: int = RESUME_MINUTES) -> Ticket | None:
    """Of several open tickets, the one with the latest message, if that was within `minutes`."""
    from datetime import timedelta
    from app.db import _now
    best, best_at = None, None
    for t in tickets:
        msgs = [e for e in t.events if e.kind == "message"]
        if msgs and (best_at is None or msgs[-1].at > best_at):
            best, best_at = t, msgs[-1].at
    return best if best is not None and best_at >= _now() - timedelta(minutes=minutes) else None


def recent_switch(db, requester: str, channel: str) -> Ticket | None:
    """An open ticket the employee was working on, on another channel, within the last hour."""
    from datetime import timedelta
    from app.db import _now
    for t in (db.query(Ticket).filter(Ticket.requester == requester, Ticket.status != "resolved")
              .order_by(Ticket.id.desc()).all()):
        if t.channel == channel or t.channel == "api":
            continue
        last = [e for e in t.events if e.kind == "message"]
        if last and last[-1].at >= _now() - timedelta(minutes=RESUME_MINUTES):
            return t
    return None


def clarify(db, held: Ticket, text: str, lang: str) -> str:
    """The message could be about the open ticket or something new: ask one natural question that mentions the
    open topic and asks what exactly they see — not a mechanical 'yes / new issue'. Scripted tests get the fixed text."""
    canned = tr_("same_or_new", lang, id=held.id, title=held.title.split(chr(10))[0][:60])
    llm = get_llm()
    if hasattr(llm, "verdict"):
        return canned
    recent = [e.content for e in held.events if e.kind == "message"][-3:]
    prompt = (f"You are an IT service desk agent. The employee has an open ticket #{held.id}: \"{held.title.split(chr(10))[0][:80]}\".\n"
              f"Recent exchange on it:\n" + "\n".join(f"- {m[:160]}" for m in recent) +
              f"\n\nThey now write: \"{text[:300]}\"\n\nIt is unclear whether this is about that ticket or a new problem. "
              f"Write ONE short, natural message in {'Turkish' if lang == 'tr' else 'English'} that (a) asks whether it is about the open topic "
              f"(name it in plain words, mention #{held.id}) or something else, and (b) asks what exactly they see (device, screen, the text of the warning). "
              f"No bullet points, no options in brackets, max two sentences.")
    from app import usage
    try:
        with usage.scope("ticket", held.id, "route", db=db):
            reply = llm.chat([{"role": "user", "content": prompt}], [])
        return (reply.text or "").strip() or canned
    except ContentFiltered:
        return canned


def resume_offer(db, requester: str, channel: str, lang: str, pending: str = "", greeting: bool = False) -> tuple[Ticket, str] | None:
    """If the employee just switched channels mid-conversation, ask whether to pick up where they left off.
    `greeting`: they said hello first, so the question doubles as the greeting (one message, not two)."""
    import time as _t
    t = recent_switch(db, requester, channel)
    if t is None:
        return None
    _RESUME.set(db, requester, {"ticket": t.id, "pending": pending, "until": _t.time() + 15 * 60})
    names = CHANNEL_NAMES["tr" if lang == "tr" else "en"]
    return t, tr_("resume_offer_hi" if greeting else "resume_offer", lang, chan=names.get(t.channel, t.channel), id=t.id,
                  title=t.title.split(chr(10))[0][:50])


def _similar_recent(db, ticket: Ticket, text: str, minutes: int = 15) -> list[Ticket]:
    """Other people's fresh tickets about the same thing: same category, or two shared words."""
    from datetime import timedelta
    from app.dashboard import CATEGORIES
    from app.db import _now
    low = text.lower()
    cat = next((k for k, words in CATEGORIES.items() if any(w in low for w in words)), None)
    words = {w for w in re.findall(r"[a-zçğıöşü0-9]{4,}", low)}
    out = []
    for t in (db.query(Ticket).filter(Ticket.requester != ticket.requester, Ticket.id != ticket.id, Ticket.status != "resolved",
                                      Ticket.created_at >= _now() - timedelta(minutes=minutes)).all()):
        tl = t.title.lower()
        if t.title.startswith("[Proactive"):
            continue
        tcat = next((k for k, ws in CATEGORIES.items() if any(w in tl for w in ws)), None)
        shared = words & {w for w in re.findall(r"[a-zçğıöşü0-9]{4,}", tl)}
        if (cat and tcat == cat and cat != "other") or len(shared) >= 2:
            out.append(t)
    return out


def emerging_incident(db, ticket: Ticket, text: str) -> str:
    """Three reports of the same thing within minutes: link them under one problem at intake and brief the agent."""
    similar = _similar_recent(db, ticket, text)
    if len(similar) < 2:
        return ""
    head = next((db.get(Ticket, t.incident_id) for t in similar if t.incident_id), None) or sorted(similar, key=lambda t: t.id)[0]
    for t in [*similar, ticket]:
        if t.id != head.id and not t.incident_id:
            t.incident_id = head.id
            log(db, t.id, "system", "note", f"linked at intake to possible incident #{head.id}: {len(similar) + 1} similar reports in 15 min")
    log(db, head.id, "system", "note", f"possible incident: {len(similar) + 1} similar reports in 15 min ({', '.join('#' + str(t.id) for t in similar)}, #{ticket.id})")
    ids = ", ".join(f"#{t.id}" for t in similar)
    return (f"[Desk notice: {len(similar)} other employees reported the same thing in the last 15 minutes ({ids}). "
            f"This is being treated as a possible outage and the ticket is linked to problem #{head.id}. Tell the employee "
            f"that others are affected and it is handled centrally; do only quick checks, no long troubleshooting; "
            f"they will get updates. If no SOP covers it, escalate_to_human for the problem.]")


@dataclass
class Agent:
    tier: str
    role: str
    capabilities: str
    heavy: bool = False   # use the heavy model (Muse Wonder): for production-impacting change planning

    def system_prompt(self, ticket: Ticket, db=None) -> str:
        def _real(host: str) -> bool:
            from app import endpoints
            return endpoints.get(host) is not None
        from app import knowledge
        peers = "\n".join(f"- {a.tier}: {a.capabilities}" for a in AGENTS.values() if a.tier != self.tier)
        sops = "\n".join(f"- {t}" for t in knowledge.titles(self.tier, db=db)) or "(empty)"
        state = (f"Ticket #{ticket.id} | requester: {ticket.requester} | status: {ticket.status} | "
                 f"identity verified: {ticket.verified}")
        if db is not None:
            from app.tools import remembered_identity
            rem = remembered_identity(db, ticket.requester)
            if rem and not ticket.verified:
                ticket.verified = True
                state = state.replace("identity verified: False", "identity verified: True")
            if ticket.verified:
                state += ("\nIdentity is verified" + (f" (verified earlier today as {rem['employee_no']}, valid until {rem['until'][11:16]} UTC)" if rem else "")
                          + "; do not ask for employee number or birth year again.")
        try:                                         # who is writing, per the directory: never guess from older messages
            from app import systems
            owners = systems.employees_by_contact(ticket.requester)
        except Exception:  # noqa: BLE001   (directory unreachable: the agent asks as before)
            owners = []
        if len(owners) == 1:
            rec = systems.get_user(owners[0]) or {}
            if rec.get("device"):
                state += (f"\nTheir device is {rec['device']}; use it for device tools without asking for the hostname"
                          + (" (a real machine with the endpoint agent: its scripts really run)." if _real(rec["device"]) else "."))
            state += (f"\nThe directory says this phone/address belongs to {rec.get('name', '?')} ({owners[0]}). The employee "
                      f"is writing about themselves: use {owners[0]} for every action and never another employee number "
                      f"mentioned earlier in the conversation."
                      + ("" if rec.get("birth_year") else
                         f" The directory holds no birth year: do NOT ask for employee number or birth year; call "
                         f"verify_identity with employee_no {owners[0]} and birth_year 0 (the number they write from is the check)."))
        said_no_sop = any(e.kind == "message" and e.actor != "user" and re.search(r"prosedür|procedure|hazır bir işlem|genel (IT )?bilgi", e.content or "", re.I)
                         for e in ticket.events)
        if said_no_sop:
            state += ("\nYou already told the employee there is no procedure for this and you are helping from general "
                      "knowledge. Do NOT say it again; answer their last message directly and move the troubleshooting forward.")
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
        lang = ticket_lang(ticket)
        state += f"\nLanguage of this ticket: {'Turkish' if lang == 'tr' else 'English'}. Write every message to the employee in it."
        history = requester_history(db, ticket) if db is not None else ""
        return (f"You are the {self.tier} agent of an autonomous IT service desk. {self.role}\n\n{RULES}\n\n"
                f"SOPs in your knowledge pool (search_knowledge to read one):\n{sops}\n\n"
                f"Other agents you can hand off to:\n{peers}\n\n{state}"
                + (f"\n\nEmployee history (other tickets of this requester, newest first):\n{history}" if history else ""))

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
        from app import usage
        llm = get_llm()
        ctx = Ctx(db=db, ticket=ticket, agent=self.tier)
        messages = [{"role": "system", "content": self.system_prompt(ticket, db)}, *(history or []),
                    {"role": "user", "content": message}]
        specs = self.tool_specs()
        with usage.scope("ticket", ticket.id, self.tier, db=db):
            return self._loop(db, ticket, ctx, llm, messages, specs, depth)

    def _loop(self, db, ticket, ctx, llm, messages, specs, depth) -> str:
        from collections import Counter
        refused: Counter = Counter(e.content for e in ticket.events
                                   if e.kind == "tool_result" and isinstance(e.data, dict) and e.data.get("ok") is False)
        for _ in range(MAX_STEPS):
            try:
                reply = llm.chat(messages, specs, heavy=self.heavy)
            except ContentFiltered:
                # the provider's safety filter is fuzzy; keep the ticket alive and ask for a rephrase
                log(db, ticket.id, self.tier, "note", "model request blocked by provider content filter")
                return tr_("filtered", ticket_lang(ticket))
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
                # a check inside a tool refused the step: the model gets the reason and may fix it, but not forever
                if isinstance(result, dict) and result.get("ok") is False and not call.name.startswith("handoff_"):
                    refused[call.name] += 1
                    if refused[call.name] >= MAX_REFUSALS and ticket.status not in ("resolved", "escalated"):
                        why = str(result.get("error") or result.get("missing") or "")[:200]
                        run_tool(ctx, "escalate_to_human", {"reason": f"{call.name} refused {refused[call.name]} times: {why}",
                                                            "tried": f"{self.tier} retried {call.name} {refused[call.name]} times"})
                        log(db, ticket.id, self.tier, "tool_call", "escalate_to_human", {"reason": f"{call.name} refused {refused[call.name]} times"})
                        return tr_("escalated", ticket_lang(ticket))
        # out of steps: park it for a human instead of leaving the employee hanging
        if ticket.status not in ("resolved", "escalated"):
            run_tool(ctx, "escalate_to_human", {"reason": "agent could not finish within its step budget",
                                                "tried": f"{MAX_STEPS} tool steps on {self.tier}"})
            log(db, ticket.id, self.tier, "tool_call", "escalate_to_human", {"reason": "step budget exhausted"})
        return tr_("escalated", ticket_lang(ticket))

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
          "servers, directory and folder permissions, vulnerability (CVE) analysis and patching with rollback",
          heavy=True),
    Agent("L4", "You handle vendors and logistics: service cases, loaner devices, shipping.",
          "hardware repair cases, warranty, loaners, courier shipments"),
]}


def _route(db, ticket: Ticket, text: str) -> str:
    """Does this message continue the open ticket ("same"), start a new problem ("new"), or is it unclear ("ask")?
    The model decides; acknowledgements always continue the ticket."""
    if is_ack(text):
        return "same"
    recent = [e.content for e in ticket.events if e.kind == "message"][-4:]
    prompt = (f"Open ticket #{ticket.id}: \"{ticket.title}\"\nRecent exchange:\n" +
              "\n".join(f"- {m[:200]}" for m in recent) +
              f"\n\nNew message from the same employee: \"{text[:400]}\"\n\n"
              "Is the new message about the SAME problem (a follow-up, more detail, confirmation, or an answer "
              "to our question), or a NEW problem? Rules: a message that states a problem of its own "
              "(another device, service, account or request) is NEW even if the open ticket is still unresolved; "
              "if the open ticket has no concrete problem yet (a greeting, a status question), a message that "
              "states one is NEW. If the message is too vague to tell what it is about (\"garip bir hata\", "
              "\"yine olmadı\" with nothing to anchor it), answer UNCLEAR so the desk asks the employee. "
              "Answer with one word: SAME, NEW or UNCLEAR.")
    llm = get_llm()
    if hasattr(llm, "verdict"):                                   # scripted (tests): default SAME unless scripted
        up = llm.verdict(("SAME", "NEW", "UNCLEAR")) or "SAME"
        return "ask" if up == "UNCLEAR" else "new" if up == "NEW" else "same"
    from app import usage
    try:
        with usage.scope("ticket", ticket.id, "route", db=db):
            reply = llm.chat([{"role": "user", "content": prompt}], [])
    except ContentFiltered:
        return "same"
    up = reply.text.upper()
    return "ask" if "UNCLEAR" in up else "new" if "NEW" in up else "same"


RATING_WINDOW_HOURS = 24
RATING_RE = re.compile(r"^\s*[1-5]\s*(?:/\s*5)?\s*(?:puan)?\s*$", re.I)
# a bare acknowledgement / answer to our question: continues the conversation, never a request of its own
ACK_WORDS = {"evet", "hayır", "hayir", "yok", "var", "tamam", "tamamdır", "ok", "okey", "oldu", "olmadı", "olmadi", "çalıştı", "calisti",
             "çalışmıyor", "çalışmadı", "çözüldü", "cozuldu", "çözülmedi", "düzeldi", "duzeldi", "düzelmedi", "hâlâ", "hala", "aynı", "ayni",
             "devam", "ediyor", "teşekkür", "teşekkürler", "tesekkurler", "sağol", "sağolun", "sagol", "eyvallah", "süper", "harika", "peki",
             "iyi", "güzel", "yes", "no", "nope", "yep", "done", "works", "worked", "fixed", "still", "same", "thanks", "thank", "you",
             "thx", "ty", "cheers", "solved", "resolved", "ok.", "puan", "kapat", "close", "kapatın", "iptal", "cancel", "yanlış", "talep"}


GREETING_WORDS = {"selam", "selamlar", "merhaba", "merhabalar", "mrb", "slm", "hello", "hi", "hey", "günaydın", "gunaydin", "iyi", "günler",
                  "gunler", "akşamlar", "aksamlar", "geceler", "kolay", "gelsin", "nasılsın", "nasilsin", "naber", "orada", "mısın", "misin",
                  "there", "good", "morning", "afternoon", "evening", "how", "are", "you", "hocam", "abi", "abla", "ben", "bana", "yardım", "lazım",
                  "destek", "istiyorum", "var", "mı", "mi", "bir", "sorum", "soru", "sorunum", "olacak", "olacaktı"}


def is_greeting(text: str) -> bool:
    """Only a greeting / "are you there" / "I need help" with no problem stated yet."""
    words = [w for w in re.split(r"[\s,.!?:;]+", text.strip().lower()) if w]
    return bool(words) and len(words) <= 6 and all(w in GREETING_WORDS for w in words)


def _is_request(db, text: str) -> bool:
    """Before opening a ticket: is this a support request / problem / question that needs the desk (REQUEST),
    or small talk that needs no record (CHAT)? Obvious greetings skip the model."""
    if is_greeting(text):
        return False
    if len(text.split()) >= 12 or text.startswith("["):
        return True
    prompt = (f"Message from an employee to the IT service desk: \"{text[:400]}\"\n\n"
              "Is this a REQUEST (a problem, an IT question, something to do, a report, an approval) that the desk should "
              "open a ticket for, or CHAT (greeting, thanks, small talk, a test of whether someone is there) that needs no "
              "ticket? A short problem statement like 'printer broken' is a REQUEST. Answer with one word: REQUEST or CHAT.")
    llm = get_llm()
    if hasattr(llm, "verdict"):                                   # scripted (tests): default REQUEST unless scripted
        return (llm.verdict(("REQUEST", "CHAT")) or "REQUEST") == "REQUEST"
    from app import usage
    try:
        with usage.scope("ticket", None, "intake", db=db):
            reply = llm.chat([{"role": "user", "content": prompt}], [])
    except ContentFiltered:
        return True
    return "CHAT" not in reply.text.upper()


def is_ack(text: str) -> bool:
    """Short confirmations, ratings, thanks, yes/no, "#12 close" — the only short messages that never open a ticket."""
    if text.startswith("["):
        return True
    words = [w for w in re.split(r"[\s,.!?]+", text.strip().lower()) if w]
    if not words or len(words) > 5:
        return False
    return all(w in ACK_WORDS or re.fullmatch(r"#?\d+(?:/5)?", w) for w in words)
# "kapat", "iptal", "yanlış talep, kapat", "bileti kapatın", "close", "cancel"
CLOSE_ALL_RE = re.compile(r"(?:tüm|bütün|hepsi|hepsini|all|every)\b.*?(?:kapat|iptal|close|cancel)|(?:kapat|close)\b.*?\b(?:hepsini|tümünü|all of them|them all)", re.I | re.S)
LIST_RE = re.compile(r"^\s*(?:(?:açık\s+)?(?:taleplerim|ticket'?lar[ıi]m|kay[ıi]tlar[ıi]m)(?:\s+neler|\s+ne durumda|\s+listele)?|(?:list|show)\s+my\s+(?:open\s+)?(?:tickets|requests)|my\s+(?:open\s+)?(?:tickets|requests))\s*[?.!]*\s*$", re.I)
REOPEN_HOURS = 48
CLOSE_RE = re.compile(r"^\s*(?:(?:yanlış|hatalı)\s+talep[\s,.]*)?(?:bileti\s+|talebi\s+)?"
                      r"(?:kapat(?:ın|ınız)?|iptal(?:\s+et(?:in)?)?|close|cancel)[\s.!]*$", re.I)


def _closing_exchange(db, requester: str, text: str) -> Ticket | None:
    """A short message ("5", "teşekkürler") right after a ticket was closed belongs to that ticket's
    closing exchange (rating / thanks), not to a new one. Anything longer is a new request."""
    from datetime import timedelta
    from app.db import _now
    if not is_ack(text):
        return None
    last = (db.query(Ticket).filter(Ticket.requester == requester, Ticket.status == "resolved",
                                    Ticket.rating.is_(None)).order_by(Ticket.id.desc()).first())
    if last is None or not last.events:
        return None
    if last.events[-1].at < _now() - timedelta(hours=RATING_WINDOW_HOURS):
        return None
    return last


APPROVAL_RE = re.compile(r"\b(onay|onayl[ıi]yorum|approve[d]?|ret|reddet|reddediyorum|deny|denied)\b\s*#?(\d+)?", re.I)


def _approval_decision(db, requester: str, text: str):
    """If the sender has pending approvals and the message carries ONAY/RET (with or without a number),
    return (approval, decision). A number must match one of their pending approvals; without a number,
    only an unambiguous single pending approval is taken."""
    from app import systems
    from app.db import Approval
    m = APPROVAL_RE.search(text)
    if not m:
        return None
    mine = systems.employees_by_phone(requester)
    if not mine:
        return None
    pending = (db.query(Approval).filter(Approval.approver.in_(mine), Approval.decision.is_(None))
               .order_by(Approval.id.desc()).all())
    if not pending:
        return None
    decision = "approved" if m.group(1).lower().startswith(("onay", "approve")) else "denied"
    if m.group(2):
        hit = [a for a in pending if a.id == int(m.group(2))]
        return (hit[0], decision) if hit else None
    return (pending[0], decision) if len(pending) == 1 else None


def handle_message(db, requester: str, text: str, image: bytes | None = None,
                   mime: str = "image/jpeg", extra_images: list[bytes] | None = None,
                   force_new: bool = False, channel: str | None = None, ticket: Ticket | None = None,
                   voice: bool = False) -> tuple[Ticket | None, str]:
    """Entry point for an employee message (optionally with a photo): reuse the open ticket
    or open a new one, then run L1. `force_new` skips the routing and always opens a ticket (system-generated
    tickets, a fresh e-mail); `ticket` pins the message to that ticket (an e-mail reply tagged [#id])."""
    from app.llm import image_message
    channel = channel or ("email" if "@" in requester else "whatsapp" if requester.startswith("+") else "api")
    if force_new:
        ticket = Ticket(requester=requester, title=text[:255], channel=channel)
        db.add(ticket)
        db.flush()
        log(db, ticket.id, "user", "message", text, {"has_image": image is not None, "voice": voice})
        answer = AGENTS["L1"].run(db, ticket, image_message(text, image, mime) if image else text)
        log(db, ticket.id, "L1", "message", answer)
        db.refresh(ticket)
        return ticket, answer
    pinned = ticket
    # 1) Is this an approver answering a pending approval? That wins over everything else, and the
    #    decision may sit inside a sentence ("tamam, ONAY 3" / "reddetmek için RET 1").
    decided = _approval_decision(db, requester, text)
    if decided is not None:
        approval, decision = decided
        return handle_approval(db, approval.id, decision, requester)
    open_tickets = (db.query(Ticket).filter(Ticket.requester == requester, Ticket.status != "resolved")
                    .order_by(Ticket.id.desc()).all())
    # 2) Did we just ask "continue where we left off?" on a channel switch?
    import time as _t
    pend = _RESUME.get(db, requester)
    if pinned is None and pend and pend["until"] > _t.time():
        _RESUME.drop(db, requester)
        held = db.get(Ticket, pend["ticket"])
        if held is not None and held.status != "resolved" and YES_RE.match(text):
            held.channel = channel
            if pend["pending"]:
                pinned, text = held, pend["pending"]            # the message that triggered the question, now on that ticket
            else:
                last = [e for e in held.events if e.kind == "message" and e.actor != "user"]
                answer = tr_("resume_ok", detect(text, None) or ticket_lang(held), id=held.id, last=(last[-1].content if last else "")[:300])
                log(db, held.id, "user", "message", text)
                log(db, held.id, "L1", "message", answer)
                db.refresh(held)
                return held, answer
        elif held is not None and NO_RE.match(text):
            held.channel = channel
            if pend["pending"]:
                return handle_message(db, requester, pend["pending"], image, mime, extra_images, force_new=True, channel=channel)
            return None, tr_("resume_new", detect(text, None) or DEFAULT_LANG)
        elif pend.get("pending"):
            text = f"{pend['pending']}\n{text}"             # their answer to our clarifying question, with what they said first
    elif pinned is None and not is_ack(text) and not is_greeting(text):
        held = recent_switch(db, requester, channel)
        if held is not None:                                   # mid-conversation channel switch: decide, ask only if unclear
            where = _route(db, held, text)
            if where == "same":
                held.channel = channel
                pinned = held
                log(db, held.id, "system", "note", f"employee continued on {channel}")
            elif where == "new":
                held.channel = channel
                return handle_message(db, requester, text, image, mime, extra_images, force_new=True, channel=channel)
            else:
                import time as _t
                lang = detect(text, None) or ticket_lang(held)
                _RESUME.set(db, requester, {"ticket": held.id, "pending": text, "until": _t.time() + 15 * 60})
                log(db, held.id, "system", "note", f"employee switched to {channel}; message unclear, asked for detail")
                return held, clarify(db, held, text, lang)
    # 3) Requests about the desk itself ("close all my tickets", "what is open?") act on the tickets, never open one.
    if pinned is None and CLOSE_ALL_RE.search(text) and len(text.split()) <= 12:
        lang = detect(text, None) or (ticket_lang(open_tickets[0]) if open_tickets else "tr")
        if not open_tickets:
            return None, tr_("nothing_open", lang)
        for t in open_tickets:
            run_tool(Ctx(db=db, ticket=t, agent="L1"), "close_ticket", {"resolution": "closed on the employee's request (all open tickets)"})
            log(db, t.id, "L1", "tool_call", "close_ticket", {"resolution": "closed on the employee's request (all open tickets)"})
            log(db, t.id, "user", "message", text)
            log(db, t.id, "L1", "message", tr_("closed_on_request", lang, id=t.id))
        ids = ", ".join(f"#{t.id}" for t in open_tickets)
        return open_tickets[0], tr_("closed_all", lang, ids=ids, n=len(open_tickets))
    if pinned is None and LIST_RE.match(text):
        lang = detect(text, None) or (ticket_lang(open_tickets[0]) if open_tickets else "tr")
        if not open_tickets:
            return None, tr_("nothing_open", lang)
        lines = "\n".join(f"#{t.id}: {t.title.split(chr(10))[0][:60]} ({tr_('st_' + t.status, lang)})" for t in open_tickets)
        return open_tickets[0], tr_("open_list", lang, n=len(open_tickets), lines=lines)
    # 4) A greeting is answered, never recorded: the desk is a conversation, a ticket is a request.
    if pinned is None and is_greeting(text):
        lang = detect(text, None) or (ticket_lang(open_tickets[0]) if open_tickets else "tr")
        offer = resume_offer(db, requester, channel, lang, greeting=True)      # hello on a new channel: greet and offer in one go
        if offer is not None:
            return offer
        if open_tickets:
            return open_tickets[0], tr_("greeting_open", lang, id=open_tickets[0].id, title=open_tickets[0].title[:60])
        return None, tr_("greeting", lang)
    ticket = pinned if pinned is not None else (open_tickets[0] if open_tickets else None)
    notice = ""
    short = is_ack(text)
    if pinned is not None:
        pass                                                   # the channel already knows which ticket this is
    elif short and RATING_RE.match(text) and _closing_exchange(db, requester, text) is not None:
        ticket = _closing_exchange(db, requester, text)      # a bare "5" right after a close is the rating
    elif len(open_tickets) > 1 and short:
        # several tickets open and a short answer: a number names the ticket; otherwise the one the desk was
        # just talking about (latest message within the hour) is meant; ask only when nothing is recent
        named = [t for t in open_tickets if re.search(rf"(?<!\d)#?{t.id}(?!\d)", text)]
        recent = _most_recent_conversation(open_tickets)
        if named:
            ticket = named[0]
            text = re.sub(rf"#?{ticket.id}", "", text, count=1).strip() or text
        elif recent is not None:
            ticket = recent
        else:
            choices = "\n".join(f"#{t.id}: {t.title[:60]}" for t in open_tickets)
            answer = tr_("which_ticket", detect(text, None) or ticket_lang(open_tickets[0]), choices=choices, first=open_tickets[0].id)
            log(db, open_tickets[0].id, "user", "message", text)
            log(db, open_tickets[0].id, "L1", "message", answer)
            db.refresh(open_tickets[0])
            return open_tickets[0], answer
    elif ticket is not None:
        where = _route(db, ticket, text)
        if where == "ask":                                    # too vague to place: ask instead of guessing
            answer = clarify(db, ticket, text, detect(text, None) or ticket_lang(ticket))
            log(db, ticket.id, "user", "message", text)
            log(db, ticket.id, "L1", "message", answer)
            db.refresh(ticket)
            return ticket, answer
        if where == "new":
            log(db, ticket.id, "system", "note", f"new unrelated request from requester, opened a separate ticket: {text[:80]}")
            ticket = None
    if ticket is None:
        ticket = _closing_exchange(db, requester, text)
    if ticket is None:
        if not _is_request(db, text):                       # small talk: answer, open nothing
            return None, tr_("greeting", detect(text, None) or DEFAULT_LANG)
        ticket = _reopen_if_same(db, requester, text, channel)   # the same problem back within 48 h: reopen, don't duplicate
    if ticket is None:
        ticket = Ticket(requester=requester, title=text[:255], channel=channel)
        db.add(ticket)
        db.flush()
        notice = emerging_incident(db, ticket, text)         # others reporting the same thing right now?
    ticket.channel = channel          # replies and reminders follow the employee's latest channel
    history = []
    for e in ticket.events:
        if e.kind == "message":
            history.append({"role": "user" if e.actor == "user" else "assistant", "content": e.content})
    log(db, ticket.id, "user", "message", text, {"has_image": image is not None, "voice": voice})
    # a plain "kapat" / "iptal" closes the ticket without a model round-trip
    if ticket.status != "resolved" and CLOSE_RE.match(text):
        run_tool(Ctx(db=db, ticket=ticket, agent="L1"), "close_ticket",
                 {"resolution": "closed on the employee's request"})
        log(db, ticket.id, "L1", "tool_call", "close_ticket", {"resolution": "closed on the employee's request"})
        answer = tr_("closed_on_request", detect(text, None) or ticket_lang(ticket), id=ticket.id)
        log(db, ticket.id, "L1", "message", answer)
        db.refresh(ticket)
        return ticket, answer
    agent_text = f"{text}\n\n{notice}" if notice else text
    if voice:
        agent_text = f"[voice note, transcribed] {agent_text}"
    content = image_message(agent_text, image, mime) if image else agent_text
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
    approver = systems.get_user(a.approver) or {}
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
