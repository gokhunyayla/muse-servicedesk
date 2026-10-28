# Demo cases and what makes each one real

The desk ran every case below against the real Meta models, real WhatsApp, real e-mail and the real support
page. What is still **simulated** is the system the agent acts on (directory, devices, vendors, mail tenant,
EDR, SAP). This list says, per case, which real system turns the simulation into a real action — so the
demo can be rebuilt on real infrastructure one case at a time. Tag `v0.11.0-demo-cases` is the last
version with the simulated cases; the database dump of that run sits in `data/archive/` (local only).

| # | Case | Channel / model path | Simulated today | Real setup that makes it real |
|---|------|----------------------|-----------------|-------------------------------|
| 1 | VPN not connecting | WhatsApp → L1 → endpoint tools | `run_script` on a device table | **Endpoint agent on the laptop** (MCP server: VPN status, DNS reset, profile renew) |
| 2 | Password reset by voice note | WhatsApp voice → Muse Voice → L1 → identity check | directory `USERS`, `reset_password` | **Microsoft Entra ID / AD** via MCP connector (Graph: user lookup, reset password, revoke sessions) |
| 3 | Blue-screen photo → L4 | photo → Muse Spark vision → L1 → L2 → L4 | `open_vendor_case`, `create_shipment` | Vendor portal API (Dell/HP/Lenovo warranty + case), courier API (UPS/Yurtiçi) or ITSM connector |
| 4 | Missing / paid license | WhatsApp → L1 → approval over WhatsApp | license table | **M365 admin (Graph)**: assign licence, check availability |
| 5 | SAP authorisation | L1 → L3 → approval → temporary role | `sap_roles` | SAP user management API (SU01/PFCG via RFC) or ITSM request |
| 7 | Vulnerability ("php2shell") | L3: inventory, snapshot, patch, verify, rollback | `find_vulnerable`, `snapshot`, `rollback` | **Patch/CMDB source** (Intune, Qualys, or a package inventory agent) + VM snapshot API (vSphere/Proxmox) |
| 8 / 9 | Onboarding / offboarding | L1 → L3: account, licences, groups, sessions | `create_user`, groups, sessions | Entra ID + M365 + loaner inventory (connector) |
| 10 | Mass outage | three similar reports → incident at intake, Supervisor merge, broadcast | nothing; real tickets | nothing — real already |
| 11 | Software request + manager approval | WhatsApp ONAY/RET | manager phone from directory | Directory connector gives the real manager; approval stays on WhatsApp |
| 12 | Short video / screen recording | video → frames → vision | nothing | nothing — real already |
| 13 | Proactive maintenance | scheduler → device table → tickets | disk %, patch age, warranty date | **Endpoint agent** (disk/patch) + vendor warranty API |
| 14 | Rating + self-written SOPs | WhatsApp → knowledge base | nothing | nothing — real already |
| 15 | Multilingual | any channel | nothing | nothing — real already |
| 16 | Visual compliance (sticky note) | camera frame → SAM 3.1 → owner → revoke sessions | `device_owner`, session revoke | Camera/RTSP feed + Entra "revoke sign-in sessions" |
| 17 | Malware response | L2: isolate, evidence, scan, release | `isolate`, `malware_scan`, `release` | **EDR connector** (Defender for Endpoint / CrowdStrike: isolate, scan, release) |
| 18 | Phishing triage | L2: analyse mail, quarantine, block sender | `analyze_email`, `quarantine`, `block_sender` | **Exchange Online / Defender for O365** (Graph security API: quarantine, tenant block) |

Cases 6 (console), 10, 12, 14, 15 and the three channels are real already.

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
