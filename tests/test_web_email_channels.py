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
    assert channels._subject(Ticket(id=3, title="a\nb   c")) == "[#3] a b c"
