import copy

import pytest

from app import systems
from app.agents import AGENTS, handle_message
from app.db import init_db, session
from app.llm import Reply, ScriptedLLM, ToolCall, set_llm


@pytest.fixture(autouse=True)
def fresh_state(tmp_path):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    servers = copy.deepcopy(systems.SERVERS)
    yield
    systems.SERVERS.clear()
    systems.SERVERS.update(servers)
    systems._snapshots.clear()


def call(name, **args):
    return ToolCall(id=f"c-{name}", name=name, arguments=args)


def test_inventory_scan_finds_only_vulnerable_php_hosts():
    hosts = {h["hostname"] for h in systems.find_vulnerable("CVE-2026-4411")}
    assert hosts == {"WEB-01", "WEB-02"}   # WEB-03 is on 8.3.6, APP-01 has no php


def test_l3_patches_with_snapshot_verifies_and_uses_heavy_model():
    llm = ScriptedLLM([
        Reply(tool_calls=[call("lookup_advisory", query="php2shell")]),                       # L1
        Reply(tool_calls=[call("handoff_L3", summary="CVE-2026-4411 php2shell: patch all affected")]),
        Reply(tool_calls=[call("find_vulnerable_servers", cve="CVE-2026-4411")]),             # L3
        Reply(tool_calls=[call("add_note", text="plan: php 8.1.2->8.1.30 on WEB-01/02; rollback: snapshot")]),
        Reply(tool_calls=[call("apply_patch", hostname="WEB-01", package="php", version="8.1.30"),
                          call("apply_patch", hostname="WEB-02", package="php", version="8.1.30")]),
        Reply(tool_calls=[call("verify_patch", hostname="WEB-01", cve="CVE-2026-4411"),
                          call("verify_patch", hostname="WEB-02", cve="CVE-2026-4411")]),
        Reply(text="2 sunucu yamalandı ve doğrulandı."),                                      # L3 done
        Reply(text="php2shell açığı kapatıldı: WEB-01 ve WEB-02 güncellendi."),         # L1 done
    ])
    set_llm(llm)
    with session() as db:
        ticket, answer = handle_message(db, "+15551110002", "php2shell açığından etkileniyor muyuz?")
        l3 = [(e.content, e.data) for e in ticket.events if e.kind == "tool_result" and e.actor == "L3"]
    scan = dict(l3)["find_vulnerable_servers"]
    assert scan["count"] == 2
    patches = [d for c, d in l3 if c == "apply_patch"]
    assert all(p["from"] == "8.1.2" and p["to"] == "8.1.30" and p["snapshot_id"] for p in patches)
    verifies = [d for c, d in l3 if c == "verify_patch"]
    assert all(v["still_vulnerable"] is False and v["service_healthy"] for v in verifies)
    assert systems.find_vulnerable("CVE-2026-4411") == []
    assert llm.heavy_calls == 5          # every L3 turn used the heavy model, L1 turns did not
    assert "php2shell" in answer


def test_rollback_restores_the_snapshot():
    set_llm(ScriptedLLM([
        Reply(tool_calls=[call("handoff_L3", summary="patch WEB-01")]),
        Reply(tool_calls=[call("apply_patch", hostname="WEB-01", package="php", version="8.1.30")]),
        Reply(tool_calls=[call("rollback_patch", snapshot_id="snap-WEB-01-1")]),
        Reply(text="geri alındı"),
        Reply(text="geri alındı"),
    ]))
    with session() as db:
        handle_message(db, "+15551110002", "yama")
    assert systems.SERVERS["WEB-01"]["packages"]["php"] == "8.1.2"


def test_only_l3_can_touch_servers():
    for name in ("find_vulnerable_servers", "apply_patch", "verify_patch", "rollback_patch"):
        for tier in ("L1", "L2", "L4"):
            assert name not in {s["function"]["name"] for s in AGENTS[tier].tool_specs()}


def test_integration_api_is_closed_on_a_public_desk_without_a_key(monkeypatch):
    from fastapi.testclient import TestClient
    from app import security
    from app.main import app
    monkeypatch.setattr(security, "API_KEY", "")
    monkeypatch.setattr(security, "_local_install", lambda: False)        # as on https://desk.example.com
    c = TestClient(app)
    assert c.post("/messages", json={"requester": "+15551110001", "text": "hi"}).status_code == 403
    assert c.post("/proactive/run").status_code == 403
    monkeypatch.setattr(security, "API_KEY", "k-123")
    assert c.post("/proactive/run").status_code == 401
    assert c.post("/proactive/run", headers={"X-API-Key": "k-123"}).status_code == 200
