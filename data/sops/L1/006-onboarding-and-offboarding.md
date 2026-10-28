# Onboarding and offboarding

Only people in the HR group start these (the tools check who is writing). HR may write on any channel and often
attaches the CV; its text arrives as [Attached document: …].

Onboarding (HR: "X starts on Monday in Sales, reports to E1000", CV attached):
1. Read name, personal e-mail and phone from the CV; start date, department, title, manager and employee number from HR's message.
2. start_onboarding with what you have. If it lists missing fields, ask HR only for those and call it again.
3. Tell HR the manager has been asked; nothing else to do on HR's ticket.
4. On the manager's ticket (the desk opened it and sent the questions): record_people_answers with whatever they answer,
   one by one or all at once; ask only for what is still missing.
5. When the last required answer is recorded the desk creates the account at once (the result carries "completed").
   Tell the manager what was created; the first password reaches them in a separate message (never repeat it).
   If something failed (e.g. an unknown licence), ask only about that and call complete_onboarding again.

Offboarding (HR: "X leaves on <date>"):
1. start_offboarding with the employee number or, when there is none, the work e-mail address, and the last day (YYYY-MM-DD).
2. On the manager's ticket: record_people_answers (handover, device_return), then complete_offboarding; it runs at the
   end of the last day and tells HR and the manager what was closed.
