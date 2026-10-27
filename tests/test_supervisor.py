import copy

import pytest

from app import knowledge, supervisor, systems
from app.agents import handle_message
from app.db import Outbound, SopDraft, SupervisorRun, Ticket, init_db, session
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
    set_llm(ScriptedLLM([Reply(text="Bakıyorum.")] * len(texts_by_phone)))
    with session() as db:
        for phone, text in texts_by_phone:
            handle_message(db, phone, text)


def test_pre_analysis_finds_clusters_and_failures():
    seed([("+905551110001", "Outlook e-posta açılmıyor"), ("+905551110002", "Outlook e-posta sunucusu hata veriyor"),
          ("+905551110003", "e-posta Outlook bağlanmıyor"), ("+905551110009", "yazıcı kağıt sıkıştırdı")])
    with session() as db:
        t = db.get(Ticket, 4)
        t.status, t.owner, t.resolution = "escalated", "human", "escalated: needs on-site"
        cl = supervisor.emerging_clusters(db, supervisor.config())
        sg = supervisor.failure_signals(db, supervisor.config())
    assert len(cl) == 1 and sorted(cl[0]["ticket_ids"]) == [1, 2, 3] and cl[0]["requesters"] == 3
    assert any(s["ticket_id"] == 4 and any("escalated" in x for x in s["signals"]) for s in sg)


def test_supervisor_merges_a_problem_drafts_a_sop_and_asks_a_human(monkeypatch):
    seed([("+905551110001", "Outlook e-posta açılmıyor"), ("+905551110002", "Outlook e-posta sunucusu hata veriyor"),
          ("+905551110003", "e-posta Outlook bağlanmıyor"), ("+905551110009", "projektör lambası yanmıyor")])
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
        d = db.query(SopDraft).one()
        assert d.status == "pending" and d.tier == "L1"
        h = db.query(Ticket).filter(Ticket.title.like("[Supervisor]%")).one()
        assert h.status == "escalated" and h.owner == "human" and "projektör" in h.title
        assert db.query(SupervisorRun).count() == 1


def test_draft_publish_writes_the_sop_and_auto_publish_setting():
    with session() as db:
        d = SopDraft(tier="L2", title="Wi-Fi roaming drops", content="Steps…", sources=["https://x"], reason="gap")
        db.add(d)
        db.flush()
        sop_id = supervisor.publish_draft(db, d, by="Admin")
        assert d.status == "published" and sop_id
    assert "Wi-Fi roaming drops" in knowledge.titles("L2")
    put_setting("supervisor", {**supervisor.DEFAULT_SUPERVISOR, "auto_publish_sops": True})
    from app.tools import Ctx
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
