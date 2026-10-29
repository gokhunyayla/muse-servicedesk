"""Malware watch: Defender detections arrive with the heartbeat; a new one is cleaned through the agent once and the
admins are told. The agent is a thread speaking the same HTTP API."""
import re
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import malwatch, vault
from app.db import Outbound, Ticket, init_db, session
from app.sla import put_setting

EICAR = {"id": "2147519003", "name": "Virus:DOS/EICAR_Test_File", "severity": 5, "detected": "2026-10-29T07:00:00Z",
         "status": "quarantined", "action_success": True, "active": False, "resources": ["file:_C:\\Users\\Public\\eicar.com"]}


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setattr(vault, "KEY_FILE", tmp_path / "k")
    put_setting("reports", {"whatsapp_recipients": "+905064974432", "recipients": "", "language": "en"})
    from app import security
    security._hits.clear()


def _device(desk):
    from app.main import app
    page = desk.post("/devices", data={"hostname": "LT-101", "owner": "E1001"}).text
    token = re.search(r"\$t='(mda_[^']+)'", page).group(1)
    return TestClient(app), {"Authorization": f"Bearer {token}"}


def test_each_detection_is_handled_once():
    assert malwatch.new_threats("LT-101", [EICAR]) == [EICAR]
    assert malwatch.new_threats("LT-101", [EICAR]) == []
    assert malwatch.new_threats("LT-102", [EICAR]) == [EICAR]          # per device
    assert malwatch.new_threats("LT-101", [{"name": "no id"}]) == []


def test_a_detection_in_the_heartbeat_is_cleaned_and_reported(desk, monkeypatch):
    c, h = _device(desk)
    started, real = [], malwatch.handle
    monkeypatch.setattr(malwatch, "handle", lambda *args: started.append(args))
    c.post("/agent/heartbeat", json={"metrics": {"threats": [EICAR]}}, headers=h)
    c.post("/agent/heartbeat", json={"metrics": {"threats": [EICAR]}}, headers=h)
    time.sleep(0.2)
    assert len(started) == 1                                            # the second heartbeat repeats it; no new work
    stop, seen = threading.Event(), []

    def agent():
        while not stop.is_set():
            for job in c.get("/agent/jobs?wait=1", headers=h).json()["jobs"]:
                seen.append(job["script"])
                c.post(f"/agent/jobs/{job['id']}/result", headers=h, json={"ok": True, "output":
                       "Defender threats on record: Virus:DOS/EICAR_Test_File; active before: 0; removed and quick scan finished. Result: clean"})
    th = threading.Thread(target=agent, daemon=True)
    th.start()
    try:
        r = real(*started[0])
    finally:
        stop.set()
        th.join(timeout=5)
    assert seen == ["malware-clean"] and r["status"] == "cleaned"
    with session() as db:
        t = db.get(Ticket, r["ticket"])
        assert t.status == "resolved" and t.owner == "L3" and "EICAR" in t.title
        msg = db.query(Outbound).filter(Outbound.to == "+905064974432").one().text
        assert "I noticed and fixed it" in msg and "eicar.com" in msg


def test_when_cleaning_fails_the_ticket_stays_with_l3(desk):
    _device(desk)                                                       # nobody answers the job: offline-style failure
    from app import endpoints
    endpoints_run = endpoints.run
    try:
        endpoints.run = lambda *a, **k: {"ok": False, "error": "device offline"}
        r = malwatch.handle("LT-101", [EICAR])
    finally:
        endpoints.run = endpoints_run
    assert r["status"] == "needs attention"
    with session() as db:
        assert db.get(Ticket, r["ticket"]).status == "open"
        assert "could not clean it automatically: device offline" in db.query(Outbound).one().text
