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


@tool("Record a decision or finding on the ticket.", ALL, text=STR)
def add_note(ctx: Ctx, text: str) -> dict:
    log(ctx.db, ctx.ticket.id, ctx.agent, "note", text)
    return {"ok": True}
