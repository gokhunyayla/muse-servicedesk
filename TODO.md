# TODO

## Must (before submission)
- [ ] **Real endpoint agent (MCP).** A small MCP server on the employee's laptop exposing real diagnostics
      and fixes: ping/DNS/gateway test, DNS cache reset, VPN status via `scutil`, disk/CPU/process report,
      CUPS queue reset, screen lock. L1/L2 call it instead of the simulated device table. README states
      clearly which systems are real (endpoint, WhatsApp, models) and which are simulated (directory, SAP,
      EDR, vendor, courier).
- [ ] **Durable inbound queue.** Webhook handler stores the raw event in an `inbox` table and returns 200
      immediately; a worker processes it with retries. Nothing is lost while the model/API is slow or down.
      Document Meta's own redelivery window (retries for up to 7 days while the webhook returns non-2xx)
      so messages sent while the app is offline still arrive once it is back.
- [x] **Emerging-incident awareness** → the Supervisor merges clusters into problem records and updates everyone.
      Still open: tell *new* reporters of a known problem immediately at intake (before the next Supervisor pass).
- [ ] Deploy to a small public server with a fixed domain + HTTPS (no tunnels, no third-party tunnel accounts).
- [ ] 3-minute demo video; link in README; tag `v1.0.0`.

## Should
- [ ] SSO (OIDC — Microsoft Entra / Google) and passkeys for the console; TOTP as a fallback.
- [ ] Session rotation on privilege change.
- [ ] Unsubscribe the legacy app from the test WhatsApp account (Meta panel, legacy app → Webhooks). Outbound retries: done.
- [ ] Dashboard: escalated queue view, per-tier latency, incident panel.

## Later
- [ ] Real directory connector (Microsoft Graph) behind the same tool shapes.
- [ ] Per-tenant SOP packs and a review step before a self-written SOP goes live.
