"""End-to-end: a real MCP server (FastMCP over stdio, spawned as a subprocess) registered as a connector,
its tools exposed to the agents, called by an agent through the normal tool path."""
import sys
import textwrap

import pytest

from app import connectors, vault
from app.agents import handle_message
from app.db import Ticket, init_db, session
from app.llm import Reply, ScriptedLLM, ToolCall, set_llm
from app.tools import REGISTRY, Ctx, run_tool, tools_for

SERVER = textwrap.dedent('''
    import os
    from mcp.server.mcpserver import MCPServer
    mcp = MCPServer("firewall-demo")

    @mcp.tool()
    def list_blocked(ip: str) -> str:
        """Is this IP blocked on the perimeter firewall?"""
        return f"{ip}: blocked=True rule=BLOCK-MALWARE key={os.environ.get('FW_KEY', 'none')}"

    @mcp.tool()
    def unblock(ip: str, reason: str = "") -> dict:
        """Remove a block for an IP."""
        return {"ip": ip, "unblocked": True, "reason": reason}

    mcp.run()
''')


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setattr(vault, "KEY_FILE", tmp_path / "k")
    (tmp_path / "server.py").write_text(SERVER)
    for k in [k for k in REGISTRY if k.startswith("mcp.")]:
        del REGISTRY[k]
    yield


def _server_cmd(tmp_path):
    return f"{sys.executable} {tmp_path / 'server.py'}"


def test_connector_registered_tested_and_used_by_an_agent(desk, tmp_path):
    r = desk.post("/connectors", json={"name": "fw", "label": "Perimeter firewall", "kind": "stdio",
                                       "command": _server_cmd(tmp_path), "secrets": {"FW_KEY": "k-42"},
                                       "tiers": ["L2", "L3"]}).json()
    assert r["ok"] and sorted(r["tools"]) == ["list_blocked", "unblock"]
    names = [t.name for t in tools_for("L3")]
    assert "mcp.fw.list_blocked" in names and "mcp.fw.unblock" in names
    assert "mcp.fw.unblock" not in [t.name for t in tools_for("L1")]           # tier authority still applies
    spec = REGISTRY["mcp.fw.unblock"].spec()["function"]
    assert spec["parameters"]["required"] == ["ip"] and "reason" in spec["parameters"]["properties"]
    # an L3 agent calls it through the normal tool path; the env secret reached the server
    with session() as db:
        out = run_tool(Ctx(db=db, ticket=None, agent="L3"), "mcp.fw.list_blocked", {"ip": "10.1.2.3"})
    assert out["ok"] and "blocked=True" in out["text"] and "key=k-42" in out["text"]
    with session() as db:
        out = run_tool(Ctx(db=db, ticket=None, agent="L1"), "mcp.fw.list_blocked", {"ip": "10.1.2.3"})
    assert out["ok"] is False and "not allowed" in out["error"]
    # the listing never echoes secrets; the page shows the connector
    lst = desk.get("/connectors").json()
    assert lst[0]["has_secrets"] and "k-42" not in str(lst) and lst[0]["last_test_ok"] is True
    page = desk.get("/settings?tab=connectors").text
    assert "Perimeter firewall" in page and "k-42" not in page


def test_agent_run_uses_connector_tool_and_logs_it(desk, tmp_path):
    desk.post("/connectors", json={"name": "fw", "label": "FW", "kind": "stdio", "command": _server_cmd(tmp_path), "tiers": ["L1"]})
    set_llm(ScriptedLLM([Reply(tool_calls=[ToolCall("c1", "mcp.fw.unblock", {"ip": "10.9.9.9", "reason": "false positive"})]),
                         Reply(text="IP serbest bırakıldı.")]))
    with session() as db:
        t, answer = handle_message(db, "+15551110001", "10.9.9.9 adresi firewall'da engelli, açar mısınız")
        res = [e for e in t.events if e.kind == "tool_result" and e.content == "mcp.fw.unblock"][0]
    assert res.data["ok"] and res.data["data"]["unblocked"] is True and "serbest" in answer


def test_disable_and_failure_are_reported(desk, tmp_path):
    cid = desk.post("/connectors", json={"name": "fw", "label": "FW", "kind": "stdio", "command": _server_cmd(tmp_path), "tiers": ["L2"]}).json()["id"]
    desk.put(f"/connectors/{cid}", json={"name": "fw", "label": "FW", "kind": "stdio", "command": _server_cmd(tmp_path), "tiers": ["L2"], "enabled": False})
    assert "mcp.fw.unblock" not in REGISTRY
    bad = desk.post("/connectors", json={"name": "broken", "label": "Broken", "kind": "stdio", "command": f"{sys.executable} -c 'import sys; sys.exit(3)'", "tiers": ["L2"]}).json()
    assert bad["ok"] is False and bad["error"]
    assert desk.delete(f"/connectors/{cid}").json()["deleted"]
    assert desk.get("/connectors/presets").json()["m365"]["label"].startswith("Microsoft")
