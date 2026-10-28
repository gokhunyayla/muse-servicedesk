import pytest
from fastapi.testclient import TestClient

from app.agents import handle_message
from app.db import init_db, session
from app.llm import Reply, ScriptedLLM, ToolCall, set_llm
from app.main import app
from app.voice import set_transcriber, transcribe


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")


def call(name, **args):
    return ToolCall(id=f"c-{name}", name=name, arguments=args)


def test_voice_note_is_transcribed_then_handled_by_l1(monkeypatch):
    """Password reset over voice: identity, then a code on the OTHER channel (e-mail) and a phrase said in a voice
    note; the reset is refused until both match."""
    from app import channels
    mails = []
    monkeypatch.setattr(channels, "send_email", lambda to, subject, text: (mails.append((to, text)) or (True, "")))
    set_transcriber(lambda wav: "şifremi unuttum, sicil numaram E1001, doğum yılım 1990")
    set_llm(ScriptedLLM([
        Reply(tool_calls=[call("verify_identity", employee_no="E1001", birth_year=1990)]),
        Reply(tool_calls=[call("reset_password", employee_no="E1001")]),                 # too early: refused
        Reply(tool_calls=[call("send_verification", employee_no="E1001")]),
        Reply(text="Kodu e-postanıza gönderdim; şu cümleyi sesli not ile söyleyin."),
    ]))
    client = TestClient(app)
    r = client.post("/voice", data={"requester": "+905551110001"}, files={"audio": ("note.wav", b"RIFF....", "audio/wav")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "E1001" in body["transcript"] and "sesli not" in body["reply"]
    with session() as db:
        from app.db import Ticket
        t = db.get(Ticket, body["ticket_id"])
        assert t.verified is True
        results = [e.data for e in t.events if e.kind == "tool_result"]
        assert results[1]["ok"] is False and "strong verification" in results[1]["error"]
        assert results[2]["ok"] is True and results[2]["code_sent_via"] == "email" and results[2]["code_sent_to"].endswith("@example.com")
        phrase = results[2]["phrase_to_say"]
    assert mails[-1][0] == "ayse.demir@example.com"
    code = mails[-1][1].split("code: ")[1][:6]
    # the phrase arrives as a voice note, the code is typed; a typed code alone is not enough
    set_transcriber(lambda wav: phrase.replace(" ", ", "))
    set_llm(ScriptedLLM([Reply(text="Aldım, şimdi kodu yazın.")]))
    client.post("/voice", data={"requester": "+905551110001"}, files={"audio": ("note.wav", b"RIFF....", "audio/wav")})
    set_llm(ScriptedLLM([
        Reply(tool_calls=[call("check_verification", code="000000")]),                   # wrong code
        Reply(tool_calls=[call("check_verification", code=code)]),
        Reply(tool_calls=[call("reset_password", employee_no="E1001")]),
        Reply(text="Doğrulandı, geçici şifreniz gönderildi."),
    ]))
    r = client.post("/messages", json={"requester": "+905551110001", "text": code}).json()
    assert "geçici" in r["reply"]
    with session() as db:
        t = db.get(Ticket, body["ticket_id"])
        results = [e.data for e in t.events if e.kind == "tool_result"][-3:]
        assert results[0]["ok"] is False and results[0]["code_ok"] is False and results[0]["voice_ok"] is True
        assert results[1]["ok"] is True
        assert results[2]["ok"] is True and results[2]["temporary_password"].startswith("Tmp-")
        assert any("strong verification passed" in e.content for e in t.events if e.kind == "note")


def test_wav_is_passed_through_without_conversion():
    seen = {}
    set_transcriber(lambda wav: seen.setdefault("wav", wav) and "ok")
    assert transcribe(b"RIFFdata", "audio/wav") == "ok"
    assert seen["wav"] == b"RIFFdata"


def test_identity_check_wrong_birth_year_blocks_reset():
    set_llm(ScriptedLLM([
        Reply(tool_calls=[call("verify_identity", employee_no="E1001", birth_year=1980)]),
        Reply(tool_calls=[call("reset_password", employee_no="E1001")]),
        Reply(text="Bilgiler eşleşmedi."),
    ]))
    with session() as db:
        ticket, _ = handle_message(db, "+905551110001", "şifremi unuttum")
        results = [e.data for e in ticket.events if e.kind == "tool_result"]
    assert results[0]["ok"] is False and results[1]["ok"] is False


def test_implausible_transcript_is_retried_on_enhanced_audio_then_given_up(monkeypatch):
    from app import voice, whatsapp
    from app.voice import plausible, transcribe
    assert plausible("Şifremi unuttum") and plausible("my vpn is down") and not plausible("শ্রীপ্রেম") and not plausible("")
    monkeypatch.setattr(voice, "to_wav", lambda audio: audio)
    monkeypatch.setattr(voice, "enhance", lambda wav: wav + b"+")
    attempts = []
    set_transcriber(lambda wav: attempts.append(wav) or ("শ্রীপ্রেম" if not wav.endswith(b"+") else "şifremi unuttum"))
    assert transcribe(b"OggS", "audio/ogg") == "şifremi unuttum" and len(attempts) == 2      # second pass rescued it
    set_transcriber(lambda wav: "শ্রীপ্রেম")
    assert transcribe(b"OggS", "audio/ogg") == ""                                           # both failed: empty
    sent = []
    monkeypatch.setattr(whatsapp, "try_send", lambda to, text: (sent.append((to, text)) or True))
    monkeypatch.setattr(whatsapp, "download_media", lambda mid: (b"OggS", "audio/ogg"))
    set_llm(ScriptedLLM([]))
    client = TestClient(app)
    payload = {"entry": [{"changes": [{"value": {"messages": [{"id": "wamid.v1", "from": "905551110001", "timestamp": "0", "type": "audio", "audio": {"id": "m1"}}]}}]}]}
    assert client.post("/webhook", json=payload).json()["handled"] == 0
    assert sent and "yazar mısınız" in sent[-1][1]
    with session() as db:
        from app.db import Ticket
        assert db.query(Ticket).count() == 0
