# Muse ServiceDesk

An autonomous, multi-tier IT service desk built on **Meta Model API** (Muse Spark 1.3) for the
Meta Global AI Developer Hackathon 2026 — Indie Developer track, theme *Build for Your World*.

Employees describe a problem in plain Turkish; four agents (L1–L4) fix it end to end with real
tool calls instead of handing out instructions.

## How it works

- **A tier is its tools.** Each tool lists the tiers allowed to run it (`app/tools.py`); an agent
  only sees and can only execute its own tools. Authority is enforced in code, not in prompts.
- **Agents call each other.** Every agent gets `handoff_<tier>` tools (`app/agents.py`); the model
  decides who to call, so L1 can go straight to L4.
- **Full audit trail.** Every message, tool call, result and handoff is stored on the ticket (`app/db.py`).
- **Simulated company systems** (`app/systems.py`) stand in for directory, VPN and endpoint
  management, behind the same functions a real connector would provide.

## Run

```bash
cp .env.example .env        # add META_API_KEY
uv venv .venv && uv pip install -p .venv -r requirements.txt
.venv/bin/python run.py     # http://localhost:1002/docs
.venv/bin/python -m pytest  # offline tests with a scripted model
```

Needs a MariaDB on 127.0.0.1:3306 (root, empty password); the database is created on start.

## Built during the hackathon

All code in this repository was written during the event, starting Oct 27, 2026.
Before the event I sketched the idea and an early prototype in a separate repository;
no code from it is used here.
