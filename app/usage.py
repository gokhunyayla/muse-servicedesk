"""Token and latency accounting for every Meta Model API call, attributed to a ticket, a chat thread, a
Supervisor run or a report via a context scope. Feeds the chat footer, the dashboard's AI usage card, the
ticket detail and the reports."""
from __future__ import annotations

import contextvars
from collections import Counter, defaultdict
from datetime import timedelta

from app.db import LlmCall, _now, session
from app.sla import get_setting

_scope: contextvars.ContextVar = contextvars.ContextVar("llm_scope", default=("other", None, ""))
DEFAULT_CONTEXT = {"muse-spark-1.3": 128000, "muse-wonder-1.0": 128000}   # editable: Settings → Models


class scope:
    """with usage.scope("ticket", 12, "L1"): ...   — attribution for calls made inside."""
    def __init__(self, kind: str, sid: int | None = None, actor: str = ""):
        self.value = (kind, sid, actor)

    def __enter__(self):
        self.token = _scope.set(self.value)
        return self

    def __exit__(self, *exc):
        _scope.reset(self.token)


def current() -> tuple[str, int | None, str]:
    return _scope.get()


def context_windows() -> dict:
    return get_setting("context", DEFAULT_CONTEXT)


def record(model: str, prompt: int, completion: int, latency_ms: int, tool_calls: int = 0, ok: bool = True) -> dict:
    kind, sid, actor = current()
    row = {"model": model, "scope": kind, "scope_id": sid, "actor": actor, "prompt_tokens": int(prompt or 0),
           "completion_tokens": int(completion or 0), "total_tokens": int((prompt or 0) + (completion or 0)),
           "latency_ms": int(latency_ms), "tool_calls": tool_calls, "ok": ok}
    try:
        with session() as db:
            db.add(LlmCall(**row))
    except Exception:          # accounting must never break a conversation
        pass
    limit = context_windows().get(model, 128000)
    row["context_limit"] = limit
    row["context_pct"] = round(100 * row["prompt_tokens"] / limit, 1) if limit else None
    return row


# ---- summaries -------------------------------------------------------------------------------

def summary(since=None, scope_kind: str | None = None, scope_id: int | None = None) -> dict:
    with session() as db:
        q = db.query(LlmCall)
        if since:
            q = q.filter(LlmCall.at >= since)
        if scope_kind:
            q = q.filter(LlmCall.scope == scope_kind)
        if scope_id is not None:
            q = q.filter(LlmCall.scope_id == scope_id)
        rows = q.all()
    by_model: dict = defaultdict(lambda: {"calls": 0, "prompt": 0, "completion": 0, "total": 0, "latency_ms": 0})
    by_scope = Counter()
    by_actor = Counter()
    for r in rows:
        m = by_model[r.model]
        m["calls"] += 1
        m["prompt"] += r.prompt_tokens
        m["completion"] += r.completion_tokens
        m["total"] += r.total_tokens
        m["latency_ms"] += r.latency_ms
        by_scope[r.scope] += r.total_tokens
        by_actor[r.actor or "?"] += r.total_tokens
    for m in by_model.values():
        m["avg_latency_ms"] = round(m["latency_ms"] / m["calls"]) if m["calls"] else 0
        del m["latency_ms"]
    total = sum(r.total_tokens for r in rows)
    tickets = {r.scope_id for r in rows if r.scope == "ticket" and r.scope_id}
    return {"calls": len(rows), "total_tokens": total, "prompt_tokens": sum(r.prompt_tokens for r in rows),
            "completion_tokens": sum(r.completion_tokens for r in rows),
            "avg_latency_ms": round(sum(r.latency_ms for r in rows) / len(rows)) if rows else 0,
            "max_prompt_tokens": max((r.prompt_tokens for r in rows), default=0),
            "by_model": dict(by_model), "by_scope": dict(by_scope), "by_actor": dict(by_actor),
            "tickets_touched": len(tickets), "tokens_per_ticket": round(total / len(tickets)) if tickets else 0,
            "errors": sum(1 for r in rows if not r.ok), "context_windows": context_windows()}


def for_thread(thread_id: int) -> dict:
    s = summary(scope_kind="chat", scope_id=thread_id)
    return {"calls": s["calls"], "total_tokens": s["total_tokens"], "max_prompt_tokens": s["max_prompt_tokens"],
            "avg_latency_ms": s["avg_latency_ms"], "context_windows": s["context_windows"]}
