import copy

import pytest

from app import systems
from app.agents import handle_message
from app.db import Outbound, Ticket, init_db, session
from app.llm import Reply, ScriptedLLM, ToolCall, set_llm
from app.proactive import findings, run_proactive_checks


@pytest.fixture(autouse=True)
def fresh_state(tmp_path):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    devices = copy.deepcopy(systems.DEVICES)
    yield
    systems.DEVICES.clear()
    systems.DEVICES.update(devices)


def call(_name, **args):
    return ToolCall(id=f"c-{_name}", name=_name, arguments=args)


def test_mass_outage_is_merged_into_one_incident_and_closed_together():
    set_llm(ScriptedLLM([Reply(text="Bakıyorum.")] * 2))
    with session() as db:
        handle_message(db, "+905551110001", "e-posta açılmıyor, Outlook bağlanamıyor")
        handle_message(db, "+905551110002", "Outlook e-posta sunucusuna bağlanamıyor diyor")
    set_llm(ScriptedLLM([
        Reply(tool_calls=[call("find_similar_tickets", query="Outlook e-posta sunucu bağlanamıyor")]),
        Reply(tool_calls=[call("link_tickets", ticket_ids=[1, 2], incident_title="Exchange bağlantı kesintisi")]),
        Reply(tool_calls=[call("broadcast_incident_update", text="E-posta kesintisi üzerinde çalışıyoruz.")]),
        Reply(text="Kesinti kaydı açıldı, herkese bilgi verildi."),
    ]))
    with session() as db:
        t, _ = handle_message(db, "+905551110003", "Outlook e-posta sunucusu hata veriyor")
        res = dict((e.content, e.data) for e in t.events if e.kind == "tool_result")
        assert res["find_similar_tickets"]["count"] == 2
        assert res["link_tickets"]["linked"] == [1, 2]
        assert res["broadcast_incident_update"]["recipients"] == 3
        assert db.query(Outbound).count() == 3
        assert t.title.startswith("[INCIDENT]")
        tid = t.id
    set_llm(ScriptedLLM([Reply(tool_calls=[call("close_ticket", resolution="Exchange düzeldi")]), Reply(text="Kapandı.")]))
    with session() as db:
        t, _ = handle_message(db, "+905551110003", "düzeldi")
        assert dict((e.content, e.data) for e in t.events if e.kind == "tool_result")["close_ticket"]["linked_closed"] == 2
        assert {x.status for x in db.query(Ticket).all()} == {"resolved"}


def test_proactive_checks_find_disk_patch_and_open_tickets_once():
    kinds = {(f["hostname"], f["kind"]) for f in findings()}
    assert ("TR-LT-102", "disk") in kinds and ("TR-LT-103", "patch") in kinds
    set_llm(ScriptedLLM([Reply(text="Bilgilendirdim.")] * 10))
    first = run_proactive_checks()
    assert len(first) == len(kinds)
    assert run_proactive_checks() == []          # no duplicates while tickets are open
    with session() as db:
        titles = [t.title for t in db.query(Ticket).all()]
    assert any(t.startswith("[Proactive:disk] TR-LT-102") for t in titles)


def test_history_is_replayed_to_the_model_on_follow_up_messages():
    llm = ScriptedLLM([Reply(text="İlk cevap"), Reply(text="İkinci cevap")])
    set_llm(llm)
    with session() as db:
        handle_message(db, "+905551110001", "ilk mesaj")
        handle_message(db, "+905551110001", "ikinci mesaj")
    roles = [(m["role"], m["content"]) for m in llm.requests[1][1:]]
    assert roles == [("user", "ilk mesaj"), ("assistant", "İlk cevap"), ("user", "ikinci mesaj")]


def test_unrelated_message_opens_a_separate_ticket_when_the_model_says_new():
    set_llm(ScriptedLLM([Reply(text="Bakıyorum.")]))
    with session() as db:
        first, _ = handle_message(db, "+905551110001", "VPN bağlanmıyor, evden çalışamıyorum")
        fid = first.id
    set_llm(ScriptedLLM([Reply(text="NEW"), Reply(text="Yazıcıya bakıyorum.")]))
    with session() as db:
        second, _ = handle_message(db, "+905551110001", "Bir de yazıcı hiç çıktı vermiyor, kuyrukta kalıyor")
        assert second.id != fid and second.title.startswith("Bir de yazıcı")
        assert db.get(Ticket, fid).status == "open"
    set_llm(ScriptedLLM([Reply(text="SAME"), Reply(text="Tamam.")]))
    with session() as db:
        third, _ = handle_message(db, "+905551110001", "yazıcı hâlâ aynı, kuyruk temizlenmedi sanırım")
        assert third.id == second.id


def test_short_replies_never_trigger_classification():
    llm = ScriptedLLM([Reply(text="?"), Reply(text="kapandı")])
    set_llm(llm)
    with session() as db:
        t, _ = handle_message(db, "+905551110001", "vpn yok")
        handle_message(db, "+905551110001", "evet çözüldü")
    assert len(llm.requests) == 2          # no extra SAME/NEW call for a 2-word message
