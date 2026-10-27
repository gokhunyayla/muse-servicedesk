# Muse ServiceDesk

An autonomous, multi-tier IT service desk built on **Meta Model API** for the Meta Global AI Developer
Hackathon 2026 — Indie Developer track, theme *Build for Your World*.

Employees message the desk over WhatsApp — text, a voice note, a photo of a blue screen, a short clip.
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
* **Knowledge pools + learning loop.** Each tier has Markdown SOPs (`data/sops/<tier>/`). Agents search
  them first (`search_knowledge`) and write new ones when they solve something uncovered (`write_sop`).
* **General, not scripted.** There is no per-scenario code. The scenarios below are acceptance tests over
  the same tools, SOPs and lifecycle; a problem nobody anticipated goes through the same path.
* **Ticket lifecycle.** Identity check before account changes, consent before intrusive scripts, manager
  approval over chat (`ONAY 12`), confirmation before closing, satisfaction rating, scheduled follow-ups,
  incident linking for mass outages, proactive tickets from inventory checks, escalation to a human engineer when no tier can solve it, desk-side close from the dashboard.
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

## Scenarios covered (all run against the real models)

1. VPN not connecting — profile renewed via endpoint tools
2. Password reset by voice note — transcription, identity check, temporary password
3. Blue-screen photo → L4: warranty, vendor case, loaner laptop, courier label
4. Missing / paid license — standard ones assigned, paid ones through manager approval
5. SAP authorization — L3, role granted after approval, temporary access expiry scheduled
6. Service desk console — queues, auto-resolution rate, satisfaction, top issues, live audit trail (English, responsive, system/light/dark theme)
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
cp .env.example .env            # add META_API_KEY (and WhatsApp tokens when you have them)
uv venv .venv && uv pip install -p .venv -r requirements.txt
.venv/bin/python run.py         # http://localhost:1002  dashboard · /docs API
.venv/bin/python -m pytest      # 52 offline tests with a scripted model
```

Needs a MariaDB on 127.0.0.1:3306 (root, empty password); the database is created on start.
`ffmpeg` is used for voice-note and video conversion.

**WhatsApp:** expose port 1002 (e.g. `cloudflared tunnel --url http://localhost:1002`), set the webhook to
`https://<tunnel>/webhook` with `WA_VERIFY_TOKEN`, subscribe to `messages`.

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
app/knowledge.py    SOP pools + learning loop        data/sops/<tier>/*.md
app/llm.py          Meta Model API client (+ scripted model for tests)
app/voice.py        Muse Voice Transcribe            app/vision.py   SAM 3.1 + frame sampling
app/camera.py       compliance watcher               app/proactive.py  inventory checks
app/scheduler.py    follow-ups, reminders, outbound  app/whatsapp.py   Cloud API channel
app/dashboard.py    manager dashboard                app/main.py       HTTP API
tests/              scenario tests over a scripted model
```

All code in this repository was written during the event, starting Oct 27, 2026.
