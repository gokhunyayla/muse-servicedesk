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


def test_dashboard_page_and_stats():
    set_llm(ScriptedLLM([Reply(tool_calls=[ToolCall("c1", "close_ticket", {"resolution": "ok"})]), Reply(text="kapandı")]))
    c = TestClient(app)
    c.post("/messages", json={"requester": "+905551110001", "text": "vpn bağlanmıyor"})
    s = c.get("/stats").json()
    assert s["total"] == 1 and s["auto_resolution_rate"] == 1.0 and s["top_issues"][0][0] == "vpn / ağ"
    assert "Muse ServiceDesk" in c.get("/").text
