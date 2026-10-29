"""The knowledge base: SOPs in the database, one pool per tier plus a `shared` pool every agent sees.
Humans manage it in the console (Knowledge); agents and the Supervisor propose drafts that an admin publishes
(or auto-publish, Settings → Supervisor). The Markdown files under data/sops/ are only the first seed.

Search is lexical (title + body term overlap) and returns the FULL text with a stable key, so the model
quotes real procedures instead of inventing them; the agent rules require citing [SOP:key] and saying so
when nothing matched."""
from __future__ import annotations

import re
from pathlib import Path

from contextlib import nullcontext

from app.db import Sop, SopVersion, _now, session


def _db(db):
    """Reuse the caller's session when given (one writer at a time on SQLite), else open one."""
    return nullcontext(db) if db is not None else session()

SOP_DIR = Path(__file__).resolve().parent.parent / "data" / "sops"
TIERS = ("L1", "L2", "L3", "L4", "shared")
_TR = str.maketrans("çğıöşüâîû", "cgiosuaiu")


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"\w+", text.lower()) if len(t) > 2}


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower().translate(_TR)).strip("-")[:60] or "sop"


# ---- seeding from files (first run only) ------------------------------------------------------

# Shipped SOPs were renamed when the knowledge base moved to English (v0.12). A database seeded before that keeps
# the old keys; renamed() moves those rows to the new key and the shipped English text, once.
RENAMED = {
    "L1/001-vpn-baglanti-sorunu": "L1/001-vpn-connection", "L1/002-sifre-sifirlama": "L1/002-password-reset",
    "L1/003-yazici": "L1/003-printer", "L1/004-lisans-ve-uygulama-erisimi": "L1/004-licences-and-app-access",
    "L1/005-oltalama-e-posta": "L1/005-phishing-email", "L1/006-ise-giris-ve-cikis": "L1/006-onboarding-and-offboarding",
    "L1/007-genel-teshis": "L1/007-general-approach", "L1/008-uyum-ihlali-kamera": "L1/008-camera-compliance-finding",
    "L1/009-acikta-birakilmis-kimlik-bilgisi-kamera-masa-bulgusu-zorunlu-sifre": "L1/009-exposed-credential",
    "L2/001-ag-ve-cihaz-teshisi": "L2/001-network-and-device-diagnosis", "L2/002-zararli-yazilim-mudahalesi": "L2/002-malware-response",
    "L2/003-uzun-sure-guncellenmemis-dizustu-stale-patch-cevrimdisi-cihaz": "L2/003-stale-patch-laptop",
    "L3/001-degisiklik-yonetimi": "L3/001-change-management", "L3/002-sap-ve-dizin-yetkileri": "L3/002-directory-and-folder-access",
    "L3/002-sap-and-directory-access": "L3/002-directory-and-folder-access",
    "L3/003-toplu-kesinti": "L3/003-mass-outage", "L4/001-donanim-servis-ve-kargo": "L4/001-hardware-repair-and-shipping",
}


def _read(key: str) -> tuple[str, str] | None:
    p = SOP_DIR / f"{key}.md"
    if not p.is_file():
        return None
    body = p.read_text(encoding="utf-8")
    return (body.splitlines()[0].lstrip("# ").strip() if body else p.stem), "\n".join(body.splitlines()[1:]).strip()


def migrate_renamed() -> int:
    """Move SOP rows seeded under the old (Turkish) keys to the English keys and text. Idempotent."""
    n = 0
    with session() as db:
        for old, new in RENAMED.items():
            row = db.query(Sop).filter(Sop.key == old).first()
            if row is None or db.query(Sop).filter(Sop.key == new).first() is not None:
                continue
            got = _read(new)
            if got is None:
                continue
            row.key, (row.title, row.content) = new, got
            n += 1
    return n


def seed_from_files() -> int:
    """Load data/sops/<tier>/*.md: every shipped SOP whose key the table does not have yet (a fresh install gets
    them all, an existing one gets the SOPs added in later releases). A shipped SOP nobody has edited follows the
    newer shipped text (the old text is kept as a version); edited or retired ones are left alone.
    Returns how many were added."""
    migrate_renamed()
    with session() as db:
        have = {s.key: s for s in db.query(Sop)}
        n = 0
        for tier in ("L1", "L2", "L3", "L4"):
            d = SOP_DIR / tier
            if not d.is_dir():
                continue
            for p in sorted(d.glob("*.md")):
                body = p.read_text(encoding="utf-8")
                title = body.splitlines()[0].lstrip("# ").strip() if body else p.stem
                content = "\n".join(body.splitlines()[1:]).strip()
                old = have.get(f"{tier}/{p.stem}")
                if old is not None:
                    if old.source == "seed" and (old.updated_by or "seed") == "seed" and old.content != content:
                        update(old.key, title, content, by="seed", db=db)
                    continue
                db.add(Sop(key=f"{tier}/{p.stem}", tier=tier, title=title, content=content, status="published",
                           author="seed", source="seed"))
                n += 1
        return n


# ---- reading ------------------------------------------------------------------------------------

def search(tier: str, query: str, limit: int = 3, db=None) -> list[dict]:
    """Best matches for this tier: own pool and the shared pool first, other tiers after (marked), so an
    agent can always reach everything the desk knows. Only published SOPs."""
    q = _tokens(query)
    scored = []
    with _db(db) as db:
        for s in db.query(Sop).filter(Sop.status == "published").all():
            hits = len(q & _tokens(s.content)) + 2 * len(q & _tokens(s.title))
            if not hits:
                continue
            own = s.tier in (tier, "shared")
            scored.append((own, hits, s))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        out = []
        for own, hits, s in scored[:limit]:
            s.used += 1
            learned = lessons(s.key, status="approved", db=db)
            content = s.content
            if learned:
                content += ("\n\nLearned on the job (approved additions; the steps above always come first and win any "
                            "conflict):\n" + "\n".join(f"- {x['text']} [lesson {x['id']}]" for x in learned))
            out.append({"id": s.key, "title": s.title, "tier": s.tier, "content": content,
                        "from_other_tier": not own, "version": s.version})
        return out


def titles(tier: str, db=None) -> list[str]:
    with _db(db) as db:
        rows = (db.query(Sop).filter(Sop.status == "published", Sop.tier.in_([tier, "shared"]))
                .order_by(Sop.tier, Sop.key).all())
        return [s.title if s.tier == tier else f"{s.title} (shared)" for s in rows]


def get(key: str, db=None) -> Sop | None:
    with _db(db) as db:
        return db.query(Sop).filter(Sop.key == key).first()


# ---- writing ------------------------------------------------------------------------------------

def _unique_key(db, tier: str, title: str) -> str:
    n = db.query(Sop).filter(Sop.tier == tier).count() + 1
    base = f"{tier}/{n:03d}-{_slug(title)}"
    key = base
    i = 1
    while db.query(Sop).filter(Sop.key == key).first():
        i += 1
        key = f"{base}-{i}"
    return key


def write(tier: str, title: str, content: str, author: str = "", source: str = "agent", status: str = "draft",
          ticket_ids: list[int] | None = None, sources: list[str] | None = None, db=None) -> str:
    """Add a SOP and return its key. Agents' and the Supervisor's writes are drafts unless told otherwise."""
    if tier not in TIERS:
        raise ValueError("tier must be L1..L4 or shared")
    with _db(db) as db:
        key = _unique_key(db, tier, title)
        db.add(Sop(key=key, tier=tier, title=title[:255], content=content.strip(), status=status, author=author,
                   source=source, ticket_ids=list(ticket_ids or []), sources=list(sources or [])))
        db.flush()
        return key


SYSTEM_WRITERS = {"L1", "L2", "L3", "L4", "supervisor", "SUP", "agent"}


def update(key: str, title: str, content: str, by: str, tier: str | None = None, db=None) -> bool:
    """Edit a SOP (new version; the old one is kept). Core procedures are changed by people only: the desk's
    own agents and the Supervisor add lessons (below) instead."""
    if by in SYSTEM_WRITERS:
        return False
    with _db(db) as db:
        s = db.query(Sop).filter(Sop.key == key).first()
        if s is None:
            return False
        db.add(SopVersion(sop_id=s.id, version=s.version, title=s.title, content=s.content, saved_by=by))
        s.title, s.content, s.version, s.updated_by, s.updated_at = title[:255], content.strip(), s.version + 1, by, _now()
        if tier in TIERS:
            s.tier = tier
        return True


def set_status(key: str, status: str, by: str, db=None) -> bool:
    if status not in ("draft", "published", "retired"):
        return False
    with _db(db) as db:
        s = db.query(Sop).filter(Sop.key == key).first()
        if s is None:
            return False
        s.status, s.updated_by, s.updated_at = status, by, _now()
        return True


def listing(status: str | None = None, db=None) -> list[dict]:
    with _db(db) as db:
        q = db.query(Sop).order_by(Sop.tier, Sop.key)
        if status:
            q = q.filter(Sop.status == status)
        return [{"key": s.key, "tier": s.tier, "title": s.title, "status": s.status, "author": s.author, "source": s.source,
                 "version": s.version, "updated_at": s.updated_at.isoformat(), "used": s.used, "content": s.content,
                 "sources": s.sources, "ticket_ids": s.ticket_ids} for s in q]


# ---- lessons: what the desk learned, kept apart from the core procedures --------------------------

# Things a lesson may never do, whoever proposes it: weaken identity checks, approvals or consent, hand out
# secrets, or switch protections off. Checked in English and Turkish because lessons are written from tickets.
FORBIDDEN = [
    (r"\b(skip|bypass|ignore|omit|without|no need (to|for)|don'?t|do not|never)\b[^.]{0,40}\b(verif\w*|identit\w*|approv\w*|consent|mfa|authenticat\w*)", "weakens identity, approval or consent"),
    (r"\b(doğrula\w*|onay\w*|kimlik\w*|izin)\b[^.]{0,40}\b(atla\w*|gerek yok|sorma\w*|yapma\w*|olmadan)", "weakens identity, approval or consent"),
    (r"\b(atla\w*|sormadan|onaysız|doğrulamadan)\b", "weakens identity, approval or consent"),
    (r"\b(disable|turn off|switch off|kapat\w*)\b[^.]{0,40}\b(defender|antivirus|firewall|mfa|audit|logging|nla|güvenlik duvarı)", "turns a protection off"),
    (r"\b(send|share|tell|paste|gönder\w*|paylaş\w*|yaz\w*)\b[^.]{0,40}\b(password|şifre\w*|token|secret|api key)\b[^.]{0,30}\b(e-?mail|chat|ticket|whatsapp|mail|bilet)", "hands out a secret over a channel"),
    (r"\b(all|every|herkes\w*|tüm)\b[^.]{0,20}\b(admin|global administrator|yönetici yetki\w*)", "grants broad privileges"),
]


def check_lesson(sop_key: str, text: str, db=None) -> dict:
    """The automatic gate every lesson passes before an admin even sees it as approvable."""
    import re as _re
    from app.tools import REGISTRY as TOOLS
    problems = []
    text = (text or "").strip()
    if not 15 <= len(text) <= 600:
        problems.append("a lesson is one or two sentences (15-600 characters)")
    sop = get(sop_key, db=db)
    if sop is None or sop.status != "published":
        problems.append(f"no published SOP {sop_key} to add it to")
    low = text.lower()
    for pattern, why in FORBIDDEN:
        if _re.search(pattern, low):
            problems.append(why)
    if sop is not None:
        named = {t for t in TOOLS if _re.search(rf"\b{_re.escape(t)}\b", text)}
        allowed = {t for t in TOOLS if sop.tier == "shared" or sop.tier in TOOLS[t].tiers}
        for t in sorted(named - allowed):
            problems.append(f"tool {t} is not available to {sop.tier}")
        for t in sorted(set(_re.findall(r"\b[a-z]+(?:_[a-z0-9]+)+\b", text)) - set(TOOLS)):
            problems.append(f"{t} is not a tool the desk has")
    return {"ok": not problems, "problems": list(dict.fromkeys(problems))}


def propose_lesson(sop_key: str, text: str, reason: str, by: str, ticket_ids: list[int] | None = None, db=None) -> dict:
    from app.db import SopLesson
    chk = check_lesson(sop_key, text, db=db)
    with _db(db) as db:
        same = db.query(SopLesson).filter(SopLesson.sop_key == sop_key, SopLesson.text == text.strip(),
                                          SopLesson.status.in_(["proposed", "approved"])).first()
        if same is not None:
            return {"ok": True, "lesson_id": same.id, "status": same.status, "note": "already proposed"}
        row = SopLesson(sop_key=sop_key, text=text.strip(), reason=reason.strip(), proposed_by=by, check=chk,
                        ticket_ids=list(ticket_ids or []), status="proposed" if chk["ok"] else "rejected",
                        note="" if chk["ok"] else "safety check: " + "; ".join(chk["problems"]))
        db.add(row)
        db.flush()
        return {"ok": chk["ok"], "lesson_id": row.id, "status": row.status, "problems": chk["problems"],
                "next": "waits for an admin to approve" if chk["ok"] else "rejected by the safety check; the core SOP stays as is"}


def decide_lesson(lesson_id: int, status: str, by: str, note: str = "", db=None) -> dict:
    """approved | rejected (admins) or suspended (admins or the Supervisor, any time). Approving re-runs the check."""
    from app.db import SopLesson
    if status not in ("approved", "rejected", "suspended"):
        return {"ok": False, "error": "status must be approved, rejected or suspended"}
    with _db(db) as db:
        row = db.get(SopLesson, int(lesson_id))
        if row is None:
            return {"ok": False, "error": "unknown lesson"}
        if status == "approved":
            if by in SYSTEM_WRITERS:
                return {"ok": False, "error": "only a person approves a lesson"}
            chk = check_lesson(row.sop_key, row.text, db=db)
            row.check = chk
            if not chk["ok"]:
                return {"ok": False, "error": "safety check fails: " + "; ".join(chk["problems"])}
        row.status, row.decided_by, row.decided_at, row.note = status, by, _now(), note or row.note
        return {"ok": True, "lesson_id": row.id, "status": status}


def lessons(sop_key: str | None = None, status: str | None = None, db=None) -> list[dict]:
    from app.db import SopLesson
    with _db(db) as db:
        q = db.query(SopLesson).order_by(SopLesson.id)
        if sop_key:
            q = q.filter(SopLesson.sop_key == sop_key)
        if status:
            q = q.filter(SopLesson.status == status)
        return [{"id": r.id, "sop_key": r.sop_key, "text": r.text, "reason": r.reason, "status": r.status,
                 "check": r.check or {}, "ticket_ids": r.ticket_ids or [], "proposed_by": r.proposed_by,
                 "decided_by": r.decided_by, "note": r.note, "created_at": r.created_at.isoformat()} for r in q]
