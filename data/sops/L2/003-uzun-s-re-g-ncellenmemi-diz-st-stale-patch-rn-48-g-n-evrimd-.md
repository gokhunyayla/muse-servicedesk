# Uzun süre güncellenmemiş dizüstü (stale patch, örn. 48 gün): çevrimdışı cihaz ve endpoint yama yolu

# Uzun süre güncellenmemiş dizüstü (stale patch)

## Symptom
Proactive alert: endpoint (laptop, not server) has no OS updates for e.g. 48 days (TR-LT-103). Do NOT bounce L1->L3->L2->L1. Owner of record: L2 endpoint diagnosis, L1 owns user contact/scheduling.

## Checks
1. L1: find_user/get_employee, verify_identity; ask user to power on, connect to internet (Wi-Fi/ethernet), plug in charger, keep device on. Confirm device name.
2. L2: network_diagnostics - online? dns_ok? If online=false: STOP remote work, do not retry scripts blindly (cf. printer-fix failures on offline TR-LT-103, ticket 4). Schedule a specific time with user via schedule_followup / request_confirmation and note 'waiting device online', not 'stuck'.
3. If online: list_endpoint_scripts - is there an OS-update/patch script? Check disk space first (disk <10% free blocks updates - run perf-report/disk check; clean disk before patching), battery, pending reboot.
4. Check update policy actually reached device: per Microsoft Intune update-ring troubleshooting, first determine whether policy was delivered/applied to device (Device and user check-in status / View report, Settings > Accounts > Access work or school on device) before troubleshooting Windows Update itself. Check deferral/pause settings and last check-in time - stale check-in explains stale patch.

## Fix steps
1. Device online + policy applied: run the OS-update endpoint script via run_endpoint_script if listed; if no laptop script exists, instruct user to run Settings > Windows Update > Check for updates while L2 monitors, then reboot when prompted.
2. apply_patch is L3 production-change tool for servers - do NOT use on end-user laptops without L3 approval/change window.
3. verify_patch after reboot: confirm OS build/update date advanced and no failed updates; add_note evidence.
4. Updates failing with errors: note exact error code, check disk, reboot once, retry; check update-ring assignment (included/excluded groups) in Intune per Microsoft guide.

## When to hand off
- Device stays offline after 2 scheduled attempts, hardware/disk failure, repeated Windows Update error codes, or policy not applied despite check-in -> L3 for Intune/WUfB policy investigation (state: online status, last check-in, policy applied Y/N, error code).
- Never hand off without those 4 facts. Unrelated new user requests (VPN, printer) get separate tickets via link_tickets, do not block patch ticket on them; printer queue fix (printer-fix script) also requires device online first.

Source: Microsoft Learn - Troubleshoot Update ring policies for Windows devices in Intune (verify prerequisites, policy delivery/check-in status on device before troubleshooting updates).

Sources:
- https://learn.microsoft.com/en-us/troubleshoot/mem/intune/device-protection/troubleshoot-update-rings

_Written by the Supervisor from research; published by admin._
