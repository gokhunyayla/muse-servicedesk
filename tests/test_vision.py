import pytest
from fastapi.testclient import TestClient

from app import camera, vision
from app.db import Ticket, init_db, session
from app.llm import Reply, ScriptedLLM, ToolCall, set_llm
from app.main import app
from app.vision import parse_boxes


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    yield
    vision.set_segmenter(vision.meta_segment)


def call(_name, **args):
    return ToolCall(id=f"c-{_name}", name=_name, arguments=args)


def test_sam_output_parsing():
    text = "<0f>0<|box;x1=353;y1=382;x2=453;y2=427;w=537;h=561|><|mask;x=0;y=0;data=46,101,~abc|>," \
           "1<|box;x1=10;y1=20;x2=30;y2=40;w=537;h=561|><|mask;x=0;y=0;data=1,1,~a|>"
    boxes = parse_boxes(text)
    assert boxes == [{"x1": 353, "y1": 382, "x2": 453, "y2": 427, "frame_w": 537, "frame_h": 561},
                     {"x1": 10, "y1": 20, "x2": 30, "y2": 40, "frame_w": 537, "frame_h": 561}]


def test_camera_finding_opens_ticket_for_desk_owner_with_location():
    vision.set_segmenter(lambda img, prompt, mime: [{"x1": 1000, "y1": 170, "x2": 1200, "y2": 360,
                                                     "frame_w": 1280, "frame_h": 720}] if prompt == "sticky note" else [])
    set_llm(ScriptedLLM([
        Reply(text='{"findings":[{"kind":"credential_note","evidence":"yellow note with Pass: Summer2026!","confidence":0.95}]}'),
        Reply(tool_calls=[call("revoke_sessions", employee_no="E1001"),
                          call("notify_user", employee_no="E1001", text="Masanızdaki şifre notunu kaldırın."),
                          call("schedule_followup", hours=2, note="camera re-check CAM-SALES-01")]),
        Reply(text="Sahibi uyarıldı, oturumlar düşürüldü, 2 saat sonra tekrar bakılacak."),
    ]))
    results = camera.process_frame("CAM-SALES-01", b"jpg")
    assert len(results) == 1 and results[0]["kind"] == "credential_note"
    assert results[0]["boxes"][0]["x1"] == 1000
    with session() as db:
        t = db.get(Ticket, results[0]["ticket_id"])
        assert t.requester == "+905551110001" and t.title.startswith("[Compliance:credential_note]")
        first = [e for e in t.events if e.kind == "message"][0]
        assert "x=1000-1200" in first.content and "ISO 27001" in first.content
        tools = [e.content for e in t.events if e.kind == "tool_call"]
    assert {"revoke_sessions", "notify_user", "schedule_followup"} <= set(tools)


def test_low_confidence_findings_are_ignored():
    vision.set_segmenter(lambda *a: [])
    set_llm(ScriptedLLM([Reply(text='{"findings":[{"kind":"visitor_badge","evidence":"maybe","confidence":0.3}]}')]))
    assert camera.inspect_frame(b"jpg") == []


def test_video_frames_are_sent_as_extra_image_parts(monkeypatch):
    monkeypatch.setattr(vision, "sample_frames", lambda v: [b"f1", b"f2", b"f3"])
    llm = ScriptedLLM([Reply(text="Videoda Wi-Fi simgesi kırmızı, bağlantı düşüyor.")])
    set_llm(llm)
    r = TestClient(app).post("/video", data={"requester": "+905551110002", "text": "sürekli kopuyor"},
                             files={"video": ("clip.mp4", b"mp4", "video/mp4")})
    assert r.status_code == 200 and r.json()["frames"] == 3
    parts = llm.requests[0][-1]["content"]
    assert parts[0]["type"] == "text" and "3 frames" in parts[0]["text"]
    assert sum(p["type"] == "image_url" for p in parts) == 3
