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


def test_login_is_rate_limited_and_audited(desk, monkeypatch):
    from app import security
    from app.db import Audit, session
    security._hits.clear()
    anon = TestClient(app)
    for _ in range(10):
        anon.post("/login", data={"email": "admin@test", "password": "nope"}, follow_redirects=False)
    assert anon.post("/login", data={"email": "admin@test", "password": "nope"}, follow_redirects=False).status_code == 429
    security._hits.clear()
    with session() as db:
        actions = [a.action for a in db.query(Audit).order_by(Audit.id)]
    assert actions[0] == "login.ok" and actions.count("login.failed") == 10
    # cross-site form posts are refused
    r = desk.post("/users", data={"name": "Evil", "email": "e@test", "role": "admin"},
                  headers={"origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403


def test_machine_endpoints_need_the_api_key_when_one_is_set(monkeypatch):
    from app import security
    monkeypatch.setattr(security, "API_KEY", "k-123")
    c = TestClient(app)
    assert c.post("/messages", json={"requester": "+905551110001", "text": "x"}).status_code == 401
    set_llm(ScriptedLLM([Reply(text="ok")]))
    assert c.post("/messages", json={"requester": "+905551110001", "text": "x"}, headers={"X-API-Key": "k-123"}).status_code == 200
    assert c.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "muse-servicedesk", "hub.challenge": "1"}).status_code == 200


def test_profile_name_and_password_change(desk):
    r = desk.post("/profile", data={"name": "Desk Boss"}, follow_redirects=False)
    assert r.status_code == 303 and desk.get("/me").json()["name"] == "Desk Boss"
    bad = desk.post("/profile/password", data={"current": "wrong", "password": "brand-new-pw1", "password2": "brand-new-pw1"}, follow_redirects=False)
    assert "error=Current" in bad.headers["location"]
    ok = desk.post("/profile/password", data={"current": "secret-pass-1", "password": "brand-new-pw1", "password2": "brand-new-pw1"}, follow_redirects=False)
    assert "info=Password" in ok.headers["location"] and desk.get("/me").status_code == 200   # own session kept
    again = TestClient(app)
    again.post("/login", data={"email": "admin@test", "password": "brand-new-pw1"}, follow_redirects=False)
    assert again.get("/me").json()["name"] == "Desk Boss"
    assert desk.get("/audit.json").json()[0]["action"] == "login.ok"
    assert "Profile" in desk.get("/profile").text and "Audit log" in desk.get("/audit").text


def test_desk_can_reply_and_change_status(desk):
    set_llm(ScriptedLLM([Reply(text="Bakıyorum.")]))
    desk.post("/messages", json={"requester": "+905551110001", "text": "projektör açılmıyor"})
    r = desk.post("/tickets/1/reply", json={"text": "Teknisyen 14:00'te gelecek."}).json()
    assert r == {"id": 1, "sent": True}
    with session() as db:
        assert db.query(Outbound).filter_by(ticket_id=1).one().text == "Teknisyen 14:00'te gelecek."
    t = desk.get("/tickets/1").json()
    assert t["events"][-1]["actor"] == "desk:Desk Admin" and t["events"][-1]["kind"] == "message"
    assert desk.post("/tickets/1/status", json={"status": "escalated"}).json()["owner"] == "human"
    assert desk.post("/tickets/1/status", json={"status": "resolved", "note": "yerinde çözüldü"}).json()["status"] == "resolved"
    assert desk.get("/tickets/1").json()["resolution"] == "yerinde çözüldü"
    assert desk.post("/tickets/1/status", json={"status": "weird"}).status_code == 422


def test_timing_and_sla_settings(desk):
    from datetime import timedelta
    from app.db import Event, Ticket, session
    set_llm(ScriptedLLM([Reply(tool_calls=[ToolCall("c1", "close_ticket", {"resolution": "ok"})]), Reply(text="kapandı")]))
    desk.post("/messages", json={"requester": "+905551110001", "text": "vpn bağlanmıyor"})
    with session() as db:      # stretch the trail: created 10 min before the first reply, resolved 20 min in
        t = db.get(Ticket, 1)
        t.created_at = t.created_at - timedelta(minutes=10)
        t.resolved_at = t.created_at + timedelta(minutes=20)
        for e in db.query(Event).filter_by(ticket_id=1):
            e.at = t.created_at + timedelta(minutes=10)
    tm = desk.get("/tickets/1").json()["timing"]
    assert tm["resolved"] and tm["total_seconds"] == 1200 and tm["first_response_seconds"] == 600
    assert tm["first_response_breached"] is True            # default target is 5 min
    assert tm["tiers"][0]["tier"] == "L1" and tm["tiers"][0]["seconds"] == 1200
    # admin raises the SLA; the same ticket is now within target
    r = desk.post("/settings/sla", data={"first_response": "15", "L1": "60", "total": "60"}, follow_redirects=False)
    assert r.status_code == 303
    tm = desk.get("/tickets/1").json()["timing"]
    assert tm["first_response_breached"] is False and tm["first_response_target_minutes"] == 15
    assert "SLA" in desk.get("/settings").text


def test_secrets_are_managed_in_the_console_and_encrypted(desk, tmp_path, monkeypatch):
    from app import vault, systems, whatsapp
    from app.db import Setting, session
    monkeypatch.setattr(vault, "KEY_FILE", tmp_path / "k")
    monkeypatch.delenv("WA_ACCESS_TOKEN", raising=False)
    assert vault.get("WA_ACCESS_TOKEN") == ""
    r = desk.post("/settings/secrets?tab=channels", data={"WA_ACCESS_TOKEN": "EAAtoken-xyz", "WA_PHONE_NUMBER_ID": "123",
                                                          "DEMO_MANAGER_PHONE": "+905000000000"}, follow_redirects=False)
    assert r.status_code == 303
    assert vault.get("WA_ACCESS_TOKEN") == "EAAtoken-xyz" and whatsapp._cfg()[1] == "123"
    assert systems.USERS["E1002"]["phone"] == "+905000000000"
    with session() as db:                                   # encrypted at rest
        raw = db.get(Setting, "secrets").value["WA_ACCESS_TOKEN"]
    assert "EAAtoken" not in raw
    page = desk.get("/settings?tab=channels").text
    assert "EAAtoken" not in page and "…-xyz" in page and "configured" in page
    desk.post("/settings/secrets?tab=channels", data={"clear_DEMO_MANAGER_PHONE": "1"}, follow_redirects=False)
    assert systems.USERS["E1002"]["phone"] == "+905551110002"


def test_knowledge_base_search_across_pools_and_human_editing(desk):
    from app import knowledge
    # seeds loaded; L2 can reach an L1 SOP (marked) and the shared pool is seen by everyone
    hits = knowledge.search("L2", "yazıcı kuyruğu takıldı")
    assert hits and hits[0]["id"].startswith("L1/") and hits[0]["from_other_tier"] is True and hits[0]["content"]
    r = desk.post("/sops", json={"tier": "shared", "title": "Guest Wi-Fi password", "content": "Ask reception; rotates monthly."}).json()
    assert r["status"] == "published"
    assert knowledge.search("L4", "guest wifi password")[0]["tier"] == "shared"
    assert "Guest Wi-Fi password (shared)" in knowledge.titles("L3")
    # edit keeps a version; retire hides it from agents; audit records it
    assert desk.put(f"/sops/{r['key']}", json={"title": "Guest Wi-Fi password", "content": "Ask reception; rotates weekly."}).json()["ok"]
    assert knowledge.get(r["key"]).version == 2
    assert desk.post(f"/sops/{r['key']}/status", json={"status": "retired"}).json()["status"] == "retired"
    assert all(h["id"] != r["key"] for h in knowledge.search("L4", "guest wifi password"))
    assert any(a["action"] == "sop.retired" for a in desk.get("/audit.json").json())
    assert "Knowledge base" in desk.get("/knowledge").text


def test_agent_written_sops_are_drafts_until_an_admin_publishes(desk):
    from app import knowledge
    set_llm(ScriptedLLM([Reply(tool_calls=[ToolCall("c1", "write_sop", {"title": "Projector HDMI handshake", "content": "Power-cycle the switcher."})]),
                         Reply(text="Yazdım.")]))
    desk.post("/messages", json={"requester": "+905551110001", "text": "projektör görüntü vermiyor"})
    drafts = [s for s in knowledge.listing("draft") if s["source"] == "agent"]
    assert drafts and drafts[0]["author"] == "L1" and drafts[0]["ticket_ids"] == [1]
    assert knowledge.search("L1", "projector hdmi") == []                      # not visible until published
    desk.post(f"/sops/{drafts[0]['key']}/status", json={"status": "published"})
    assert knowledge.search("L1", "projector hdmi")[0]["title"] == "Projector HDMI handshake"


def test_search_result_tells_the_agent_when_nothing_matches():
    from app.tools import Ctx, run_tool
    with session() as db:
        out = run_tool(Ctx(db=db, ticket=None, agent="L1"), "search_knowledge", {"query": "quantum flux capacitor"})
    assert out["matched"] is False and "No SOP covers this" in out["note"]
