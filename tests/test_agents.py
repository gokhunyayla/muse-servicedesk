import copy

import pytest

from app import systems
from app.agents import AGENTS, handle_message
from app.db import init_db, session
from app.llm import Reply, ScriptedLLM, ToolCall, set_llm


@pytest.fixture(autouse=True)
def fresh_state(tmp_path):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    devices = copy.deepcopy(systems.DEVICES)
    yield
    systems.DEVICES.clear()
    systems.DEVICES.update(devices)


def call(name, **args):
    return ToolCall(id=f"c-{name}", name=name, arguments=args)


def test_l1_fixes_expired_vpn_profile():
    set_llm(ScriptedLLM([
        Reply(tool_calls=[call("vpn_status", hostname="LT-101")]),
        Reply(tool_calls=[call("renew_vpn_profile", hostname="LT-101")]),
        Reply(text="VPN profiliniz yenilendi, tekrar bağlanmayı dener misiniz?"),
    ]))
    with session() as db:
        ticket, answer = handle_message(db, "+15551110001", "VPN bağlanmıyor")
        kinds = [(e.kind, e.content) for e in ticket.events]
    assert "yenilendi" in answer
    assert ("tool_call", "renew_vpn_profile") in kinds
    assert systems.DEVICES["LT-101"]["vpn_profile"] == "valid"


def test_password_reset_blocked_until_identity_verified():
    set_llm(ScriptedLLM([
        Reply(tool_calls=[call("reset_password", employee_no="E1001")]),
        Reply(text="Önce kimliğinizi doğrulamam gerekiyor."),
    ]))
    with session() as db:
        ticket, _ = handle_message(db, "+15551110001", "şifremi unuttum")
        results = [e.data for e in ticket.events if e.kind == "tool_result"]
    assert results[0]["ok"] is False


def test_tier_cannot_use_tool_outside_its_authority():
    # network_diagnostics belongs to L2 only
    set_llm(ScriptedLLM([
        Reply(tool_calls=[call("network_diagnostics", hostname="LT-102")]),
        Reply(text="Bunu L2'ye iletiyorum."),
    ]))
    with session() as db:
        ticket, _ = handle_message(db, "+15551110002", "internet yavaş")
        result = [e.data for e in ticket.events if e.kind == "tool_result"][0]
    assert result["ok"] is False and "not allowed" in result["error"]


def test_handoff_runs_target_agent_and_returns_its_result():
    set_llm(ScriptedLLM([
        Reply(tool_calls=[call("handoff_L2", summary="LT-102 internete çıkamıyor")]),   # L1
        Reply(tool_calls=[call("network_diagnostics", hostname="LT-102")]),          # L2
        Reply(text="DNS hatalı."),                                                      # L2 done
        Reply(text="L2 DNS sorununu buldu, düzeltiyoruz."),                              # L1 done
    ]))
    with session() as db:
        ticket, answer = handle_message(db, "+15551110002", "internet yok")
        actors = [(e.actor, e.kind, e.content) for e in ticket.events]
    assert ("L2", "tool_call", "network_diagnostics") in actors
    assert any(k == "handoff" for _, k, _ in actors)
    assert "DNS" in answer


def test_every_agent_can_hand_off_to_every_other():
    for tier, agent in AGENTS.items():
        names = {s["function"]["name"] for s in agent.tool_specs()}
        assert {f"handoff_{p}" for p in AGENTS if p != tier} <= names
