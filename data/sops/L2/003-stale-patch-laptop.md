# Laptop not updated for a long time (stale patch, e.g. 48 days): offline device and the endpoint patch path

## Symptom
Proactive alert: an endpoint (a laptop, not a server) has had no OS updates for e.g. 48 days (LT-103). Do NOT bounce L1 → L3 → L2 → L1. Owners: L2 diagnoses the endpoint, L1 owns contact and scheduling with the employee.

## Checks
1. L1: find_user / get_employee, verify_identity; ask the employee to power the laptop on, connect it (Wi-Fi or cable), plug in the charger and keep it on. Confirm the device name.
2. L2: network_diagnostics — online? DNS ok? If offline: STOP remote work and do not retry scripts blindly. Agree a time with the employee via schedule_followup / request_confirmation and note "waiting for device online", not "stuck".
3. If online: list_endpoint_scripts — is there an OS update script? Check disk space first (under 10% free blocks updates: perf-report, then disk-cleanup), battery and pending reboots.
4. Check that the update policy actually reached the device: per Microsoft's Intune update-ring troubleshooting, first confirm the policy was delivered and applied (device check-in status; Settings > Accounts > Access work or school) before troubleshooting Windows Update itself. Check deferral or pause settings and the last check-in time; a stale check-in explains a stale patch.

## Fix steps
1. Device online and policy applied: run the OS update script with run_endpoint_script if one is listed; otherwise guide the employee through Settings > Windows Update > Check for updates while L2 watches, then reboot when asked.
2. apply_patch is the L3 production-change tool for servers; do NOT use it on laptops without L3 approval and a change window.
3. After the reboot, verify_patch: the OS build or update date moved forward and nothing failed; add_note the evidence.
4. Updates failing: note the exact error code, check the disk, reboot once, retry; check the update-ring assignment (included and excluded groups) in Intune.

## When to hand off
- Device still offline after 2 scheduled attempts, hardware or disk failure, repeated Windows Update errors, or the policy not applied despite check-in: hand off to L3 for the Intune / Windows Update for Business policy, with online status, last check-in, policy applied yes/no, error code.
- Never hand off without those four facts. Unrelated new requests (VPN, printer) become separate tickets via link_tickets and never block the patch ticket; the printer-fix script also needs the device online.

Sources:
- https://learn.microsoft.com/en-us/troubleshoot/mem/intune/device-protection/troubleshoot-update-rings

_Written by the Supervisor from research; published by admin._
