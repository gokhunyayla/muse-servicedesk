"""Blue screens: the agent reports recorded crashes; a new one becomes one proactive ticket for the owner,
history is not news, and the crash drill is for admins only."""
import re
import time

import pytest
from fastapi.testclient import TestClient

from app import crashwatch, proactive, vault
from app.db import Ticket, init_db, session
from app.tools import Ctx, run_tool

CRASH = {"id": "4711", "at": "2026-10-29T08:00:00Z", "code": "0x000000d1", "name": "DRIVER_IRQL_NOT_LESS_OR_EQUAL", "hint": "driver"}


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setattr(vault, "KEY_FILE", tmp_path / "k")
    from app import security
    security._hits.clear()


def _device(desk):
    from app.main import app
    page = desk.post("/devices", data={"hostname": "LT-101", "owner": "E1001"}).text
    token = re.search(r"\$t='(mda_[^']+)'", page).group(1)
    return TestClient(app), {"Authorization": f"Bearer {token}"}


def test_history_is_not_news_but_a_new_crash_is():
    old = {**CRASH, "id": "1"}
    assert crashwatch.new_crashes("LT-101", [old]) == []                # first report: what happened before enrolment
    assert crashwatch.new_crashes("LT-101", [old, CRASH]) == [CRASH]
    assert crashwatch.new_crashes("LT-101", [old, CRASH]) == []


def test_a_crash_opens_one_proactive_ticket_for_the_owner(desk, monkeypatch):
    c, h = _device(desk)
    opened = []
    monkeypatch.setattr(proactive, "open_ticket", lambda f: opened.append(f) or 1)
    c.post("/agent/heartbeat", json={"metrics": {"crashes": []}}, headers=h)
    c.post("/agent/heartbeat", json={"metrics": {"crashes": [CRASH]}}, headers=h)
    time.sleep(0.3)
    assert len(opened) == 1
    f = opened[0]
    assert f["kind"] == "crash" and f["employee_no"] == "E1001" and "DRIVER_IRQL_NOT_LESS_OR_EQUAL" in f["text"] and "crash-report" in f["text"]


def test_a_second_crash_joins_the_open_ticket(desk, monkeypatch):
    _device(desk)
    with session() as db:
        db.add(Ticket(requester="+15551110001", title="[Proactive:crash] LT-101", status="open", owner="L2"))
    monkeypatch.setattr(proactive, "open_ticket", lambda f: pytest.fail("opened a second ticket"))
    tid = crashwatch.handle("LT-101", [CRASH])
    with session() as db:
        assert any("blue screen" in e.content for e in db.get(Ticket, tid).events)


def test_the_crash_drill_is_never_offered_to_the_agents(desk):
    _device(desk)
    with session() as db:
        t = Ticket(requester="+15551110001", title="x", status="open", owner="L2")
        db.add(t)
        db.flush()
        r = run_tool(Ctx(db=db, ticket=t, agent="L2"), "run_endpoint_script", {"hostname": "LT-101", "script": "crash-drill"})
        assert r["ok"] is False and "crash-drill" not in str(r.get("error"))
    from app.tools import REGISTRY
    assert "crash-drill" not in REGISTRY["run_endpoint_script"].description
    assert "crash-report" in REGISTRY["run_endpoint_script"].description
