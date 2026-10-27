import copy

import pytest
from fastapi.testclient import TestClient

from app import systems
from app.agents import handle_message
from app.db import init_db, session
from app.llm import Reply, ScriptedLLM, ToolCall, image_message, set_llm
from app.main import app


@pytest.fixture(autouse=True)
def fresh_state(tmp_path):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    loaners = copy.deepcopy(systems.LOANERS)
    yield
    systems.LOANERS.clear()
    systems.LOANERS.update(loaners)


def call(name, **args):
    return ToolCall(id=f"c-{name}", name=name, arguments=args)


def test_photo_is_sent_to_the_model_as_image_part():
    llm = ScriptedLLM([Reply(text="Fotoğrafı aldım.")])
    set_llm(llm)
    with session() as db:
        handle_message(db, "+905551110001", "ekran böyle", b"\xff\xd8jpegbytes", "image/jpeg")
    user_msg = llm.requests[0][-1]
    assert user_msg["content"][0] == {"type": "text", "text": "ekran böyle"}
    assert user_msg["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_blue_screen_flows_to_l4_case_loaner_and_shipment():
    set_llm(ScriptedLLM([
        Reply(tool_calls=[call("handoff_L4", summary="TR-LT-101 BSOD CRITICAL_PROCESS_DIED, nvlddmkm.sys")]),
        Reply(tool_calls=[call("check_warranty", hostname="TR-LT-101")]),                   # L4
        Reply(tool_calls=[call("open_service_case", hostname="TR-LT-101", summary="BSOD GPU driver")]),
        Reply(tool_calls=[call("assign_loaner", employee_no="E1001")]),
        Reply(tool_calls=[call("create_shipment", employee_no="E1001", device="TR-LN-01", case_no="DE-100200")]),
        Reply(text="Servis kaydı DE-100200 açıldı, yedek cihaz TR-LN-01 kargoda."),            # L4 done
        Reply(text="Yedek laptopunuz yarın elinizde olacak, takip no TR48100000."),            # L1 done
    ]))
    with session() as db:
        ticket, answer = handle_message(db, "+905551110001", "mavi ekran", b"jpg", "image/jpeg")
        results = {e.content: e.data for e in ticket.events if e.kind == "tool_result" and e.actor == "L4"}
    assert results["check_warranty"]["in_warranty"] is True
    assert results["open_service_case"]["case_no"].startswith("DE-")
    assert results["assign_loaner"]["loaner"] == "TR-LN-01"
    assert results["create_shipment"]["tracking_no"].startswith("TR")
    assert "kargo" in answer or "TR48" in answer


def test_l1_cannot_open_vendor_case_itself():
    set_llm(ScriptedLLM([
        Reply(tool_calls=[call("open_service_case", hostname="TR-LT-101", summary="x")]),
        Reply(text="L4'e devrediyorum."),
    ]))
    with session() as db:
        ticket, _ = handle_message(db, "+905551110001", "laptop bozuldu")
        res = [e.data for e in ticket.events if e.kind == "tool_result"][0]
    assert res["ok"] is False and "not allowed" in res["error"]


def test_image_endpoint():
    set_llm(ScriptedLLM([Reply(text="Fotoğrafı inceledim.")]))
    r = TestClient(app).post("/image", data={"requester": "+905551110002", "text": "bu ne?"},
                             files={"image": ("bsod.jpg", b"jpg", "image/jpeg")})
    assert r.status_code == 200 and r.json()["reply"] == "Fotoğrafı inceledim."
