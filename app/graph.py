"""Microsoft Entra ID / Microsoft 365 through Microsoft Graph — the real directory and mailbox behind the desk.

When enabled (Settings → Connectors → Microsoft 365), the directory tools the agents already use (find the
employee, verify, reset password, revoke sessions, licences, groups, on/offboarding) act on the real tenant
instead of the simulated ``systems.USERS`` table, and the e-mail channel can read and send through a Microsoft
365 mailbox (Exchange Online no longer accepts password sign-in for IMAP). One app registration with
application permissions; the client secret lives in the vault.

Records are returned in the same shape as the simulated directory so nothing else in the desk changes:
``{"name", "phone", "email", "department", "manager", "licenses", "groups", "enabled", "device", "birth_year"}``.
The employee number is read from the attribute chosen in Settings (``employeeId`` by default), otherwise the
user-principal name. Nothing here assumes a country or a language: new accounts take the tenant's own country.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

from app.sla import get_setting

logger = logging.getLogger("graph")
BASE = "https://graph.microsoft.com/v1.0"
DEFAULT_GRAPH = {"enabled": False, "tenant": "", "client_id": "", "mailbox": "", "domain": "",
                 "usage_location": "",          # ISO country for new accounts; empty = the tenant's own country
                 "employee_attr": "employeeId", # where the employee number lives (employeeId or extensionAttribute1..15)
                 "birth_year_attr": "",         # optional extensionAttribute holding a birth year; empty = none
                 "last_received": ""}           # mailbox position for the e-mail channel (ISO time)
TIMEOUT = 25
USER_FIELDS = ("id,displayName,userPrincipalName,mail,mobilePhone,businessPhones,employeeId,department,jobTitle,"
               "accountEnabled,onPremisesExtensionAttributes")


class GraphError(Exception):
    pass


def config() -> dict:
    return {**DEFAULT_GRAPH, **get_setting("graph", {})}


def secret() -> str:
    from app import vault
    return vault.get("GRAPH_CLIENT_SECRET")


def enabled() -> bool:
    c = config()
    return bool(c["enabled"] and c["tenant"] and c["client_id"] and secret())


# ---- transport ------------------------------------------------------------------------------------

_tok: dict = {"value": "", "until": 0.0, "key": ""}
_lock = threading.Lock()


def _token() -> str:
    c = config()
    key = f"{c['tenant']}:{c['client_id']}"
    with _lock:
        if _tok["value"] and _tok["key"] == key and time.time() < _tok["until"] - 60:
            return _tok["value"]
        r = httpx.post(f"https://login.microsoftonline.com/{c['tenant']}/oauth2/v2.0/token",
                       data={"client_id": c["client_id"], "client_secret": secret(), "grant_type": "client_credentials",
                             "scope": "https://graph.microsoft.com/.default"}, timeout=TIMEOUT)
        if r.status_code != 200:
            raise GraphError(f"token: {r.status_code} {r.text[:200]}")
        d = r.json()
        _tok.update(value=d["access_token"], until=time.time() + int(d.get("expires_in", 3600)), key=key)
        return _tok["value"]


def _call(method: str, path: str, json: dict | None = None, params: dict | None = None, headers: dict | None = None):
    """One Graph request; raises GraphError with Microsoft's message. Tests replace this function."""
    h = {"Authorization": f"Bearer {_token()}", **(headers or {})}
    r = httpx.request(method, path if path.startswith("http") else BASE + path, json=json, params=params, headers=h, timeout=TIMEOUT)
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", {}).get("message", "")
        except ValueError:
            msg = r.text
        raise GraphError(f"{method} {path.split('?')[0]}: {r.status_code} {msg[:300]}")
    if r.status_code == 204 or not r.content:
        return {}
    return r.json()


def _q(s: str) -> str:
    return str(s).replace("'", "''")


# ---- directory --------------------------------------------------------------------------------------

def _attr(u: dict, name: str) -> str:
    if not name:
        return ""
    if name.startswith("extensionAttribute"):
        return str((u.get("onPremisesExtensionAttributes") or {}).get(name) or "")
    return str(u.get(name) or "")


def _attr_path(name: str) -> str:
    return f"onPremisesExtensionAttributes/{name}" if name.startswith("extensionAttribute") else name


def _emp_no(u: dict) -> str:
    return (_attr(u, config()["employee_attr"] or "employeeId") or u.get("userPrincipalName") or "").strip()


def _record(u: dict, groups: list[str] | None = None, licenses: list[str] | None = None) -> dict:
    by = _attr(u, config()["birth_year_attr"]).strip()
    return {"id": u.get("id"), "name": u.get("displayName", ""), "upn": u.get("userPrincipalName", ""),
            "email": (u.get("mail") or u.get("userPrincipalName") or "").lower(),
            "phone": (u.get("mobilePhone") or (u.get("businessPhones") or [""])[0] or "").replace(" ", ""),
            "department": u.get("department") or "", "title": u.get("jobTitle") or "", "manager": None,
            "licenses": licenses if licenses is not None else [], "groups": groups if groups is not None else [],
            "enabled": bool(u.get("accountEnabled", True)), "device": None,
            "birth_year": int(by) if str(by).isdigit() else 0, "source": "entra"}


def _find_all(filter_: str, top: int = 5) -> list[dict]:
    # phone filters are "advanced queries" in Graph: they need $count=true with ConsistencyLevel: eventual
    d = _call("GET", "/users", params={"$filter": filter_, "$select": USER_FIELDS, "$top": top, "$count": "true"},
              headers={"ConsistencyLevel": "eventual"})
    return d.get("value") or []


def _find(filter_: str) -> dict | None:
    v = _find_all(filter_, 1)
    return v[0] if v else None


def raw_user(employee_no: str) -> dict | None:
    """Entra user by employee number, UPN or e-mail."""
    e = (employee_no or "").strip()
    if not e:
        return None
    if "@" in e:
        return _find(f"userPrincipalName eq '{_q(e)}' or mail eq '{_q(e)}'")
    hit = _find(f"{_attr_path(config()['employee_attr'] or 'employeeId')} eq '{_q(e)}'")
    return hit or _find(f"userPrincipalName eq '{_q(e)}'")


def _manager_no(uid: str) -> str | None:
    try:
        m = _call("GET", f"/users/{uid}/manager", params={"$select": "employeeId,userPrincipalName"})
        return _emp_no(m) or None
    except GraphError:
        return None


def _groups(uid: str) -> list[str]:
    d = _call("GET", f"/users/{uid}/memberOf/microsoft.graph.group", params={"$select": "displayName", "$top": 50})
    return [g["displayName"] for g in d.get("value", [])]


def _licenses(uid: str) -> list[str]:
    d = _call("GET", f"/users/{uid}/licenseDetails", params={"$select": "skuPartNumber"})
    return [x["skuPartNumber"] for x in d.get("value", [])]


def get_user(employee_no: str, full: bool = True) -> dict | None:
    u = raw_user(employee_no)
    if not u:
        return None
    rec = _record(u, _groups(u["id"]) if full else None, _licenses(u["id"]) if full else None)
    if full:
        rec["manager"] = _manager_no(u["id"])
    return rec


def users_by_contact(contact: str) -> list[tuple[str, dict]]:
    """Phone number or e-mail → every (employee_no, record) it belongs to (a shared desk phone may map to several)."""
    c = (contact or "").strip()
    if not c:
        return []
    if "@" in c:
        found = _find_all(f"mail eq '{_q(c.lower())}' or userPrincipalName eq '{_q(c.lower())}'")
    else:
        found = _find_all(f"mobilePhone eq '{_q(c)}'")
        if not found and c.startswith("+"):
            found = _find_all(f"businessPhones/any(p:p eq '{_q(c)}')")
    return [(_emp_no(u), _record(u)) for u in found]


def user_by_contact(contact: str) -> tuple[str, dict] | None:
    """Phone number or e-mail → (employee_no, record) of the first match."""
    hits = users_by_contact(contact)
    return hits[0] if hits else None


def search(query: str) -> list[dict]:
    d = _call("GET", "/users", params={"$search": f'"displayName:{query}" OR "mail:{query}"', "$select": USER_FIELDS, "$top": 5},
              headers={"ConsistencyLevel": "eventual"})
    return [{"employee_no": _emp_no(u), **_record(u)} for u in d.get("value", [])]


def employees_by_phone(phone: str) -> list[str]:
    return [e for e, _ in users_by_contact(phone)]


def _uid(employee_no: str) -> str:
    u = raw_user(employee_no)
    if not u:
        raise GraphError(f"unknown employee {employee_no}")
    return u["id"]


def reset_password(employee_no: str) -> str:
    import secrets as _s
    pw = "Tmp-" + _s.token_urlsafe(9)
    _call("PATCH", f"/users/{_uid(employee_no)}", json={"passwordProfile": {"forceChangePasswordNextSignIn": True, "password": pw}})
    return pw


def revoke_sessions(employee_no: str) -> bool:
    d = _call("POST", f"/users/{_uid(employee_no)}/revokeSignInSessions")
    return bool(d.get("value", True))


def set_enabled(employee_no: str, on: bool) -> None:
    _call("PATCH", f"/users/{_uid(employee_no)}", json={"accountEnabled": on})


def create_user(name: str, department: str, manager_no: str, phone: str, extra: dict | None = None) -> tuple[str, str, str]:
    """New account in the tenant's domain. Returns (employee_no, upn, initial password)."""
    import secrets as _s
    import unicodedata
    c = config()
    domain = c["domain"] or _default_domain()
    nick = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower().strip().replace(" ", ".")
    nick = "".join(ch for ch in nick if ch.isalnum() or ch in "._-") or "user"
    upn = f"{nick}@{domain}"
    pw = "Tmp-" + _s.token_urlsafe(9)
    body = {"accountEnabled": True, "displayName": name, "mailNickname": nick, "userPrincipalName": upn,
            "department": department, "mobilePhone": phone or None,
            "passwordProfile": {"forceChangePasswordNextSignIn": True, "password": pw}}
    body.update({k: v for k, v in (extra or {}).items() if v})   # set at creation: a PATCH right after may 404 (replication)
    where = c["usage_location"].strip().upper() or _tenant_country()
    if where:
        body["usageLocation"] = where        # needed before a licence can be assigned
    u = _call("POST", "/users", json=body)
    if manager_no:
        try:
            mid = _uid(manager_no)
            _call("PUT", f"/users/{u['id']}/manager/$ref", json={"@odata.id": f"{BASE}/users/{mid}"})
        except GraphError as e:
            logger.warning("manager not set for %s: %s", upn, e)
    return upn, upn, pw          # the employee number comes from HR later; until then the UPN identifies them


def _tenant_country() -> str:
    try:
        o = _call("GET", "/organization", params={"$select": "countryLetterCode"})
        return ((o.get("value") or [{}])[0].get("countryLetterCode") or "").upper()
    except GraphError:
        return ""


def _default_domain() -> str:
    d = _call("GET", "/domains", params={"$select": "id,isDefault,isVerified"})
    for x in d.get("value", []):
        if x.get("isDefault"):
            return x["id"]
    return (d.get("value") or [{"id": ""}])[0]["id"]


def license_catalog() -> dict:
    """skuPartNumber → {"available": n, "sku_id": …}; approval policy stays the desk's own (LICENSE_CATALOG)."""
    d = _call("GET", "/subscribedSkus", params={"$select": "skuId,skuPartNumber,prepaidUnits,consumedUnits"})
    out = {}
    for s in d.get("value", []):
        total = (s.get("prepaidUnits") or {}).get("enabled", 0)
        out[s["skuPartNumber"]] = {"sku_id": s["skuId"], "available": max(0, total - int(s.get("consumedUnits", 0))), "total": total}
    return out


def assign_license(employee_no: str, sku_part: str) -> list[str]:
    cat = license_catalog()
    if sku_part not in cat:
        raise GraphError(f"no such licence in the tenant: {sku_part}")
    uid = _uid(employee_no)
    _call("POST", f"/users/{uid}/assignLicense", json={"addLicenses": [{"skuId": cat[sku_part]["sku_id"], "disabledPlans": []}], "removeLicenses": []})
    return _licenses(uid)


def revoke_license(employee_no: str, sku_part: str) -> list[str]:
    cat = license_catalog()
    uid = _uid(employee_no)
    if sku_part in cat:
        _call("POST", f"/users/{uid}/assignLicense", json={"addLicenses": [], "removeLicenses": [cat[sku_part]["sku_id"]]})
    return _licenses(uid)


def _group_id(name: str) -> str:
    d = _call("GET", "/groups", params={"$filter": f"displayName eq '{_q(name)}' or mailNickname eq '{_q(name)}'", "$select": "id,displayName", "$top": 1})
    v = d.get("value") or []
    if not v:
        raise GraphError(f"no such group: {name}")
    return v[0]["id"]


def add_to_group(employee_no: str, group: str) -> list[str]:
    uid = _uid(employee_no)
    try:
        _call("POST", f"/groups/{_group_id(group)}/members/$ref", json={"@odata.id": f"{BASE}/directoryObjects/{uid}"})
    except GraphError as e:
        if "already exist" not in str(e):
            raise
    return _groups(uid)


def remove_from_groups(employee_no: str, group: str | None = None) -> list[str]:
    uid = _uid(employee_no)
    d = _call("GET", f"/users/{uid}/memberOf/microsoft.graph.group", params={"$select": "id,displayName", "$top": 50})
    for g in d.get("value", []):
        if group is None or g["displayName"] == group:
            try:
                _call("DELETE", f"/groups/{g['id']}/members/{uid}/$ref")
            except GraphError as e:
                logger.warning("leave %s failed: %s", g["displayName"], e)
    return _groups(uid)


# ---- mailbox (e-mail channel) ------------------------------------------------------------------------

def _mailbox() -> str:
    m = config()["mailbox"].strip()
    if not m:
        raise GraphError("no Microsoft 365 mailbox configured (Settings → Connectors)")
    return m


def send_mail(to: str, subject: str, text: str) -> None:
    body = {"message": {"subject": subject, "body": {"contentType": "Text", "content": text},
                        "toRecipients": [{"emailAddress": {"address": to}}]}, "saveToSentItems": True}
    _call("POST", f"/users/{_mailbox()}/sendMail", json=body)


def new_messages(since_iso: str, limit: int = 20) -> list[dict]:
    """Inbox mail received after ``since_iso``, oldest first, in the e-mail channel's parsed shape."""
    import re
    params = {"$filter": f"receivedDateTime gt {since_iso}", "$orderby": "receivedDateTime asc", "$top": limit,
              "$select": "id,subject,from,receivedDateTime,body,hasAttachments,internetMessageId,isDraft"}
    d = _call("GET", f"/users/{_mailbox()}/mailFolders/inbox/messages", params=params)
    out = []
    for m in d.get("value", []):
        if m.get("isDraft"):
            continue
        addr = ((m.get("from") or {}).get("emailAddress") or {})
        text = m.get("body", {}).get("content", "") or ""
        if m.get("body", {}).get("contentType", "").lower() == "html":
            text = re.sub(r"<style.*?</style>|<script.*?</script>", " ", text, flags=re.S | re.I)
            text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", text, flags=re.I)
            text = re.sub(r"<[^>]+>", " ", text)
            import html as _h
            text = _h.unescape(text)
        image, mime, document = None, "image/jpeg", None
        if m.get("hasAttachments"):
            import base64
            from app.attachments import is_document
            try:
                att = _call("GET", f"/users/{_mailbox()}/messages/{m['id']}/attachments", params={"$select": "id,name,contentType,size"})
                for a in att.get("value", []):
                    ctype, size = str(a.get("contentType", "")), int(a.get("size", 0))
                    if size > 10_000_000:
                        continue
                    if image is None and ctype.startswith("image/"):
                        full = _call("GET", f"/users/{_mailbox()}/messages/{m['id']}/attachments/{a['id']}")
                        image, mime = base64.b64decode(full.get("contentBytes", "")), ctype
                    elif document is None and is_document(a.get("name", ""), ctype):
                        full = _call("GET", f"/users/{_mailbox()}/messages/{m['id']}/attachments/{a['id']}")
                        document = (a.get("name", "document"), ctype, base64.b64decode(full.get("contentBytes", "")))
            except GraphError as e:
                logger.warning("attachment read failed: %s", e)
        out.append({"from": (addr.get("address") or "").lower(), "name": addr.get("name", ""), "subject": (m.get("subject") or "")[:200],
                    "raw_text": text, "image": image, "mime": mime, "document": document, "received": m.get("receivedDateTime", ""),
                    "message_id": m.get("internetMessageId", "")})
    return out


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---- health ---------------------------------------------------------------------------------------------

def test() -> dict:
    """What the admin sees after Save & test: tenant name, how many users, whether the mailbox answers."""
    org = _call("GET", "/organization", params={"$select": "displayName,verifiedDomains"})
    o = (org.get("value") or [{}])[0]
    domains = [d["name"] for d in o.get("verifiedDomains", []) if not d["name"].endswith(".onmicrosoft.com")] or [d["name"] for d in o.get("verifiedDomains", [])]
    count = _call("GET", "/users/$count", headers={"ConsistencyLevel": "eventual"})
    out = {"ok": True, "tenant": o.get("displayName", ""), "domains": domains, "users": count if isinstance(count, int) else "?"}
    mb = config()["mailbox"].strip()
    if mb:
        try:
            _call("GET", f"/users/{mb}/mailFolders/inbox", params={"$select": "totalItemCount"})
            out["mailbox"] = "ok"
        except GraphError as e:
            out["mailbox"] = f"failed: {e}"[:200]
    return out
