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


def user_by_phone(phone: str) -> tuple[str, dict] | None:
    for emp, u in USERS.items():
        if u["phone"] == phone:
            return emp, u
    return None


def reset_password(employee_no: str) -> str:
    """Returns a one-time temporary password (must be changed at next sign-in)."""
    return "Tmp-" + secrets.token_urlsafe(6)
