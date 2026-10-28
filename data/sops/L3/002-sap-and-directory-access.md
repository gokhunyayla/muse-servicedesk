# SAP and directory permissions

Symptom: "I can't run transaction X in SAP, authorisation error", "I can't open the folder".

1. A permission change needs manager approval: request_manager_approval (the ticket waits for it).
2. When approved: grant_sap_role (role and validity) or add_to_group.
3. Ask the employee to sign out of SAP and back in; get confirmation.
4. Temporary access: schedule_followup on the end date to take it back.
