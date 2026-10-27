import pytest

from app import chat  # noqa: F401  (registers DESK tools)
from app.agents import handle_message
from app.db import Approval, Ticket, init_db, session
from app.llm import Reply, ScriptedLLM, ToolCall, set_llm


@pytest.fixture(autouse=True)
def fresh(tmp_path):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")


def call(_name, **args):
    return ToolCall(id=f"c-{_name}", name=_name, arguments=args)


def test_chat_overview_and_links(desk):
    set_llm(ScriptedLLM([Reply(text="Bakıyorum."), Reply(text="Bakıyorum.")]))
    with session() as db:
        handle_message(db, "+905551110001", "vpn bağlanmıyor")
        handle_message(db, "+905551110002", "Project lisansı lazım")
        db.add(Approval(ticket_id=2, approver="E1002", request="Project lisansı"))
    th = desk.post("/chat/threads").json()
    llm = ScriptedLLM([Reply(tool_calls=[call("desk_overview")]),
                       Reply(text="2 open tickets: [#1](/tickets?id=1) VPN (L1) and #2 license (L1). One approval is pending (#1).")])
    set_llm(llm)
    r = desk.post(f"/chat/threads/{th['id']}", json={"text": "What is the desk working on?"}).json()
    assert r["actions"] == [{"tool": "desk_overview", "args": {}, "ok": True}] and "[#1](/tickets?id=1)" in r["answer"]
    assert llm.heavy_calls == 2                                     # the brain thinks with the heavy model
    # the tool saw the real state
    import json
    tool_msg = json.loads(llm.requests[1][-1]["content"])
    assert tool_msg["open_total"] == 2 and tool_msg["pending_approvals"][0]["approver"] == "E1002"
    assert desk.get("/chat/threads").json()[0]["title"] == "What is the desk working on?"
    msgs = desk.get(f"/chat/threads/{th['id']}").json()
    assert [m["role"] for m in msgs] == ["user", "assistant"]


def test_chat_acts_with_the_users_authority(desk):
    set_llm(ScriptedLLM([Reply(text="Bakıyorum.")]))
    with session() as db:
        handle_message(db, "+905551110001", "projektör lambası yanmıyor")
    th = desk.post("/chat/threads").json()
    set_llm(ScriptedLLM([Reply(tool_calls=[call("assign", ticket_id=1, to="me", note="I'll replace the lamp")]),
                         Reply(text="Done — #1 is yours now.")]))
    r = desk.post(f"/chat/threads/{th['id']}", json={"text": "assign #1 to me"}).json()
    assert r["actions"][0]["ok"]
    with session() as db:
        t = db.get(Ticket, 1)
        assert t.status == "escalated" and t.owner == "human"
        assert any("Desk Admin" in e.content for e in t.events if e.kind == "note")
    assert any(a["action"] == "ticket.assign" and "chat" in a["detail"] for a in desk.get("/audit.json").json())
    # approval from the chat continues the employee's ticket through L1
    with session() as db:
        db.add(Approval(ticket_id=1, approver="E1002", request="yeni lamba satın alma"))
    set_llm(ScriptedLLM([Reply(tool_calls=[call("decide_approval", approval_id=1, decision="approved")]),
                         Reply(text="Sipariş onaylandı."),          # L1 continuing the ticket
                         Reply(text="Approved; L1 told the employee.")]))
    r = desk.post(f"/chat/threads/{th['id']}", json={"text": "approve it"}).json()
    assert r["actions"][0]["ok"] and "Approved" in r["answer"]
    with session() as db:
        assert db.get(Approval, 1).decision == "approved"


def test_threads_are_private_and_deletable(desk):
    th = desk.post("/chat/threads").json()
    from fastapi.testclient import TestClient
    from app.auth import hash_password
    from app.db import User
    from app.main import app
    with session() as db:
        db.add(User(email="other@test", name="Other", password_hash=hash_password("other-pass-1"), role="agent"))
    from app import security
    security._hits.clear()
    other = TestClient(app)
    other.post("/login", data={"email": "other@test", "password": "other-pass-1"}, follow_redirects=False)
    assert other.get(f"/chat/threads/{th['id']}").status_code == 404
    assert desk.delete(f"/chat/threads/{th['id']}").json()["deleted"] and desk.get("/chat/threads").json() == []
    assert "Desk Chat" in desk.get("/chat").text
