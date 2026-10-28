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
        t, _ = handle_message(db, "+15551110001", "visio lisansı lazım")
        assert dict(results_of(t))["assign_license"]["ok"] is True
    assert "Visio" in systems.USERS["E1001"]["licenses"]


def test_paid_license_needs_manager_approval_then_flows_on_decision():
    set_llm(ScriptedLLM([
        Reply(tool_calls=[call("assign_license", employee_no="E1001", license="Adobe-CC")]),
        Reply(tool_calls=[call("request_manager_approval", employee_no="E1001", request="Adobe-CC lisansı")]),
        Reply(text="Yöneticinizin onayına gönderdim."),
    ]))
    with session() as db:
        t, _ = handle_message(db, "+15551110001", "adobe lazım")
        res = dict(results_of(t))
        assert res["assign_license"]["ok"] is False and "approval" in res["assign_license"]["error"]
        assert t.status == "waiting_approval"
        a = db.query(Approval).one()
        assert a.approver == "E1002"
        out = db.query(Outbound).one()
        assert out.to == "+15551110002" and f"ONAY {a.id}" in out.text
        aid, tid = a.id, t.id

    # the manager answers over the same channel
    llm = ScriptedLLM([Reply(tool_calls=[call("assign_license", employee_no="E1001", license="Adobe-CC")]),
                       Reply(text="Onaylandı ve atandı.")])
    set_llm(llm)
    with session() as db:
        t, answer = handle_message(db, "+15551110002", f"ONAY {aid}")
        assert t.id == tid and t.status == "open"
        assert dict(results_of(t))["assign_license"]["ok"] is True
    assert "Approval" in llm.requests[0][-1]["content"]
    assert "Adobe-CC" in systems.USERS["E1001"]["licenses"]


def test_only_the_assigned_manager_can_approve():
    set_llm(ScriptedLLM([Reply(tool_calls=[call("request_manager_approval", employee_no="E1001", request="x")]),
                         Reply(text="bekliyor")]))
    with session() as db:
        t, _ = handle_message(db, "+15551110001", "x")
        aid = db.query(Approval).one().id
    set_llm(ScriptedLLM([Reply(text="?")]))
    with session() as db:                       # someone else's "ONAY" is just a message, not a decision
        handle_message(db, "+15551110003", f"ONAY {aid}")
        assert db.get(Approval, aid).decision is None


def test_decision_inside_a_sentence_wins_over_open_tickets_of_the_manager():
    set_llm(ScriptedLLM([Reply(tool_calls=[call("request_manager_approval", employee_no="E1001", request="Project lisansı")]),
                         Reply(text="bekliyor")]))
    with session() as db:
        handle_message(db, "+15551110001", "Project lisansı lazım")
        aid = db.query(Approval).one().id
    set_llm(ScriptedLLM([Reply(text="Bakıyorum."), Reply(text="NEW"), Reply(text="Bakıyorum.")]))
    with session() as db:                       # the manager has two open tickets of their own
        handle_message(db, "+15551110002", "benim de vpn bağlanmıyor")
        handle_message(db, "+15551110002", "ayrıca yazıcı kağıt sıkıştırıyor sürekli")
        n = db.query(Ticket).count()
    set_llm(ScriptedLLM([Reply(text="Talebiniz reddedildi.")]))
    with session() as db:
        t, answer = handle_message(db, "+15551110002", f"reddetmek için RET {aid}")
        assert db.get(Approval, aid).decision == "denied"
        assert t.requester == "+15551110001"   # the employee's ticket, continued by L1
        assert db.query(Ticket).count() == n     # no stray ticket for the manager



def test_onboarding_creates_account_license_and_groups():
    set_llm(ScriptedLLM([
        Reply(tool_calls=[call("create_account", name="Can Yılmaz", department="Sales", manager_employee_no="E1002",
                               phone="+15551110004")]),
        Reply(tool_calls=[call("assign_license", employee_no="E1004", license="M365-E3"),
                          call("add_to_group", employee_no="E1004", group="sales-tr")]),
        Reply(text="Hesap hazır."),
    ]))
    with session() as db:
        t, _ = handle_message(db, "+15551110002", "Pazartesi Can Yılmaz başlıyor, satış ekibi")
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
        handle_message(db, "+15551110002", "Emma Johnson bugün ayrıldı")
    u = systems.USERS["E1001"]
    assert u["enabled"] is False and u["groups"] == [] and u["licenses"] == []


def test_identity_is_remembered_for_hours_and_only_for_oneself(desk):
    from app.agents import AGENTS, handle_message
    from app.tools import Ctx, run_tool
    from app.db import Ticket, session
    set_llm(ScriptedLLM([Reply(tool_calls=[call("verify_identity", employee_no="E1001", birth_year=1990)]), Reply(text="Doğrulandı.")]))
    with session() as db:
        t, _ = handle_message(db, "+15551110001", "şifremi unuttum, E1001 1990")
        assert t.verified
    set_llm(ScriptedLLM([Reply(text="NEW"), Reply(text="Bakıyorum.")]))                 # a different problem: the router says NEW
    with session() as db:                                              # a new ticket an hour later: already verified, prompt says so
        t2, _ = handle_message(db, "+15551110001", "VPN bağlanmıyor")
        assert t2.id != t.id and t2.verified and "verified earlier today" in AGENTS["L1"].system_prompt(t2, db)
        assert "do not ask for employee number" in AGENTS["L1"].system_prompt(t2, db)
    with session() as db:                                              # Emma's number cannot verify as Liam
        out = run_tool(Ctx(db=db, ticket=db.get(Ticket, t2.id), agent="L1"), "verify_identity", {"employee_no": "E1002", "birth_year": 1985})
        assert out["ok"] is False and "only for" in out["error"] and "Emma Johnson" in out["error"]
    r = desk.post("/settings/verification", data={"identity_hours": "0"}, follow_redirects=False)        # 0 = ask on every ticket
    assert r.status_code == 303
    set_llm(ScriptedLLM([Reply(text="NEW"), Reply(text="Bakıyorum.")]))
    with session() as db:
        for x in db.query(Ticket):
            x.status = "resolved"
    set_llm(ScriptedLLM([Reply(text="NEW"), Reply(text="Bakıyorum.")]))
    with session() as db:
        t3, _ = handle_message(db, "+15551110001", "yazıcı bozuk")
        assert not t3.verified


def test_admin_opens_a_ticket_for_an_employee_from_the_console(desk):
    from app.db import Outbound, Ticket, session
    set_llm(ScriptedLLM([Reply(text="Ekran titremesi için sürücüye bakıyorum.")]))
    r = desk.post("/tickets/new", json={"requester": "E1001", "text": "Laptop ekranı sabahtan beri titriyor", "note": "called in by phone"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["requester"] == "+15551110001" and "sürücüye" in j["reply"]
    with session() as db:
        t = db.get(Ticket, j["id"])
        assert t.requester == "+15551110001" and t.title.startswith("Laptop ekranı")
        assert any("opened by the desk on behalf" in e.content for e in t.events if e.kind == "note")
        assert db.query(Outbound).filter(Outbound.to == "+15551110001").count() == 1      # the reply goes to the employee
    assert desk.post("/tickets/new", json={"requester": "nobody", "text": "x"}).status_code == 422
    assert 'id="newBtn"' in desk.get("/tickets", headers={"accept": "text/html"}).text
