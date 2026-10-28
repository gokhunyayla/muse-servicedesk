import copy

import pytest

from app import knowledge, supervisor, systems
from app.agents import handle_message
from app.db import Outbound, Sop, SupervisorRun, Ticket, init_db, session
from app.llm import Reply, ScriptedLLM, ToolCall, set_llm
from app.sla import put_setting


@pytest.fixture(autouse=True)
def fresh_state(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setattr(knowledge, "SOP_DIR", tmp_path / "sops")
    devices = copy.deepcopy(systems.DEVICES)
    yield
    systems.DEVICES.clear()
    systems.DEVICES.update(devices)


def call(_name, **args):
    return ToolCall(id=f"c-{_name}", name=_name, arguments=args)


def seed(texts_by_phone):
    """Reports spread over hours: intake sees nothing unusual (it links only bursts within minutes),
    so the pattern is the Supervisor's to find."""
    from datetime import timedelta
    set_llm(ScriptedLLM([Reply(text="Bakıyorum.")] * len(texts_by_phone)))
    with session() as db:
        for i, (phone, text) in enumerate(texts_by_phone):
            t, _ = handle_message(db, phone, text)
            t.created_at = t.created_at - timedelta(hours=len(texts_by_phone) - i)
            for e in t.events:
                e.at = e.at - timedelta(hours=len(texts_by_phone) - i)
            db.flush()


def test_pre_analysis_finds_clusters_and_failures():
    seed([("+15551110001", "Outlook e-posta açılmıyor"), ("+15551110002", "Outlook e-posta sunucusu hata veriyor"),
          ("+15551110003", "e-posta Outlook bağlanmıyor"), ("+15551110009", "yazıcı kağıt sıkıştırdı")])
    with session() as db:
        t = db.get(Ticket, 4)
        t.status, t.owner, t.resolution = "escalated", "human", "escalated: needs on-site"
        cl = supervisor.emerging_clusters(db, supervisor.config())
        sg = supervisor.failure_signals(db, supervisor.config())
    assert len(cl) == 1 and sorted(cl[0]["ticket_ids"]) == [1, 2, 3] and cl[0]["requesters"] == 3
    assert any(s["ticket_id"] == 4 and any("escalated" in x for x in s["signals"]) for s in sg)


def test_supervisor_merges_a_problem_drafts_a_sop_and_asks_a_human(monkeypatch):
    seed([("+15551110001", "Outlook e-posta açılmıyor"), ("+15551110002", "Outlook e-posta sunucusu hata veriyor"),
          ("+15551110003", "e-posta Outlook bağlanmıyor"), ("+15551110009", "projektör lambası yanmıyor")])
    monkeypatch.setattr(supervisor, "search_web", lambda q, limit=6: [{"title": "Fix Outlook", "url": "https://vendor.example/kb", "snippet": "…"}])
    monkeypatch.setattr(supervisor.httpx, "get", lambda *a, **k: type("R", (), {"text": "<html><body>Repair the OST file via Control Panel.</body></html>"})())
    llm = ScriptedLLM([
        Reply(tool_calls=[call("create_problem", ticket_ids=[1, 2, 3], title="Exchange outage", update="Biliyoruz, çalışıyoruz.")]),
        Reply(tool_calls=[call("read_ticket", ticket_id=4)]),
        Reply(tool_calls=[call("web_search", query="Outlook cannot connect Exchange fix")]),
        Reply(tool_calls=[call("fetch_page", url="https://vendor.example/kb")]),
        Reply(tool_calls=[call("propose_sop", tier="L1", title="Outlook bağlantı sorunu", content="1. OST onar\n2. Profil yenile",
                               sources=["https://vendor.example/kb"], reason="agents had no procedure", ticket_ids=[1])]),
        Reply(tool_calls=[call("open_human_ticket", need="Toplantı odası projektörü için yerinde lamba değişimi",
                               why="Uzaktan yapılamaz", ticket_ids=[4])]),
        Reply(text="Merged 3 tickets into problem #1, drafted a SOP for L1, asked a human for the projector."),
    ])
    set_llm(llm)
    r = supervisor.run("manual")
    assert r.error == "" and "Merged" in r.summary
    assert [a["tool"] for a in r.actions] == ["create_problem", "read_ticket", "web_search", "fetch_page", "propose_sop", "open_human_ticket"]
    assert all(a["ok"] for a in r.actions)
    assert llm.heavy_calls == 7                    # the Supervisor thinks with the heavy model
    with session() as db:
        assert db.get(Ticket, 1).title == "[INCIDENT] Exchange outage" and db.get(Ticket, 2).incident_id == 1
        assert db.query(Outbound).count() == 3
        d = db.query(Sop).filter(Sop.source == "supervisor").one()
        assert d.status == "draft" and d.tier == "L1" and d.sources == ["https://vendor.example/kb"]
        h = db.query(Ticket).filter(Ticket.title.like("[Supervisor]%")).one()
        assert h.status == "escalated" and h.owner == "human" and "projektör" in h.title
        assert db.query(SupervisorRun).count() == 1


def test_draft_publish_and_auto_publish_setting():
    from app.tools import Ctx
    with session() as db:
        ctx = Ctx(db=db, ticket=None, agent="SUP")
        res = supervisor.propose_sop(ctx, "L2", "Wi-Fi roaming drops", "Steps…", ["https://x"], "gap")
        assert res["status"] == "draft"
        assert "Wi-Fi roaming drops" not in knowledge.titles("L2")          # drafts are invisible to agents
        sop = knowledge.get(res["sop_id"], db=db)
        supervisor.publish_draft(db, sop, by="Admin")
    assert "Wi-Fi roaming drops" in knowledge.titles("L2")
    put_setting("supervisor", {**supervisor.DEFAULT_SUPERVISOR, "auto_publish_sops": True})
    with session() as db:
        res = supervisor.propose_sop(Ctx(db=db, ticket=None, agent="SUP"), "L3", "Patch rollback", "…", [], "gap")
        assert res["status"] == "published"


def test_nothing_to_do_and_cadence():
    r = supervisor.run("manual")
    assert "Nothing to do" in r.summary and r.actions == []
    assert supervisor.due(r) is False                       # just ran
    assert supervisor.due(None) is False                    # default mode is manual: never due on its own
    put_setting("supervisor", {**supervisor.DEFAULT_SUPERVISOR, "mode": "auto"})
    assert supervisor.due(None) is True
    st = supervisor.stats()
    assert st["runs"] == 1 and st["problems"] == 0


def test_sla_at_risk_detection_and_supervisor_actions():
    from datetime import timedelta
    from app.db import Approval, Event, Outbound
    from app.sla import at_risk
    from app.tools import Ctx
    # ticket 1: L1 holding it for 25 of 30 minutes → at risk (default 80%); ticket 2: waiting for approval
    seed([("+15551110001", "VPN bağlanmıyor"), ("+15551110002", "Project lisansı lazım")])
    with session() as db:
        from app.db import _now
        t1 = db.get(Ticket, 1)
        t1.created_at = _now() - timedelta(minutes=25)
        for e in db.query(Event).filter_by(ticket_id=1):
            e.at = t1.created_at + timedelta(seconds=5)
        t2 = db.get(Ticket, 2)
        t2.status, t2.created_at = "waiting_approval", _now() - timedelta(minutes=2)       # fresh: not at risk
        for e in db.query(Event).filter_by(ticket_id=2):
            e.at = t2.created_at + timedelta(seconds=5)
        db.add(Approval(ticket_id=2, approver="E1002", request="Project lisansı"))
    with session() as db:
        risks = at_risk(db)
        assert risks and risks[0]["ticket_id"] == 1 and risks[0]["metric"] == "L1" and 80 <= risks[0]["pct"] < 100 and not risks[0]["breached"]
        sig = supervisor.failure_signals(db, supervisor.config())
        assert any("SLA at risk" in x for s in sig for x in s["signals"] if s["ticket_id"] == 1)
    put_setting("sla", {**__import__("app.sla", fromlist=["DEFAULT_SLA"]).DEFAULT_SLA, "at_risk_pct": 95})
    with session() as db:
        assert at_risk(db) == []                                   # threshold raised by the admin → not at risk yet
    put_setting("sla", __import__("app.sla", fromlist=["DEFAULT_SLA"]).DEFAULT_SLA)
    # the Supervisor's levers
    set_llm(ScriptedLLM([Reply(text="L2 devraldım, teşhis başlıyor.")]))
    with session() as db:
        ctx = Ctx(db=db, ticket=None, agent="SUP")
        assert supervisor.reassign_ticket(ctx, 1, "L2", "Run network_diagnostics on LT-101")["owner"] == "L2"
        assert db.get(Ticket, 1).owner == "L2"
        assert supervisor.remind_approver(ctx, 2)["approver"] == "E1002"
        assert supervisor.nudge_employee(ctx, 1, "Bağlantıyı tekrar dener misiniz?")["ok"]
        assert supervisor.bump_to_human(ctx, 1, "needs on-site")["status"] == "escalated"
        assert db.get(Ticket, 1).owner == "human"
        outs = [o.to for o in db.query(Outbound)]
        assert "+15551110002" in outs and "+15551110001" in outs
