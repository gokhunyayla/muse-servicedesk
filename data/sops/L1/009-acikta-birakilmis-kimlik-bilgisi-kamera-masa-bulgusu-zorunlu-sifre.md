# Açıkta bırakılmış kimlik bilgisi (kamera/masa bulgusu): zorunlu şifre sıfırlama ve oturum iptali

# Açıkta bırakılmış kimlik bilgisi (kamera/masa bulgusu)

## Symptom
Camera/compliance finding shows a password, sticky note, unlocked screen showing mail, or credential in photo/message. Example: CAM-SALES-01, credential_note for emma.johnson, plus unlocked_screen with Outlook open. Treat any readable password as COMPROMISED - notification alone is NOT closure.

## Checks (L1, immediate, do not wait for user reply to start containment)
1. collect_evidence / add_note: record camera, location, time, username seen; NEVER repeat the full password in messages to user or broadcast.
2. find_user / get_employee: identify owner and device.
3. verify_identity with employee (sicil + birth year etc.). If employee unreachable >30 min, do not stall: escalate_to_human/L2 to force reset - exposed credential containment outranks user convenience.

## Fix steps
1. reset_password for affected account immediately (temporary password via secure channel only). In hybrid AD, Microsoft guidance is to reset twice if pass-the-hash/replication delay is a concern.
2. revoke_sessions for the user - password change alone does not kill existing refresh/session tokens (Entra access tokens can live ~1h; app session tokens persist).
3. reset_mfa if MFA method, device, or inbox access may also have been exposed; otherwise confirm MFA still in user's possession.
4. Instruct user, in same contact: remove and destroy the sticky note, lock screen with Windows+L, confirm verbally/in writing. Ask: did anyone photograph/copy it, is password reused elsewhere? If reused, tell user to change it there too.
5. Check unlocked-screen exposure: what app/data was visible (e.g. confidential CFO mail in Outlook)? add_note scope; if confidential data was visible to a public/sales floor, escalate_to_human for security/compliance assessment.
6. verify_patch not relevant; verify by: user logs in with new password, old sessions require re-auth, confirmation note destroyed.

## When to hand off
- User identity cannot be verified, account is privileged/admin, signs of actual misuse, confidential data exposure, or password reused on external systems -> L2/security via escalate_to_human immediately, after steps 1-2 (contain first, investigate second).
- Do NOT close on 'user notified'. Close only after: reset_password done + revoke_sessions done + note destroyed confirmed + lock habit confirmed.

Source: Microsoft Entra ID - Revoke user access in an emergency (compromised accounts: disable/reset password, Revoke sessions, token lifetime risks).

Sources:
- https://learn.microsoft.com/en-us/entra/identity/users/users-revoke-access

_Written by the Supervisor from research; published by admin._
