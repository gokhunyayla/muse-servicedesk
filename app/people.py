"""Joiners and leavers, started by HR and finished by the desk.

Onboarding: someone in the HR group writes on any channel (often with the CV attached). The agent reads the
details, asks HR for anything missing, and calls ``start_onboarding``. The desk then asks the new hire's manager
the configured questions (groups, licence, device, network access …) on the manager's own channel; the manager
may answer one by one or all at once (``record_people_answers``). When everything required is answered,
``complete_onboarding`` creates the account in the directory (Entra when connected), the groups, the licence and
mailbox, the support-page client record, sends the username to the new hire's personal e-mail and the first
password to the manager, and tells HR.

Offboarding mirrors it: HR names the employee and the last day, the manager says who takes over and how the
device comes back, and on the last day the account is closed, sessions ended, groups and licences removed.

Who may start these is a directory group (``hr`` by default, Settings → Channels → Joiners & leavers); the
questions are a list in the same place, so every company asks its own.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from app import systems
from app.db import Client, Followup, Outbound, PeopleProcess, Ticket, _now, log, resolve
from app.sla import get_setting
from app.tools import Ctx, STR, tool

logger = logging.getLogger("people")

DEFAULT_PEOPLE = {
    "hr_group": "hr",                 # directory group whose members may start joiners and leavers
    "vpn_group": "vpn-users",         # directory group that grants company network / VPN access
    "devices": "Standard laptop, Developer laptop, No device",
    "onboarding_questions": [
        {"key": "groups", "required": True,
         "en": "Which groups should they join (shared folders, mail lists, apps)?",
         "tr": "Hangi gruplara dahil olsun (ortak klasörler, e-posta listeleri, uygulamalar)?"},
        {"key": "license", "required": True,
         "en": "Which licence?", "tr": "Hangi lisans?"},
        {"key": "device", "required": True,
         "en": "Which device?", "tr": "Hangi cihaz?"},
        {"key": "vpn", "required": True,
         "en": "Access to the company network / VPN (yes / no)?", "tr": "Şirket ağı / VPN erişimi olsun mu (evet / hayır)?"},
        {"key": "other", "required": False,
         "en": "Anything else (shared mailboxes, applications)?", "tr": "Başka bir erişim (ortak posta kutusu, uygulama)?"},
    ],
    "offboarding_questions": [
        {"key": "handover", "required": True,
         "en": "Who takes over their e-mail and files?", "tr": "E-postalarını ve dosyalarını kim devralacak?"},
        {"key": "device_return", "required": True,
         "en": "How does the device come back (courier pick-up / hand-in at the office)?",
         "tr": "Cihaz nasıl iade edilecek (kurye / ofise teslim)?"},
    ],
}


def config() -> dict:
    return {**DEFAULT_PEOPLE, **get_setting("people", {})}


def _lang(ticket: Ticket | None) -> str:
    from app.i18n import DEFAULT_LANG, ticket_lang
    return ticket_lang(ticket) if ticket is not None else DEFAULT_LANG


def _contact(employee_no: str) -> str:
    u = systems.get_user(employee_no) or {}
    return u.get("phone") or systems.user_email(employee_no) or ""


def is_hr(contact: str) -> bool:
    """Is the person writing from this phone/address in the HR group?"""
    group = str(config().get("hr_group") or "hr").lower()
    for emp in systems.employees_by_contact(contact):
        u = systems.get_user(emp) or {}
        if group in [str(g).lower() for g in u.get("groups") or []]:
            return True
    return False


def _questions(kind: str) -> list[dict]:
    return list(config().get(f"{kind}_questions") or DEFAULT_PEOPLE[f"{kind}_questions"])


def _options_text(lang: str) -> str:
    """What the manager can choose from: the directory's groups and the licences with free seats."""
    tr = lang == "tr"
    lines = []
    try:
        g = systems._graph()
        if g:
            rows = g._call("GET", "/groups", params={"$select": "displayName,securityEnabled,mailEnabled,groupTypes", "$top": 100}).get("value", [])
            groups = sorted({x["displayName"] for x in rows if x.get("securityEnabled") and not x.get("mailEnabled")
                             and "Unified" not in (x.get("groupTypes") or [])})
        else:
            groups = sorted({x for u in systems.USERS.values() for x in u.get("groups", [])})
        if groups:
            lines.append(("Gruplar: " if tr else "Groups: ") + ", ".join(groups))
    except Exception as e:  # noqa: BLE001
        logger.warning("listing groups failed: %s", e)
    try:
        g = systems._graph()
        if g:
            cat = g.license_catalog()
            lic = [f"{k} ({v['available']} {'boş' if tr else 'free'})" for k, v in cat.items()]
        else:
            lic = list(systems.LICENSE_CATALOG)
        if lic:
            lines.append(("Lisanslar: " if tr else "Licences: ") + ", ".join(lic) + (", yok" if tr else ", none"))
    except Exception as e:  # noqa: BLE001
        logger.warning("listing licences failed: %s", e)
    lines.append(("Cihazlar: " if tr else "Devices: ") + str(config().get("devices", "")))
    return "\n".join(lines)


def _questionnaire(p: PeopleProcess, lang: str) -> str:
    tr = lang == "tr"
    d = p.data
    if p.kind == "onboarding":
        head = (f"Merhaba, İK yeni bir çalışan bildirdi: {d['name']} ({d.get('title') or d.get('department')}), "
                f"başlangıç {d['start_date']}, yöneticisi sizsiniz. Hesabını açabilmem için:" if tr else
                f"Hello, HR reported a new hire: {d['name']} ({d.get('title') or d.get('department')}), starting "
                f"{d['start_date']}, reporting to you. To set up their account:")
    else:
        head = (f"Merhaba, İK {d['name']} için ayrılış bildirdi; son gün {d['last_day']}. Hesabı kapatmadan önce:" if tr else
                f"Hello, HR reported that {d['name']} is leaving; last day {d['last_day']}. Before the account is closed:")
    qs = "\n".join(f"{i}. {q.get(lang) or q.get('en')}" for i, q in enumerate(_questions(p.kind), 1))
    tail = (("\n\nSeçenekler:\n" + _options_text(lang)) if p.kind == "onboarding" else "")
    end = ("\n\nTek tek ya da hepsini bir mesajda cevaplayabilirsiniz." if tr else
           "\n\nAnswer one by one or all in one message.")
    return f"{head}\n{qs}{tail}{end}"


def _open_manager_ticket(ctx: Ctx, p: PeopleProcess) -> Ticket:
    lang = _lang(ctx.ticket)
    kind_label = "Onboarding" if p.kind == "onboarding" else "Offboarding"
    channel = "email" if "@" in p.manager_contact else "whatsapp"
    mt = Ticket(requester=p.manager_contact, title=f"{kind_label} #{p.id}: {p.data['name']} — manager questions",
                channel=channel, status="open")
    ctx.db.add(mt)
    ctx.db.flush()
    p.manager_ticket_id = mt.id
    log(ctx.db, mt.id, "system", "note",
        f"[{kind_label} process] Started by HR ({p.hr_contact}) on ticket #{ctx.ticket.id}. The person writing on this "
        f"ticket is the manager ({p.manager_no}). Record whatever they answer with record_people_answers(answers={{…}}); "
        f"the desk finds the process from this ticket, and the last required answer carries it out. Ask only what "
        f"record_people_answers lists as missing; nothing else is needed (no surnames, addresses or device names).",
        data={"process_id": p.id, "kind": p.kind})
    text = _questionnaire(p, lang)
    log(ctx.db, mt.id, "L1", "message", text)
    ctx.db.add(Outbound(to=p.manager_contact, ticket_id=mt.id, text=text))
    return mt


def _process(ctx: Ctx, process_id: int | None, kind: str | None = None) -> PeopleProcess | dict:
    """The process this conversation is about. The ticket decides, not the number the model passes: a manager's
    ticket belongs to exactly one process, and a model easily confuses the ticket number with the process number."""
    q = ctx.db.query(PeopleProcess)
    if kind:
        q = q.filter(PeopleProcess.kind == kind)
    p = q.filter(PeopleProcess.manager_ticket_id == ctx.ticket.id).order_by(PeopleProcess.id.desc()).first()
    if p is None and process_id:
        cand = ctx.db.get(PeopleProcess, int(process_id))
        if cand is not None and (not kind or cand.kind == kind):
            p = cand
    if p is None:                                        # the manager wrote on a new ticket: their open process
        p = (q.filter(PeopleProcess.manager_contact == ctx.ticket.requester,
                      PeopleProcess.status == "waiting_manager").order_by(PeopleProcess.id.desc()).first())
    if p is None:
        return {"ok": False, "error": "no onboarding/offboarding is waiting for this person's answers"}
    return p


def _missing(p: PeopleProcess) -> list[str]:
    return [q["key"] for q in _questions(p.kind) if q.get("required") and not str(p.answers.get(q["key"], "")).strip()]


# ---- onboarding ---------------------------------------------------------------------------------------------

@tool("Start onboarding a new employee. Only people in the HR group may ask for it: the tool checks who is writing. "
      "Read the details from what HR wrote and from an attached CV ([Attached document: …]); personal_email and "
      "personal_phone are the new hire's own (from the CV). If the result lists missing fields, ask HR for them and "
      "call again. On success the desk asks the manager its questions on the manager's own channel.",
      ("L1",), name=STR, start_date=STR, department=STR, manager_employee_no=STR,
      title={"type": "string", "optional": True}, personal_email={"type": "string", "optional": True},
      personal_phone={"type": "string", "optional": True}, employee_no={"type": "string", "optional": True})
def start_onboarding(ctx: Ctx, name: str, start_date: str, department: str, manager_employee_no: str, title: str = "",
                     personal_email: str = "", personal_phone: str = "", employee_no: str = "") -> dict:
    if not is_hr(ctx.ticket.requester):
        return {"ok": False, "error": f"only members of the '{config()['hr_group']}' group may start an onboarding"}
    missing = [k for k, v in {"name": name, "start_date": start_date, "department": department,
                              "manager_employee_no": manager_employee_no}.items() if not str(v or "").strip()]
    if not (personal_email or personal_phone):
        missing.append("personal_email or personal_phone")
    if missing:
        return {"ok": False, "missing": missing, "note": "ask HR for these, then call start_onboarding again"}
    mgr = systems.get_user(manager_employee_no)
    if not mgr:
        return {"ok": False, "error": f"no employee {manager_employee_no} in the directory to be the manager"}
    contact = _contact(manager_employee_no)
    if not contact:
        return {"ok": False, "error": f"{mgr.get('name')} has no phone or e-mail the desk can reach"}
    if personal_phone and systems.employees_by_contact(personal_phone):
        return {"ok": False, "error": "that phone number already belongs to an employee; a number is never shared"}
    for open_p in ctx.db.query(PeopleProcess).filter(PeopleProcess.kind == "onboarding",
                                                     PeopleProcess.status.in_(("waiting_manager", "scheduled"))):
        if open_p.data.get("name", "").lower() == name.strip().lower():
            return {"ok": True, "process_id": open_p.id, "status": "already in progress; the manager was asked on "
                    f"ticket #{open_p.manager_ticket_id}", "duplicate": True}
    p = PeopleProcess(kind="onboarding", hr_contact=ctx.ticket.requester, hr_ticket_id=ctx.ticket.id,
                      manager_no=manager_employee_no, manager_contact=contact,
                      data={"name": name.strip(), "start_date": start_date.strip(), "department": department.strip(),
                            "title": (title or "").strip(), "personal_email": (personal_email or "").strip().lower(),
                            "personal_phone": (personal_phone or "").replace(" ", ""), "employee_no": (employee_no or "").strip()})
    ctx.db.add(p)
    ctx.db.flush()
    mt = _open_manager_ticket(ctx, p)
    ctx.ticket.status = "waiting_approval"
    return {"ok": True, "process_id": p.id, "manager": mgr.get("name"), "manager_ticket": mt.id,
            "status": "questions sent to the manager; HR is told when the account is ready"}


@tool("Record the manager's answers for an onboarding or offboarding (one, several or all at once). Keys are the "
      "question keys (onboarding: groups, license, device, vpn, other; offboarding: handover, device_return). "
      "Returns what is still missing.",
      ("L1",), process_id={"type": "integer", "optional": True, "description": "optional; the desk finds it from this ticket"},
      answers={"type": "object", "description": "question key -> the manager's answer", "additionalProperties": {"type": "string"}})
def record_people_answers(ctx: Ctx, answers: dict, process_id: int | None = None) -> dict:
    p = _process(ctx, process_id)
    if isinstance(p, dict):
        return p
    if ctx.ticket.requester != p.manager_contact:
        return {"ok": False, "error": "only the manager can answer these questions"}
    keys = {q["key"] for q in _questions(p.kind)}
    p.answers = {**(p.answers or {}), **{k: str(v).strip() for k, v in (answers or {}).items() if k in keys and str(v).strip()}}
    p.updated_at = _now()
    if _missing(p) or p.status != "waiting_manager":
        return {"ok": True, "answers": p.answers, "missing": _missing(p)}
    # every required answer is in: the manager's answers are the decision, carry it out now
    done = complete_onboarding(ctx, p.id) if p.kind == "onboarding" else complete_offboarding(ctx, p.id)
    return {"ok": True, "answers": p.answers, "missing": [], "completed": done}


def _match_license(answer: str) -> str | None:
    a = (answer or "").strip().lower()
    if not a or a in ("none", "yok", "no", "hayır", "-"):
        return None
    catalog = list(systems.license_catalog())
    for k in catalog:
        if a == k.lower() or k.lower() in a:
            return k
    friendly = {"O365_BUSINESS_ESSENTIALS": "business basic", "O365_BUSINESS_PREMIUM": "business standard",
                "SPB": "business premium", "SPE_E3": "e3", "SPE_E5": "e5"}
    for k in catalog:
        if friendly.get(k) and friendly[k] in a:
            return k
    import difflib                                       # a typo ("O365_BUSINESS_ESSENTIAL") is still that licence
    words = [w for w in a.replace(",", " ").split() if len(w) > 3] or [a]
    for w in words:
        hit = difflib.get_close_matches(w, [k.lower() for k in catalog], n=1, cutoff=0.85)
        if hit:
            return next(k for k in catalog if k.lower() == hit[0])
    raise ValueError(f"no licence matching '{answer}'; available: {', '.join(catalog) or 'none'}")


def _yes(answer: str) -> bool:
    return str(answer or "").strip().lower() in ("yes", "y", "evet", "e", "olsun", "true", "1", "var")


@tool("Create the new employee once the manager has answered every required question: directory account (Entra when "
      "connected), groups, licence and mailbox, network/VPN group, support-page client record; the username goes to "
      "their personal e-mail, the first password to the manager, a summary to HR. Call it on the manager's ticket "
      "after confirming the answers with them.",
      ("L1",), process_id={"type": "integer", "optional": True, "description": "optional; the desk finds it from this ticket"})
def complete_onboarding(ctx: Ctx, process_id: int | None = None) -> dict:
    p = _process(ctx, process_id, "onboarding")
    if isinstance(p, dict):
        return p
    if ctx.ticket.requester != p.manager_contact:
        return {"ok": False, "error": "the manager completes the onboarding on their own ticket"}
    if p.status == "done":
        return {"ok": True, "status": "already done", "result": p.result}
    if _missing(p):
        return {"ok": False, "missing": _missing(p)}
    d, a, done, problems = p.data, p.answers, {}, []
    try:
        license_key = _match_license(a.get("license", ""))
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    g = systems._graph()
    # 1. the account
    if g:
        extra = {"jobTitle": d.get("title") or None, "otherMails": [d["personal_email"]] if d.get("personal_email") else None}
        attr = g.config().get("employee_attr") or "employeeId"
        if d.get("employee_no") and attr == "employeeId":
            extra["employeeId"] = d["employee_no"]
        emp, upn, pw = g.create_user(d["name"], d["department"], p.manager_no, d.get("personal_phone", ""), extra=extra)
        if extra.get("employeeId"):
            emp = d["employee_no"]
    else:
        emp = systems.create_user(d["name"], d["department"], p.manager_no, d.get("personal_phone", ""))
        upn, pw = f"{emp.lower()}@company.com", systems.reset_password(emp)
        if d.get("personal_email"):
            systems.USERS[emp]["email"] = d["personal_email"]
    done.update(employee_no=emp, upn=upn)
    # 2. groups, network access, licence
    wanted = [x.strip() for x in a.get("groups", "").replace(";", ",").split(",") if x.strip() and x.strip().lower() not in ("none", "yok", "-")]
    if _yes(a.get("vpn", "")) and config().get("vpn_group"):
        wanted.append(config()["vpn_group"])
    wanted = list(dict.fromkeys(wanted))                   # "vpn-users" named and "VPN: yes" is one group
    joined = []
    for grp in wanted:
        try:
            if g:
                g.add_to_group(emp if not upn else upn, grp)
            else:
                systems.USERS[emp]["groups"].append(grp)
            joined.append(grp)
        except Exception as e:  # noqa: BLE001
            problems.append(f"group {grp}: {e}")
    done["groups"] = joined
    if license_key:
        try:
            if g:
                g.assign_license(upn, license_key)
            else:
                systems.USERS[emp]["licenses"].append(license_key)
            done["license"] = license_key
        except Exception as e:  # noqa: BLE001
            problems.append(f"licence {license_key}: {e}")
    # 3. device: from the inventory system (simulated until one is connected)
    device = a.get("device", "")
    if device and device.strip().lower() not in ("no device", "none", "yok", "cihaz yok"):
        done["device"] = f"{device} — ordered for {d['start_date']} (simulated inventory and courier)"
    # 4. support page: the new hire can reach the desk with their personal phone/e-mail before the first sign-in
    ctx.db.add(Client(name=d["name"], phone=d.get("personal_phone", ""), email=d.get("personal_email", ""), employee_no=emp))
    # 5. who hears what
    lang = _lang(ctx.db.get(Ticket, p.hr_ticket_id) if p.hr_ticket_id else None)
    tr = lang == "tr"
    from app.config import auth_settings
    support = f"{auth_settings.public_url.rstrip('/')}/support-agent"
    if d.get("personal_email"):
        from app.channels import send_email
        body = ((f"Merhaba {d['name']},\n\nAramıza hoş geldiniz! {d['start_date']} tarihinde başlıyorsunuz.\n\n"
                 f"Kullanıcı adınız: {upn}\nİlk şifrenizi ilk gün yöneticiniz verecek; ilk girişte değiştirmeniz istenecek.\n\n"
                 f"Bir sorununuz olursa BT servis masası: {support} (bu e-posta adresinizle girebilirsiniz).\n")
                if tr else
                (f"Hello {d['name']},\n\nWelcome aboard! You start on {d['start_date']}.\n\nYour username: {upn}\n"
                 f"Your manager will give you your first password on day one; you will be asked to change it at the first sign-in.\n\n"
                 f"If anything does not work, the IT service desk is at {support} (sign in with this e-mail address).\n"))
        ok, err = send_email(d["personal_email"], "Hoş geldiniz / Welcome" if tr else "Welcome", body)
        done["welcome_mail"] = "sent" if ok else f"failed: {err}"
    ctx.db.add(Outbound(to=p.manager_contact, ticket_id=ctx.ticket.id,
                        text=(f"{d['name']} için hesap hazır. Kullanıcı adı: {upn} · İlk şifre: {pw} (ilk girişte değişecek). "
                              f"Lütfen ilk gün kendisine siz iletin." if tr else
                              f"The account for {d['name']} is ready. Username: {upn} · First password: {pw} (changed at the "
                              f"first sign-in). Please hand it over on day one.")))
    summary = (f"{d['name']}: {upn}" + (f", lisans {done['license']}" if tr and done.get("license") else f", licence {done['license']}" if done.get("license") else "")
               + (f", gruplar {', '.join(joined)}" if tr and joined else f", groups {', '.join(joined)}" if joined else "")
               + (f", {done['device']}" if done.get("device") else ""))
    if p.hr_ticket_id:
        hr = ctx.db.get(Ticket, p.hr_ticket_id)
        if hr is not None:
            hr.resolution = f"onboarding done: {summary}"
            resolve(hr)
            log(ctx.db, hr.id, "system", "note", f"onboarding #{p.id} done", data=done)
            ctx.db.add(Outbound(to=p.hr_contact, ticket_id=hr.id,
                                text=(f"İşe giriş tamamlandı — {summary}. Kullanıcı adı kişisel e-postasına, ilk şifre yöneticisine gönderildi."
                                      if tr else f"Onboarding complete — {summary}. Username sent to their personal e-mail, first password to the manager.")))
    p.status, p.result, p.updated_at = ("done" if not problems else "done_with_problems"), {**done, "problems": problems}, _now()
    ctx.ticket.resolution = f"onboarding #{p.id} done"
    return {"ok": True, "employee_no": emp, "upn": upn, "result": done, "problems": problems,
            "note": "first password sent to the manager by message; do not repeat it"}


# ---- offboarding ---------------------------------------------------------------------------------------------

@tool("Start offboarding an employee who is leaving. Only people in the HR group may ask for it. employee_no is "
      "their employee number OR their work e-mail / user name (people without an employee number are identified by "
      "it; never ask HR for a number when you have the address). last_day as YYYY-MM-DD ('today' → today's date). "
      "The desk asks the manager who takes over and how the device comes back; on the last day the account is "
      "closed, sessions ended, groups and licences removed.",
      ("L1",), employee_no=STR, last_day=STR, reason={"type": "string", "optional": True})
def start_offboarding(ctx: Ctx, employee_no: str, last_day: str, reason: str = "") -> dict:
    if not is_hr(ctx.ticket.requester):
        return {"ok": False, "error": f"only members of the '{config()['hr_group']}' group may start an offboarding"}
    u = systems.get_user(employee_no)
    if not u:
        return {"ok": False, "error": f"no employee {employee_no}"}
    try:
        date.fromisoformat(last_day.strip())
    except ValueError:
        return {"ok": False, "missing": ["last_day as YYYY-MM-DD"]}
    mgr = u.get("manager")
    contact = _contact(mgr) if mgr else ""
    if not contact:
        return {"ok": False, "error": "the employee has no manager the desk can reach; ask HR who decides the handover"}
    for open_p in ctx.db.query(PeopleProcess).filter(PeopleProcess.kind == "offboarding",
                                                     PeopleProcess.status.in_(("waiting_manager", "scheduled"))):
        if open_p.data.get("employee_no") == employee_no:
            return {"ok": True, "process_id": open_p.id, "status": "already in progress", "duplicate": True}
    p = PeopleProcess(kind="offboarding", hr_contact=ctx.ticket.requester, hr_ticket_id=ctx.ticket.id, manager_no=mgr,
                      manager_contact=contact, data={"name": u.get("name", employee_no), "employee_no": employee_no,
                                                    "upn": u.get("upn", ""), "last_day": last_day.strip(), "reason": reason})
    ctx.db.add(p)
    ctx.db.flush()
    mt = _open_manager_ticket(ctx, p)
    ctx.ticket.status = "waiting_approval"
    return {"ok": True, "process_id": p.id, "manager_ticket": mt.id, "status": "questions sent to the manager"}


def _end_of_day_utc(day: str) -> datetime:
    import os
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(os.getenv("DESK_TZ", "UTC"))
    local = datetime.combine(date.fromisoformat(day), datetime.min.time()).replace(hour=18, tzinfo=tz)
    return local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


@tool("Confirm an offboarding once the manager answered the required questions: it runs at the end of the last "
      "day (now, if that has passed). Call it on the manager's ticket.", ("L1",),
      process_id={"type": "integer", "optional": True, "description": "optional; the desk finds it from this ticket"})
def complete_offboarding(ctx: Ctx, process_id: int | None = None) -> dict:
    p = _process(ctx, process_id, "offboarding")
    if isinstance(p, dict):
        return p
    if ctx.ticket.requester != p.manager_contact:
        return {"ok": False, "error": "the manager confirms the offboarding on their own ticket"}
    if _missing(p):
        return {"ok": False, "missing": _missing(p)}
    if p.status in ("scheduled", "done", "done_with_problems"):
        return {"ok": True, "status": f"already {p.status}"}
    when = _end_of_day_utc(p.data["last_day"])
    if when > _now():
        p.status = "scheduled"
        ctx.db.add(Followup(ticket_id=p.hr_ticket_id or ctx.ticket.id, due_at=when, note=f"offboarding:{p.id}"))
        return {"ok": True, "status": "scheduled", "runs_at_utc": when.isoformat(timespec="minutes")}
    return {"ok": True, **execute_offboarding(ctx.db, p.id)}


def execute_offboarding(db, process_id: int) -> dict:
    """The last day has ended: close everything and tell HR and the manager. Each step is attempted; failures are reported."""
    p = db.get(PeopleProcess, process_id)
    if p is None or p.status == "done":
        return {"status": "nothing to do"}
    emp = p.data["employee_no"]
    g = systems._graph()
    done, problems = [], []

    def step(label, fn):
        try:
            fn()
            done.append(label)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{label}: {e}")
    step("account disabled", lambda: systems.set_enabled(emp, False))
    if g:
        step("sessions revoked", lambda: g.revoke_sessions(emp))
        step("groups removed", lambda: g.remove_from_groups(emp))

        def licences():
            rec = g.get_user(emp) or {}
            for sku in rec.get("licenses", []):
                g.revoke_license(emp, sku)
        step("licences removed", licences)
    else:
        step("groups removed", lambda: systems.USERS[emp].update(groups=[]))
        step("licences removed", lambda: systems.USERS[emp].update(licenses=[]))
    for c in db.query(Client).filter(Client.employee_no == emp).all():
        c.enabled = False
    done.append("support-page access closed")
    a = p.answers or {}
    done.append(f"device return: {a.get('device_return', '—')} (simulated courier)")
    handover = a.get("handover", "")
    p.status, p.result, p.updated_at = ("done" if not problems else "done_with_problems"), {"done": done, "problems": problems, "handover": handover}, _now()
    lang = _lang(db.get(Ticket, p.hr_ticket_id) if p.hr_ticket_id else None)
    tr = lang == "tr"
    text = ((f"{p.data['name']} için ayrılış tamamlandı: " + "; ".join(done) + (f". Sorunlar: {'; '.join(problems)}" if problems else "")
             + (f". E-posta ve dosya devri ({handover}) bir mühendise iletildi." if handover else "."))
            if tr else
            (f"Offboarding of {p.data['name']} complete: " + "; ".join(done) + (f". Problems: {'; '.join(problems)}" if problems else "")
             + (f". Mailbox and file handover to {handover} is passed to an engineer." if handover else ".")))
    for to, tid in ((p.hr_contact, p.hr_ticket_id), (p.manager_contact, p.manager_ticket_id)):
        if to:
            db.add(Outbound(to=to, ticket_id=tid, text=text))
    for tid in (p.hr_ticket_id, p.manager_ticket_id):
        t = db.get(Ticket, tid) if tid else None
        if t is not None and t.status != "resolved":
            t.resolution = f"offboarding #{p.id} done"
            resolve(t)
            log(db, t.id, "system", "note", text, data=p.result)
    db.flush()
    return {"status": p.status, "done": done, "problems": problems}
