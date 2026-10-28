"""Company systems behind the agents' tools. The directory functions switch to the real Microsoft Entra ID
tenant when Settings → Connectors → Microsoft 365 is enabled (``app.graph``); until then, and for the
endpoint/vendor/security systems, the tables below simulate what a connector would return."""
from __future__ import annotations

import os
import secrets

USERS = {
    "E1001": {"name": "Emma Johnson", "phone": "+15551110001", "birth_year": 1990, "device": "LT-101",
              "department": "Sales", "manager": "E1002", "licenses": ["M365-E3"], "groups": ["sales-tr"],
              "sap_roles": [], "enabled": True},
    "E1002": {"name": "Liam Carter", "phone": "+15551110002", "birth_year": 1985, "device": "LT-102",
              "department": "Sales", "manager": "E1003", "licenses": ["M365-E3", "Visio"], "groups": ["sales-tr", "managers"],
              "sap_roles": ["SD_DISPLAY"], "enabled": True},
    "E1003": {"name": "Olivia Bennett", "phone": "+15551110003", "birth_year": 1994, "device": "LT-103",
              "department": "Finance", "manager": None, "licenses": ["M365-E3"], "groups": ["finance", "managers"],
              "sap_roles": ["FI_DISPLAY", "FI_POST"], "enabled": True},
}

_SEED_PHONES = {"E1002": USERS["E1002"]["phone"], "E1003": USERS["E1003"]["phone"]}


def apply_demo_phone(phone: str | None) -> None:
    """Demo: point the seeded managers' phones at a real number so approval requests land on your WhatsApp
    (Settings → Channels, or DEMO_MANAGER_PHONE in .env). Empty restores the seed numbers."""
    for emp, seed in _SEED_PHONES.items():
        USERS[emp]["phone"] = phone or seed


apply_demo_phone(os.getenv("DEMO_MANAGER_PHONE"))

LICENSE_CATALOG = {   # license -> needs manager approval?
    "M365-E3": False, "Teams-Phone": False, "Visio": False, "Project": True, "Adobe-CC": True, "AutoCAD": True,
}
SAP_ROLES = {"SD_DISPLAY": "sales display", "SD_CREATE": "create sales orders", "FI_DISPLAY": "finance display",
             "FI_POST": "post journal entries", "MM_PURCHASE": "create purchase orders"}


def _graph():
    from app import graph
    return graph if graph.enabled() else None


def get_user(employee_no: str) -> dict | None:
    """Directory record by employee number (Entra when connected, else the simulated table)."""
    g = _graph()
    if g:
        try:
            return g.get_user(employee_no)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"directory: {e}") from e
    return USERS.get(employee_no)


def employees_by_phone(phone: str) -> list[str]:
    g = _graph()
    if g:
        return g.employees_by_phone(phone)
    return [e for e, u in USERS.items() if u.get("phone") == phone]


def employees_by_contact(contact: str) -> list[str]:
    """Every employee a phone number or e-mail address belongs to."""
    g = _graph()
    if g:
        return g.employees_by_phone(contact)
    contact = (contact or "").strip().lower()
    if "@" in contact:
        return [e for e in USERS if user_email(e).lower() == contact]
    return [e for e, u in USERS.items() if u.get("phone") == contact]


def license_catalog() -> dict:
    """Licence → needs manager approval. With Entra, the tenant's SKUs; approval policy from LICENSE_CATALOG by name."""
    g = _graph()
    if g:
        return {k: LICENSE_CATALOG.get(k, False) for k in g.license_catalog()}
    return LICENSE_CATALOG


def set_enabled(employee_no: str, on: bool) -> None:
    g = _graph()
    if g:
        g.set_enabled(employee_no, on)
    elif employee_no in USERS:
        USERS[employee_no]["enabled"] = on


def create_user(name: str, department: str, manager: str, phone: str) -> str:
    g = _graph()
    if g:
        return g.create_user(name, department, manager, phone)[0]
    emp = f"E{1000 + len(USERS) + 1}"
    USERS[emp] = {"name": name, "phone": phone, "birth_year": 0, "device": None, "department": department,
                  "manager": manager, "licenses": [], "groups": [], "sap_roles": [], "enabled": True}
    return emp

DEVICES = {
    "LT-101": {"vpn_profile": "expired", "online": True, "dns_ok": True, "disk_used_pct": 61, "os_patch_days": 12},
    "LT-102": {"vpn_profile": "valid", "online": True, "dns_ok": False, "disk_used_pct": 97, "os_patch_days": 5},
    "LT-103": {"vpn_profile": "valid", "online": False, "dns_ok": True, "disk_used_pct": 40, "os_patch_days": 48},
}


def device_owner(hostname: str) -> tuple[str, dict] | None:
    for emp, u in USERS.items():
        if u.get("device") == hostname:
            return emp, u
    return None


WARRANTY = {   # hostname -> vendor warranty record
    "LT-101": {"vendor": "Dell", "service_tag": "7KX2Q93", "valid_until": "2027-03-15"},
    "LT-102": {"vendor": "Lenovo", "service_tag": "PF3M8K1", "valid_until": "2025-11-30"},
    "LT-103": {"vendor": "Dell", "service_tag": "9HT4R21", "valid_until": "2027-08-01"},
}

LOANERS = {"LN-01": "available", "LN-02": "available", "LN-03": "assigned"}

_cases: list[dict] = []
_shipments: list[dict] = []


def open_vendor_case(hostname: str, summary: str) -> dict:
    rec = WARRANTY[hostname]
    case = {"case_no": f"{rec['vendor'][:2].upper()}-{100200 + len(_cases)}", "vendor": rec["vendor"],
            "service_tag": rec["service_tag"], "summary": summary, "status": "opened"}
    _cases.append(case)
    return case


def create_shipment(to_name: str, device: str, case_no: str) -> dict:
    s = {"tracking_no": f"TR{48100000 + len(_shipments)}", "carrier": "Yurtiçi Kargo", "to": to_name,
         "contents": device, "case_no": case_no, "pickup": "next business day", "status": "label_created"}
    _shipments.append(s)
    return s


SERVERS = {   # CMDB: hostname -> software inventory
    "TR-WEB-01": {"role": "intranet web", "os": "Ubuntu 22.04", "packages": {"php": "8.1.2", "nginx": "1.18.0"}},
    "TR-WEB-02": {"role": "customer portal", "os": "Ubuntu 22.04", "packages": {"php": "8.1.2", "nginx": "1.18.0"}},
    "TR-APP-01": {"role": "SAP gateway", "os": "RHEL 9", "packages": {"java": "17.0.9"}},
    "TR-WEB-03": {"role": "docs site", "os": "Ubuntu 24.04", "packages": {"php": "8.3.6", "nginx": "1.24.0"}},
}

ADVISORIES = {   # vulnerability feed: CVE id -> affected package, vulnerable versions, fixed version
    "CVE-2026-4411": {"name": "php2shell", "package": "php", "vulnerable_below": "8.1.30", "fixed": "8.1.30",
                      "severity": "critical", "summary": "remote code execution via crafted multipart upload"},
}

_snapshots: dict[str, dict] = {}   # snapshot_id -> {"hostname", "packages"}


def _version_lt(a: str, b: str) -> bool:
    return tuple(int(x) for x in a.split(".")) < tuple(int(x) for x in b.split("."))


def find_vulnerable(cve: str) -> list[dict]:
    adv = ADVISORIES.get(cve)
    if not adv:
        return []
    return [{"hostname": h, "role": s["role"], "package": adv["package"], "version": s["packages"][adv["package"]]}
            for h, s in SERVERS.items()
            if adv["package"] in s["packages"] and _version_lt(s["packages"][adv["package"]], adv["vulnerable_below"])]


def snapshot(hostname: str) -> str:
    sid = f"snap-{hostname}-{len(_snapshots) + 1}"
    _snapshots[sid] = {"hostname": hostname, "packages": dict(SERVERS[hostname]["packages"])}
    return sid


def rollback(snapshot_id: str) -> bool:
    s = _snapshots.get(snapshot_id)
    if not s:
        return False
    SERVERS[s["hostname"]]["packages"] = dict(s["packages"])
    return True


# ---- security: EDR + mail security (simulated; same shapes as Defender / Exchange Online) ----
_isolated: set[str] = set()
_blocked_senders: set[str] = set()
_quarantined: list[dict] = []

KNOWN_BAD = {"domains": {"micros0ft-login.com", "dhl-tracking-tr.net", "hr-bonus-portal.com"},
             "hashes": {"44d88612fea8a8f36de82e1278abb02f": "EICAR test file"}}


def isolate(hostname: str) -> dict:
    if hostname not in DEVICES:
        return {"ok": False, "error": "unknown device"}
    _isolated.add(hostname)
    return {"ok": True, "hostname": hostname, "isolated": True, "management_channel": "open"}


def release(hostname: str) -> dict:
    _isolated.discard(hostname)
    return {"ok": True, "hostname": hostname, "isolated": False}


def collect_evidence(hostname: str) -> dict:
    return {"ok": True, "hostname": hostname, "isolated": hostname in _isolated,
            "suspicious_processes": [{"name": "svch0st.exe", "path": "C:\\Users\\Public\\svch0st.exe", "parent": "outlook.exe"}],
            "recent_downloads": ["Fatura_2026_10.pdf.exe"],
            "outbound_connections": ["185.220.101.4:443"],
            "last_logon": "E1001"}


def malware_scan(hostname: str) -> dict:
    findings = [{"file": "C:\\Users\\Public\\svch0st.exe", "hash": "44d88612fea8a8f36de82e1278abb02f",
                 "verdict": "Trojan:Win32/Agent (EICAR-marked sample)", "action": "quarantined"}]
    return {"ok": True, "hostname": hostname, "findings": findings, "clean_after_action": True}


def analyze_email(sender: str, subject: str, body: str) -> dict:
    import re
    domain = sender.split("@")[-1].lower()
    links = re.findall(r"https?://[^\s)>\]]+", body)
    link_domains = {re.sub(r"^https?://([^/]+).*", r"\1", l).lower() for l in links}
    signals = []
    if domain in KNOWN_BAD["domains"] or link_domains & KNOWN_BAD["domains"]:
        signals.append("known phishing domain")
    if any(w in (subject + body).lower() for w in ("acil", "urgent", "hesabınız askıya", "şifrenizi doğrulayın", "24 saat")):
        signals.append("urgency / credential lure")
    if domain not in ("company.com",) and any(w in body.lower() for w in ("şifre", "password", "giriş yapın", "login")):
        signals.append("external sender asks for credentials")
    verdict = "phishing" if len(signals) >= 2 or "known phishing domain" in signals else ("suspicious" if signals else "safe")
    return {"ok": True, "verdict": verdict, "signals": signals, "sender_domain": domain, "links": links}


def quarantine(subject: str, sender: str) -> dict:
    hit = {"subject": subject, "sender": sender, "mailboxes_affected": 17}
    _quarantined.append(hit)
    return {"ok": True, **hit, "tenant_wide": True}


def block_sender(sender: str) -> dict:
    _blocked_senders.add(sender.lower())
    return {"ok": True, "blocked": sorted(_blocked_senders)}


# Allow-listed endpoint scripts. Only these names can run on a laptop; the endpoint agent
# refuses anything else. Simulated here; the real endpoint agent returns the same shape.
SCRIPTS = {
    "vpn-reset":       {"desc": "restart VPN client, flush DNS cache", "needs_consent": False},
    "dns-reset":       {"desc": "reset DNS settings and cache", "needs_consent": False},
    "printer-fix":     {"desc": "clear print queue, restart spooler", "needs_consent": False},
    "perf-report":     {"desc": "CPU/memory/disk/startup report", "needs_consent": False},
    "driver-rollback": {"desc": "roll back the last updated driver (reboots)", "needs_consent": True},
    "disk-cleanup":    {"desc": "remove temp files and old updates", "needs_consent": True},
    "lock-screen":     {"desc": "lock the workstation immediately", "needs_consent": False},
}


def run_script(hostname: str, script: str) -> dict:
    d = DEVICES.get(hostname)
    if not d:
        return {"ok": False, "error": "unknown device"}
    if not d["online"]:
        return {"ok": False, "error": "device offline"}
    if script == "dns-reset":
        d["dns_ok"] = True
    if script == "vpn-reset" and d["vpn_profile"] == "valid":
        d["vpn_connected"] = True
    out = {"perf-report": "cpu 92% (chrome 41 tabs), ram 15.6/16 GB, disk 97% full, 14 startup apps"}.get(
        script, "completed")
    return {"ok": True, "hostname": hostname, "script": script, "output": out}


def user_by_phone(phone: str) -> tuple[str, dict] | None:
    g = _graph()
    if g:
        return g.user_by_contact(phone)
    for emp, u in USERS.items():
        if u["phone"] == phone:
            return emp, u
    return None


def user_email(emp: str) -> str:
    """Directory e-mail: explicit when present, otherwise name-derived (emma.johnson@example.com)."""
    u = (get_user(emp) if _graph() else USERS.get(emp)) or {}
    if u.get("email"):
        return u["email"]
    import unicodedata
    base = unicodedata.normalize("NFKD", u.get("name", "")).encode("ascii", "ignore").decode().lower().replace(" ", ".")
    return f"{base}@example.com" if base else ""


def user_by_contact(contact: str) -> tuple[str, dict] | None:
    """Phone number or e-mail address → directory entry."""
    contact = (contact or "").strip().lower()
    g = _graph()
    if g:
        return g.user_by_contact(contact)
    if "@" in contact:
        for emp, u in USERS.items():
            if user_email(emp).lower() == contact:
                return emp, u
        return None
    return user_by_phone(contact)


def reset_password(employee_no: str) -> str:
    """Returns a one-time temporary password (must be changed at next sign-in)."""
    g = _graph()
    if g:
        return g.reset_password(employee_no)
    return "Tmp-" + secrets.token_urlsafe(6)
