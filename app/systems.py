"""Simulated company systems (directory, endpoints). Each function mirrors what a real
connector (Entra ID, Intune, VPN gateway) would expose, so swapping one in is local."""
from __future__ import annotations

import secrets

USERS = {
    "E1001": {"name": "Ayşe Demir", "phone": "+905551110001", "birth_year": 1990, "device": "TR-LT-101"},
    "E1002": {"name": "Mehmet Kaya", "phone": "+905551110002", "birth_year": 1985, "device": "TR-LT-102"},
    "E1003": {"name": "Zeynep Arslan", "phone": "+905551110003", "birth_year": 1994, "device": "TR-LT-103"},
}

DEVICES = {
    "TR-LT-101": {"vpn_profile": "expired", "online": True, "dns_ok": True},
    "TR-LT-102": {"vpn_profile": "valid", "online": True, "dns_ok": False},
    "TR-LT-103": {"vpn_profile": "valid", "online": False, "dns_ok": True},
}


WARRANTY = {   # hostname -> vendor warranty record
    "TR-LT-101": {"vendor": "Dell", "service_tag": "7KX2Q93", "valid_until": "2027-03-15"},
    "TR-LT-102": {"vendor": "Lenovo", "service_tag": "PF3M8K1", "valid_until": "2025-11-30"},
    "TR-LT-103": {"vendor": "Dell", "service_tag": "9HT4R21", "valid_until": "2027-08-01"},
}

LOANERS = {"TR-LN-01": "available", "TR-LN-02": "available", "TR-LN-03": "assigned"}

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


def user_by_phone(phone: str) -> tuple[str, dict] | None:
    for emp, u in USERS.items():
        if u["phone"] == phone:
            return emp, u
    return None


def reset_password(employee_no: str) -> str:
    """Returns a one-time temporary password (must be changed at next sign-in)."""
    return "Tmp-" + secrets.token_urlsafe(6)
