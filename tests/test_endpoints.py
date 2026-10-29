"""Real endpoints: a machine running the endpoint agent enrols with a token, reports health, and runs the
allow-listed scripts the agents ask for. The agent side is played here by a thread speaking the same HTTP API."""
import re
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import agent_api, endpoints, systems, vault
from app.db import DeviceJob, Event, Ticket, init_db, session
from app.tools import Ctx, run_tool


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setattr(vault, "KEY_FILE", tmp_path / "k")
    from app import security
    security._hits.clear()


METRICS = {"disk_used_pct": 41, "disk_free_gb": 70.2, "cpu_pct": 7, "mem_used_pct": 55, "uptime_hours": 3,
           "last_patch_days": 4, "pending_reboot": False, "spooler": "Running", "critical_events_24h": 0, "dns_ok": True}


def _enrol(desk, hostname="LT-101", owner="E1001") -> str:
    page = desk.post("/devices", data={"hostname": hostname, "owner": owner}).text
    return re.search(r"\$t='(mda_[^']+)'", page).group(1)


def _agent(token, answers, stop):
    """A fake endpoint agent: heartbeat, then poll jobs and answer them like the PowerShell agent would."""
    from app.main import app
    c = TestClient(app)
    h = {"Authorization": f"Bearer {token}"}
    c.post("/agent/heartbeat", json={"version": "1.0.0", "os": "Windows Server 2022", "metrics": METRICS}, headers=h)
    while not stop.is_set():
        for job in c.get("/agent/jobs?wait=1", headers=h).json()["jobs"]:
            ok, out = answers.get(job["script"], (False, "not allow-listed on this device"))
            c.post(f"/agent/jobs/{job['id']}/result", json={"ok": ok, "output": out}, headers=h)


def test_enrolment_heartbeat_and_auth(desk):
    from app.main import app
    token = _enrol(desk)
    c = TestClient(app)
    assert c.post("/agent/heartbeat", json={}, headers={"Authorization": "Bearer nope"}).status_code == 401
    r = c.post("/agent/heartbeat", json={"version": "1.0.0", "os": "Windows Server 2022", "metrics": METRICS},
               headers={"Authorization": f"Bearer {token}"})
    assert r.json()["hostname"] == "LT-101" and r.json()["agent_version"] == agent_api.agent_version()     # agents update themselves to this
    st = systems.device("LT-101")
    assert st["real"] and st["online"] and st["disk_used_pct"] == 41 and st["os_patch_days"] == 4
    assert systems.device_owner("LT-101")[0] == "E1001"
    assert systems.get_user("E1001")["device"] == "LT-101"
    page = desk.get("/settings?tab=connectors").text
    assert "Endpoint agents" in page and "LT-101" in page and "online" in page and token not in page
    assert "muse-agent" in c.get("/agent/muse-agent.ps1").text and "Invoke-Allowed" in c.get("/agent/muse-agent.ps1").text


def test_agent_tool_runs_the_script_on_the_real_machine(desk):
    token = _enrol(desk)
    stop = threading.Event()
    th = threading.Thread(target=_agent, args=(token, {"printer-fix": (True, "Print spooler restarted (Running); 3 stuck job file(s) removed.")}, stop), daemon=True)
    th.start()
    for _ in range(50):
        if systems.device("LT-101") and systems.device("LT-101")["online"]:
            break
        time.sleep(0.1)
    try:
        with session() as db:
            t = Ticket(requester="+15551110001", title="printer")
            db.add(t)
            db.flush()
            ctx = Ctx(db=db, ticket=t, agent="L1")
            r = run_tool(ctx, "run_endpoint_script", {"hostname": "LT-101", "script": "printer-fix"})
            assert r["ok"] and r["real_device"] and "3 stuck job" in r["output"]
            assert run_tool(ctx, "run_endpoint_script", {"hostname": "LT-101", "script": "format-c"})["ok"] is False
            d = run_tool(Ctx(db=db, ticket=t, agent="L2"), "network_diagnostics", {"hostname": "LT-101"})   # an L2 tool
            assert d.get("real_device"), d
            assert d["disk_used_pct"] == 41 and d["spooler"] == "Running"
            tid = t.id
        with session() as db:
            assert db.query(DeviceJob).one().status == "done"
            assert any("printer-fix: ok" in e.content for e in db.query(Event).filter(Event.ticket_id == tid))
    finally:
        stop.set()
        th.join(timeout=5)


def test_offline_device_is_reported_not_waited_for(desk, monkeypatch):
    _enrol(desk)                                   # enrolled but never sent a heartbeat
    r = endpoints.run("LT-101", "dns-reset", None, "L1", wait=1)
    assert r["ok"] is False and "offline" in r["error"]
    # hostnames without an agent keep the simulated inventory
    assert systems.device("LT-102")["disk_used_pct"] == 97 and not systems.device("LT-102").get("real")


def test_live_metrics_raise_proactive_findings(desk):
    from app.main import app
    from app.proactive import findings
    token = _enrol(desk, "LT-201", "E1002")
    TestClient(app).post("/agent/heartbeat", json={"metrics": {**METRICS, "disk_used_pct": 95, "spooler": "Stopped"}},
                         headers={"Authorization": f"Bearer {token}"})
    kinds = {(f["hostname"], f["kind"]) for f in findings()}
    assert ("LT-201", "disk") in kinds and ("LT-201", "service") in kinds


def test_late_device_result_resumes_the_ticket(desk, monkeypatch):
    """The agent stopped waiting; when the machine answers, the ticket continues and the employee hears back."""
    from app import endpoints as ep
    from app.db import Outbound
    from app.llm import Reply, ScriptedLLM, set_llm
    from app.main import app
    token = _enrol(desk)
    h = {"Authorization": f"Bearer {token}"}
    c = TestClient(app)
    c.post("/agent/heartbeat", json={"metrics": METRICS}, headers=h)
    with session() as db:
        t = Ticket(requester="+15551110001", title="slow laptop")
        db.add(t)
        db.flush()
        tid = t.id
    r = ep.run("LT-101", "perf-report", tid, "L1", wait=1)            # nobody answers within the wait
    assert r["status"] == "queued"
    set_llm(ScriptedLLM([Reply(text="Raporu aldım: işlemci %95, Chrome çok kaynak kullanıyor.")]))
    started = []
    monkeypatch.setattr(ep, "resume_with_result", lambda job_id: started.append(job_id))
    job = c.get("/agent/jobs?wait=0", headers=h).json()["jobs"][0]
    c.post(f"/agent/jobs/{job['id']}/result", json={"ok": True, "output": "CPU 95%"}, headers=h)
    time.sleep(0.2)
    assert started == [job["id"]]
    monkeypatch.undo()
    ep.resume_with_result(job["id"])                                   # what the background thread does
    with session() as db:
        out = db.query(Outbound).filter(Outbound.ticket_id == tid).all()
        assert out and "Raporu aldım" in out[-1].text and out[-1].to == "+15551110001"


def test_background_update_finishing_resumes_the_ticket(desk, monkeypatch):
    from app import endpoints as ep
    from app.main import app
    token = _enrol(desk)
    h = {"Authorization": f"Bearer {token}"}
    c = TestClient(app)
    c.post("/agent/heartbeat", json={"metrics": METRICS}, headers=h)
    with session() as db:
        t = Ticket(requester="+15551110001", title="updates")
        db.add(t)
        db.flush()
        tid = t.id
    started = []
    monkeypatch.setattr(ep, "resume_with_result", lambda job_id, text=None: started.append((job_id, text)))
    ep.run("LT-101", "install-updates", tid, "L1", wait=0)
    job = c.get("/agent/jobs?wait=0", headers=h).json()["jobs"][0]
    c.post(f"/agent/jobs/{job['id']}/result", json={"ok": True, "output": "Started installing 2 update(s)"}, headers=h)
    started.clear()                                        # the "queued" resume, not what this test is about
    beat = lambda run: c.post("/agent/heartbeat", json={"metrics": {**METRICS, "updates_run": run}}, headers=h)
    beat("started 2026-10-28 17:47:00Z")
    assert started == []                                   # still running
    beat("finished 2026-10-28 18:05:00Z; reboot required: False\nDefender: succeeded")
    time.sleep(0.2)
    assert len(started) == 1 and "Defender: succeeded" in started[0][1]
    beat("finished 2026-10-28 18:05:00Z; reboot required: False\nDefender: succeeded")
    time.sleep(0.2)
    assert len(started) == 1                               # reported once


def test_proactive_finding_messages_the_owner_first(desk):
    from app.db import Outbound
    from app.llm import Reply, ScriptedLLM, set_llm
    from app.main import app
    from app.proactive import run_proactive_checks
    token = _enrol(desk, "LT-301", "E1001")
    TestClient(app).post("/agent/heartbeat", json={"metrics": {**METRICS, "disk_used_pct": 95}},
                         headers={"Authorization": f"Bearer {token}"})
    set_llm(ScriptedLLM([Reply(text="Diskiniz %95 dolu; temizleyeyim mi?")] * 5))
    opened = run_proactive_checks()
    with session() as db:
        msgs = [o for o in db.query(Outbound).all() if o.ticket_id in opened]
        assert any("temizleyeyim" in o.text and o.to == "+15551110001" for o in msgs)
