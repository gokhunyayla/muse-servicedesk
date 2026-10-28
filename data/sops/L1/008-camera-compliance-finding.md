# Visual compliance finding (camera)

Findings from the camera monitor: a password note, an unlocked screen, a visitor without a badge.

1. Password note seen: treat the credential as exposed. revoke_sessions at once, then ask the employee to destroy the note and change the password (a reset needs identity verification; do it when they write).
2. Unlocked screen: lock it remotely with run_endpoint_script `lock-screen` and remind the employee of Windows+L.
3. Visitor without a badge: ask the desk owner and inform reception with notify_user.
4. Write the policy clause on the ticket (ISO 27001 A.5.17 / clear screen) and schedule_followup to check again in 2 hours.
5. Repeated finding (same person twice or more): inform their manager with notify_user.
