"""Joiners and leavers: HR starts (with a CV), the manager answers on their own channel, the desk creates or
closes the account. Runs on the simulated directory; the Graph calls are covered in test_graph."""
import copy
from datetime import date, timedelta

import pytest

from app import attachments, systems, vault
from app.agents import handle_message
from app.db import Client, Followup, Outbound, PeopleProcess, Ticket, init_db, session
from app.llm import Reply, ScriptedLLM, ToolCall, set_llm

HR = "+15551110003"          # Olivia (simulated directory) — put in the HR group below
MANAGER = "+15551110002"     # Liam


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setattr(vault, "KEY_FILE", tmp_path / "k")
    users = copy.deepcopy(systems.USERS)
    systems.USERS["E1003"]["groups"].append("hr")
    from app import channels
    sent = []
    monkeypatch.setattr(channels, "send_email", lambda to, subject, text: (sent.append((to, subject, text)) or (True, "")))
    yield sent
    systems.USERS.clear()
    systems.USERS.update(users)


def call(_name, **args):
    return ToolCall(id=f"c-{_name}", name=_name, arguments=args)


def results(ticket, name):
    return [e.data for e in ticket.events if e.kind == "tool_result" and e.content == name]


CV = ("Noah Williams\nSales Executive\nnoah.williams.cv@example.org · +44 7700 900123\n"
      "Experience: 6 years B2B sales. Languages: English, Turkish.")


def test_documents_are_read_on_the_way_in():
    text = attachments.with_document("Yeni çalışan", "cv.txt", "text/plain", CV.encode())
    assert "[Attached document: cv.txt]" in text and "noah.williams.cv@example.org" in text
    bad = attachments.with_document("", "cv.exe", "application/octet-stream", b"MZ")
    assert "could not be read" in bad


def test_only_hr_can_start_and_missing_details_are_asked_for(fresh):
    set_llm(ScriptedLLM([Reply(tool_calls=[call("start_onboarding", name="Noah Williams", start_date="2026-11-09",
                                                  department="Sales", manager_employee_no="E1002",
                                                  personal_email="noah@example.org")]),
                         Reply(text="Bunu sadece İK başlatabilir.")]))
    with session() as db:
        t, _ = handle_message(db, "+15551110001", "Noah'ı işe al")             # Emma is not HR
        assert "only members" in results(t, "start_onboarding")[0]["error"]
    set_llm(ScriptedLLM([Reply(tool_calls=[call("start_onboarding", name="Noah Williams", start_date="",
                                                  department="Sales", manager_employee_no="E1002")]),
                         Reply(text="Başlangıç tarihi ve kişisel iletişim bilgisi lazım.")]))
    with session() as db:
        t, _ = handle_message(db, HR, "Noah Williams Satış'a başlıyor")
        assert set(results(t, "start_onboarding")[0]["missing"]) == {"start_date", "personal_email or personal_phone"}
        assert db.query(PeopleProcess).count() == 0


def test_onboarding_from_cv_to_account(fresh):
    sent = fresh
    msg = attachments.with_document("Noah Williams 9 Kasım'da Satış'ta başlıyor, yöneticisi E1002, unvan Sales Executive.",
                                    "noah-cv.txt", "text/plain", CV.encode())
    set_llm(ScriptedLLM([Reply(tool_calls=[call("start_onboarding", name="Noah Williams", start_date="2026-11-09",
                                                  department="Sales", manager_employee_no="E1002", title="Sales Executive",
                                                  personal_email="noah.williams.cv@example.org", personal_phone="+447700900123")]),
                         Reply(text="Yöneticisine sordum, hazır olunca haber vereceğim.")]))
    with session() as db:
        hr_t, _ = handle_message(db, HR, msg)
        pid = results(hr_t, "start_onboarding")[0]["process_id"]
        p = db.get(PeopleProcess, pid)
        assert p.manager_contact == MANAGER and hr_t.status == "waiting_approval"
        q = db.query(Outbound).filter(Outbound.to == MANAGER).one()
        assert "Noah Williams" in q.text and "Visio" in q.text and "Standard laptop" in q.text      # options offered
        mt_id = p.manager_ticket_id
    # the manager answers some, then the rest
    set_llm(ScriptedLLM([Reply(tool_calls=[call("record_people_answers", process_id=pid,
                                                  answers={"groups": "sales-tr", "license": "M365-E3"})]),
                         Reply(text="Cihaz ve VPN?")]))
    with session() as db:
        t, _ = handle_message(db, MANAGER, "sales-tr grubu, lisans M365-E3")
        assert t.id == mt_id
        assert set(results(t, "record_people_answers")[0]["missing"]) == {"device", "vpn"}
    set_llm(ScriptedLLM([Reply(tool_calls=[call("record_people_answers", process_id=pid,
                                                  answers={"device": "Standard laptop", "vpn": "evet"})]),
                         Reply(tool_calls=[call("complete_onboarding", process_id=pid)]),
                         Reply(text="Hesap hazır.")]))
    with session() as db:
        t, _ = handle_message(db, MANAGER, "standart laptop, VPN evet")
        r = results(t, "record_people_answers")[-1]["completed"]             # the last answer creates the account
        assert r["ok"] and not r["problems"]
        assert results(t, "complete_onboarding")[0]["status"] == "already done"
        emp = r["employee_no"]
        u = systems.USERS[emp]
        assert u["name"] == "Noah Williams" and u["manager"] == "E1002" and "sales-tr" in u["groups"] and "vpn-users" in u["groups"]
        assert u["licenses"] == ["M365-E3"] and u["phone"] == "+447700900123"
        assert db.query(Client).filter(Client.employee_no == emp, Client.email == "noah.williams.cv@example.org").count() == 1
        import re
        pw_msgs = [o.text for o in db.query(Outbound).filter(Outbound.to == MANAGER).all() if re.search(r"(First password|İlk şifre): ", o.text)]
        assert pw_msgs                                                                              # password to the manager only
        password = re.search(r"(?:First password|İlk şifre): (\S+)", pw_msgs[0]).group(1)
        hr = db.get(Ticket, hr_t.id)
        assert hr.status == "resolved" and any("Onboarding complete" in o.text or "İşe giriş tamamlandı" in o.text
                                               for o in db.query(Outbound).filter(Outbound.to == HR))
    assert sent and sent[0][0] == "noah.williams.cv@example.org" and "username" in sent[0][2].lower() or "Kullanıcı adınız" in sent[0][2]
    assert not any(password in x[2] for x in sent)                                                  # never by e-mail


def test_a_phone_number_is_never_given_to_a_second_person(fresh):
    set_llm(ScriptedLLM([Reply(tool_calls=[call("start_onboarding", name="X", start_date="2026-11-09", department="Sales",
                                                  manager_employee_no="E1002", personal_phone="+15551110001")]),
                         Reply(text="Bu numara başka birine ait.")]))
    with session() as db:
        t, _ = handle_message(db, HR, "X başlıyor")
        assert "never shared" in results(t, "start_onboarding")[0]["error"]


def test_offboarding_is_scheduled_and_runs_on_the_last_day(fresh):
    from app import scheduler
    last = (date.today() + timedelta(days=3)).isoformat()
    set_llm(ScriptedLLM([Reply(tool_calls=[call("start_offboarding", employee_no="E1001", last_day=last)]),
                         Reply(text="Yöneticisine sordum.")]))
    with session() as db:
        hr_t, _ = handle_message(db, HR, f"Emma {last} tarihinde ayrılıyor")
        pid = results(hr_t, "start_offboarding")[0]["process_id"]
        p = db.get(PeopleProcess, pid)
        mgr_contact = p.manager_contact
    set_llm(ScriptedLLM([Reply(tool_calls=[call("record_people_answers", process_id=pid,
                                                  answers={"handover": "Liam", "device_return": "kurye"})]),
                         Reply(tool_calls=[call("complete_offboarding", process_id=pid)]),
                         Reply(text="Son gün akşam kapanacak.")]))
    with session() as db:
        t, _ = handle_message(db, mgr_contact, "Liam devralsın, cihaz kuryeyle gelsin")
        assert results(t, "record_people_answers")[0]["completed"]["status"] == "scheduled"
        assert results(t, "complete_offboarding")[0]["status"] == "already scheduled"     # never twice
        f = db.query(Followup).filter(Followup.note == f"offboarding:{pid}").one()
        f.due_at = f.due_at - timedelta(days=5)                       # the last day has ended
    assert systems.USERS["E1001"]["enabled"] is True
    scheduler.run_due_followups()
    assert systems.USERS["E1001"]["enabled"] is False and systems.USERS["E1001"]["groups"] == [] and systems.USERS["E1001"]["licenses"] == []
    with session() as db:
        assert db.get(PeopleProcess, pid).status == "done"
        assert any("Offboarding of Emma Johnson complete" in o.text or "ayrılış tamamlandı" in o.text for o in db.query(Outbound).filter(Outbound.to == HR))


def test_people_settings_page(desk):
    page = desk.get("/settings?tab=channels").text
    assert "Joiners &amp; leavers" in page and "groups | required" in page
    desk.post("/settings/people", data={"hr_group": "people-team", "vpn_group": "vpn", "devices": "MacBook, ThinkPad",
                                        "onboarding_questions": "groups | required | Which groups? | Hangi gruplar?\nparking | optional | Parking card? | Otopark kartı?",
                                        "offboarding_questions": "handover | required | Who takes over? | Kim devralır?"})
    from app.people import config
    c = config()
    assert c["hr_group"] == "people-team" and [q["key"] for q in c["onboarding_questions"]] == ["groups", "parking"]


def test_answers_find_their_process_from_the_ticket_whatever_number_the_model_passes(fresh):
    set_llm(ScriptedLLM([Reply(tool_calls=[call("start_onboarding", name="Ada Lovelace", start_date="2026-11-09", department="Sales",
                                                  manager_employee_no="E1002", personal_email="ada@example.org")]),
                         Reply(text="ok")]))
    with session() as db:
        handle_message(db, HR, "Ada başlıyor")
    set_llm(ScriptedLLM([Reply(tool_calls=[call("record_people_answers", process_id=9999,     # a ticket number, say
                                                  answers={"groups": "sales-tr"})]), Reply(text="ok")]))
    with session() as db:
        t, _ = handle_message(db, MANAGER, "sales-tr")
        r = results(t, "record_people_answers")[-1]
        assert r["ok"] and r["answers"] == {"groups": "sales-tr"}
