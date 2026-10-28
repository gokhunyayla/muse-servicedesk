# Production change (patch, configuration)

Order for every change that affects production:
1. Scope: find_vulnerable_servers / inventory.
2. add_note with the change plan and the rollback plan.
3. Apply: apply_patch (snapshot taken automatically). One server at a time; in load-balanced pairs, the secondary first.
4. verify_patch; on failure rollback_patch.
5. schedule_followup for 24-hour stability monitoring (hours=24, "stability check").
6. Evidence: add_note (versions before/after, snapshot id) and inform the requester (notify_user).
