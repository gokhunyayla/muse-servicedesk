# TODO

## Must (before submission)
- [ ] **Make the demo cases real** one by one — see `docs/DEMO-CASES.md` (endpoint agent → Entra/M365 → Defender). README states real vs simulated per case.
- [x] **Real endpoint agent.** `agent/muse-agent.ps1` on LT-101 (Azure, Windows Server 2022, Entra-joined): heartbeat
      metrics + allow-listed scripts; device tools and proactive checks use it (Settings → Connectors → Endpoint agents).
- [x] **Microsoft Entra ID / Microsoft 365** through Graph (directory actions, support@cloudrium.com mailbox).
- [ ] **Jury access** (prepare with the submission): demo accounts the judges can use on the support page and in
      Entra (e.g. a judge persona with a mailbox they can open, or a demo sign-in that shows the one-time code),
      written instructions in the README and the submission form, and which requests to try ("reset my password",
      "my laptop is slow", "clean my disk"). Reset demo state before judging.
- [ ] Azure quota: request 4 vCPU Basv2 in Sweden Central (portal → Quotas), then resize LT-101 to B2als_v2 (~50 $/month
      instead of ~145 $). Delete the `muse-desk-lab` resource group after the results.
- [ ] **Durable inbound queue.** Webhook handler stores the raw event in an `inbox` table and returns 200
      immediately; a worker processes it with retries. Nothing is lost while the model/API is slow or down.
      Document Meta's own redelivery window (retries for up to 7 days while the webhook returns non-2xx)
      so messages sent while the app is offline still arrive once it is back.
- [x] **Emerging-incident awareness** → Supervisor merges clusters; intake links three similar reports within minutes and briefs the agent.
- [x] Support page (`/support-agent`: chat + one-time-code sign-in, own tickets) and e-mail intake next to WhatsApp; replies follow the employee's last channel.
- [ ] One-command local run for judges (SQLite default, MariaDB optional) + demo data.
- [x] Deployed at https://cloudrium.com with CI/CD (push to main → tests → server pulls and restarts).
- [ ] **WhatsApp go-live**: own phone number on the WABA (display name e.g. "Quick Service Agent by Webtangular"), business verification, app to Live mode; until then the test number only reaches the 5 listed recipients.
- [ ] Reset the live database for real cases (archive of the demo-cases run is in `data/archive/`, tag `v0.11.0-demo-cases`).
- [ ] 3-minute demo video; link in README; tag `v1.0.0`.

- [x] Credential resets: identity + one-time code on the other channel; optional spoken phrase.
- [x] Agents notice more: channel continuity, repeat issues, frustration → human, cost in money.

## Should
- [ ] SSO (OIDC — Microsoft Entra / Google) and passkeys for the console; TOTP as a fallback.
- [ ] Session rotation on privilege change.
- [ ] Unsubscribe the legacy app from the test WhatsApp account (Meta panel, legacy app → Webhooks). Outbound retries: done.
- [ ] Dashboard: escalated queue view, per-tier latency, incident panel.

## Later
- [ ] Real directory connector (Microsoft Graph) behind the same tool shapes.
- [ ] Per-tenant SOP packs and a review step before a self-written SOP goes live.
