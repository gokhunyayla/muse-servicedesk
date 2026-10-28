# Demo cases and what makes each one real

The desk ran every case below against the real Meta models, real WhatsApp, real e-mail and the real support
page. What is still **simulated** is the system the agent acts on (directory, devices, vendors, mail tenant,
EDR, SAP). This list says, per case, which real system turns the simulation into a real action — so the
demo can be rebuilt on real infrastructure one case at a time. Tag `v0.11.0-demo-cases` is the last
version with the simulated cases; the database dump of that run sits in `data/archive/` (local only).

| # | Case | Channel / model path | Simulated today | Real setup that makes it real |
|---|------|----------------------|-----------------|-------------------------------|
| 1 | VPN not connecting | WhatsApp → L1 → endpoint tools | `run_script` on a device table | **Endpoint agent on the laptop** (MCP server: VPN status, DNS reset, profile renew) |
| 2 | Password reset by voice note | WhatsApp voice → Muse Voice → L1 → identity check | — | **real since v0.12: Entra ID through Graph** (`app/graph.py`: user lookup, reset password, revoke sessions) |
| 3 | Blue-screen photo → L4 | photo → Muse Spark vision → L1 → L2 → L4 | `open_vendor_case`, `create_shipment` | Vendor portal API (Dell/HP/Lenovo warranty + case), courier API (UPS/Yurtiçi) or ITSM connector |
| 4 | Missing / paid license | WhatsApp → L1 → approval over WhatsApp | — | **real since v0.12: tenant SKUs + assignLicense through Graph** |
| 5 | SAP authorisation | L1 → L3 → approval → temporary role | `sap_roles` | SAP user management API (SU01/PFCG via RFC) or ITSM request |
| 7 | Vulnerability ("php2shell") | L3: inventory, snapshot, patch, verify, rollback | `find_vulnerable`, `snapshot`, `rollback` | **Patch/CMDB source** (Intune, Qualys, or a package inventory agent) + VM snapshot API (vSphere/Proxmox) |
| 8 / 9 | Onboarding / offboarding | L1 → L3: account, licences, groups, sessions | loaner inventory | **real since v0.12: account, groups, licences, disable, revoke sessions through Graph**; loaners stay simulated |
| 10 | Mass outage | three similar reports → incident at intake, Supervisor merge, broadcast | nothing; real tickets | nothing — real already |
| 11 | Software request + manager approval | WhatsApp ONAY/RET | manager phone from directory | Directory connector gives the real manager; approval stays on WhatsApp |
| 12 | Short video / screen recording | video → frames → vision | nothing | nothing — real already |
| 13 | Proactive maintenance | scheduler → device table → tickets | disk %, patch age, warranty date | **Endpoint agent** (disk/patch) + vendor warranty API |
| 14 | Rating + self-written SOPs | WhatsApp → knowledge base | nothing | nothing — real already |
| 15 | Multilingual | any channel | nothing | nothing — real already |
| 16 | Visual compliance (sticky note) | camera frame → SAM 3.1 → owner → revoke sessions | `device_owner` (camera → desk owner table) | Camera/RTSP feed; **session revoke real since v0.12 (Graph)** |
| 17 | Malware response | L2: isolate, evidence, scan, release | `isolate`, `malware_scan`, `release` | **EDR connector** (Defender for Endpoint / CrowdStrike: isolate, scan, release) |
| 18 | Phishing triage | L2: analyse mail, quarantine, block sender | `analyze_email`, `quarantine`, `block_sender` | **Exchange Online / Defender for O365** (Graph security API: quarantine, tenant block) |

Cases 6 (console), 10, 12, 14, 15 and the three channels are real already. Since v0.12 the directory side of 2, 4, 8, 9 and 16 is real when Settings → Connectors → Microsoft 365 is enabled (Entra tenant with the cloudrium.com domain).

## Order that makes the demo most real with least work

1. **Endpoint agent on the laptop** (cases 1, 13, part of 3 and 7): a small MCP server the desk calls through
   Settings → Connectors — VPN status, DNS reset, disk/patch report, screen lock, printer queue reset. One
   laptop is enough for the video.
2. **Microsoft Entra ID / Microsoft 365 connector** (cases 2, 4, 8, 9, 16): Graph API through an MCP server;
   lookup, password reset, licence assignment, group membership, revoke sessions. A trial tenant works.
3. **Defender (endpoint + O365)** (cases 17, 18): isolate/scan/release and mail quarantine through Graph security.
4. **Vendor + courier** (case 3): usually ITSM or manual; keep simulated and say so, or route to a human ticket.
5. **SAP** (case 5): keep as approval + human engineer unless an SAP sandbox exists.

Every connector is registered in Settings → Connectors with the tiers that may use it; the agents call
`mcp.<connector>.<tool>` through the same authority gate as built-in tools. README must list, per case,
"real" or "simulated" at submission time.

## Lab architecture — what the real environment is made of

Goal: a small but real environment that judges can trigger from the support page and watch change.

| Layer | Real system | Who provides the MCP server | Desk tools (tier) | Cost |
|---|---|---|---|---|
| 1. Closed network | Cloud VMs reachable only over VPN (Hetzner / Oracle Always Free) | — (infrastructure) | — | 0–8 €/month |
| 2. VPN access | **Tailscale** (API) | **ours, thin**: `vpn_status`, `renew_vpn_key`, `authorize_device`, `list_devices` | L1/L2 | free (3 users / 100 devices) |
| 3. Employee PCs | Windows VMs on the network (Azure credit / AWS free tier) or a real laptop | **ours: endpoint agent** — `net_test`, `dns_reset`, `disk_report`, `patch_status`, `printer_queue_reset`, `lock_screen`, `driver_rollback`; runs as a service, reaches the desk over Tailscale; **also sends heartbeat and alerts** (offline, disk/CPU/memory thresholds, service crash, critical event-log entries, patch age, VPN state) → proactive tickets | L1/L2 | free |
| 3b. Live support access | **MeshCentral** (self-hosted; alternatives RustDesk, Tactical RMM) — remote desktop, terminal, files, inventory, events, web API | existing product; the desk gets a "Connect" button on the ticket and logs engineer sessions | human engineers | free, open source |
| 4. Identity | **Microsoft Entra ID** (free tier tenant) | **ours, thin, over Graph**: `find_user`, `reset_password`, `revoke_sessions`, `add_to_group`, `disable_user` (community alternative: ms-365-mcp-server) | L1/L3 | free |
| 5. Licences / mail | **Microsoft 365** (1-month trial) | same Graph MCP: `assign_license`, `mailbox_status` | L1/L2 | trial, then ~6 $/user/month |
| 6. Device management | **Intune** (in the M365 trial) | same Graph MCP: `device_compliance`, `sync_device`, `remote_lock` | L2 | trial, then paid |
| 7. Security | **Defender for Endpoint / Office 365** (trial) | Microsoft's Sentinel/Defender MCP (preview) or ours over the Graph security API: `isolate`, `release`, `run_scan`, `quarantine_mail`, `block_sender` | L2 | trial, then paid |
| 8. Servers | 1–2 Linux VMs on the network | **ours**: `inventory`, `snapshot` (cloud API), `apply_patch`, `verify`, `rollback` | L3 | VM cost only |
| 9. Vendor / courier / SAP | none | stays simulated; human step | L4/L3 | — |

Every MCP server is a small Python process registered in Settings → Connectors with the tiers that may use it;
the agents call `mcp.<connector>.<tool>` through the same authority gate as built-in tools.

During the hackathon the whole lab costs between nothing and a few euros (VMs); only M365/Defender licences and
VMs cost money afterwards.

### Build order
1. Tailscale account + one VM as the "employee laptop" (Tailscale client + endpoint agent) → case 1 real, case 13 real, judge-triggerable with a lab status page.
2. Endpoint agent alerts (heartbeat, thresholds, event log) → proactive tickets from a real machine.
3. MeshCentral on the lab VM → "Connect" on escalated tickets, engineer sessions logged.
4. Entra ID free tenant + Graph MCP → cases 2, 8, 9 (and 16's session revoke) real.
5. M365 trial (licences) → case 4; Intune → device tools.
6. Defender trial → cases 17, 18.
7. Linux server VM + patch tools → case 7.
8. Fixed-domain deployment of the desk itself (no tunnel) and the WhatsApp go-live.
