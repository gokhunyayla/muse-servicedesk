# Exposed credential (camera or desk finding): forced password reset and session revocation

## Symptom
A camera or compliance finding shows a password, a sticky note, an unlocked screen with mail open, or a credential in a photo or message. Example: CAM-SALES-01, credential_note for emma.johnson, plus unlocked_screen with Outlook open. Treat any readable password as COMPROMISED: a notification alone is NOT closure.

## Checks (L1, immediate; do not wait for the employee to start containment)
1. collect_evidence / add_note: record the camera, location, time and user name seen; NEVER repeat the password in messages or broadcasts.
2. find_user / get_employee: identify the owner and the device.
3. verify_identity with the employee. If they are unreachable for more than 30 minutes, do not stall: escalate_to_human or L2 to force the reset; containing an exposed credential outranks convenience.

## Fix steps
1. reset_password for the affected account at once (temporary password over a secure channel only). In hybrid AD, Microsoft advises resetting twice if replication delay is a concern.
2. revoke_sessions: a password change alone does not end existing refresh and session tokens (Entra access tokens can live about an hour; app sessions persist).
3. reset_mfa if the MFA method, device or mailbox may also have been exposed; otherwise confirm the employee still holds their MFA.
4. Tell the employee, in the same contact: remove and destroy the note, lock the screen with Windows+L, confirm in writing. Ask whether anyone photographed it and whether the password is reused elsewhere; if so, they change it there too.
5. Unlocked screen: what was visible (e.g. a confidential mail)? add_note the scope; if confidential data was visible in an open area, escalate_to_human for a security and compliance assessment.
6. Verify: the employee signs in with the new password, old sessions need re-authentication, destruction of the note is confirmed.

## When to hand off
- Identity cannot be verified, the account is privileged, there are signs of actual misuse, confidential data was exposed, or the password is reused on external systems: escalate_to_human right after steps 1-2 (contain first, investigate second).
- Do NOT close on "employee notified". Close only after: reset_password done, revoke_sessions done, note destroyed and screen-lock habit confirmed.

Sources:
- https://learn.microsoft.com/en-us/entra/identity/users/users-revoke-access

_Written by the Supervisor from research; published by admin._
