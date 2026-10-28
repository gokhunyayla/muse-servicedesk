import pytest

from app import chat, usage  # noqa: F401
from app.agents import handle_message
from app.db import LlmCall, init_db, session
from app.llm import Reply, ScriptedLLM, ToolCall, set_llm


@pytest.fixture(autouse=True)
def fresh(tmp_path):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")


def test_every_model_call_is_accounted_to_its_ticket():
    set_llm(ScriptedLLM([Reply(tool_calls=[ToolCall("c1", "search_knowledge", {"query": "vpn"})]), Reply(text="ok")]))
    with session() as db:
        t, _ = handle_message(db, "+905551110001", "vpn bağlanmıyor")
    with session() as db:
        rows = db.query(LlmCall).all()
    assert len(rows) == 2 and all(r.scope == "ticket" and r.scope_id == t.id and r.actor == "L1" for r in rows)
    assert rows[0].tool_calls == 1 and rows[0].prompt_tokens > 0 and rows[0].model == "muse-spark-1.3"
    s = usage.summary(scope_kind="ticket", scope_id=t.id)
    assert s["calls"] == 2 and s["total_tokens"] > 0 and s["by_actor"] == {"L1": s["total_tokens"]} and s["tickets_touched"] == 1


def test_chat_turns_carry_usage_and_context_share(desk):
    th = desk.post("/chat/threads").json()
    set_llm(ScriptedLLM([Reply(tool_calls=[ToolCall("c", "desk_overview", {})]), Reply(text="Quiet desk.")]))
    r = desk.post(f"/chat/threads/{th['id']}", json={"text": "status?"}).json()
    u = r["usage"]
    assert u["model"] == "muse-wonder-1.0" and u["prompt_tokens"] > 0 and u["context_limit"] == 128000 and 0 <= u["context_pct"] < 100
    assert r["thread_usage"]["calls"] == 2 and r["thread_usage"]["total_tokens"] > 0
    assert desk.get(f"/chat/threads/{th['id']}").json()[-1]["usage"]["model"] == "muse-wonder-1.0"
    assert desk.get(f"/chat/threads/{th['id']}/usage").json()["calls"] == 2


def test_dashboard_usage_endpoint_and_context_setting(desk):
    set_llm(ScriptedLLM([Reply(text="ok")]))
    desk.post("/messages", json={"requester": "+905551110001", "text": "yazıcı"})
    ai = desk.get("/ai/usage?period=today").json()
    assert ai["calls"] == 1 and "muse-spark-1.3" in ai["by_model"] and ai["by_scope"] == {"ticket": ai["total_tokens"]}
    assert desk.get("/tickets/1").json()["ai"]["calls"] == 1
    r = desk.post("/settings/context", data={"muse-spark-1.3": "64000", "muse-wonder-1.0": "200000"}, follow_redirects=False)
    assert r.status_code == 303 and usage.context_windows()["muse-wonder-1.0"] == 200000
    assert "Context windows" in desk.get("/settings?tab=models").text


def test_ai_activity_endpoints_and_page(desk):
    set_llm(ScriptedLLM([Reply(tool_calls=[ToolCall("c1", "run_endpoint_script", {"hostname": "TR-LT-101", "script": "rm-rf"})]),
                         Reply(tool_calls=[ToolCall("c2", "search_knowledge", {"query": "vpn"})]), Reply(text="ok")]))
    desk.post("/messages", json={"requester": "+905551110001", "text": "vpn"})
    calls = desk.get("/ai/calls?period=today").json()
    assert len(calls) == 3 and calls[0]["scope"] == "ticket" and calls[0]["at"] and calls[-1]["tool_calls"] == 1
    assert desk.get("/ai/calls?period=today&scope=chat").json() == []
    day = desk.get("/ai/daily?period=all").json()
    assert len(day) == 1 and day[0]["calls"] == 3 and day[0]["tokens"] > 0
    tools = {t["tool"]: t for t in desk.get("/ai/tools").json()}
    assert tools["run_endpoint_script"]["failures"] == 1 and tools["run_endpoint_script"]["failure_rate"] == 100.0
    assert tools["search_knowledge"]["tiers"] == {"L1": 1}
    csv = desk.get("/ai/calls.csv?period=all").text.splitlines()
    assert csv[0].startswith("id,at,model") and len(csv) == 4
    assert "AI activity" in desk.get("/ai").text


def test_pricing_turns_tokens_into_money_everywhere(desk):
    set_llm(ScriptedLLM([Reply(text="ok")]))
    desk.post("/messages", json={"requester": "+905551110001", "text": "yazıcı"})
    assert usage.summary()["priced"] is False and usage.summary()["cost_label"] == ""          # unpriced: no money shown
    r = desk.post("/settings/pricing", data={"currency": "$", "muse-spark-1.3__prompt": "0.5", "muse-spark-1.3__completion": "2",
                                             "muse-wonder-1.0__prompt": "3", "muse-wonder-1.0__completion": "15"}, follow_redirects=False)
    assert r.status_code == 303
    s = usage.summary()
    expected = (s["prompt_tokens"] * 0.5 + s["completion_tokens"] * 2) / 1_000_000
    assert s["priced"] and abs(s["cost"] - expected) < 1e-6 and s["cost_label"].startswith("$") and s["cost_per_ticket"] == s["cost"]
    assert desk.get("/tickets/1").json()["ai"]["cost_label"] == s["cost_label"]
    assert desk.get("/ai/daily?period=all").json()[0]["cost_label"] == s["cost_label"]
    assert "Pricing" in desk.get("/settings?tab=models").text
    from app import reports
    n = reports.numbers("daily")
    assert n["ai"]["priced"] and n["ai"]["cost_label"] == s["cost_label"]
    assert "AI cost" in reports.render_html(n, "narrative", "en") and "maliyet" in reports.render_text(n, "x", "tr", "Daily")
