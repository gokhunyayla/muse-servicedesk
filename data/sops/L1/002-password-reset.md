# Password reset and locked account

Symptom: "I forgot my password", "my account is locked", "I can't sign in".

1. Never reset a password without identity: verify_identity. The phone number or address the employee writes from must be their own; where the directory holds a second factor (e.g. birth year), ask for it too. Spoken numbers in voice notes may arrive as words.
2. Strong verification before any credential change: send_verification (a one-time code to the employee's other channel), then check_verification with the code they type back.
3. Then reset_password; pass on the temporary password and tell them it must be changed at the first sign-in.
4. Two failed verifications: stop, tell the employee to contact their manager, and add_note on the ticket.
5. If the problem is MFA rather than the password (no code arrives): reset_mfa, which needs the same verification.
