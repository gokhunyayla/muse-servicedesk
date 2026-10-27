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


def calls(since=None, limit: int = 500, scope_kind: str | None = None, model: str | None = None) -> list[dict]:
    with session() as db:
        q = db.query(LlmCall).order_by(LlmCall.id.desc())
        if since:
            q = q.filter(LlmCall.at >= since)
        if scope_kind:
            q = q.filter(LlmCall.scope == scope_kind)
        if model:
            q = q.filter(LlmCall.model == model)
        return [{"id": r.id, "at": r.at.isoformat(), "model": r.model, "scope": r.scope, "scope_id": r.scope_id, "actor": r.actor,
                 "prompt_tokens": r.prompt_tokens, "completion_tokens": r.completion_tokens, "total_tokens": r.total_tokens,
                 "latency_ms": r.latency_ms, "tool_calls": r.tool_calls, "ok": r.ok} for r in q.limit(min(limit, 5000))]


def daily(since=None) -> list[dict]:
    """Per day (desk time zone): calls, tokens, average latency, errors."""
    import os
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(os.getenv("DESK_TZ", "Europe/Istanbul"))
    from datetime import UTC
    with session() as db:
        q = db.query(LlmCall)
        if since:
            q = q.filter(LlmCall.at >= since)
        rows = q.all()
    days: dict = {}
    for r in rows:
        d = r.at.replace(tzinfo=UTC).astimezone(tz).strftime("%Y-%m-%d")
        x = days.setdefault(d, {"day": d, "calls": 0, "tokens": 0, "latency_ms": 0, "errors": 0})
        x["calls"] += 1
        x["tokens"] += r.total_tokens
        x["latency_ms"] += r.latency_ms
        x["errors"] += 0 if r.ok else 1
    out = sorted(days.values(), key=lambda x: x["day"])
    for x in out:
        x["avg_latency_ms"] = round(x["latency_ms"] / x["calls"]) if x["calls"] else 0
        del x["latency_ms"]
    return out


def tool_performance(since=None) -> list[dict]:
    """Per tool: calls, failures and which tiers use it — from the ticket trails."""
    from app.db import Event
    with session() as db:
        q = db.query(Event).filter(Event.kind == "tool_result")
        if since:
            q = q.filter(Event.at >= since)
        stats: dict = {}
        for e in q.all():
            s = stats.setdefault(e.content, {"tool": e.content, "calls": 0, "failures": 0, "tiers": Counter()})
            s["calls"] += 1
            if isinstance(e.data, dict) and e.data.get("ok") is False:
                s["failures"] += 1
            s["tiers"][e.actor] += 1
    out = []
    for s in stats.values():
        out.append({"tool": s["tool"], "calls": s["calls"], "failures": s["failures"],
                    "failure_rate": round(100 * s["failures"] / s["calls"], 1) if s["calls"] else 0,
                    "tiers": dict(s["tiers"])})
    out.sort(key=lambda x: -x["calls"])
    return out
