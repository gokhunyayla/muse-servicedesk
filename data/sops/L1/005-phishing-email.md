# Suspicious or phishing e-mail

Symptom: "is this mail real?", "I got a suspicious link", "I clicked something", a forwarded mail or a screenshot of one.

1. Get the sender address and the subject (from the forward, the screenshot or by asking). analyze_email finds the mail in the company mailboxes and checks it like an analyst: sender authentication (SPF/DKIM/DMARC), look-alike domains, reply-to, where the links really open. Verdict safe / suspicious / phishing, plus how many mailboxes got it and who opened it.
2. Phishing: quarantine_email moves every copy to Deleted Items (recoverable) and warns the recipients and the admins; then block_sender. Suspicious: tell the employee not to click and ask L3 to look; do not remove.
3. Anyone who clicked the link or typed a password (the reporter, or someone who answers the warning): revoke_sessions and reset the password (SOP L1/002-password-reset), then hand the device to L2 (possible malware).
4. A false alarm after removal: restore_email puts the mail back into every inbox.
5. Thank the reporter and send a short awareness note: "Don't open mails from the same source; forward them to us."
