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


def test_voice_note_is_transcribed_then_handled_by_l1():
    set_transcriber(lambda wav: "şifremi unuttum, sicil numaram E1001, doğum yılım 1990")
    set_llm(ScriptedLLM([
        Reply(tool_calls=[call("verify_identity", employee_no="E1001", birth_year=1990)]),
        Reply(tool_calls=[call("reset_password", employee_no="E1001")]),
        Reply(text="Kimliğiniz doğrulandı, geçici şifreniz gönderildi."),
    ]))
    client = TestClient(app)
    r = client.post("/voice", data={"requester": "+905551110001"},
                    files={"audio": ("note.wav", b"RIFF....", "audio/wav")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "E1001" in body["transcript"]
    assert "geçici" in body["reply"]
    with session() as db:
        from app.db import Ticket
        t = db.get(Ticket, body["ticket_id"])
        assert t.verified is True
        results = [e.data for e in t.events if e.kind == "tool_result"]
    assert results[1]["ok"] is True and results[1]["temporary_password"].startswith("Tmp-")


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
