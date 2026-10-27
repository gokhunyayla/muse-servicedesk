import pytest
from fastapi.testclient import TestClient

from app import whatsapp
from app.db import Outbound, init_db, session
from app.llm import Reply, ScriptedLLM, ToolCall, set_llm
from app.main import app
from app.voice import set_transcriber


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    sent = []
    monkeypatch.setattr(whatsapp, "send", lambda to, text: sent.append((to, text)) or True)
    monkeypatch.setattr(whatsapp, "download_media", lambda mid: (b"OggS-bytes", "audio/ogg"))
    yield sent


def wa(msg: dict) -> dict:
    return {"entry": [{"changes": [{"value": {"messages": [msg]}}]}]}


def test_webhook_verification():
    c = TestClient(app)
    r = c.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "muse-servicedesk", "hub.challenge": "4242"})
    assert r.status_code == 200 and r.json() == 4242
    assert c.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"}).status_code == 403


def test_text_message_is_answered_on_whatsapp(fresh):
    set_llm(ScriptedLLM([Reply(text="Merhaba, bakıyorum.")]))
    r = TestClient(app).post("/webhook", json=wa({"from": "905551110001", "type": "text", "text": {"body": "vpn yok"}}))
    assert r.json() == {"handled": 1}
    assert fresh == [("+905551110001", "Merhaba, bakıyorum.")]


def test_voice_note_is_transcribed_then_answered(fresh):
    set_transcriber(lambda wav: "şifremi unuttum")
    import app.voice as voice
    voice.to_wav = lambda audio: b"RIFF"          # skip ffmpeg in tests
    set_llm(ScriptedLLM([Reply(text="Kimlik doğrulayalım.")]))
    TestClient(app).post("/webhook", json=wa({"from": "905551110001", "type": "audio", "audio": {"id": "m1"}}))
    assert fresh[-1] == ("+905551110001", "Kimlik doğrulayalım.")


def test_outbound_queue_is_drained_to_whatsapp(fresh):
    with session() as db:
        db.add(Outbound(to="+905551110002", text="Onay talebi #1"))
    assert whatsapp.drain_outbound() == 1
    assert fresh == [("+905551110002", "Onay talebi #1")]
    with session() as db:
        assert db.query(Outbound).one().sent is True


def test_dashboard_page_and_stats(desk):
    set_llm(ScriptedLLM([Reply(tool_calls=[ToolCall("c1", "close_ticket", {"resolution": "ok"})]), Reply(text="kapandı")]))
    c = desk
    c.post("/messages", json={"requester": "+905551110001", "text": "vpn bağlanmıyor"})
    s = c.get("/stats").json()
    assert s["total"] == 1 and s["auto_resolution_rate"] == 1.0 and s["top_issues"][0][0] == "vpn / network"
    assert "Muse ServiceDesk" in c.get("/").text


def test_desk_merges_tickets_into_a_problem_and_assigns_it(fresh, desk):
    set_llm(ScriptedLLM([Reply(text="Bakıyorum.")] * 3))
    c = desk
    for who, text in (("+905551110001", "Outlook açılmıyor"), ("+905551110002", "mail sunucusu hata veriyor"),
                      ("+905551110003", "e-posta gelmiyor")):
        c.post("/messages", json={"requester": who, "text": text})
    r = c.post("/problems", json={"ticket_ids": [1, 2, 3], "title": "Exchange outage",
                                  "update": "Biliyoruz, çalışıyoruz."}).json()
    assert r == {"problem_id": 1, "linked": [2, 3]}
    with session() as db:
        assert db.query(Outbound).count() == 3 and db.query(Outbound).first().text.startswith("[#1]")
    ts = {t["id"]: t for t in c.get("/tickets").json()}
    assert ts[1]["title"] == "[INCIDENT] Exchange outage" and ts[2]["incident_id"] == 1 and ts[3]["incident_id"] == 1
    # assign the problem to L3: the agent takes over with the desk's note
    set_llm(ScriptedLLM([Reply(text="Exchange kuyruğunu temizledim.")]))
    r = c.post("/tickets/1/assign", json={"owner": "L3", "note": "Exchange kuyruğuna bak"}).json()
    assert r["owner"] == "L3" and "temizledim" in r["reply"]
    # and to a human: escalated, no model call
    r = c.post("/tickets/2/assign", json={"owner": "human", "assignee": "Mehmet"}).json()
    assert r == {"id": 2, "status": "escalated", "owner": "human"}
    assert c.post("/problems", json={"ticket_ids": [1], "title": "x"}).status_code == 422


def test_console_requires_sign_in_and_admin_manages_users(desk):
    anon = TestClient(app)
    assert anon.get("/stats").status_code == 401
    assert anon.get("/", headers={"accept": "text/html"}, follow_redirects=False).headers["location"] == "/login"
    assert anon.post("/login", data={"email": "admin@test", "password": "wrong"}, follow_redirects=False).headers["location"].startswith("/login?error")
    # admin adds a support agent; the agent can sign in but cannot manage users
    # admin invites an agent: no password typed; the invite link (no SMTP) comes back to the admin
    r = desk.post("/users", data={"name": "Zeynep", "email": "z@test", "role": "agent"}, follow_redirects=False)
    assert r.status_code == 303 and "&link=" in r.headers["location"]
    token = r.headers["location"].split("token=")[1]
    assert [u["name"] for u in desk.get("/users.json").json()] == ["Desk Admin", "Zeynep"]
    agent = TestClient(app)
    assert agent.post("/login", data={"email": "z@test", "password": "anything"}, follow_redirects=False).headers["location"].startswith("/login?error")   # invited, no password yet
    r = agent.post("/reset", data={"token": token, "password": "agent-pass-1", "password2": "agent-pass-1"}, follow_redirects=False)
    assert r.headers["location"].startswith("/login?info")
    agent.post("/login", data={"email": "z@test", "password": "agent-pass-1"}, follow_redirects=False)
    assert agent.get("/me").json()["role"] == "agent"
    assert agent.get("/users").status_code == 403
    # forgot / reset: token is issued, password changes, old sessions die
    from app.db import User, session
    desk.post("/forgot", data={"email": "z@test"}, follow_redirects=False)
    with session() as db:
        token = db.query(User).filter_by(email="z@test").one().reset_token
    assert token
    r = agent.post("/reset", data={"token": token, "password": "new-pass-123", "password2": "new-pass-123"}, follow_redirects=False)
    assert r.headers["location"].startswith("/login?info")
    assert agent.get("/me").status_code == 401
    again = TestClient(app)
    again.post("/login", data={"email": "z@test", "password": "new-pass-123"}, follow_redirects=False)
    assert again.get("/me").json()["name"] == "Zeynep"


def test_first_run_setup_creates_the_admin_and_roles_can_change(fresh):
    c = TestClient(app)
    assert c.get("/login", follow_redirects=False).headers["location"] == "/setup"
    r = c.post("/setup", data={"name": "Gökhun", "email": "g@test", "password": "first-admin-1", "password2": "first-admin-1"}, follow_redirects=False)
    assert r.status_code == 303 and c.get("/me").json()["role"] == "admin"
    assert c.post("/setup", data={"name": "x", "email": "x@test", "password": "12345678", "password2": "12345678"}).status_code == 403
    c.post("/users", data={"name": "Ali", "email": "a@test", "role": "agent"}, follow_redirects=False)
    uid = [u for u in c.get("/users.json").json() if u["name"] == "Ali"][0]["id"]
    c.post(f"/users/{uid}/role", data={"role": "admin"}, follow_redirects=False)
    assert [u for u in c.get("/users.json").json() if u["name"] == "Ali"][0]["role"] == "admin"
    me = c.get("/me").json()["id"]
    assert c.post(f"/users/{me}/role", data={"role": "agent"}).status_code == 400
