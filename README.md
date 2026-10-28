# Muse ServiceDesk

An autonomous, multi-tier IT service desk built on **Meta Model API** for the Meta Global AI Developer
Hackathon 2026 — Indie Developer track, theme *Build for Your World*.

Employees message the desk over WhatsApp — text, a voice note, a photo of a blue screen, a short clip — or over
the support page (`/support-agent`: a chat for anyone the desk knows, and a one-time-code sign-in to follow their own
tickets) or by e-mail. All three land on the same tickets and agents.
Four agents (**L1–L4**) fix the problem end to end with real tool calls: reset the password, renew the VPN
profile, open the vendor case and the courier pickup, patch the vulnerable server, quarantine the phishing
mail — and close the ticket only after the employee confirms. A camera watcher adds visual compliance
monitoring (credentials on a sticky note, unlocked screens). No human in the loop.

> Built by an IT & Data Infrastructure manager who runs a real service desk in Türkiye — every scenario here
> is a ticket we actually get.

## How it works

```
WhatsApp / API ──► L1  talks to the employee, solves standard problems, hands off ──► ticket store (MariaDB)
                    │   every tier is a tool of the others — no fixed order         full audit trail
                    ├─ L2  device & network diagnosis, malware response (isolate → evidence → scan → release)
                    ├─ L3  servers, SAP/directory permissions, CVE patching with snapshot, verify, rollback
                    └─ L4  vendor service cases, loaners, courier shipments
Camera watcher ──► compliance findings → tickets          Scheduler ──► reminders, follow-ups, proactive checks
```

* **A tier is its tools.** Every tool declares which tiers may run it (`app/tools.py`); an agent only sees
  and can only execute its own. Authority lives in code, not in prompts.
* **Agents find each other.** Each agent gets `handoff_<tier>` tools and the model picks the target
  (`app/agents.py`). L1 can go straight to L4; L2 can pull in L3.
* **Knowledge base + learning loop.** SOPs live in the database: one pool per tier plus a `shared` pool every
  agent sees (`data/sops/` is only the first seed). Agents search before acting (`search_knowledge` returns the
  full text with a stable key) and must follow only what the text says, citing `[SOP:key]`; when nothing matches
  they say so instead of inventing a procedure. Agents and the Supervisor propose new SOPs as drafts; desk
  users add, edit (versioned) and retire SOPs and publish drafts at `/knowledge`.
* **Reference policies & procedures.** Desk users upload PDF, DOCX, Markdown or text documents (HR/IT policies,
  compliance rules, vendor manuals) tagged as policy / guideline / manual and **strict** or **flexible**. The text
  is split into passages that agents search (`search_references`) before any action with consequences and must
  quote as `[REF:doc#n]`: a strict passage binds the agent even if the employee insists; a flexible one may be
  deviated from with a recorded reason; nothing is ever stated without a passage behind it. The Supervisor
  checks actions against strict rules (`check_compliance`).
* **General, not scripted.** There is no per-scenario code. The scenarios below are acceptance tests over
  the same tools, SOPs and lifecycle; a problem nobody anticipated goes through the same path.
* **Supervisor.** A desk-manager agent runs on a schedule (Settings → Supervisor; manual by default, automatic on a cadence if enabled): clusters open tickets into
  problem records, detects where the tier agents fail (stuck, escalated, SLA breach, tool errors, low ratings),
  researches the gap on the web and writes a draft SOP that an admin publishes at `/knowledge`, or opens a
  `[Supervisor]` ticket asking a human for exactly what is missing (access, purchase, policy, on-site work).
  It also watches SLA: tickets at or past the admin-set at-risk share of a target (Settings → SLA) get the least
  drastic fix that moves the clock — nudge the employee, remind the approver, reassign to another tier, or
  escalate to a human.
* **AI usage accounting.** Every Meta Model API call is recorded with tokens, latency and what it served (ticket,
  chat thread, Supervisor run, report). Desk Chat shows tokens, time and context share under each answer; the
  dashboard has an AI usage card per period (calls, tokens by model, latency, tokens per ticket); each ticket shows
  its AI cost; context windows are set under Settings → Models. `/ai` lists every call with date and time (model,
  what it served, tokens in/out, latency, tool calls, errors), per-day totals, tool performance (calls, failure rate,
  by tier) and a CSV export.
* **Desk Chat.** A conversation with the desk's brain for desk users (`/chat`): what is the desk working on, who
  holds what, what is at SLA risk, what waits for me — with links into the console — and actions with the
  user's authority (assign to me, reply to the employee, change status, approve, run the Supervisor, publish a
  draft), every one audited.
* **Reports.** Daily, weekly and monthly reports (Settings → Reports: recipients, schedule; mailbox under Settings → E-mail): numbers from the
  ticket store, a narrative by Muse Wonder, delivered as HTML e-mail; history and "send test" in the console.
* **Ticket lifecycle.** Identity check before account changes, consent before intrusive scripts, manager
  approval over chat (`ONAY 12`), confirmation before closing, satisfaction rating, scheduled follow-ups,
  incident linking for mass outages, proactive tickets from inventory checks, escalation to a human engineer when no tier can solve it, desk-side close from the dashboard.
* **Connectors (MCP).** Admins register MCP servers under Settings → Connectors — Microsoft 365 / Azure (Graph),
  Cisco Meraki, firewalls, ITSM, anything with an MCP endpoint (HTTP) or a local command (stdio). Credentials are
  stored encrypted; a connector's tools appear to the chosen tiers as `mcp.<name>.<tool>` and every call lands on the
  ticket trail with the same authority rules as built-in tools. Tested end to end against a real stdio MCP server.
* **Simulated enterprise systems** (`app/systems.py`) stand in for Entra/M365, Defender, SAP, the vendor
  portal and the courier, behind the same function shapes a real connector would expose.

### Where Meta's models are used

| Capability | Model | Where |
|---|---|---|
| Reasoning, tool calling, Turkish/English dialogue | Muse Spark 1.3 | all agents (`app/llm.py`) |
| Production change planning (patch + rollback + verify) | Muse Wonder 1.0 | L3 (`heavy=True`) |
| Voice notes → text (spoken numbers handled) | Muse Voice Transcribe | `app/voice.py` |
| Photos, screenshots, video frames | Muse Spark vision | intake (`image_message`), `/video` |
| Locating objects in camera frames (sticky note, monitor) | SAM 3.1 | `app/vision.py`, `app/camera.py` |
| Supervisor: reviews the desk, merges emerging problems, researches gaps on the web and drafts SOPs, asks humans for what is missing | Muse Wonder 1.0 | `app/supervisor.py` |
| Daily / weekly / monthly management reports (narrative over the desk's numbers, e-mailed) | Muse Wonder 1.0 | `app/reports.py` |
| Desk Chat: desk users talk to the desk's brain — status, who holds what, SLA risk — and act with their authority | Muse Wonder 1.0 | `app/chat.py` |

## Scenarios covered (all run against the real models)

1. VPN not connecting — profile renewed via endpoint tools
2. Password reset by voice note — transcription, identity check (employee no + birth year), then strong verification: a one-time code on the employee's other channel (e-mail) and a random phrase they must say in a voice note; both must match before the temporary password is issued
3. Blue-screen photo → L4: warranty, vendor case, loaner laptop, courier label
4. Missing / paid license — standard ones assigned, paid ones through manager approval
5. SAP authorization — L3, role granted after approval, temporary access expiry scheduled
6. Service desk console — Dashboard (numbers: period, tier cards, KPIs, top issues, tier load, handoffs) and Tickets (filters, search, conversation timeline, reply, status, assign, merge into problem); sign-in, desk users, audit log; responsive, system/light/dark theme
7. Vulnerability ("are we affected by php2shell?") — inventory scan, change + rollback plan, patch, verify, evidence
8. Onboarding / 9. Offboarding — account, licenses, groups, sessions, loaner return
10. Mass outage — similar tickets merged into one incident, single broadcast, closed together
11. Software request with manager approval over WhatsApp
12. Short video / screen recording — frames sampled and read by the model
13. Proactive maintenance — disk, patch age, warranty expiry open tickets before users notice
14. Satisfaction rating + self-written SOPs (learning loop)
15. Multilingual — replies in the employee's language
16. Visual compliance — camera frame → credential note located by SAM → owner notified, sessions revoked, re-check
17. Malware response — isolate, collect evidence, scan/quarantine, credential reset, release
18. Phishing triage — verdict, tenant-wide quarantine, sender block, awareness note

## Run

```bash
cp .env.example .env            # database URL, port, time zone — no API keys here
uv venv .venv && uv pip install -p .venv -r requirements.txt
.venv/bin/python run.py         # http://localhost:1002  dashboard · /docs API
.venv/bin/python -m pytest      # 91 offline tests with a scripted model
```

Needs a MariaDB on 127.0.0.1:3306 (root, empty password); the database is created on start.

**First run:** start the server, open the console → `/setup` creates the first admin → Settings → Models: paste the
Meta API key → Settings → Channels: WhatsApp phone number ID, token, verify token. Done; no restart.

**Console sign-in:** on first run the console opens `/setup` to create the first admin; credentials live only in the
`users` table (PBKDF2 hashes). Admins invite support agents or admins at `/users` (they set their own password via a link) and can change anyone's role; everyone
can reset a password via `/forgot` (SMTP optional — without it the reset link is written to the server log).
Employees never sign in to the console; they use WhatsApp, the support page at `/support-agent` (recognised by phone or
e-mail from the directory or the admin's Clients list; a one-time code over WhatsApp/e-mail signs them in to their own
tickets, no password) or e-mail (Settings → Channels: IMAP mailbox; replies go out over
the SMTP settings under Reports, and `[#id]` in the subject keeps a thread on its ticket).

**Secrets:** the Meta API key, WhatsApp token/phone ID/verify token, SMTP, the integration API key and the Tavily
key are entered by admins in the console (Settings → Models / Channels / E-mail) and stored encrypted (Fernet; key
in `data/.secret_key`, git-ignored). Nothing secret lives in `.env`. Changes apply immediately.

**Hardening:** PBKDF2 password hashes, HttpOnly/SameSite (Secure on HTTPS) session cookies, login and reset rate limits,
Origin checks on every form post, `X-API-Key` on the integration endpoints when `API_KEY` is set, and an `audit_log`
table of every desk action (sign-ins, invitations, role changes, assignments, merges, closes).
`ffmpeg` is used for voice-note and video conversion.

**WhatsApp:** expose port 1002 (e.g. `cloudflared tunnel --url http://localhost:1002`), set the webhook to
`https://<tunnel>/webhook` with `WA_VERIFY_TOKEN`, subscribe to `messages`. Not needed to try the desk: the web
page at `http://localhost:1002/support-agent` runs the same agents with no public address or Meta app.

### Test users (simulated directory, `app/systems.py`)

Identity checks ask for employee no + birth year. Say e.g. *"Ayşe Demir, E1001, 1990, TR-LT-101"*.

| Employee no | Name | Birth year | Device | Dept | Manager | Seeded state |
|---|---|---|---|---|---|---|
| E1001 | Ayşe Demir | 1990 | TR-LT-101 | Sales | E1002 | VPN profile expired |
| E1002 | Mehmet Kaya | 1985 | TR-LT-102 | Sales | E1003 | disk 97% full, SAP `SD_DISPLAY` |
| E1003 | Zeynep Arslan | 1994 | TR-LT-103 | Finance | — | SAP `FI_DISPLAY`, `FI_POST` |

Useful seeds: licenses `M365-E3`, `Teams-Phone`, `Visio` are self-service; `Project`, `Adobe-CC`, `AutoCAD` need manager
approval (`ONAY <id>` over chat). SAP roles: `SD_CREATE`, `MM_PURCHASE` go through L3 + approval. Advisory
`CVE-2026-4411` ("php2shell") has vulnerable servers in the CMDB. Endpoint scripts: `vpn-reset`, `dns-reset`,
`printer-fix`, `perf-report`, `driver-rollback`, `disk-cleanup`, `lock-screen`. Cameras: `CAM-SALES-01` (E1001),
`CAM-FIN-01` (E1003). Manager approval requests go to the manager's seeded phone; point one at your own number to
answer them from WhatsApp.

**API without WhatsApp:** `POST /messages {requester, text}` · `POST /voice` · `POST /image` · `POST /video` ·
`POST /camera/{camera_id}` · `POST /approvals/{id}` · `GET /tickets/{id}`.

## Layout

```
run.py              entry point (API + scheduler)
app/agents.py       agent loop, tier definitions, handoffs, approvals
app/tools.py        LLM-callable tools with per-tier authority
app/systems.py      simulated enterprise systems (directory, EDR, mail, CMDB, vendor, courier)
app/knowledge.py    knowledge base (SOPs, drafts)    app/references.py uploaded policies (PDF/DOCX/MD) → passages
app/llm.py          Meta Model API client (+ scripted model for tests)
app/voice.py        Muse Voice Transcribe            app/vision.py   SAM 3.1 + frame sampling
app/camera.py       compliance watcher               app/proactive.py  inventory checks
app/scheduler.py    follow-ups, reminders, outbound  app/whatsapp.py   Cloud API channel
app/supervisor.py   desk-manager agent (problems, research, SOP drafts, human asks)
app/reports.py      daily/weekly/monthly e-mail reports (Muse Wonder narrative)
app/chat.py         Desk Chat: the desk's brain for desk users (status + actions)
app/connectors.py   MCP connectors: real systems as agent tools (Settings → Connectors)
app/sla.py          timing & SLA from the trail      app/auth.py       console sign-in, users, settings, knowledge
app/dashboard.py    manager dashboard                app/main.py       HTTP API
tests/              scenario tests over a scripted model
```

All code in this repository was written during the event, starting Oct 27, 2026.
