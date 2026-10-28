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


def test_closed_proactive_finding_is_not_reopened_within_the_cooldown():
    from app.proactive import run_proactive_checks
    set_llm(ScriptedLLM([Reply(text="ok")] * 6))
    first = run_proactive_checks()
    assert first
    with session() as db:
        for tid in first:
            db.get(Ticket, tid).status = "resolved"
    assert run_proactive_checks() == []                    # closed yesterday-ish: no nagging on the next run / restart


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
    llm = ScriptedLLM([Reply(text="İlk cevap"), Reply(text="SAME"), Reply(text="İkinci cevap")])   # 2nd reply: the router's verdict
    set_llm(llm)
    with session() as db:
        handle_message(db, "+905551110001", "ilk mesaj")
        handle_message(db, "+905551110001", "ikinci mesaj")
    roles = [(m["role"], m["content"]) for m in llm.requests[1][1:]]
    assert roles == [("user", "ilk mesaj"), ("assistant", "İlk cevap"), ("user", "ikinci mesaj")]


def test_vague_message_makes_the_desk_ask_instead_of_guessing():
    set_llm(ScriptedLLM([Reply(text="Bakıyorum.")]))
    with session() as db:
        first, _ = handle_message(db, "+905551110001", "VPN bağlanmıyor, evden çalışamıyorum")
        fid = first.id
    set_llm(ScriptedLLM([Reply(text="UNCLEAR")]))
    with session() as db:
        t, answer = handle_message(db, "+905551110001", "yine garip bir şey oldu")
        assert t.id == fid and f"#{fid}" in answer and "yeni bir konu mu" in answer
        assert db.query(Ticket).count() == 1
    # a short acknowledgement after a close is the closing exchange; a 2-word problem is not
    from app.agents import is_ack
    assert is_ack("evet çözüldü teşekkürler") and is_ack("#2 close") and is_ack("5") and is_ack("yes solved")
    assert not is_ack("yazıcım çalışmıyor") and not is_ack("garip bir hata")


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


def test_short_reply_goes_to_the_ticket_just_discussed_and_asks_only_when_nothing_is_recent():
    from datetime import timedelta
    set_llm(ScriptedLLM([Reply(text="Bakıyorum."), Reply(text="NEW"), Reply(text="Yazıcıya bakıyorum."), Reply(text="Tamam, kapatıyorum.")]))
    with session() as db:
        a, _ = handle_message(db, "+905551110001", "VPN bağlanmıyor, evden çalışamıyorum")
        b, _ = handle_message(db, "+905551110001", "Bir de yazıcı hiç çıktı vermiyor, kuyrukta kalıyor")
        t, answer = handle_message(db, "+905551110001", "evet çözüldü")          # no number: the printer, the one just discussed
        assert t.id == b.id and "hangisi" not in answer
    set_llm(ScriptedLLM([Reply(text="ok")]))
    with session() as db:
        for tk in db.query(Ticket).all():                                        # hours later: nothing is "just discussed"
            for e in tk.events:
                e.at = e.at - timedelta(hours=3)
            tk.status = "open"
        t, answer = handle_message(db, "+905551110001", "evet çözüldü")
        assert "hangisi" in answer


def test_short_reply_with_two_open_tickets_asks_which_one_then_routes_by_number():
    set_llm(ScriptedLLM([Reply(text="Bakıyorum.")]))
    with session() as db:
        a, _ = handle_message(db, "+905551110001", "VPN bağlanmıyor, evden çalışamıyorum")
    set_llm(ScriptedLLM([Reply(text="NEW"), Reply(text="Yazıcıya bakıyorum.")]))
    with session() as db:
        b, _ = handle_message(db, "+905551110001", "Bir de yazıcı hiç çıktı vermiyor, kuyrukta kalıyor")
        aid, bid = a.id, b.id
    set_llm(ScriptedLLM([Reply(tool_calls=[call("close_ticket", resolution="printer ok")]), Reply(text="Kapandı.")]))
    with session() as db:
        t, answer = handle_message(db, "+905551110001", "evet çözüldü")        # the printer: the ticket just discussed
        assert t.id == bid and "hangisi" not in answer
    set_llm(ScriptedLLM([Reply(tool_calls=[call("close_ticket", resolution="VPN ok")]), Reply(text="Kapandı.")]))
    with session() as db:
        t, _ = handle_message(db, "+905551110001", f"#{aid} çözüldü")            # a number still picks the ticket explicitly
        assert t.id == aid and t.status == "resolved"
        assert db.get(Ticket, bid).status == "resolved"                            # closed by the earlier "evet çözüldü"


def test_burst_of_similar_reports_is_linked_at_intake_and_the_agent_is_briefed():
    llm = ScriptedLLM([Reply(text="Bakıyorum.")] * 3)
    set_llm(llm)
    with session() as db:
        a, _ = handle_message(db, "+905551110001", "Outlook e-posta açılmıyor, sunucuya bağlanamıyor")
        b, _ = handle_message(db, "+905551110002", "Outlook e-posta sunucusu hata veriyor")
        assert a.incident_id is None and b.incident_id is None                      # two is not a pattern yet
        c, _ = handle_message(db, "+905551110003", "e-posta Outlook bağlanmıyor")
        assert c.incident_id == a.id and db.get(Ticket, b.id).incident_id == a.id   # third report: linked under the first
        notes = [e.content for e in db.get(Ticket, a.id).events if e.kind == "note"]
        assert any("possible incident" in n for n in notes)
        assert "Desk notice" not in [e.content for e in c.events if e.kind == "message"][0]   # the employee's own words stay clean
    assert "Desk notice" in llm.requests[-1][-1]["content"] and "others are affected" in llm.requests[-1][-1]["content"]


def test_channel_switch_continues_silently_and_asks_only_when_unclear():
    from app import agents
    agents._RESUME.clear()
    set_llm(ScriptedLLM([Reply(text="VPN profilini yeniledim, dener misiniz?")]))
    with session() as db:
        t, _ = handle_message(db, "+905551110001", "VPN bağlanmıyor", channel="whatsapp")
        tid = t.id
    # clearly the same problem, now from the web: no question, the ticket just follows the employee
    set_llm(ScriptedLLM([Reply(text="SAME"), Reply(text="Bakıyorum.")]))
    with session() as db:
        t, answer = handle_message(db, "+905551110001", "hâlâ bağlanmıyor, ekran görüntüsünü ekledim", channel="web")
        assert t.id == tid and t.channel == "web" and answer == "Bakıyorum." and db.query(Ticket).count() == 1
    # clearly a new problem on another channel: opened at once, no question
    set_llm(ScriptedLLM([Reply(text="NEW"), Reply(text="Yazıcıya bakıyorum.")]))
    with session() as db:
        t2, answer = handle_message(db, "+905551110001", "yazıcı da bozuldu", channel="whatsapp")
        assert t2.id != tid and t2.title == "yazıcı da bozuldu" and "Yazıcıya" in answer
    # unclear: the desk asks, and "evet" lands the held message on the ticket
    agents._RESUME.clear()
    set_llm(ScriptedLLM([Reply(text="UNCLEAR"), Reply(text="Bakıyorum.")]))
    with session() as db:
        db.get(Ticket, t2.id).status = "resolved"
        db.get(Ticket, tid).channel = "web"                     # the VPN conversation was last on the web
        held, answer = handle_message(db, "+905551110001", "yine olmadı", channel="whatsapp")
        assert held.id == tid and f"#{tid}" in answer and "yeni bir konu mu" in answer       # a clarifying question, nothing opened
        t, answer = handle_message(db, "+905551110001", "evet", channel="whatsapp")
        assert t.id == tid and t.channel == "whatsapp" and answer == "Bakıyorum."
        assert [e.content for e in t.events if e.kind == "message" and e.actor == "user"][-1] == "yine olmadı"
    # a free-text answer to the clarifying question: both sentences travel together into routing
    set_llm(ScriptedLLM([Reply(text="UNCLEAR"), Reply(text="SAME"), Reply(text="Tamam.")]))
    with session() as db:
        db.get(Ticket, tid).channel = "web"
        handle_message(db, "+905551110001", "ekranda garip bir uyarı var", channel="whatsapp")
        t, answer = handle_message(db, "+905551110001", "modemin ekranında, line error yazıyor", channel="whatsapp")
        assert t.id == tid and answer == "Tamam."
        last = [e.content for e in t.events if e.kind == "message" and e.actor == "user"][-1]
        assert last.startswith("ekranda garip bir uyarı var") and "line error" in last
    # a hello on the other channel: one message that greets and offers, never two
    agents._RESUME.clear()
    with session() as db:
        db.get(Ticket, tid).channel = "web"
        held, answer = handle_message(db, "+905551110001", "selam", channel="whatsapp")
        assert held.id == tid and answer.startswith("Merhaba!") and "devam edelim mi" in answer
