"""Reference policies and procedures: documents desk users upload (PDF, DOCX, Markdown, text). The text is
extracted and split into passages; agents search them (`search_references`) and must quote the passage they
rely on as [REF:doc#n]. Files stay on disk under data/references/ (git-ignored); passages live in the DB."""
from __future__ import annotations

import io
import re
from contextlib import nullcontext
from pathlib import Path

from app.db import RefChunk, RefDoc, session

REF_DIR = Path(__file__).resolve().parent.parent / "data" / "references"
MAX_BYTES = 25 * 1024 * 1024
CHUNK_CHARS = 900
ALLOWED = {"pdf", "docx", "md", "markdown", "txt", "text"}


def _db(db):
    return nullcontext(db) if db is not None else session()


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"\w+", text.lower()) if len(t) > 2}


# ---- extraction --------------------------------------------------------------------------------

def extract(filename: str, data: bytes) -> list[tuple[int | None, str]]:
    """(page, text) pairs. PDF: per page; DOCX: one stream with headings kept; MD/TXT: the file."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return [(i + 1, (p.extract_text() or "")) for i, p in enumerate(reader.pages)]
    if ext == "docx":
        import docx
        d = docx.Document(io.BytesIO(data))
        lines = []
        for p in d.paragraphs:
            t = p.text.strip()
            if not t:
                continue
            if p.style is not None and p.style.name.lower().startswith("heading"):
                lines.append(f"\n# {t}\n")
            else:
                lines.append(t)
        for table in d.tables:
            for row in table.rows:
                lines.append(" | ".join(c.text.strip() for c in row.cells))
        return [(None, "\n".join(lines))]
    if ext in ALLOWED:
        return [(None, data.decode("utf-8", errors="replace"))]
    raise ValueError(f"unsupported file type .{ext}; use PDF, DOCX, Markdown or text")


def chunk(pages: list[tuple[int | None, str]]) -> list[dict]:
    """Split by headings/blank lines into ~CHUNK_CHARS passages, remembering page and nearest heading."""
    out, heading = [], ""
    for page, text in pages:
        buf = ""
        text = re.sub(r"^(#+ .*)$", r"\1\n", text.replace("\r", ""), flags=re.M)   # headings stand alone
        for para in re.split(r"\n\s*\n|\n(?=#)", text):
            para = para.strip()
            if not para:
                continue
            m = re.match(r"^#+\s*(.+)$", para.splitlines()[0])
            if m and len(para.splitlines()) <= 2:
                if buf.strip():                       # a new section starts a new passage
                    out.append({"page": page, "heading": heading, "text": buf.strip()})
                    buf = ""
                heading = m.group(1).strip()[:255]
                continue
            if len(buf) + len(para) > CHUNK_CHARS and buf:
                out.append({"page": page, "heading": heading, "text": buf.strip()})
                buf = ""
            buf += para + "\n\n"
        if buf.strip():
            out.append({"page": page, "heading": heading, "text": buf.strip()})
    return [c for c in out if len(c["text"]) > 40]


# ---- storage -----------------------------------------------------------------------------------

KINDS = ("policy", "guideline", "manual")
ENFORCEMENT = ("strict", "flexible")


def add(title: str, filename: str, data: bytes, by: str, scope: str = "shared", description: str = "",
        mime: str = "", kind: str = "policy", enforcement: str = "strict", db=None) -> RefDoc:
    if len(data) > MAX_BYTES:
        raise ValueError("file too large (max 25 MB)")
    if scope not in ("shared", "L1", "L2", "L3", "L4"):
        raise ValueError("scope must be shared or L1..L4")
    if kind not in KINDS or enforcement not in ENFORCEMENT:
        raise ValueError("kind must be policy/guideline/manual and enforcement strict/flexible")
    pages = extract(filename, data)
    chunks = chunk(pages)
    if not chunks:
        raise ValueError("no readable text found in the file (scanned PDF? export it with text first)")
    with _db(db) as db:
        doc = RefDoc(title=title[:255] or filename, filename=filename[:255], mime=mime, size=len(data), scope=scope,
                     kind=kind, enforcement=enforcement, description=description[:2000], uploaded_by=by, chunks=len(chunks))
        db.add(doc)
        db.flush()
        for i, c in enumerate(chunks, 1):
            db.add(RefChunk(doc_id=doc.id, idx=i, page=c["page"], heading=c["heading"], text=c["text"]))
        REF_DIR.mkdir(parents=True, exist_ok=True)
        (REF_DIR / f"{doc.id}-{re.sub(r'[^A-Za-z0-9._-]+', '_', filename)[:80]}").write_bytes(data)
        db.flush()
        db.refresh(doc)
        return doc


def remove(doc_id: int, by: str, db=None) -> bool:
    with _db(db) as db:
        d = db.get(RefDoc, doc_id)
        if d is None:
            return False
        d.status = "removed"
        return True


def listing(db=None) -> list[dict]:
    with _db(db) as db:
        return [{"id": d.id, "title": d.title, "filename": d.filename, "size": d.size, "scope": d.scope, "kind": d.kind,
                 "enforcement": d.enforcement, "description": d.description,
                 "uploaded_by": d.uploaded_by, "created_at": d.created_at.isoformat(), "chunks": d.chunks}
                for d in db.query(RefDoc).filter(RefDoc.status == "active").order_by(RefDoc.id.desc())]


# ---- search ------------------------------------------------------------------------------------

def search(tier: str, query: str, limit: int = 4, db=None) -> list[dict]:
    """Passages that overlap the query most; own-scope and shared documents first. Full passage text is
    returned so the model quotes real policy wording and cites [REF:doc#idx]."""
    q = _tokens(query)
    if not q:
        return []
    scored = []
    with _db(db) as db:
        rows = (db.query(RefChunk, RefDoc).join(RefDoc, RefChunk.doc_id == RefDoc.id)
                .filter(RefDoc.status == "active", RefDoc.scope.in_(["shared", tier])).all())
        for c, d in rows:
            hits = len(q & _tokens(c.text)) + 2 * len(q & _tokens(c.heading)) + 2 * len(q & _tokens(d.title))
            if hits:
                scored.append((hits, c, d))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [{"ref": f"{d.id}#{c.idx}", "document": d.title, "kind": d.kind, "enforcement": d.enforcement,
                 "page": c.page, "heading": c.heading, "text": c.text, "scope": d.scope} for _, c, d in scored[:limit]]
