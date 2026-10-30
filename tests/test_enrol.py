"""Self-enrolment: a stranger with the invitation code gets an account by chatting; known people and the
ticket flow are untouched; limits, lock-out and expiry hold."""
import copy

import pytest

from app import enrol, systems, vault
from app.db import Client, Outbound, Ticket, init_db, session
from app.sla import put_setting

NEW = "+15559990001"          # nobody the desk knows
KNOWN = "+15551110001"        # Emma, in the simulated directory


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setattr(vault, "KEY_FILE", tmp_path / "k")
    users = copy.deepcopy(systems.USERS)
    put_setting("reports", {"whatsapp_recipients": "+905064974432", "recipients": "", "language": "en"})
    enrol._last_tick = 0.0
    yield
    systems.USERS.clear()
    systems.USERS.update(users)


def on(**kw):
    put_setting("enrol", {**enrol.DEFAULT, "enabled": True, "code": "MUSE-JURY-2026", "expires_on": "2026-11-16", **kw})


def test_off_by_default_strangers_still_reach_the_ticket_flow():
    assert enrol.wants(NEW) is False
    on(enabled=False)
    assert enrol.wants(NEW) is False
    on()
    assert enrol.wants(NEW) is True and enrol.wants(KNOWN) is False


def test_a_stranger_enrols_by_chatting():
    on()
    assert "invitation code" in enrol.step(NEW, "Hello, I would like to try the desk")
    assert "attempts left" in enrol.step(NEW, "wrong-code")
    assert "first and last name" in enrol.step(NEW, "muse-jury-2026")           # case does not matter
    assert "first and last name" in enrol.step(NEW, "Ada")                        # one word is not a name
    assert "e-mail" in enrol.step(NEW, "ada lovelace")
    reply = enrol.step(NEW, "ada@example.org")
    assert "Your account is ready, Ada" in reply and "Username:" in reply and "First password:" in reply
    with session() as db:
        c = db.query(Client).filter(Client.phone == NEW).one()
        assert c.name == "Ada Lovelace" and c.email == "ada@example.org" and c.employee_no
        assert systems.USERS[c.employee_no]["department"] == "Evaluation" and "jury" in systems.USERS[c.employee_no]["groups"]
        assert db.query(Ticket).count() == 0                                      # no ticket was opened
    assert enrol.wants(NEW) is False                                               # now known: the ticket flow takes over
    assert enrol.accounts()[0]["name"] == "Ada Lovelace"


def test_five_wrong_codes_lock_the_contact_and_the_cap_holds():
    on(max_accounts=1)
    enrol.step(NEW, "hi")
    for _ in range(5):
        r = enrol.step(NEW, "nope")
    assert "try again in an hour" in r and "try again in an hour" in enrol.step(NEW, "MUSE-JURY-2026")
    other = "+15559990002"
    enrol.step(other, "hi"); enrol.step(other, "MUSE-JURY-2026"); enrol.step(other, "Grace Hopper"); enrol.step(other, "skip")
    third = "+15559990003"
    enrol.step(third, "hi")
    assert "no more accounts" in enrol.step(third, "MUSE-JURY-2026")


def test_accounts_are_disabled_after_the_expiry_day():
    from datetime import date
    on()
    enrol.step(NEW, "hi"); enrol.step(NEW, "MUSE-JURY-2026"); enrol.step(NEW, "Ada Lovelace"); enrol.step(NEW, "skip")
    emp = enrol.accounts()[0]["employee_no"]
    assert enrol.expire_due(date(2026, 11, 16)) == 0 and systems.USERS[emp]["enabled"]
    assert enrol.expire_due(date(2026, 11, 17)) == 1 and systems.USERS[emp]["enabled"] is False
    with session() as db:
        assert db.query(Client).filter(Client.employee_no == emp).one().enabled is False
    assert enrol.expire_due(date(2026, 11, 18)) == 0                              # once


def test_whatsapp_routes_strangers_to_enrolment_and_known_people_to_tickets(monkeypatch):
    from app import whatsapp
    on()
    sent = []
    monkeypatch.setattr(whatsapp, "try_send", lambda to, text: sent.append((to, text)) or True)
    payload = lambda frm, body: {"entry": [{"changes": [{"value": {"messages": [{"id": f"m-{frm}-{body}", "type": "text", "from": frm.lstrip("+"), "text": {"body": body}}]}}]}]}
    whatsapp._handle_payload(payload(NEW, "hello"))
    assert "invitation code" in sent[-1][1]
    with session() as db:
        assert db.query(Ticket).count() == 0
    from app.llm import Reply, ScriptedLLM, set_llm
    set_llm(ScriptedLLM([Reply(text="Looking into your printer.")]))
    whatsapp._handle_payload(payload(KNOWN, "my printer is broken"))
    with session() as db:
        assert db.query(Ticket).filter(Ticket.requester == KNOWN).count() == 1


def test_settings_page_and_admin_group_refused(desk):
    r = desk.post("/settings/enrol", data={"enabled": "1", "code": "X", "max_accounts": "5", "expires_on": "2026-11-16",
                                           "department": "Evaluation", "group": "jury", "license": "", "max_licensed": "2", "manager_employee_no": ""},
                  follow_redirects=False)
    assert r.status_code == 303 and enrol.config()["enabled"] and enrol.config()["max_accounts"] == 5
    r = desk.post("/settings/enrol", data={"enabled": "1", "code": "X", "group": "Global Administrators"}, follow_redirects=False)
    assert "admin+group" in r.headers["location"]
    assert "Self-enrolment" in desk.get("/settings?tab=channels").text


def test_support_page_proves_the_contact_then_enrols(monkeypatch):
    from app import support
    on()
    codes = []
    monkeypatch.setattr(support, "_deliver", lambda contact, code: codes.append(code) or True)
    who = {"mode": "anon"}
    reply, who = support.identify(who, "+1 555 999 0001")
    assert "invitation code" in reply and who["stage"] == "ask_code" and who["enrol"] and codes
    reply, who = support.identify(who, codes[-1])
    assert who["stage"] == "enrol" and "invitation code" in reply
    reply, who = support.identify(who, "MUSE-JURY-2026")
    reply, who = support.identify(who, "Grace Hopper")
    reply, who = support.identify(who, "skip")
    assert "Your account is ready, Grace" in reply and "Welcome, Grace Hopper" in reply and who["mode"] == "client"
    # the ordinary path for a known employee is untouched
    codes.clear()
    reply, who2 = support.identify({"mode": "anon"}, "+1 555 111 0001")
    assert who2["stage"] == "ask_code" and not who2.get("enrol") and codes
