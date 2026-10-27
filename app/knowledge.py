"""Per-tier knowledge pools: Markdown SOPs under data/sops/<tier>/. Agents search them before
acting, and write new ones when they solve something the pool didn't cover (learning loop).

Search is lexical (title + body term overlap). It's deliberately simple: the pools are small,
and the model reads whole SOPs, not snippets.
"""
from __future__ import annotations

import re
from pathlib import Path

SOP_DIR = Path(__file__).resolve().parent.parent / "data" / "sops"


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"\w+", text.lower()) if len(t) > 2}


def _pool(tier: str) -> list[tuple[str, str]]:
    d = SOP_DIR / tier
    return sorted((p.stem, p.read_text(encoding="utf-8")) for p in d.glob("*.md")) if d.is_dir() else []


def search(tier: str, query: str, limit: int = 3) -> list[dict]:
    q = _tokens(query)
    scored = []
    for name, body in _pool(tier):
        title = body.splitlines()[0].lstrip("# ").strip() if body else name
        hits = len(q & _tokens(body)) + 2 * len(q & _tokens(title))
        if hits:
            scored.append((hits, name, title, body))
    scored.sort(reverse=True)
    return [{"id": n, "title": t, "content": b} for _, n, t, b in scored[:limit]]


def titles(tier: str) -> list[str]:
    return [body.splitlines()[0].lstrip("# ").strip() for _, body in _pool(tier) if body]


_TR = str.maketrans("çğıöşüâîû", "cgiosuaiu")


def _ascii(text: str) -> str:
    return text.translate(_TR)


def write(tier: str, title: str, content: str) -> str:
    """Add a SOP. Returns its id. Existing ids are never overwritten."""
    slug = re.sub(r"[^a-z0-9]+", "-", _ascii(title.lower())).strip("-")[:60] or "sop"
    d = SOP_DIR / tier
    d.mkdir(parents=True, exist_ok=True)
    n = len(list(d.glob("*.md"))) + 1
    path = d / f"{n:03d}-{slug}.md"
    path.write_text(f"# {title}\n\n{content.strip()}\n", encoding="utf-8")
    return path.stem
