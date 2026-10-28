"""Token and latency accounting for every Meta Model API call, attributed to a ticket, a chat thread, a
Supervisor run or a report via a context scope. Feeds the chat footer, the dashboard's AI usage card, the
ticket detail and the reports."""
from __future__ import annotations

import contextvars
from collections import Counter, defaultdict
from datetime import timedelta

from app.db import LlmCall, _now, session
from app.sla import get_setting

_scope: contextvars.ContextVar = contextvars.ContextVar("llm_scope", default=("other", None, "", None))
DEFAULT_CONTEXT = {"muse-spark-1.3": 128000, "muse-wonder-1.0": 128000}   # editable: Settings → Models
# price per 1M tokens, in the currency below; 0 = unknown (then no money figures are shown). Settings → Models.
DEFAULT_PRICING = {"currency": "$", "models": {"muse-spark-1.3": {"prompt": 0.0, "completion": 0.0},
                                               "muse-wonder-1.0": {"prompt": 0.0, "completion": 0.0}}}


def pricing() -> dict:
    saved = get_setting("pricing", {})
    out = {"currency": saved.get("currency") or DEFAULT_PRICING["currency"], "models": dict(DEFAULT_PRICING["models"])}
    out["models"].update(saved.get("models", {}))
    return out


def priced() -> bool:
    return any(float(v.get("prompt", 0) or 0) + float(v.get("completion", 0) or 0) > 0 for v in pricing()["models"].values())


def cost_of(prompt_tokens: int, completion_tokens: int, model: str, p: dict | None = None) -> float:
    p = p or pricing()
    rates = p["models"].get(model) or {}
    return (float(prompt_tokens or 0) * float(rates.get("prompt", 0) or 0) + float(completion_tokens or 0) * float(rates.get("completion", 0) or 0)) / 1_000_000


def money(amount: float | None, p: dict | None = None) -> str:
    """'$0.0123' / '$1.24' — or '' when nothing is priced."""
    if amount is None or not priced():
        return ""
    cur = (p or pricing())["currency"]
    return f"{cur}{amount:.4f}" if amount < 0.1 else f"{cur}{amount:.2f}"


class scope:
    """with usage.scope("ticket", 12, "L1"): ...   — attribution for calls made inside."""
    def __init__(self, kind: str, sid: int | None = None, actor: str = "", db=None):
        self.value = (kind, sid, actor, db)     # db: the caller's open session, so recording never opens a second one

    def __enter__(self):
        self.token = _scope.set(self.value)
        return self

    def __exit__(self, *exc):
        _scope.reset(self.token)


def current() -> tuple[str, int | None, str]:
    return _scope.get()[:3]


def context_windows() -> dict:
    return get_setting("context", DEFAULT_CONTEXT)


def record(model: str, prompt: int, completion: int, latency_ms: int, tool_calls: int = 0, ok: bool = True) -> dict:
    kind, sid, actor, db = _scope.get()
    row = {"model": model, "scope": kind, "scope_id": sid, "actor": actor, "prompt_tokens": int(prompt or 0),
           "completion_tokens": int(completion or 0), "total_tokens": int((prompt or 0) + (completion or 0)),
           "latency_ms": int(latency_ms), "tool_calls": tool_calls, "ok": ok}
    try:
        if db is not None:                 # inside the caller's transaction (SQLite would otherwise wait on the lock)
            db.add(LlmCall(**row))
            db.flush()
        else:
            with session() as own:
                own.add(LlmCall(**row))
    except Exception:          # accounting must never break a conversation
        pass
    limit = context_windows().get(model, 128000)
    row["context_limit"] = limit
    row["context_pct"] = round(100 * row["prompt_tokens"] / limit, 1) if limit else None
    return row


# ---- summaries -------------------------------------------------------------------------------

def _rows(db, since, scope_kind, scope_id):
    q = db.query(LlmCall)
    if since:
        q = q.filter(LlmCall.at >= since)
    if scope_kind:
        q = q.filter(LlmCall.scope == scope_kind)
    if scope_id is not None:
        q = q.filter(LlmCall.scope_id == scope_id)
    return q.all()


def summary(since=None, scope_kind: str | None = None, scope_id: int | None = None, db=None) -> dict:
    """`db`: reuse the caller's open session (needed to see rows recorded in the same transaction)."""
    if db is not None:
        rows = _rows(db, since, scope_kind, scope_id)
    else:
        with session() as own:
            rows = _rows(own, since, scope_kind, scope_id)
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
    p = pricing()
    cost = sum(cost_of(r.prompt_tokens, r.completion_tokens, r.model, p) for r in rows)
    for name, m in by_model.items():
        m["cost"] = round(cost_of(m["prompt"], m["completion"], name, p), 6)
    return {"calls": len(rows), "total_tokens": total, "prompt_tokens": sum(r.prompt_tokens for r in rows),
            "cost": round(cost, 6), "cost_label": money(cost, p), "currency": p["currency"], "priced": priced(),
            "cost_per_ticket": round(cost / len(tickets), 6) if tickets else 0.0,
            "cost_per_ticket_label": money(cost / len(tickets), p) if tickets else "",
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
    p = pricing()
    for r in rows:
        d = r.at.replace(tzinfo=UTC).astimezone(tz).strftime("%Y-%m-%d")
        x = days.setdefault(d, {"day": d, "calls": 0, "tokens": 0, "latency_ms": 0, "errors": 0, "cost": 0.0})
        x["calls"] += 1
        x["tokens"] += r.total_tokens
        x["cost"] += cost_of(r.prompt_tokens, r.completion_tokens, r.model, p)
        x["latency_ms"] += r.latency_ms
        x["errors"] += 0 if r.ok else 1
    out = sorted(days.values(), key=lambda x: x["day"])
    for x in out:
        x["avg_latency_ms"] = round(x["latency_ms"] / x["calls"]) if x["calls"] else 0
        x["cost"], x["cost_label"] = round(x["cost"], 6), money(x["cost"], p)
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
