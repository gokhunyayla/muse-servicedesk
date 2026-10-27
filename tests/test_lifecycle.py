from datetime import timedelta

import pytest

from app import knowledge
from app.agents import AGENTS, handle_message
from app.db import Followup, Outbound, Ticket, _now, init_db, session
from app.llm import Reply, ScriptedLLM, ToolCall, set_llm
from app.scheduler import run_due_followups


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setattr(knowledge, "SOP_DIR", tmp_path / "sops")   # don't let tests write real SOPs
    (tmp_path / "sops" / "L1").mkdir(parents=True)
    (tmp_path / "sops" / "L1" / "001-vpn.md").write_text("# VPN bağlantı sorunu\n\nvpn_status sonra renew_vpn_profile.\n")


def call(name, **args):
    return ToolCall(id=f"c-{name}", name=name, arguments=args)


def test_agent_sees_sop_titles_and_can_read_one():
    llm = ScriptedLLM([Reply(tool_calls=[call("search_knowledge", query="vpn bağlanmıyor")]), Reply(text="ok")])
    set_llm(llm)
    with session() as db:
        ticket, _ = handle_message(db, "+905551110001", "vpn")
        res = [e.data for e in ticket.events if e.kind == "tool_result"][0]
    assert "VPN bağlantı sorunu" in llm.requests[0][0]["content"]
    assert res["results"][0]["title"] == "VPN bağlantı sorunu"


def test_ticket_waits_for_confirmation_then_closes_with_rating():
    set_llm(ScriptedLLM([
        Reply(tool_calls=[call("request_confirmation", summary="VPN profili yenilendi")]),
        Reply(text="Bağlanabildiniz mi?"),
    ]))
    with session() as db:
        ticket, _ = handle_message(db, "+905551110001", "vpn bağlanmıyor")
        assert ticket.status == "waiting_user"
        assert db.query(Followup).filter_by(ticket_id=ticket.id).count() == 1
        tid = ticket.id

    llm = ScriptedLLM([Reply(tool_calls=[call("close_ticket", resolution="VPN profili yenilendi, teyit alındı")]),
                       Reply(text="Sevindim! 1-5 arası puanlar mısınız?")])
    set_llm(llm)
    with session() as db:
        ticket, _ = handle_message(db, "+905551110001", "evet çözüldü teşekkürler")
        assert ticket.id == tid and ticket.status == "resolved"
    assert "asked to confirm" in llm.requests[0][0]["content"]

    # the rating arrives after the ticket is closed: it belongs to the closing exchange, not a new ticket
    llm = ScriptedLLM([Reply(tool_calls=[call("record_rating", rating=5)]), Reply(text="Teşekkürler!")])
    set_llm(llm)
    with session() as db:
        ticket, _ = handle_message(db, "+905551110001", "5")
        assert ticket.id == tid and ticket.rating == 5 and ticket.status == "resolved"
        assert db.query(Ticket).count() == 1
    assert "already resolved" in llm.requests[0][0]["content"]

    # once rated, a new message from the same employee is a new ticket
    set_llm(ScriptedLLM([Reply(text="Bakıyorum.")]))
    with session() as db:
        ticket, _ = handle_message(db, "+905551110001", "yazıcı çalışmıyor")
        assert ticket.id != tid


def test_due_reminder_becomes_outbound_message():
    set_llm(ScriptedLLM([Reply(tool_calls=[call("request_confirmation", summary="x")]), Reply(text="?")]))
    with session() as db:
        ticket, _ = handle_message(db, "+905551110001", "vpn")
        f = db.query(Followup).filter_by(ticket_id=ticket.id).one()
        f.due_at = _now() - timedelta(minutes=1)
    assert run_due_followups() == 1
    with session() as db:
        out = db.query(Outbound).one()
        assert out.to == "+905551110001" and "işe yaradı mı" in out.text
        assert db.query(Followup).one().done is True


def test_scheduled_followup_runs_l1_with_tools():
    set_llm(ScriptedLLM([Reply(tool_calls=[call("schedule_followup", hours=24, note="stabilite kontrolü")]),
                         Reply(text="24 saat sonra kontrol edeceğim.")]))
    with session() as db:
        ticket, _ = handle_message(db, "+905551110002", "yama")
        db.query(Followup).filter_by(ticket_id=ticket.id).one().due_at = _now() - timedelta(minutes=1)
    llm = ScriptedLLM([Reply(text="Stabil, sorun yok.")])
    set_llm(llm)
    run_due_followups()
    assert "[Scheduled follow-up] stabilite kontrolü" in llm.requests[0][-1]["content"]


def test_write_sop_adds_to_pool_and_endpoint_scripts_are_allow_listed():
    set_llm(ScriptedLLM([
        Reply(tool_calls=[call("run_endpoint_script", hostname="TR-LT-101", script="rm-rf")]),
        Reply(tool_calls=[call("run_endpoint_script", hostname="TR-LT-101", script="printer-fix")]),
        Reply(tool_calls=[call("write_sop", title="Yazıcı kuyruğu takılması", content="printer-fix çalıştır.")]),
        Reply(text="done"),
    ]))
    with session() as db:
        ticket, _ = handle_message(db, "+905551110001", "yazıcı")
        results = [e.data for e in ticket.events if e.kind == "tool_result"]
    assert results[0]["ok"] is False and "allow-listed" in results[0]["error"]
    assert results[1]["ok"] is True
    assert knowledge.search("L1", "yazıcı kuyruğu")[0]["title"] == "Yazıcı kuyruğu takılması"


def test_every_tier_has_the_general_tools():
    for tier in AGENTS:
        names = {s["function"]["name"] for s in AGENTS[tier].tool_specs()}
        assert {"search_knowledge", "write_sop", "add_note", "schedule_followup", "notify_user"} <= names


def test_employee_can_close_the_ticket_directly_without_a_model_call():
    llm = ScriptedLLM([Reply(text="Bakıyorum.")])
    set_llm(llm)
    with session() as db:
        t, _ = handle_message(db, "+905551110001", "vpn bağlanmıyor")
        t2, answer = handle_message(db, "+905551110001", "yanlış talep, kapat")
        assert t2.id == t.id and t2.status == "resolved" and "kapatıldı" in answer
        assert t2.resolution == "closed on the employee's request"
    assert len(llm.requests) == 1            # the close was handled without the model
    for text in ("kapat", "İptal", "bileti kapatın.", "cancel"):
        from app.agents import CLOSE_RE
        assert CLOSE_RE.match(text), text
    assert not CLOSE_RE.match("kapatmayın, hâlâ bağlanamıyorum")


def test_unsolvable_request_is_escalated_to_a_human_and_desk_can_close_it():
    from fastapi.testclient import TestClient
    from app.main import app
    set_llm(ScriptedLLM([
        Reply(tool_calls=[call("escalate_to_human", reason="projektör fiziksel arızalı, yerinde müdahale gerek",
                               tried="uzaktan teşhis, sürücü kontrolü")]),
        Reply(text="Talebiniz bir destek mühendisine aktarıldı, sizinle iletişime geçilecek."),
    ]))
    with session() as db:
        t, answer = handle_message(db, "+905551110001", "toplantı odası projektörü hiç açılmıyor, lambası yanmıyor")
        assert t.status == "escalated" and t.owner == "human" and "aktarıldı" in answer
        tid = t.id
    r = TestClient(app).post(f"/tickets/{tid}/close", json={"resolution": "lamba değiştirildi"})
    assert r.json() == {"id": tid, "status": "resolved"}
    with session() as db:
        assert db.get(Ticket, tid).resolution == "lamba değiştirildi"
        out = db.query(Outbound).filter_by(ticket_id=tid).one()
        assert out.to == "+905551110001" and "lamba değiştirildi" in out.text


def test_step_budget_exhaustion_escalates_instead_of_looping():
    set_llm(ScriptedLLM([Reply(tool_calls=[call("search_knowledge", query="x")])] * 12))
    with session() as db:
        t, answer = handle_message(db, "+905551110001", "garip bir hata")
        assert t.status == "escalated" and "aktarıldı" in answer
