"""Vulnerability watch: once a day the desk reads the public sources, matches them against what the endpoint agents
report as installed, and — when the policy allows and the agent can patch that application — fixes it, verifies it
and tells the support admins what it found and did.

Sources (no keys, no accounts):
* CISA Known Exploited Vulnerabilities (KEV) — vulnerabilities attackers are using now; refreshed daily.
* NVD (National Vulnerability Database) — for a KEV entry that names installed software: the CVSS score and the exact
  affected version ranges, so a match is a version check, not a name guess.

Remediation is the agent's ``app-update`` script: the vendor's own release feed, checksum verified, a backup first,
the version read back afterwards; ``app-rollback`` restores the backup. Every finding becomes a ticket with the trail.
"""
from __future__ import annotations

import logging
import re
import time

import httpx

from app.db import Event, Outbound, Ticket, _now, log, resolve, session
from app.sla import get_setting, put_setting

logger = logging.getLogger("vulnwatch")
KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
NVD_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
DEFAULT_WATCH = {"enabled": True,          # check the sources once a day
                 "auto_remediate": True,   # patch endpoints automatically when the agent has an updater for the app
                 "last_run": 0.0}
EVERY_SEC = 24 * 3600
_kev_cache: dict = {"at": 0.0, "items": []}
_nvd_last = 0.0


def config() -> dict:
    return {**DEFAULT_WATCH, **get_setting("vulnwatch", {})}


# ---- sources -------------------------------------------------------------------------------------------------

def fetch_kev() -> list[dict]:
    """The KEV catalogue (cached for 12 hours)."""
    if _kev_cache["items"] and time.time() - _kev_cache["at"] < 12 * 3600:
        return _kev_cache["items"]
    r = httpx.get(KEV_URL, timeout=60, headers={"User-Agent": "muse-servicedesk-vulnwatch"})
    r.raise_for_status()
    items = r.json().get("vulnerabilities", [])
    _kev_cache.update(at=time.time(), items=items)
    return items


def nvd(cve: str) -> dict:
    """CVSS score, description and affected application ranges for one CVE (cached in the settings table)."""
    cache = get_setting("nvd_cache", {})
    if cve in cache:
        return cache[cve]
    # NVD without an API key allows 5 requests per 30 seconds: stay under it, and wait once if it still says 429
    global _nvd_last
    for attempt in range(2):
        wait = 6.5 - (time.time() - _nvd_last)
        if wait > 0:
            time.sleep(wait)
        _nvd_last = time.time()
        r = httpx.get(NVD_URL, params={"cveId": cve}, timeout=60, headers={"User-Agent": "muse-servicedesk-vulnwatch"})
        if r.status_code != 429:
            break
        time.sleep(30)
    r.raise_for_status()
    vulns = r.json().get("vulnerabilities", [])
    if not vulns:
        return {}
    c = vulns[0]["cve"]
    metrics = c.get("metrics", {})
    score = None
    for key in ("cvssMetricV31", "cvssMetricV40", "cvssMetricV30"):
        if metrics.get(key):
            score = metrics[key][0].get("cvssData", {}).get("baseScore")
            break
    ranges = []
    for conf in c.get("configurations", []):
        for node in conf.get("nodes", []):
            for m in node.get("cpeMatch", []):
                parts = m.get("criteria", "").split(":")
                if len(parts) > 5 and parts[2] == "a" and m.get("vulnerable"):
                    ranges.append({"vendor": parts[3], "product": parts[4], "exact": parts[5] if parts[5] not in ("*", "-") else "",
                                   "start": m.get("versionStartIncluding", ""), "end_excl": m.get("versionEndExcluding", ""),
                                   "end_incl": m.get("versionEndIncluding", "")})
    out = {"cve": cve, "score": score, "description": (c.get("descriptions") or [{}])[0].get("value", "")[:500], "ranges": ranges}
    cache[cve] = out
    put_setting("nvd_cache", dict(list(cache.items())[-300:]))
    return out


# ---- matching -------------------------------------------------------------------------------------------------

def _v(s: str) -> tuple:
    nums = re.findall(r"\d+", str(s or ""))
    return tuple(int(x) for x in nums[:4]) if nums else ()


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def affected_range(version: str, ranges: list[dict]) -> dict | None:
    v = _v(version)
    if not v:
        return None
    for r in ranges:
        if r["exact"] and _v(r["exact"]) != v:
            continue
        if r["start"] and v < _v(r["start"]):
            continue
        if r["end_excl"] and v >= _v(r["end_excl"]):
            continue
        if r["end_incl"] and v > _v(r["end_incl"]):
            continue
        if r["exact"] or r["start"] or r["end_excl"] or r["end_incl"]:
            return r
    return None


def _candidates(software: list[dict], kev: list[dict]) -> list[tuple[dict, dict]]:
    """(installed item, KEV entry) pairs that name the same application: the installed name starts with the KEV
    product and the vendor fits the publisher or the name. NVD's version ranges decide afterwards. The operating system
    itself is left to OS updates (install-updates), not matched here."""
    out = []
    for item in software:
        name = _norm(item.get("name"))
        who = _norm(f"{item.get('publisher', '')} {item.get('name', '')}")
        if not name:
            continue
        for k in kev:
            product = _norm(k.get("product"))
            vendor = _norm(k.get("vendorProject")).replace(" group", "").replace(" foundation", "").replace(" inc", "")
            if len(product) < 3 or product.startswith("windows"):
                continue
            if not (name == product or name.startswith(product + " ")):
                continue
            if vendor and not any(w in who for w in vendor.split() if len(w) > 2):
                continue
            out.append((item, k))
    return out


def findings(devices: dict[str, dict] | None = None, kev: list[dict] | None = None) -> list[dict]:
    """Installed software on real machines that a known-exploited vulnerability affects, with the fixed version."""
    from app import endpoints
    devices = devices if devices is not None else endpoints.all_states()
    kev = kev if kev is not None else fetch_kev()
    out = []
    for host, d in devices.items():
        software = (d.get("metrics") or {}).get("software") or []
        for item, k in _candidates(software, kev):
            info = nvd(k["cveID"])
            r = affected_range(item.get("version", ""), [x for x in info.get("ranges", []) if _norm(x["product"]) in _norm(item.get("name"))])
            if not r:
                continue
            out.append({"hostname": host, "cve": k["cveID"], "name": k.get("vulnerabilityName", ""), "app": item.get("name"),
                        "version": item.get("version"), "path": item.get("path", ""), "fixed_in": r["end_excl"] or "",
                        "score": info.get("score"), "ransomware": k.get("knownRansomwareCampaignUse") == "Known",
                        "description": info.get("description") or k.get("shortDescription", ""), "owner": d.get("owner", "")})
    return out


def _ver(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4])


def _per_app(found: list[dict]) -> list[dict]:
    """One finding per application on a device: a single update to the highest fixed version clears all its CVEs,
    so one ticket, one patch and one message, the most severe CVE named first."""
    groups: dict[tuple, list[dict]] = {}
    for f in found:
        groups.setdefault((f["hostname"], f["app"]), []).append(f)
    out = []
    for fs in groups.values():
        fs.sort(key=lambda f: (f["ransomware"], f["score"] or 0), reverse=True)
        top = dict(fs[0])
        if len(fs) > 1:
            top["cve"] = ", ".join(f["cve"] for f in fs)
            top["name"] = f"{fs[0]['name']} +{len(fs) - 1}"
            top["fixed_in"] = max((f["fixed_in"] for f in fs), key=_ver)
            top["ransomware"] = any(f["ransomware"] for f in fs)
        out.append(top)
    return out


# ---- acting -----------------------------------------------------------------------------------------------------

def _admins() -> list[str]:
    """Who hears about it: the report recipients (WhatsApp and e-mail), the desk's admins as a fallback."""
    from app.db import User
    from app.reports import config as report_config
    cfg = report_config()
    to = [x.strip() for x in str(cfg.get("whatsapp_recipients", "")).split(",") if x.strip()]
    to += [x.strip() for x in str(cfg.get("recipients", "")).split(",") if x.strip()]
    if not to:
        with session() as db:
            to = [u.email for u in db.query(User).filter(User.role == "admin", User.active.is_(True))]
    return list(dict.fromkeys(to))


def _already(db, f: dict) -> bool:
    return db.query(Ticket).filter(Ticket.title == f"[Vulnerability] {f['cve']} on {f['hostname']}",
                                   Ticket.status != "resolved").count() > 0


def remediate(f: dict) -> dict:
    """Open the ticket, patch through the agent when allowed, verify, tell the admins. Returns what happened."""
    from app import endpoints, systems
    from app.agents import AGENTS  # noqa: F401  (agents module wires the tools the trail refers to)
    with session() as db:
        if _already(db, f):
            return {"status": "already open"}
        owner = f.get("owner") or ""
        requester = (systems.get_user(owner) or {}).get("phone") if owner else ""
        requester = requester or (_admins() or ["desk"])[0]
        t = Ticket(requester=requester, title=f"[Vulnerability] {f['cve']} on {f['hostname']}", owner="L3", status="open",
                   channel="whatsapp" if requester.startswith("+") else "email")
        db.add(t)
        db.flush()
        log(db, t.id, "system", "note",
            f"Vulnerability watch: {f['cve']} ({f['name']}, CVSS {f['score']}, CISA KEV{', used in ransomware' if f['ransomware'] else ''}) "
            f"affects {f['app']} {f['version']} on {f['hostname']}; fixed in {f['fixed_in'] or 'a newer release'}.", data=f)
        tid = t.id
    app = _norm(f["app"]).split(" ")[0]
    result: dict = {"ticket": tid}
    if config()["auto_remediate"] and app in systems.PATCHABLE_APPS:
        args = {"app": app, "min_version": f["fixed_in"]} if f["fixed_in"] else {"app": app}
        if f.get("path"):
            args["path"] = f["path"]
        r = endpoints.run(f["hostname"], "app-update", tid, "vulnwatch", wait=600, args=args)
        result["patch"] = r
    with session() as db:
        t = db.get(Ticket, tid)
        r = result.get("patch")
        if r and r.get("ok") and r.get("output") and "updated" in r["output"]:
            backup = (re.search(r"Backup: (\S+)", r["output"]) or [None, ""])[1]
            text = (f"🔒 {f['cve']} ({f['name']}, CVSS {f['score']}, CISA'nın aktif istismar listesinde"
                    f"{', fidye yazılımlarında kullanılıyor' if f['ransomware'] else ''}) {f['hostname']} üzerinde "
                    f"{f['app']} {f['version']} sürümünü etkiliyordu. Fark ettim ve giderdim: {r['output'].split(' from ')[0]}. "
                    f"Sürüm tekrar okunup doğrulandı; yedek {backup}. Geri almak gerekirse bilet #{tid} üzerinden 'geri al' yazmanız yeterli.")
            t.resolution = f"{f['cve']} remediated on {f['hostname']}: {r['output'][:200]}"
            resolve(t)
            status = "remediated"
        else:
            why = (r or {}).get("error") or (r or {}).get("output") or ("automatic patching is off" if not config()["auto_remediate"]
                                                                       else f"no automatic updater for {f['app']}")
            text = (f"⚠️ {f['cve']} ({f['name']}, CVSS {f['score']}, CISA KEV) {f['hostname']} üzerindeki {f['app']} {f['version']} "
                    f"sürümünü etkiliyor; düzeltme {f['fixed_in'] or 'yeni sürümde'}. Otomatik düzeltmedim: {why}. Bilet #{tid} L3'te.")
            status = "needs attention"
        log(db, tid, "system", "note", text)
        for to in _admins():
            db.add(Outbound(to=to, ticket_id=tid, text=text))
    result["status"] = status
    return result


def run_watch(force: bool = False) -> list[dict]:
    """The daily pass (or an on-demand one): every finding is handled once while its ticket is open."""
    cfg = config()
    if not force and (not cfg["enabled"] or time.time() - float(cfg.get("last_run") or 0) < EVERY_SEC):
        return []
    put_setting("vulnwatch", {**get_setting("vulnwatch", {}), "last_run": time.time()})
    from app import endpoints
    done, error = [], ""
    devices = endpoints.all_states()
    checked = sum(len((d.get("metrics") or {}).get("software") or []) for d in devices.values())
    try:
        kev = fetch_kev()
        for f in _per_app(findings(devices, kev)):
            done.append({**f, **remediate(f)})
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"[:300]
        logger.warning("vulnerability watch failed: %s", e)
    put_setting("vulnwatch", {**get_setting("vulnwatch", {}), "last_result": {
        "at": time.time(), "devices": len(devices), "software_checked": checked, "kev_entries": len(_kev_cache["items"]),
        "findings": [{k: f.get(k) for k in ("hostname", "cve", "app", "version", "fixed_in", "status", "ticket")} for f in done],
        "error": error}})
    return done
