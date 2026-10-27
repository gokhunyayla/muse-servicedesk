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

def seed_from_files() -> int:
    """Load data/sops/<tier>/*.md into the table when it is empty. Returns how many were added."""
    with session() as db:
        if db.query(Sop).count():
            return 0
        n = 0
        for tier in ("L1", "L2", "L3", "L4"):
            d = SOP_DIR / tier
            if not d.is_dir():
                continue
            for p in sorted(d.glob("*.md")):
                body = p.read_text(encoding="utf-8")
                title = body.splitlines()[0].lstrip("# ").strip() if body else p.stem
                content = "\n".join(body.splitlines()[1:]).strip()
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
            out.append({"id": s.key, "title": s.title, "tier": s.tier, "content": s.content,
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


def update(key: str, title: str, content: str, by: str, tier: str | None = None, db=None) -> bool:
    """Edit a SOP (new version; the old one is kept)."""
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
