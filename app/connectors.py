"""Connectors: MCP servers the desk's agents can use as tools — Microsoft 365 / Azure, Cisco, a firewall,
an ITSM, anything that speaks MCP. Admins register them under Settings → Connectors; each connector's tools
appear to the allowed tiers as `mcp.<connector>.<tool>` and every call lands on the ticket trail like any
other tool call. Credentials are stored encrypted; `.env` is not involved."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import threading
from contextlib import nullcontext

from app.db import Connector, _now, session
from app.tools import REGISTRY, Ctx, Tool

logger = logging.getLogger(__name__)
TIMEOUT = 40

PRESETS = {   # templates admins start from; the actual MCP server is whatever the vendor/community ships
    "custom": {"label": "Custom MCP server", "kind": "http", "url": "https://", "hint": "Any MCP endpoint (streamable HTTP) or a local command (stdio)."},
    "m365": {"label": "Microsoft 365 / Azure (Graph)", "kind": "stdio", "command": "npx -y @softeria/ms-365-mcp-server",
             "hint": "Users, groups, licenses, devices via Microsoft Graph. Needs an Entra app registration; put the client secret in Secrets as env."},
    "meraki": {"label": "Cisco Meraki", "kind": "stdio", "command": "npx -y meraki-mcp-server",
               "hint": "Networks, clients, switch ports, wireless. Env: MERAKI_API_KEY."},
    "fortigate": {"label": "FortiGate firewall", "kind": "http", "url": "https://fortigate.example/mcp",
                  "hint": "Policies, addresses, VPN tunnels. Header: Authorization: Bearer <token>."},
    "paloalto": {"label": "Palo Alto firewall", "kind": "http", "url": "https://panorama.example/mcp",
                 "hint": "Security rules, objects, logs. Header: X-PAN-KEY."},
    "itsm": {"label": "ITSM (ServiceNow / Jira)", "kind": "http", "url": "https://instance.service-now.com/mcp",
             "hint": "Mirror tickets, CMDB lookups, change requests."},
}


def _db(db):
    return nullcontext(db) if db is not None else session()


def _fernet():
    from app.vault import _fernet
    return _fernet()


def encrypt_secrets(d: dict) -> str:
    return _fernet().encrypt(json.dumps(d).encode()).decode() if d else ""


def decrypt_secrets(blob: str) -> dict:
    if not blob:
        return {}
    try:
        return json.loads(_fernet().decrypt(blob.encode()).decode())
    except Exception:
        return {}


# ---- talking to a server (sync wrappers over the async SDK) ------------------------------------

def _unwrap(e: BaseException) -> BaseException:
    """ExceptionGroup → the first real exception, so admins see 'Connection refused', not 'TaskGroup'."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return e


def _run(coro):
    """Run an MCP coroutine from sync code, whether or not an event loop is already running here."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            return asyncio.run(coro)
        except BaseException as e:
            raise _unwrap(e) from None
    out: dict = {}

    def runner():
        try:
            out["v"] = asyncio.run(coro)
        except BaseException as e:   # surfaced below
            out["e"] = e
    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(TIMEOUT + 5)
    if "e" in out:
        raise _unwrap(out["e"]) from None
    return out.get("v")


async def _with_session(c: Connector, fn):
    from mcp import ClientSession
    sec = decrypt_secrets(c.secrets)
    if c.kind == "stdio":
        import os
        from mcp.client.stdio import StdioServerParameters, stdio_client
        parts = shlex.split(c.command)
        if not parts:
            raise ValueError("stdio connector has no command")
        params = StdioServerParameters(command=parts[0], args=parts[1:], env={**os.environ, **{k: str(v) for k, v in sec.items()}})
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await asyncio.wait_for(s.initialize(), TIMEOUT)
                return await fn(s)
    import httpx
    from mcp.client.streamable_http import streamable_http_client
    headers = {k: str(v) for k, v in sec.items()}
    async with httpx.AsyncClient(headers=headers, timeout=TIMEOUT) as hc:
        async with streamable_http_client(c.url, http_client=hc) as streams:
            r, w = streams[0], streams[1]
            async with ClientSession(r, w) as s:
                await asyncio.wait_for(s.initialize(), TIMEOUT)
                return await fn(s)


def list_tools(c: Connector) -> list[dict]:
    async def fn(s):
        res = await s.list_tools()
        return [{"name": t.name, "description": (t.description or "")[:500],
                 "schema": getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {}} for t in res.tools]
    return _run(_with_session(c, fn))


def call(c: Connector, tool: str, args: dict) -> dict:
    async def fn(s):
        res = await asyncio.wait_for(s.call_tool(tool, args or {}), TIMEOUT)
        texts = []
        for item in getattr(res, "content", []) or []:
            if getattr(item, "type", "") == "text":
                texts.append(item.text)
            else:
                texts.append(json.dumps(getattr(item, "model_dump", lambda: str(item))(), default=str)[:2000])
        structured = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
        is_error = getattr(res, "is_error", None)
        if is_error is None:
            is_error = getattr(res, "isError", False)
        text = "\n".join(texts)
        if structured is None and text.strip().startswith(("{", "[")):
            try:
                structured = json.loads(text)
            except ValueError:
                pass
        if isinstance(structured, dict) and set(structured) == {"result"}:
            structured = structured["result"]          # MCP wraps plain returns as {"result": ...}
        return {"ok": not is_error, "text": text[:6000], "data": structured}
    return _run(_with_session(c, fn))


# ---- lifecycle -------------------------------------------------------------------------------------

def test(c_id: int) -> dict:
    """Connect, list tools, cache them. Returns what the admin sees."""
    with session() as db:
        c = db.get(Connector, c_id)
        if c is None:
            return {"ok": False, "error": "no such connector"}
        try:
            tools = list_tools(c)
            c.tools, c.last_test_ok, c.last_error, c.last_test_at = tools, True, "", _now()
            db.flush()
            register(c)
            return {"ok": True, "tools": [t["name"] for t in tools]}
        except Exception as e:
            c.last_test_ok, c.last_error, c.last_test_at = False, str(e)[:500], _now()
            return {"ok": False, "error": str(e)[:500]}


def _tool_name(connector: str, tool: str) -> str:
    return f"mcp.{connector}.{re.sub(r'[^A-Za-z0-9_.-]', '_', tool)}"


def register(c: Connector) -> int:
    """Expose a connector's cached tools to its tiers (replacing any older registration)."""
    unregister(c.name)
    if not c.enabled or not c.tools:
        return 0
    n = 0
    for t in c.tools:
        schema = t.get("schema") or {}
        props = dict(schema.get("properties") or {})
        required = set(schema.get("required") or [])
        params = {k: {**(v if isinstance(v, dict) else {"type": "string"}), **({} if k in required else {"optional": True})}
                  for k, v in props.items()}
        name = _tool_name(c.name, t["name"])
        cid, tool_name, label = c.id, t["name"], c.label

        def func(ctx: Ctx, _cid=cid, _tool=tool_name, **kwargs):
            with session() as db:
                conn = db.get(Connector, _cid)
            if conn is None or not conn.enabled:
                return {"ok": False, "error": "connector disabled"}
            try:
                return call(conn, _tool, kwargs)
            except Exception as e:
                return {"ok": False, "error": f"{label}: {str(e)[:300]}"}
        func.__name__ = name
        REGISTRY[name] = Tool(name, f"[{label}] {t.get('description') or t['name']}", tuple(c.tiers or []), params, func)
        n += 1
    return n


def unregister(connector: str) -> None:
    for k in [k for k in REGISTRY if k.startswith(f"mcp.{connector}.")]:
        del REGISTRY[k]


def register_all() -> int:
    """At startup: expose every enabled connector's cached tools (no network)."""
    n = 0
    try:
        with session() as db:
            for c in db.query(Connector).all():
                n += register(c)
    except Exception:       # tables may not exist yet on a very first run
        pass
    return n


def listing(db=None) -> list[dict]:
    with _db(db) as db:
        return [{"id": c.id, "name": c.name, "label": c.label, "kind": c.kind, "command": c.command, "url": c.url,
                 "has_secrets": bool(c.secrets), "tiers": c.tiers, "enabled": c.enabled, "tools": [t["name"] for t in (c.tools or [])],
                 "last_test_at": c.last_test_at.isoformat() if c.last_test_at else None, "last_test_ok": c.last_test_ok,
                 "last_error": c.last_error} for c in db.query(Connector).order_by(Connector.id)]
