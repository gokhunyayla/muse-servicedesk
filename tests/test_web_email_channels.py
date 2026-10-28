"""Two more employee channels next to WhatsApp: the public web chat and e-mail.
All of them land on the same tickets; replies follow the channel the employee last used."""
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from app import channels, vault
from app.db import Outbound, Ticket, init_db, session
from app.llm import Reply, ScriptedLLM, set_llm


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setattr(vault, "KEY_FILE", tmp_path / "k")
    from app import security
    security._hits.clear()


def _employee() -> TestClient:
    from app.main import app
    c = TestClient(app)
    assert "Start chat" in c.get("/portal").text
    r = c.post("/portal/start", data={"name": "Ayşe Demir", "phone": "0555 111 00 01"}, follow_redirects=False)
    assert r.status_code == 303 and "muse_portal" in c.cookies
    return c


def test_web_chat_opens_a_ticket_and_receives_desk_replies(desk):
    emp = _employee()
    assert "Signed in as <b>Ayşe Demir</b>" in emp.get("/portal").text
    set_llm(ScriptedLLM([Reply(text="VPN için önce ağınızı kontrol edelim.")]))
    r = emp.post("/portal/say", data={"text": "vpn bağlanmıyor"}).json()
    assert r["text"].startswith("VPN") and r["ticket"] == 1
    with session() as db:
        t = db.get(Ticket, 1)
        assert t.requester == "+905551110001" and t.channel == "web"
    # a person on the desk replies from the console: queued, then picked up by the web chat, never sent to WhatsApp
    desk.post("/tickets/1/reply", json={"text": "Merhaba, bakıyorum."})
    assert channels.drain() == 0
    upd = emp.get("/portal/updates").json()
    assert [m["text"] for m in upd] == ["Merhaba, bakıyorum."] and upd[0]["who"] == "desk"
    assert emp.get("/portal/updates").json() == []            # delivered once
    hist = emp.get("/portal/history").json()
    assert [h["who"] for h in hist] == ["you", "desk", "desk"]
    # the same employee continues over WhatsApp: the ticket follows, replies switch channel
    set_llm(ScriptedLLM([Reply(text="Tamam.")]))
    desk.post("/messages", json={"requester": "+905551110001", "text": "hâlâ aynı"})
    with session() as db:
        assert db.get(Ticket, 1).channel == "whatsapp" and db.query(Ticket).count() == 1


def test_web_chat_can_be_switched_off_and_needs_a_valid_phone(desk):
    from app.main import app
    c = TestClient(app)
    assert c.post("/portal/start", data={"name": "X", "phone": "abc"}, follow_redirects=False).headers["location"].endswith("error=phone")
    r = desk.post("/settings/portal", data={"title": "Help me", "note": "Internal use"}, follow_redirects=False)   # enabled unchecked
    assert r.status_code == 303
    assert "switched off" in c.get("/portal").text
    assert c.get("/portal/history").status_code == 401
    assert "Web chat" in desk.get("/settings?tab=channels").text and "E-mail" in desk.get("/settings?tab=channels").text


def _mail(frm="mehmet.kaya@example.com", subject="Yazıcı çalışmıyor", body="3. kat yazıcı kağıt sıkıştırıyor.\n\n> eski alıntı\nOn Mon, X wrote:\n> bla"):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = f"Mehmet Kaya <{frm}>", "desk@example.com", subject
    m.set_content(body)
    return m


def test_email_parsing_drops_quotes_and_auto_replies():
    p = channels.parse_email(_mail().as_bytes())
    assert p["from"] == "mehmet.kaya@example.com" and p["text"] == "3. kat yazıcı kağıt sıkıştırıyor." and p["ticket_id"] is None
    assert channels.parse_email(_mail(frm="noreply@example.com").as_bytes()) is None
    m = _mail(subject="Re: [#7] Yazıcı")
    m["Auto-Submitted"] = "auto-replied"
    assert channels.parse_email(m.as_bytes()) is None
    assert channels.parse_email(_mail(subject="Re: [#7] Yazıcı").as_bytes())["ticket_id"] == 7


def test_email_opens_a_ticket_and_answers_by_mail(desk, monkeypatch):
    sent = []
    monkeypatch.setattr(channels, "send_email", lambda to, subject, text: (sent.append((to, subject, text)) or (True, "")))
    set_llm(ScriptedLLM([Reply(text="Kağıt sıkışmasını açmak için tepsiyi çıkarın.")]))
    t, answer = channels.ingest_email(channels.parse_email(_mail().as_bytes()))
    assert t.channel == "email" and t.requester == "mehmet.kaya@example.com"
    assert sent[0][0] == "mehmet.kaya@example.com" and sent[0][1].startswith(f"[#{t.id}]") and "tepsiyi" in sent[0][2]
    # later desk messages to this requester go out by mail through the normal queue
    desk.post(f"/tickets/{t.id}/reply", json={"text": "Teknisyen yolda."})
    assert channels.drain() == 1 and sent[-1][2] == "Teknisyen yolda." and sent[-1][1].startswith(f"[#{t.id}]")
    with session() as db:
        assert db.query(Outbound).filter(Outbound.sent.is_(True)).count() == 1


def test_each_new_mail_is_its_own_ticket_and_tagged_replies_continue(desk, monkeypatch):
    sent = []
    monkeypatch.setattr(channels, "send_email", lambda to, subject, text: (sent.append((to, subject, text)) or (True, "")))
    set_llm(ScriptedLLM([Reply(text="Yazıcıya bakıyorum."), Reply(text="Bilgisayar için güç düğmesine basınca ne oluyor?"),
                         Reply(text="Tamam, yazıcı için teknisyen yolda.")]))
    t1, _ = channels.ingest_email(channels.parse_email(_mail().as_bytes()))
    t2, _ = channels.ingest_email(channels.parse_email(_mail(subject="Bilgisayarım açılmıyor", body="Güç gelmiyor.").as_bytes()))
    assert t2.id != t1.id and t2.title.startswith("Bilgisayarım")           # an untagged mail never lands on the open ticket
    t3, _ = channels.ingest_email(channels.parse_email(_mail(subject=f"Re: [#{t1.id}] Yazıcı çalışmıyor", body="Hâlâ sıkışıyor.").as_bytes()))
    assert t3.id == t1.id and sent[-1][1] == f"[#{t1.id}] Re: Yazıcı çalışmıyor"
    with session() as db:
        assert db.query(Ticket).count() == 2


def test_email_settings_saved_and_password_in_vault(desk):
    r = desk.post("/settings/email", data={"enabled": "1", "imap_host": "imap.example.com", "imap_port": "993", "user": "desk@example.com",
                                           "folder": "INBOX", "poll_minutes": "5"}, follow_redirects=False)
    assert r.status_code == 303 and channels.email_config()["imap_host"] == "imap.example.com" and channels.email_config()["enabled"]
    desk.post("/settings/secrets?tab=channels", data={"IMAP_PASSWORD": "app-pass"}, follow_redirects=False)
    assert vault.get("IMAP_PASSWORD") == "app-pass" and "app-pass" not in desk.get("/settings?tab=channels").text
    assert channels.poll_email(force=True) == 0        # unreachable host: logged, never raises


def test_sender_allow_list_and_folded_subjects():
    cfg = {"allowed": "company.com, partner@other.com"}
    assert channels.sender_allowed("ali@company.com", cfg) and channels.sender_allowed("x@mail.company.com", cfg)
    assert channels.sender_allowed("partner@other.com", cfg) and not channels.sender_allowed("someone@other.com", cfg)
    assert channels.sender_allowed("anyone@anywhere.com", {"allowed": ""})
    m = _mail(subject="Re: Aktif Olmayan Make Flow'ları ve Aktif'lerin Sırayla n8n'e Taşınması, uzun bir konu satırı daha")
    assert "\n" not in channels.parse_email(m.as_bytes())["subject"]      # folded header lines are joined again
    from app.db import Ticket
    assert channels._subject(Ticket(id=3, title="a\nb   c")) == "[#3] Re: a b c"
    assert channels._subject(Ticket(id=3, title="x"), "Re: [#3] Yazıcı") == "[#3] Re: Yazıcı"


def test_greetings_and_small_talk_open_no_ticket(desk):
    from app.agents import handle_message, is_greeting
    assert is_greeting("Selam") and is_greeting("merhaba, orada mısın?") and is_greeting("hello there") and not is_greeting("selam, vpn bağlanmıyor")
    set_llm(ScriptedLLM([]))                                                   # a greeting needs no model at all
    with session() as db:
        t, answer = handle_message(db, "+905551110001", "Selam")
        assert t is None and answer.startswith("Merhaba!") and db.query(Ticket).count() == 0
    set_llm(ScriptedLLM([Reply(text="CHAT")]))                                 # the model says it is small talk
    with session() as db:
        t, answer = handle_message(db, "+905551110001", "bugün hava çok güzel değil mi")
        assert t is None and db.query(Ticket).count() == 0
    set_llm(ScriptedLLM([Reply(text="REQUEST"), Reply(text="Bakıyorum.")]))
    with session() as db:
        t, _ = handle_message(db, "+905551110001", "yazıcı bozuk")
        assert t is not None and t.title == "yazıcı bozuk"
    emp = _employee()                                                          # Ayşe has an open ticket: greeting points at it, opens nothing
    r = emp.post("/portal/say", data={"text": "merhaba"}).json()
    assert r["ticket"] == t.id and "hâlâ açık" in r["text"] and "clear chat" in emp.get("/portal").text
    with session() as db:
        assert db.query(Ticket).count() == 1
    assert desk.post("/messages", json={"requester": "+905551110002", "text": "günaydın"}).json()["ticket_id"] is None


def test_stale_and_duplicate_whatsapp_messages_are_skipped(desk, monkeypatch):
    import time
    from app import whatsapp
    monkeypatch.setattr(whatsapp, "try_send", lambda to, text: True)
    set_llm(ScriptedLLM([Reply(text="Bakıyorum.")]))
    def payload(mid, ts, text):
        return {"entry": [{"changes": [{"value": {"messages": [{"id": mid, "from": "905551110001", "timestamp": str(ts), "type": "text", "text": {"body": text}}]}}]}]}
    old = desk.post("/webhook", json=payload("wamid.old", int(time.time()) - 2 * 3600, "Selam")).json()
    fresh = desk.post("/webhook", json=payload("wamid.1", int(time.time()), "yazıcı bozuk")).json()
    again = desk.post("/webhook", json=payload("wamid.1", int(time.time()), "yazıcı bozuk")).json()
    assert old["handled"] == 0 and fresh["handled"] == 1 and again["handled"] == 0
    with session() as db:
        assert db.query(Ticket).count() == 1
