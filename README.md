# Muse ServiceDesk

An autonomous, multi-tier IT service desk built on **Meta Model API** for the Meta Global AI Developer
Hackathon 2026 — Indie Developer track, theme *Build for Your World*.

Employees message the desk over WhatsApp — text, a voice note, a photo of a blue screen, a short clip, a document —
or over the support page (`/support-agent`) or by e-mail. Four agents (**L1–L4**) fix the problem end to end with
real tool calls on real systems: a Microsoft 365 tenant (Entra ID, mailboxes, licences), a Windows machine in Azure
running the desk's endpoint agent, a mesh VPN. The desk also works when nobody writes in: it watches the machines,
the vulnerability feeds and the antivirus, fixes what it finds, and tells the admins *"I noticed and fixed it"*.
People step in only where the desk's own checks say they must: identity, consent, a manager's approval, a human
engineer for what no tier can do.

> Built by an IT & Data Infrastructure manager who runs a real service desk — every scenario here is a ticket we
> actually get. All code in this repository was written during the event (first commit Oct 27, 2026); nothing
> pre-existed.

## What it does, in one picture

```
WhatsApp / web / e-mail ──► L1  talks to the employee, solves standard problems, hands off ──► tickets (MariaDB)
                             │   every tier is a tool of the others — no fixed order            full audit trail
                             ├─ L2  devices & network: real scripts on the machine, malware, crashes, VPN
                             ├─ L3  servers, directory, security: patches, phishing, permissions
                             └─ L4  vendors, loaners, shipping
Endpoint agent (Windows) ──► heartbeat: health, software, Defender, crashes, VPN ──► proactive tickets
Daily watch ──► CISA KEV + NVD against installed software ──► patch through the agent ──► "noticed and fixed"
Supervisor ──► reviews the desk, merges problems, researches gaps, proposes lessons, asks humans for what is missing
```

## Verifiable by design

The model does the reasoning; checks that do not trust the model decide whether the work may move on. They sit
**inside the tools**, at the moments that matter, so neither a confident model nor a bug in the loop can skip them.

| Principle | Where it lives |
|---|---|
| **The model chooses every tool call**; code runs it and keeps the record | `app/agents.py` — one loop for every tier, no scenario code; every call and result on the ticket trail |
| **Authority is code, not prompt** | `app/tools.py` — each tool names the tiers that may run it; an agent only sees its own |
| **Checks inside the tools, refusal with the reason** | identity before a reset (`verify_identity` binds the sender's number to the employee), consent before intrusive scripts, an allow-list of scripts the endpoint agent will run, validated arguments |
| **A refused step may be fixed, not repeated forever** | the same tool refused 3× on a ticket hands it to a person (`MAX_REFUSALS`); a step budget does the same |
| **Escalation pauses the run until a person acts** | manager approvals over WhatsApp (`APPROVE 12`), human escalation, device jobs that report back by themselves |
| **Nothing is stated without a source** | SOPs cited as `[SOP:key]`, uploaded policies as `[REF:doc#n]`; no passage, no rule |
| **Sign-off before the final step** | the employee confirms before a ticket closes; an admin publishes SOP drafts and approves lessons |
| **Learning cannot rewrite the rules** | core SOPs are immutable to the system; a *lesson* passes an automatic safety check (never weakens identity, approval, consent or protections; only the tier's own tools) and an admin, and can be suspended at any time (`app/knowledge.py`) |
| **Every action reversible** | patches keep a backup and roll back; removed mail goes to Deleted Items and comes back; a disabled driver is kept, not deleted; enrolled accounts expire |
| **Secrets never readable** | passwords go to the person over chat, never by e-mail, never stored in clear; API keys live encrypted in the console's vault, not in `.env` or the repo |

## The 17 cases — what is real

Everything below ran end to end on the live installation (Microsoft 365 tenant, Azure VM `LT-101` with the endpoint
agent, Tailscale VPN, a WhatsApp Business number).

| # | Case | Real on the live desk |
|---|---|---|
| 1 | VPN not connecting | the agent reads the Windows/Tailscale VPN state, drops and redials the tunnel, tests an internal address |
| 2 | Password reset by voice note | Muse Voice Transcribe → identity bound to the sender → one-time code on the other channel → Entra password reset |
| 3 | Blue screen | the desk notices the crash itself (WER event in the heartbeat), reads stop code, recent drivers and disk health, repairs the system (DISM + SFC in the background) or disables the offending driver; hardware → manager approval + a human task |
| 4 | Licence request | Entra licence assignment; paid ones through manager approval over WhatsApp |
| 6 | Desk console | dashboard, tickets, knowledge base, AI usage, audit log, settings |
| 7 | Vulnerability patching | **daily**: CISA KEV + NVD against the machines' installed software; affected app patched from the vendor's own feed (checksum verified, backup first), version read back, admins told; one ticket per app |
| 8 | Onboarding from a CV | HR sends a CV on any channel; the manager answers on theirs; Entra account, groups, licence, mailbox, welcome mail |
| 9 | Offboarding | scheduled for the last day: account disabled, sessions revoked, groups and licences removed |
| 10 | Mass outage | similar tickets merged into one incident, one broadcast, closed together |
| 11 | Software with manager approval | approval request and decision over WhatsApp |
| 12 | Video / screen recording | frames sampled and read by Muse Spark |
| 13 | Proactive maintenance | disk, patch age, stopped services, critical events from the real machine open tickets before anyone writes |
| 14 | Rating + learning | satisfaction ratings; agents and the Supervisor propose SOPs and lessons (see above) |
| 15 | Multilingual | the employee's language, detected per message |
| 16 | Camera compliance | SAM 3.1 locates the sticky note; owner notified, sessions revoked |
| 17 | Malware | Microsoft Defender detections arrive with the heartbeat (minutes); threat removed, signatures updated, quick scan, admins told |
| 18 | Phishing | the mail is found in every company mailbox, headers (SPF/DKIM/DMARC), look-alike domains and link targets analysed; every copy moved to Deleted Items (recoverable), recipients warned, sender blocked |

Still simulated behind the same function shapes: the vendor portal, courier shipments and loaner stock (case 3/8/9
logistics), and the directory when no Entra tenant is connected (`app/systems.py`).

### Autonomous: nobody has to write in

* **Vulnerability watch** (`app/vulnwatch.py`): once a day, paced to NVD's limits; findings become L3 tickets; the
  endpoint agent patches (`app-update`, PHP today), verifies, keeps a backup; the admins get *"noticed and fixed"*
  or *"needs attention"* in the report language.
* **Malware watch** (`app/malwatch.py`): every heartbeat; `malware-clean` through Defender.
* **Crash watch** (`app/crashwatch.py`): blue screens the machine recorded; one proactive ticket per device.
* **Proactive checks** (`app/proactive.py`): disk, patch age, services, events, warranty.
* **Background work reports back by itself**: OS updates and system repair run as one-off tasks on the machine and
  resume the ticket when they finish.

### The endpoint agent

`agent/muse-agent.ps1` enrols a Windows machine with a token shown once in the console, runs as SYSTEM, sends a
heartbeat every minute (health, installed software, Defender status and detections, crashes, disk health, VPN state),
long-polls for jobs and runs **only allow-listed scripts**: `dns-reset`, `vpn-reset`, `printer-fix`, `perf-report`,
`disk-cleanup`, `lock-screen`, `patch-status`, `install-updates`, `system-repair`, `crash-report`, `malware-clean`,
`driver-rollback`, `app-update` / `app-rollback`. It updates itself when the desk ships a newer version. Admin-only
drills (`crash-drill` with Microsoft's NotMyFault, `defender-test` with the EICAR file, `vpn-drill`) exist so the
autonomous paths can be shown on demand; the agents never see them.

### Where Meta's models are used

| Capability | Model | Where |
|---|---|---|
| Reasoning, tool calling, dialogue in the employee's language | Muse Spark 1.3 | all tier agents (`app/llm.py`) |
| Production changes (patch + rollback + verify), Supervisor, reports, Desk Chat | Muse Wonder 1.0 | `heavy=True` paths |
| Voice notes → text (spoken numbers handled) | Muse Voice Transcribe | `app/voice.py` |
| Photos, screenshots, video frames, documents (CVs) | Muse Spark vision | intake, `/video`, `app/attachments.py` |
| Locating objects in camera frames | SAM 3.1 | `app/vision.py`, `app/camera.py` |
| Supervisor: reviews the desk, merges problems, researches gaps on the web, drafts SOPs, proposes lessons, asks humans | Muse Wonder 1.0 | `app/supervisor.py` |

Every call is accounted (`/ai`): tokens, latency, what it served, tool failure rates by tier, CSV export.

## Try it

* **WhatsApp**: the desk's number and an invitation code are in the submission notes. Someone the desk does not
  know can **enrol by chatting**: the code, a name, an e-mail → a real Entra account, username and first password
  over WhatsApp; then every case above with their own account ("I forgot my password", "my printer is broken",
  forward a suspicious mail…). Enrolled accounts are capped, grouped, and expire on a set day (Settings → Channels).
* **Web**: `/support-agent` — the same chat; a one-time code over WhatsApp or e-mail signs an employee in to their
  own tickets.
* **Console**: `/` dashboard, `/tickets`, `/knowledge` (SOPs, lessons awaiting review), `/ai`, Settings.

## Run your own

```bash
cp .env.example .env            # database URL, port, time zone — no API keys here
uv venv .venv && uv pip install -p .venv -r requirements.txt
.venv/bin/python run.py         # http://localhost:1002  console · /docs API
.venv/bin/python -m pytest      # 185 offline tests over a scripted model
```

Needs MariaDB (or set a SQLite URL in `.env`). First run: `/setup` creates the admin → Settings → Models: Meta API key →
Settings → Channels: WhatsApp (account ID + token, then **Connect WhatsApp**) → Settings → Connectors: the Entra app
registration (optional; without it the simulated directory answers) and enrol machines. `deploy/` holds a CyberPanel
install script and the GitHub Actions deploy used for the live desk.

**Secrets** are entered in the console and stored encrypted (Fernet; key from `SECRET_KEY` or `data/.secret_key`).
**Hardening:** PBKDF2 password hashes, HttpOnly/SameSite/Secure cookies, rate limits on sign-in, codes and invitation
attempts, Origin checks on every form post, `X-API-Key` on the integration endpoints (required on public installs),
an audit log of every desk action, allow-listed device scripts with validated arguments, utf8mb4 everywhere.

### Test users (simulated directory, when no Entra tenant is connected)

| Employee no | Name | Birth year | Device | Dept | Manager |
|---|---|---|---|---|---|
| E1001 | Emma Johnson | 1990 | LT-101 | Sales | E1002 |
| E1002 | Liam Carter | 1985 | LT-102 | Sales | E1003 |
| E1003 | Olivia Bennett | 1994 | LT-103 | Finance | — |

**API without WhatsApp:** `POST /messages {requester, text}` · `POST /voice` · `POST /image` · `POST /video` ·
`POST /camera/{camera_id}` · `POST /approvals/{id}` · `GET /tickets/{id}` · `POST /vulnwatch/run`.

## What we learned about the model

* Muse Spark 1.3 runs 10–15 tool calls per ticket reliably when every tool answers with a reason; the few failure modes
  we saw — asking the employee for what a tool could find, picking a tool the tier does not have, retrying a refused
  step — are all caught by code (tool descriptions that say *"do not ask, call"*, authority in the registry, the
  three-refusals rule), not by prompting harder.
* Language: a single detector bug (Python's case folding of İ/ı) once made English look Turkish; worth a test.
* Muse Wonder is worth its cost for the Supervisor: it reads a dozen ticket trails, decides knowledge gap vs
  capability gap vs waiting, researches, and writes a usable SOP or a one-line lesson.

## Layout

```
run.py              entry point (API + scheduler)
app/agents.py       agent loop, tiers, handoffs, approvals, refusal/step budgets
app/tools.py        LLM-callable tools with per-tier authority
app/systems.py      company systems: directory (Entra when connected, else simulated), vendor, courier; script allow-list
app/graph.py        Microsoft Entra ID / Microsoft 365 through Graph: directory, mail, phishing response
app/endpoints.py    enrolled machines, jobs, heartbeats       agent/muse-agent.ps1   the Windows endpoint agent
app/vulnwatch.py    daily KEV/NVD watch + remediation          app/malwatch.py        Defender detections
app/crashwatch.py   blue screens → proactive tickets           app/proactive.py       inventory checks
app/people.py       onboarding / offboarding                   app/enrol.py           self-enrolment with an invitation code
app/phishing.py     analysis, tenant-wide removal, restore     app/knowledge.py       SOPs, drafts, lessons + safety check
app/supervisor.py   desk-manager agent                         app/references.py      uploaded policies → passages
app/llm.py          Meta Model API client (+ scripted model)   app/voice.py / vision.py / camera.py
app/whatsapp.py     Cloud API channel (+ template outside 24 h) app/channels.py       e-mail, web, outbound
app/scheduler.py    follow-ups, reminders, watches             app/auth.py            console, settings, knowledge pages
app/dashboard.py    manager dashboard                          app/main.py            HTTP API
tests/              185 scenario tests over a scripted model
```
