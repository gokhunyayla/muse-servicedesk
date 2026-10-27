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


@tool("Verify identity with employee number and birth year. Required before account changes.",
      ("L1",), employee_no=STR, birth_year=INT)
def verify_identity(ctx: Ctx, employee_no: str, birth_year: int) -> dict:
    u = systems.USERS.get(employee_no)
    ok = bool(u) and u["birth_year"] == int(birth_year)
    if ok:
        ctx.ticket.verified = True
    return {"ok": ok}


@tool("Reset the employee's password; returns a one-time temporary password.", ("L1", "L3"), employee_no=STR)
def reset_password(ctx: Ctx, employee_no: str) -> dict:
    if not ctx.ticket.verified:
        return {"ok": False, "error": "identity not verified in this ticket"}
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
    u = systems.USERS.get(employee_no)
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
    return systems.USERS.get(employee_no)


@tool("Show an employee's directory record: department, manager, licenses, groups, SAP roles.",
      ("L1", "L3"), employee_no=STR)
def get_employee(ctx: Ctx, employee_no: str) -> dict:
    u = _user(employee_no)
    if not u:
        return {"ok": False, "error": "unknown employee"}
    return {"ok": True, "employee_no": employee_no, **{k: v for k, v in u.items() if k != "birth_year"}}


@tool("List licenses available in the catalog and whether each needs manager approval.", ("L1",))
def list_licenses(ctx: Ctx) -> dict:
    return {"ok": True, "catalog": systems.LICENSE_CATALOG}


@tool("Assign a license. Licenses marked needs_approval require an approved request first.",
      ("L1",), employee_no=STR, license=STR)
def assign_license(ctx: Ctx, employee_no: str, license: str) -> dict:
    u = _user(employee_no)
    if not u or license not in systems.LICENSE_CATALOG:
        return {"ok": False, "error": "unknown employee or license"}
    if systems.LICENSE_CATALOG[license] and not _approved(ctx, license):
        return {"ok": False, "error": f"{license} needs manager approval; use request_manager_approval first"}
    if license not in u["licenses"]:
        u["licenses"].append(license)
    return {"ok": True, "employee_no": employee_no, "licenses": u["licenses"]}


@tool("Remove a license from an employee.", ("L1",), employee_no=STR, license=STR)
def revoke_license(ctx: Ctx, employee_no: str, license: str) -> dict:
    u = _user(employee_no)
    if not u:
        return {"ok": False, "error": "unknown employee"}
    if license in u["licenses"]:
        u["licenses"].remove(license)
    return {"ok": True, "licenses": u["licenses"]}


@tool("Add an employee to a directory group (mail lists, shared folders, app access).", ("L1", "L3"),
      employee_no=STR, group=STR)
def add_to_group(ctx: Ctx, employee_no: str, group: str) -> dict:
    u = _user(employee_no)
    if not u:
        return {"ok": False, "error": "unknown employee"}
    if group not in u["groups"]:
        u["groups"].append(group)
    return {"ok": True, "groups": u["groups"]}


@tool("Remove an employee from all groups (offboarding) or from one group.", ("L1", "L3"),
      employee_no=STR, group={"type": "string", "optional": True})
def remove_from_groups(ctx: Ctx, employee_no: str, group: str | None = None) -> dict:
    u = _user(employee_no)
    if not u:
        return {"ok": False, "error": "unknown employee"}
    u["groups"] = [g for g in u["groups"] if group and g != group]
    return {"ok": True, "groups": u["groups"]}


@tool("Create a directory account and mailbox for a new hire.", ("L1",), name=STR, department=STR,
      manager_employee_no=STR, phone=STR)
def create_account(ctx: Ctx, name: str, department: str, manager_employee_no: str, phone: str) -> dict:
    if manager_employee_no not in systems.USERS:
        return {"ok": False, "error": "unknown manager"}
    emp = systems.create_user(name, department, manager_employee_no, phone)
    return {"ok": True, "employee_no": emp, "upn": f"{emp.lower()}@company.com", "initial_password": systems.reset_password(emp)}


@tool("Disable an account immediately (offboarding or compromise).", ("L1", "L3"), employee_no=STR)
def disable_account(ctx: Ctx, employee_no: str) -> dict:
    u = _user(employee_no)
    if not u:
        return {"ok": False, "error": "unknown employee"}
    u["enabled"] = False
    return {"ok": True, "enabled": False}


@tool("Revoke all active sessions and tokens of an employee.", ("L1", "L3"), employee_no=STR)
def revoke_sessions(ctx: Ctx, employee_no: str) -> dict:
    if not _user(employee_no):
        return {"ok": False, "error": "unknown employee"}
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
    ctx.db.add(Outbound(to=systems.USERS[mgr]["phone"], ticket_id=ctx.ticket.id,
                        text=f"Onay talebi #{a.id}: {u['name']} için \"{request}\". "
                             f"Cevap: ONAY {a.id} veya RET {a.id}"))
    ctx.ticket.status = "waiting_approval"
    return {"ok": True, "approval_id": a.id, "approver": systems.USERS[mgr]["name"], "status": "waiting_approval"}


# ---- knowledge & learning ------------------------------------------------------------------

@tool("Search this tier's SOP knowledge pool. Do this before acting on any request.", ALL, query=STR)
def search_knowledge(ctx: Ctx, query: str) -> dict:
    from app import knowledge
    return {"ok": True, "results": knowledge.search(ctx.agent, query)}


@tool("Write a new SOP into this tier's pool when you solved something the pool did not cover.",
      ALL, title=STR, content=STR)
def write_sop(ctx: Ctx, title: str, content: str) -> dict:
    from app import knowledge
    sid = knowledge.write(ctx.agent, title, content)
    log(ctx.db, ctx.ticket.id, ctx.agent, "note", f"new SOP written: {sid}")
    return {"ok": True, "sop_id": sid}


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
    u = systems.USERS.get(employee_no)
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


@tool("Close the ticket after the employee confirmed the fix. Ask them for a 1-5 rating in your reply.",
      ("L1",), resolution=STR)
def close_ticket(ctx: Ctx, resolution: str) -> dict:
    ctx.ticket.status = "resolved"
    ctx.ticket.resolution = resolution
    closed = 0
    for child in ctx.db.query(Ticket).filter(Ticket.incident_id == ctx.ticket.id, Ticket.status != "resolved"):
        child.status, child.resolution = "resolved", f"incident #{ctx.ticket.id}: {resolution}"
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
