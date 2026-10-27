import copy

import pytest

from app import systems
from app.agents import handle_message
from app.db import Approval, Outbound, Ticket, init_db, session
from app.llm import Reply, ScriptedLLM, ToolCall, set_llm


@pytest.fixture(autouse=True)
def fresh_state(tmp_path):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    users = copy.deepcopy(systems.USERS)
    yield
    systems.USERS.clear()
    systems.USERS.update(users)


def call(_name, **args):
    return ToolCall(id=f"c-{_name}", name=_name, arguments=args)


def results_of(ticket):
    return [(e.content, e.data) for e in ticket.events if e.kind == "tool_result"]


def test_standard_license_is_assigned_without_approval():
    set_llm(ScriptedLLM([Reply(tool_calls=[call("assign_license", employee_no="E1001", license="Visio")]),
                         Reply(text="Visio atandı.")]))
    with session() as db:
        t, _ = handle_message(db, "+905551110001", "visio lisansı lazım")
        assert dict(results_of(t))["assign_license"]["ok"] is True
    assert "Visio" in systems.USERS["E1001"]["licenses"]


def test_paid_license_needs_manager_approval_then_flows_on_decision():
    set_llm(ScriptedLLM([
        Reply(tool_calls=[call("assign_license", employee_no="E1001", license="Adobe-CC")]),
        Reply(tool_calls=[call("request_manager_approval", employee_no="E1001", request="Adobe-CC lisansı")]),
        Reply(text="Yöneticinizin onayına gönderdim."),
    ]))
    with session() as db:
        t, _ = handle_message(db, "+905551110001", "adobe lazım")
        res = dict(results_of(t))
        assert res["assign_license"]["ok"] is False and "approval" in res["assign_license"]["error"]
        assert t.status == "waiting_approval"
        a = db.query(Approval).one()
        assert a.approver == "E1002"
        out = db.query(Outbound).one()
        assert out.to == "+905551110002" and f"ONAY {a.id}" in out.text
        aid, tid = a.id, t.id

    # the manager answers over the same channel
    llm = ScriptedLLM([Reply(tool_calls=[call("assign_license", employee_no="E1001", license="Adobe-CC")]),
                       Reply(text="Onaylandı ve atandı.")])
    set_llm(llm)
    with session() as db:
        t, answer = handle_message(db, "+905551110002", f"ONAY {aid}")
        assert t.id == tid and t.status == "open"
        assert dict(results_of(t))["assign_license"]["ok"] is True
    assert "Approval" in llm.requests[0][-1]["content"]
    assert "Adobe-CC" in systems.USERS["E1001"]["licenses"]


def test_only_the_assigned_manager_can_approve():
    set_llm(ScriptedLLM([Reply(tool_calls=[call("request_manager_approval", employee_no="E1001", request="x")]),
                         Reply(text="bekliyor")]))
    with session() as db:
        t, _ = handle_message(db, "+905551110001", "x")
        aid = db.query(Approval).one().id
    with pytest.raises(PermissionError):
        with session() as db:
            handle_message(db, "+905551110003", f"ONAY {aid}")


def test_sap_role_goes_through_l3_and_needs_approval():
    set_llm(ScriptedLLM([
        Reply(tool_calls=[call("handoff_L3", summary="E1001 SD_CREATE rolü istiyor")]),
        Reply(tool_calls=[call("grant_sap_role", employee_no="E1001", role="SD_CREATE")]),          # L3: blocked
        Reply(tool_calls=[call("request_manager_approval", employee_no="E1001", request="SAP SD_CREATE")]),
        Reply(text="onay bekleniyor"), Reply(text="Yönetici onayına gönderildi."),
    ]))
    with session() as db:
        t, _ = handle_message(db, "+905551110001", "SAP'de sipariş açamıyorum, yetki hatası")
        l3 = [(e.content, e.data) for e in t.events if e.kind == "tool_result" and e.actor == "L3"]
    assert dict(l3)["grant_sap_role"]["ok"] is False
    assert t.status == "waiting_approval"


def test_onboarding_creates_account_license_and_groups():
    set_llm(ScriptedLLM([
        Reply(tool_calls=[call("create_account", name="Can Yılmaz", department="Sales", manager_employee_no="E1002",
                               phone="+905551110004")]),
        Reply(tool_calls=[call("assign_license", employee_no="E1004", license="M365-E3"),
                          call("add_to_group", employee_no="E1004", group="sales-tr")]),
        Reply(text="Hesap hazır."),
    ]))
    with session() as db:
        t, _ = handle_message(db, "+905551110002", "Pazartesi Can Yılmaz başlıyor, satış ekibi")
        assert dict(results_of(t))["create_account"]["initial_password"].startswith("Tmp-")
    new = systems.USERS["E1004"]
    assert new["licenses"] == ["M365-E3"] and new["groups"] == ["sales-tr"] and new["manager"] == "E1002"


def test_offboarding_disables_revokes_and_strips():
    set_llm(ScriptedLLM([
        Reply(tool_calls=[call("disable_account", employee_no="E1001"), call("revoke_sessions", employee_no="E1001"),
                          call("remove_from_groups", employee_no="E1001"), call("revoke_license", employee_no="E1001", license="M365-E3")]),
        Reply(text="Çıkış tamamlandı."),
    ]))
    with session() as db:
        handle_message(db, "+905551110002", "Ayşe Demir bugün ayrıldı")
    u = systems.USERS["E1001"]
    assert u["enabled"] is False and u["groups"] == [] and u["licenses"] == []
