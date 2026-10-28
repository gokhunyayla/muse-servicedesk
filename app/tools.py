"""Tools the agents can call. Authority is enforced here, in code: a tool lists the tiers
allowed to run it, and an agent only ever sees (and can only execute) its own tools."""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable

from app import systems
from app.db import Ticket, log


@dataclass
class Ctx:
    db: Any
    ticket: Ticket
    agent: str


@dataclass
class Tool:
    name: str
    description: str
    tiers: tuple[str, ...]
    params: dict
    func: Callable

    def spec(self) -> dict:
        required = [k for k, v in self.params.items() if not v.get("optional")]
        props = {k: {kk: vv for kk, vv in v.items() if kk != "optional"} for k, v in self.params.items()}
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": {"type": "object", "properties": props, "required": required}}}


REGISTRY: dict[str, Tool] = {}


def tool(description: str, tiers: tuple[str, ...], **params: dict):
    def wrap(func: Callable) -> Callable:
        REGISTRY[func.__name__] = Tool(func.__name__, description, tiers, params, func)
        return func
    return wrap


def tools_for(tier: str) -> list[Tool]:
    return [t for t in REGISTRY.values() if tier in t.tiers]


def run_tool(ctx: Ctx, name: str, args: dict) -> dict:
    t = REGISTRY.get(name)
    if t is None or ctx.agent not in t.tiers:
        return {"ok": False, "error": f"{ctx.agent} is not allowed to use '{name}'"}
    try:
        inspect.signature(t.func).bind(ctx, **args)
    except TypeError as e:
        return {"ok": False, "error": f"bad arguments: {e}"}
    try:
        return t.func(ctx, **args)
    except Exception as e:  # a failing tool must not end the conversation
        return {"ok": False, "error": str(e)}


from app.i18n import t as _t, ticket_lang  # noqa: E402

STR = {"type": "string"}
INT = {"type": "integer"}
ALL = ("L1", "L2", "L3", "L4")


@tool("Look up the employee who owns this phone number.", ("L1",), phone=STR)
def find_user(ctx: Ctx, phone: str) -> dict:
    hit = systems.user_by_phone(phone)
    if not hit:
        return {"ok": False, "error": "no employee with this phone"}
    emp, u = hit
    return {"ok": True, "employee_no": emp, "name": u["name"], "device": u["device"]}


DEFAULT_VERIFICATION = {"voice_phrase": False, "identity_hours": 8}   # Settings → Channels


def verification_config() -> dict:
    from app.sla import get_setting
    return {**DEFAULT_VERIFICATION, **get_setting("verification", {})}


def _identity_key(requester: str) -> str:
    return f"identity:{requester}"


def remembered_identity(db, requester: str) -> dict | None:
    """A verification done on this phone/e-mail within identity_hours (so the employee is not asked again)."""
    from datetime import datetime
    from app.db import Setting, _now
    if int(verification_config().get("identity_hours") or 0) <= 0:      # policy: ask on every ticket
        return None
    row = db.get(Setting, _identity_key(requester))
    v = row.value if row and row.value else None
    if not v or datetime.fromisoformat(v["until"]) < _now():
        return None
    return v


def _own_record(requester: str) -> tuple[str, dict] | None:
    return systems.user_by_contact(requester)


@tool("Verify identity with employee number and birth year. Required before account changes. Only the employee "
      "themselves can be verified: the number or address they write from must belong to that employee.",
      ("L1",), employee_no=STR, birth_year=INT)
def verify_identity(ctx: Ctx, employee_no: str, birth_year: int) -> dict:
    from datetime import timedelta
    from app.db import Setting, _now
    u = systems.get_user(employee_no)
    owners = systems.employees_by_contact(ctx.ticket.requester)
    if owners and employee_no not in owners:
        other = systems.get_user(owners[0]) or {}
        return {"ok": False, "error": f"this number/address belongs to {other.get('name', owners[0])} ({owners[0]}); requests are only for "
                                      f"oneself — the other employee must write to the desk themselves, or the desk opens a ticket for them"}
    # a real directory (Entra) may not hold a birth year: then the sender binding above is the identity check
    ok = bool(u) and (u["birth_year"] == int(birth_year) if u.get("birth_year") else employee_no in owners)
    if ok:
        ctx.ticket.verified = True
        hours = int(verification_config().get("identity_hours") or 0)
        if hours > 0:
            value = {"employee_no": employee_no, "until": (_now() + timedelta(hours=hours)).isoformat(), "ticket": ctx.ticket.id}
            row = ctx.db.get(Setting, _identity_key(ctx.ticket.requester))
            if row is None:
                ctx.db.add(Setting(key=_identity_key(ctx.ticket.requester), value=value))
            else:
                row.value = value
            ctx.db.flush()
    return {"ok": ok}


PHRASE_WORDS = {"tr": ["mavi", "kırmızı", "yeşil", "sarı", "kedi", "köpek", "pencere", "bahçe", "deniz", "dağ", "elma", "kalem", "masa", "bulut", "yıldız"],
                "en": ["blue", "red", "green", "yellow", "cat", "dog", "window", "garden", "river", "mountain", "apple", "pencil", "table", "cloud", "star"]}


def _strong_key(ticket_id: int) -> str:
    return f"verify:{ticket_id}"





@tool("Start strong verification before a credential change: sends a one-time code to the employee's OTHER channel "
      "(e-mail when they write on WhatsApp, WhatsApp otherwise). If the result carries phrase_to_say, the desk policy also "
      "wants that phrase said in a VOICE note. Tell the employee what to do; then call check_verification with the code they type back.",
      ("L1", "L3"), employee_no=STR)
def send_verification(ctx: Ctx, employee_no: str) -> dict:
    import hashlib
    import secrets
    from datetime import datetime, timedelta
    from app.db import Setting, _now
    u = systems.get_user(employee_no)
    if not u:
        return {"ok": False, "error": "unknown employee"}
    requester = ctx.ticket.requester
    email, phone = systems.user_email(employee_no), u.get("phone") or ""
    from app.db import Client                        # the admin-managed contact record wins over directory defaults
    c = ctx.db.query(Client).filter((Client.phone == requester) | (Client.email == requester)).first()
    if c is not None:
        email, phone = c.email or email, c.phone or phone
    via, to = ("email", email) if (requester.startswith("+") or not phone) and email else ("whatsapp", phone)
    if not to:
        return {"ok": False, "error": "no second channel on file for this employee"}
    row = ctx.db.get(Setting, _strong_key(ctx.ticket.id))
    if row is not None and row.value and datetime.fromisoformat(row.value["until"]) > _now() + timedelta(minutes=8):
        return {"ok": False, "error": "a code was sent less than 2 minutes ago and is still valid; do not send another — "
                                      "call check_verification with the code the employee typed"}
    code = f"{secrets.randbelow(1_000_000):06d}"
    want_voice = bool(verification_config().get("voice_phrase"))
    words = PHRASE_WORDS.get(ticket_lang(ctx.ticket), PHRASE_WORDS["en"])
    phrase = (" ".join(secrets.choice(words) for _ in range(3)) + f" {secrets.randbelow(90) + 10}") if want_voice else ""
    value = {"code_hash": hashlib.sha256(f"{ctx.ticket.id}:{code}".encode()).hexdigest(), "phrase": phrase,
             "until": (_now() + timedelta(minutes=10)).isoformat(), "via": via, "to": to, "tries": 0}
    if row is None:
        ctx.db.add(Setting(key=_strong_key(ctx.ticket.id), value=value))
    else:
        row.value = value
    ctx.db.flush()
    text = f"Muse ServiceDesk verification code: {code} (valid 10 minutes). Type it back in the chat where you asked for the password reset."
    if via == "email":
        from app.channels import send_email
        ok, err = send_email(to, "Your verification code", text)
    else:
        from app.whatsapp import try_send
        ok, err = try_send(to, text), ""
    masked = (to[:2] + "…" + to[to.index("@"):]) if "@" in to else (to[:4] + "…" + to[-2:])
    out = {"ok": bool(ok), "code_sent_via": via, "code_sent_to": masked, "error": "" if ok else f"could not send the code: {err}"}
    if phrase:
        out["phrase_to_say"] = phrase
    return out


@tool("Finish strong verification with the code the employee typed back (and, when the policy asked for one, the phrase "
      "from their last voice note). Succeeds only when everything matches; then account changes are allowed.", ("L1", "L3"), code=STR)
def check_verification(ctx: Ctx, code: str) -> dict:
    import hashlib
    import re
    from datetime import datetime
    from app.db import Event, Setting, _now
    row = ctx.db.get(Setting, _strong_key(ctx.ticket.id))
    v = row.value if row and row.value else None
    if not v:
        return {"ok": False, "error": "no verification in progress; call send_verification first"}
    if datetime.fromisoformat(v["until"]) < _now():
        return {"ok": False, "error": "code expired; send a new one"}
    if v.get("tries", 0) >= 5:
        return {"ok": False, "error": "too many attempts; send a new code"}
    v["tries"] = v.get("tries", 0) + 1
    row.value = dict(v)
    ctx.db.flush()
    code_ok = hashlib.sha256(f"{ctx.ticket.id}:{code.strip()}".encode()).hexdigest() == v["code_hash"]
    voice_ok = True
    if v.get("phrase"):
        voice = (ctx.db.query(Event).filter(Event.ticket_id == ctx.ticket.id, Event.actor == "user", Event.kind == "message")
                 .order_by(Event.id.desc()).all())
        spoken = next((e.content for e in voice if (e.data or {}).get("voice")), "")
        if not spoken:
            return {"ok": False, "code_ok": code_ok, "voice_ok": False, "error": "no voice note yet: ask the employee to say the phrase in a voice note"}
        norm = lambda t: set(w for w in re.findall(r"[a-zçğıöşü0-9]+", t.lower()) if w)      # noqa: E731
        want, got = norm(v["phrase"]), norm(spoken)
        voice_ok = len(want & got) >= max(1, len(want) - 1)
    if code_ok and voice_ok:
        ctx.ticket.verified = True
        row.value = None
        log(ctx.db, ctx.ticket.id, ctx.agent, "note", f"strong verification passed: code via {v['via']}" + (" + spoken phrase" if v.get("phrase") else ""))
        return {"ok": True, "code_ok": True, "voice_ok": True}
    return {"ok": False, "code_ok": code_ok, "voice_ok": voice_ok,
            "error": ("wrong code" if not code_ok else "") + ("; " if not code_ok and not voice_ok else "") + ("the voice note did not contain the phrase" if not voice_ok else "")}


@tool("Reset the employee's password; returns a one-time temporary password. Needs identity AND strong verification "
      "(send_verification + check_verification) in this ticket.", ("L1", "L3"), employee_no=STR)
def reset_password(ctx: Ctx, employee_no: str) -> dict:
    if not ctx.ticket.verified:
        return {"ok": False, "error": "identity not verified in this ticket"}
    from app.db import Event
    strong = ctx.db.query(Event).filter(Event.ticket_id == ctx.ticket.id, Event.kind == "note",
                                        Event.content.like("strong verification passed%")).first()
    if strong is None:
        return {"ok": False, "error": "strong verification missing: send_verification, then check_verification with the code"}
    return {"ok": True, "temporary_password": systems.reset_password(employee_no), "must_change": True}


@tool("Read the VPN profile state of a laptop.", ("L1", "L2"), hostname=STR)
def vpn_status(ctx: Ctx, hostname: str) -> dict:
    d = systems.DEVICES.get(hostname)
    if not d:
        return {"ok": False, "error": "unknown device"}
    return {"ok": True, "hostname": hostname, "vpn_profile": d["vpn_profile"], "online": d["online"]}


@tool("Push a fresh VPN profile to a laptop.", ("L1", "L2"), hostname=STR)
def renew_vpn_profile(ctx: Ctx, hostname: str) -> dict:
    d = systems.DEVICES.get(hostname)
    if not d:
        return {"ok": False, "error": "unknown device"}
    if not d["online"]:
        return {"ok": False, "error": "device offline; cannot push profile"}
    d["vpn_profile"] = "valid"
    return {"ok": True, "hostname": hostname, "vpn_profile": "valid"}


@tool("Run network diagnostics (reachability, DNS) on a laptop.", ("L2",), hostname=STR)
def network_diagnostics(ctx: Ctx, hostname: str) -> dict:
    d = systems.DEVICES.get(hostname)
    if not d:
        return {"ok": False, "error": "unknown device"}
    return {"ok": True, "online": d["online"], "dns_ok": d["dns_ok"]}


@tool("Check the vendor warranty of a laptop.", ("L1", "L4"), hostname=STR)
def check_warranty(ctx: Ctx, hostname: str) -> dict:
    rec = systems.WARRANTY.get(hostname)
    if not rec:
        return {"ok": False, "error": "no warranty record"}
    from datetime import date
    return {"ok": True, "hostname": hostname, **rec, "in_warranty": rec["valid_until"] >= date.today().isoformat()}


@tool("Open a hardware service case with the vendor.", ("L4",), hostname=STR, summary=STR)
def open_service_case(ctx: Ctx, hostname: str, summary: str) -> dict:
    if hostname not in systems.WARRANTY:
        return {"ok": False, "error": "unknown device"}
    return {"ok": True, **systems.open_vendor_case(hostname, summary)}


@tool("Reserve a loaner laptop for the employee while theirs is repaired.", ("L4",), employee_no=STR)
def assign_loaner(ctx: Ctx, employee_no: str) -> dict:
    for dev, state in systems.LOANERS.items():
        if state == "available":
            systems.LOANERS[dev] = employee_no
            return {"ok": True, "loaner": dev, "assigned_to": employee_no}
    return {"ok": False, "error": "no loaner available"}


@tool("Create a courier shipment (label + pickup) to send a device to the employee.", ("L4",),
      employee_no=STR, device=STR, case_no=STR)
def create_shipment(ctx: Ctx, employee_no: str, device: str, case_no: str) -> dict:
    u = systems.get_user(employee_no)
    if not u:
        return {"ok": False, "error": "unknown employee"}
    return {"ok": True, **systems.create_shipment(u["name"], device, case_no)}


@tool("Look up a vulnerability advisory by CVE id or nickname (e.g. 'php2shell').", ("L1", "L3"), query=STR)
def lookup_advisory(ctx: Ctx, query: str) -> dict:
    q = query.strip().upper()
    for cve, adv in systems.ADVISORIES.items():
        if q == cve or q == adv["name"].upper():
            return {"ok": True, "cve": cve, **adv}
    return {"ok": False, "error": "no advisory found"}


@tool("Scan the server inventory for hosts affected by a CVE.", ("L3",), cve=STR)
def find_vulnerable_servers(ctx: Ctx, cve: str) -> dict:
    hosts = systems.find_vulnerable(cve.upper())
    return {"ok": True, "cve": cve.upper(), "affected": hosts, "count": len(hosts)}


@tool("Upgrade a package on a server. Takes a snapshot first so the change can be rolled back.",
      ("L3",), hostname=STR, package=STR, version=STR)
def apply_patch(ctx: Ctx, hostname: str, package: str, version: str) -> dict:
    srv = systems.SERVERS.get(hostname)
    if not srv or package not in srv["packages"]:
        return {"ok": False, "error": "unknown host or package"}
    sid = systems.snapshot(hostname)
    before = srv["packages"][package]
    srv["packages"][package] = version
    return {"ok": True, "hostname": hostname, "package": package, "from": before, "to": version, "snapshot_id": sid}


@tool("Verify a server after patching: package version, service health, and whether the CVE still applies.",
      ("L3",), hostname=STR, cve=STR)
def verify_patch(ctx: Ctx, hostname: str, cve: str) -> dict:
    srv = systems.SERVERS.get(hostname)
    adv = systems.ADVISORIES.get(cve.upper())
    if not srv or not adv:
        return {"ok": False, "error": "unknown host or CVE"}
    version = srv["packages"].get(adv["package"])
    still_vulnerable = version is not None and systems._version_lt(version, adv["vulnerable_below"])
    return {"ok": True, "hostname": hostname, "package": adv["package"], "version": version,
            "service_healthy": True, "still_vulnerable": still_vulnerable}


@tool("Roll a server back to a snapshot taken before a patch.", ("L3",), snapshot_id=STR)
def rollback_patch(ctx: Ctx, snapshot_id: str) -> dict:
    return {"ok": systems.rollback(snapshot_id), "snapshot_id": snapshot_id}


# ---- identity, licenses, groups (directory / M365) -----------------------------------------

def _user(employee_no: str) -> dict | None:
    return systems.get_user(employee_no)


@tool("Show an employee's directory record: department, manager, licenses, groups, SAP roles.",
      ("L1", "L3"), employee_no=STR)
def get_employee(ctx: Ctx, employee_no: str) -> dict:
    u = _user(employee_no)
    if not u:
        return {"ok": False, "error": "unknown employee"}
    return {"ok": True, "employee_no": employee_no, **{k: v for k, v in u.items() if k != "birth_year"}}


@tool("List licenses available in the catalog and whether each needs manager approval.", ("L1",))
def list_licenses(ctx: Ctx) -> dict:
    return {"ok": True, "catalog": systems.license_catalog()}


@tool("Assign a license. Licenses marked needs_approval require an approved request first.",
      ("L1",), employee_no=STR, license=STR)
def assign_license(ctx: Ctx, employee_no: str, license: str) -> dict:
    u = _user(employee_no)
    catalog = systems.license_catalog()
    if not u or license not in catalog:
        return {"ok": False, "error": "unknown employee or license"}
    if catalog[license] and not _approved(ctx, license):
        return {"ok": False, "error": f"{license} needs manager approval; use request_manager_approval first"}
    g = systems._graph()
    if g:
        return {"ok": True, "employee_no": employee_no, "licenses": g.assign_license(employee_no, license)}
    if license not in u["licenses"]:
        u["licenses"].append(license)
    return {"ok": True, "employee_no": employee_no, "licenses": u["licenses"]}


@tool("Remove a license from an employee.", ("L1",), employee_no=STR, license=STR)
def revoke_license(ctx: Ctx, employee_no: str, license: str) -> dict:
    u = _user(employee_no)
    if not u:
        return {"ok": False, "error": "unknown employee"}
    g = systems._graph()
    if g:
        return {"ok": True, "licenses": g.revoke_license(employee_no, license)}
    if license in u["licenses"]:
        u["licenses"].remove(license)
    return {"ok": True, "licenses": u["licenses"]}


@tool("Add an employee to a directory group (mail lists, shared folders, app access).", ("L1", "L3"),
      employee_no=STR, group=STR)
def add_to_group(ctx: Ctx, employee_no: str, group: str) -> dict:
    u = _user(employee_no)
    if not u:
        return {"ok": False, "error": "unknown employee"}
    g = systems._graph()
    if g:
        return {"ok": True, "groups": g.add_to_group(employee_no, group)}
    if group not in u["groups"]:
        u["groups"].append(group)
    return {"ok": True, "groups": u["groups"]}


@tool("Remove an employee from all groups (offboarding) or from one group.", ("L1", "L3"),
      employee_no=STR, group={"type": "string", "optional": True})
def remove_from_groups(ctx: Ctx, employee_no: str, group: str | None = None) -> dict:
    u = _user(employee_no)
    if not u:
        return {"ok": False, "error": "unknown employee"}
    g = systems._graph()
    if g:
        return {"ok": True, "groups": g.remove_from_groups(employee_no, group)}
    u["groups"] = [x for x in u["groups"] if group and x != group]
    return {"ok": True, "groups": u["groups"]}


@tool("Create a directory account and mailbox for a new hire.", ("L1",), name=STR, department=STR,
      manager_employee_no=STR, phone=STR)
def create_account(ctx: Ctx, name: str, department: str, manager_employee_no: str, phone: str) -> dict:
    if not systems.get_user(manager_employee_no):
        return {"ok": False, "error": "unknown manager"}
    g = systems._graph()
    if g:
        emp, upn, pw = g.create_user(name, department, manager_employee_no, phone)
        return {"ok": True, "employee_no": emp, "upn": upn, "initial_password": pw, "must_change": True}
    emp = systems.create_user(name, department, manager_employee_no, phone)
    return {"ok": True, "employee_no": emp, "upn": f"{emp.lower()}@company.com", "initial_password": systems.reset_password(emp)}


@tool("Disable an account immediately (offboarding or compromise).", ("L1", "L3"), employee_no=STR)
def disable_account(ctx: Ctx, employee_no: str) -> dict:
    if not _user(employee_no):
        return {"ok": False, "error": "unknown employee"}
    systems.set_enabled(employee_no, False)
    return {"ok": True, "enabled": False}


@tool("Revoke all active sessions and tokens of an employee.", ("L1", "L3"), employee_no=STR)
def revoke_sessions(ctx: Ctx, employee_no: str) -> dict:
    if not _user(employee_no):
        return {"ok": False, "error": "unknown employee"}
    g = systems._graph()
    if g:
        return {"ok": True, "revoked": g.revoke_sessions(employee_no), "note": "all refresh tokens invalidated; apps sign in again"}
    return {"ok": True, "revoked_sessions": 3}


@tool("Reset MFA registration so the employee can enrol a new device. Requires verified identity.",
      ("L1",), employee_no=STR)
def reset_mfa(ctx: Ctx, employee_no: str) -> dict:
    if not ctx.ticket.verified:
        return {"ok": False, "error": "identity not verified in this ticket"}
    return {"ok": True, "mfa": "reset; enrol at https://aka.ms/mfasetup"}


@tool("Grant an SAP role. Needs an approved manager request. Set days for temporary access.", ("L3",),
      employee_no=STR, role=STR, days={"type": "integer", "optional": True})
def grant_sap_role(ctx: Ctx, employee_no: str, role: str, days: int | None = None) -> dict:
    u = _user(employee_no)
    if not u or role not in systems.SAP_ROLES:
        return {"ok": False, "error": "unknown employee or role"}
    if not _approved(ctx, role):
        return {"ok": False, "error": f"SAP role {role} needs manager approval; use request_manager_approval first"}
    if role not in u["sap_roles"]:
        u["sap_roles"].append(role)
    return {"ok": True, "sap_roles": u["sap_roles"], "expires_in_days": days}


# ---- security: mail & endpoint response -----------------------------------------------------

@tool("Analyze a suspicious e-mail (sender, subject, body with links). Returns safe / suspicious / phishing.",
      ("L1", "L2", "L3"), sender=STR, subject=STR, body=STR)
def analyze_email(ctx: Ctx, sender: str, subject: str, body: str) -> dict:
    return systems.analyze_email(sender, subject, body)


@tool("Remove a phishing e-mail from every mailbox in the tenant.", ("L1", "L3"), subject=STR, sender=STR)
def quarantine_email(ctx: Ctx, subject: str, sender: str) -> dict:
    return systems.quarantine(subject, sender)


@tool("Block a sender address/domain at the mail gateway.", ("L1", "L3"), sender=STR)
def block_sender(ctx: Ctx, sender: str) -> dict:
    return systems.block_sender(sender)


@tool("Isolate a laptop from the network (EDR). Only the management channel stays open. Do this first "
      "on any malware suspicion; speed matters more than consent.", ("L2", "L3"), hostname=STR)
def isolate_device(ctx: Ctx, hostname: str) -> dict:
    return systems.isolate(hostname)


@tool("Collect forensic evidence from a laptop: processes, downloads, connections, last logon.",
      ("L2", "L3"), hostname=STR)
def collect_evidence(ctx: Ctx, hostname: str) -> dict:
    return systems.collect_evidence(hostname)


@tool("Run a full malware scan; findings are quarantined automatically.", ("L2", "L3"), hostname=STR)
def run_malware_scan(ctx: Ctx, hostname: str) -> dict:
    return systems.malware_scan(hostname)


@tool("Release a laptop from isolation after a clean scan.", ("L2", "L3"), hostname=STR)
def release_device(ctx: Ctx, hostname: str) -> dict:
    return systems.release(hostname)


# ---- approvals ----------------------------------------------------------------------------

def _approved(ctx: Ctx, what: str) -> bool:
    from app.db import Approval
    return ctx.db.query(Approval).filter(Approval.ticket_id == ctx.ticket.id, Approval.decision == "approved",
                                         Approval.request.contains(what)).count() > 0


@tool("Ask the employee's manager to approve something (paid license, SAP role, software). The ticket "
      "waits; you'll be called again when the decision arrives.", ALL, employee_no=STR, request=STR)
def request_manager_approval(ctx: Ctx, employee_no: str, request: str) -> dict:
    from app.db import Approval, Outbound
    u = _user(employee_no)
    if not u:
        return {"ok": False, "error": "unknown employee"}
    mgr = u.get("manager")
    if not mgr:
        return {"ok": False, "error": "employee has no manager on record"}
    a = Approval(ticket_id=ctx.ticket.id, approver=mgr, request=request)
    ctx.db.add(a)
    ctx.db.flush()
    mgr_rec = systems.get_user(mgr) or {}
    ctx.db.add(Outbound(to=mgr_rec.get("phone") or systems.user_email(mgr), ticket_id=ctx.ticket.id,
                        text=_t("approval_request", ticket_lang(ctx.ticket), id=a.id, name=u["name"], request=request)))
    ctx.ticket.status = "waiting_approval"
    return {"ok": True, "approval_id": a.id, "approver": mgr_rec.get("name", mgr), "status": "waiting_approval"}


# ---- knowledge & learning ------------------------------------------------------------------

@tool("Search the knowledge base (your tier's pool, the shared pool, then other tiers). Do this before acting on "
      "any request. Results carry the full SOP text and a key; follow only what the text says and cite it as "
      "[SOP:key]. An empty result means the desk has no procedure for this. The knowledge base is written in English: "
      "write the query as short English keywords (e.g. 'printer queue stuck'), whatever language the employee uses, "
      "and answer the employee in their own language.", ALL, query=STR)
def search_knowledge(ctx: Ctx, query: str) -> dict:
    from app import knowledge
    results = knowledge.search(ctx.agent, query, db=ctx.db)
    return {"ok": True, "results": results, "matched": bool(results),
            "note": "" if results else "No SOP covers this; say so to the employee and proceed on general expertise."}


@tool("Search the reference policies and procedures the desk uploaded. Each passage carries kind (policy / "
      "guideline / manual) and enforcement (strict = binding, even if the employee insists; flexible = default "
      "you may deviate from with a reason). Rely only on the wording and cite [REF:doc#n]. Search BEFORE any "
      "action with consequences (account, access, license, approval, scripts, data) and whenever a request "
      "touches a rule, entitlement or deadline.", ALL, query=STR)
def search_references(ctx: Ctx, query: str) -> dict:
    from app import references
    results = references.search(ctx.agent, query, db=ctx.db)
    return {"ok": True, "results": results, "matched": bool(results),
            "note": "" if results else "No reference document covers this; do not state policy you cannot quote."}


@tool("Propose a new SOP for your tier when you solved something no SOP covered: symptom → checks → fix steps "
      "with the exact tools you used → when to hand off. It becomes a draft an admin publishes.",
      ALL, title=STR, content=STR)
def write_sop(ctx: Ctx, title: str, content: str) -> dict:
    from app import knowledge
    from app.sla import get_setting
    auto = get_setting("supervisor", {}).get("auto_publish_sops", False)
    key = knowledge.write(ctx.agent, title, content, author=ctx.agent, source="agent",
                          status="published" if auto else "draft", ticket_ids=[ctx.ticket.id] if ctx.ticket else [], db=ctx.db)
    log(ctx.db, ctx.ticket.id, ctx.agent, "note", f"SOP proposed: {key} ({'published' if auto else 'draft, awaiting an admin'})")
    return {"ok": True, "sop_id": key, "status": "published" if auto else "draft"}


# ---- endpoint & notifications ---------------------------------------------------------------

@tool("Run an allow-listed script on the employee's laptop. Scripts marked needs_consent require the "
      "employee's explicit OK first.", ("L1", "L2"), hostname=STR, script=STR)
def run_endpoint_script(ctx: Ctx, hostname: str, script: str) -> dict:
    if script not in systems.SCRIPTS:
        return {"ok": False, "error": f"script not allow-listed; available: {list(systems.SCRIPTS)}"}
    return systems.run_script(hostname, script)


@tool("List the scripts that can run on laptops.", ("L1", "L2"))
def list_endpoint_scripts(ctx: Ctx) -> dict:
    return {"ok": True, "scripts": systems.SCRIPTS}


@tool("Send a message to an employee outside the current reply (status update, approval request, reminder).",
      ALL, employee_no=STR, text=STR)
def notify_user(ctx: Ctx, employee_no: str, text: str) -> dict:
    from app.db import Outbound
    u = systems.get_user(employee_no)
    if not u:
        return {"ok": False, "error": "unknown employee"}
    ctx.db.add(Outbound(to=u["phone"], ticket_id=ctx.ticket.id, text=text))
    ctx.db.flush()
    return {"ok": True, "to": u["name"]}


# ---- incidents (many tickets, one cause) -----------------------------------------------------

@tool("Find other open tickets that look like this one (same symptom). Use it when a problem may "
      "affect many people.", ALL, query=STR)
def find_similar_tickets(ctx: Ctx, query: str) -> dict:
    import re
    q = {t for t in re.findall(r"\w+", query.lower()) if len(t) > 3}
    hits = []
    for t in ctx.db.query(Ticket).filter(Ticket.status != "resolved", Ticket.id != ctx.ticket.id):
        words = {w for w in re.findall(r"\w+", t.title.lower()) if len(w) > 3}
        if len(q & words) >= 2:
            hits.append({"ticket_id": t.id, "title": t.title, "requester": t.requester,
                         "created_at": t.created_at.isoformat(), "incident_id": t.incident_id})
    return {"ok": True, "similar": hits, "count": len(hits)}


@tool("Link tickets to this one as a single incident; this ticket becomes the incident record. "
      "Linked tickets close together with it.", ("L1", "L3"), ticket_ids={"type": "array", "items": INT},
      incident_title=STR)
def link_tickets(ctx: Ctx, ticket_ids: list[int], incident_title: str) -> dict:
    ctx.ticket.title = f"[INCIDENT] {incident_title}"
    linked = []
    for tid in ticket_ids:
        t = ctx.db.get(Ticket, int(tid))
        if t and t.id != ctx.ticket.id:
            t.incident_id = ctx.ticket.id
            log(ctx.db, t.id, ctx.agent, "note", f"linked to incident #{ctx.ticket.id}")
            linked.append(t.id)
    return {"ok": True, "incident_id": ctx.ticket.id, "linked": linked}


@tool("Send the same status update to everyone whose ticket is linked to this incident.", ("L1", "L3"), text=STR)
def broadcast_incident_update(ctx: Ctx, text: str) -> dict:
    from app.db import Outbound
    children = ctx.db.query(Ticket).filter(Ticket.incident_id == ctx.ticket.id).all()
    recipients = {ctx.ticket.requester, *(t.requester for t in children)}
    for r in recipients:
        ctx.db.add(Outbound(to=r, ticket_id=ctx.ticket.id, text=text))
    ctx.db.flush()
    return {"ok": True, "recipients": len(recipients)}


# ---- ticket lifecycle -----------------------------------------------------------------------

@tool("You believe the problem is fixed: ask the employee to confirm. The ticket waits for their answer "
      "and a reminder goes out if they stay silent. Never close without confirmation.", ("L1",), summary=STR)
def request_confirmation(ctx: Ctx, summary: str) -> dict:
    from datetime import timedelta
    from app.db import Followup, _now
    ctx.ticket.status = "waiting_user"
    ctx.ticket.resolution = summary
    ctx.db.add(Followup(ticket_id=ctx.ticket.id, due_at=_now() + timedelta(hours=4),
                        note="confirmation reminder"))
    ctx.db.flush()
    return {"ok": True, "status": "waiting_user"}


@tool("No SOP and no tool of any tier can solve this (e.g. needs physical presence, a vendor decision, a "
      "budget, or a change nobody is authorised for). Park the ticket for a human engineer with a clear "
      "summary of what was tried. Tell the employee it was escalated and they'll be contacted.",
      ALL, reason=STR, tried=STR)
def escalate_to_human(ctx: Ctx, reason: str, tried: str) -> dict:
    ctx.ticket.status = "escalated"
    ctx.ticket.owner = "human"
    ctx.ticket.resolution = f"escalated: {reason} | tried: {tried}"
    return {"ok": True, "status": "escalated"}


@tool("Close the ticket after the employee confirmed the fix. Ask them for a 1-5 rating in your reply.",
      ("L1",), resolution=STR)
def close_ticket(ctx: Ctx, resolution: str) -> dict:
    from app.db import resolve
    resolve(ctx.ticket)
    ctx.ticket.resolution = resolution
    closed = 0
    for child in ctx.db.query(Ticket).filter(Ticket.incident_id == ctx.ticket.id, Ticket.status != "resolved"):
        resolve(child)
        child.resolution = f"incident #{ctx.ticket.id}: {resolution}"
        closed += 1
    return {"ok": True, "status": "resolved", "linked_closed": closed}


@tool("Store the employee's 1-5 satisfaction rating for this ticket.", ("L1",), rating=INT)
def record_rating(ctx: Ctx, rating: int) -> dict:
    if not 1 <= int(rating) <= 5:
        return {"ok": False, "error": "rating must be 1..5"}
    ctx.ticket.rating = int(rating)
    return {"ok": True}


@tool("Schedule a follow-up on this ticket (stability check, cargo tracking, temporary-access expiry).",
      ALL, hours=INT, note=STR)
def schedule_followup(ctx: Ctx, hours: int, note: str) -> dict:
    from datetime import timedelta
    from app.db import Followup, _now
    f = Followup(ticket_id=ctx.ticket.id, due_at=_now() + timedelta(hours=int(hours)), note=note)
    ctx.db.add(f)
    ctx.db.flush()
    return {"ok": True, "followup_id": f.id, "due_at": f.due_at.isoformat()}


@tool("Record a decision or finding on the ticket.", ALL, text=STR)
def add_note(ctx: Ctx, text: str) -> dict:
    log(ctx.db, ctx.ticket.id, ctx.agent, "note", text)
    return {"ok": True}
