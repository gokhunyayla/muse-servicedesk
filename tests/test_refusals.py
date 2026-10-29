"""Verification inside a tool refuses a step with a reason; the model may fix it, but the same tool refused
three times on one ticket hands the ticket to a person instead of looping."""
import pytest

from app import vault
from app.agents import handle_message
from app.db import Ticket, init_db, session
from app.llm import Reply, ScriptedLLM, ToolCall, set_llm


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setattr(vault, "KEY_FILE", tmp_path / "k")


def reset(i):
    return Reply(tool_calls=[ToolCall(id=f"c{i}", name="reset_password", arguments={"employee_no": "E1001"})])


def test_three_refusals_of_one_tool_escalate_to_a_person():
    # identity never verified: reset_password is refused every time; the third refusal ends it
    set_llm(ScriptedLLM([reset(1), reset(2), reset(3), Reply(text="should not be reached")]))
    with session() as db:
        t, answer = handle_message(db, "+15551110001", "reset my password")
        results = [e for e in t.events if e.kind == "tool_result" and e.content == "reset_password"]
        assert len(results) == 3 and all(r.data.get("ok") is False for r in results)
        assert t.status == "escalated" and t.owner == "human"
        assert "refused 3 times" in t.resolution
    assert "support engineer" in answer or "destek mühendisi" in answer


def test_two_refusals_then_success_is_normal_work():
    set_llm(ScriptedLLM([reset(1), reset(2),
                         Reply(tool_calls=[ToolCall(id="v", name="verify_identity", arguments={"employee_no": "E1001", "birth_year": 1990})]),
                         Reply(text="Verified; here is your temporary password.")]))
    with session() as db:
        t, answer = handle_message(db, "+15551110001", "reset my password")
        assert t.status != "escalated" and "Verified" in answer
