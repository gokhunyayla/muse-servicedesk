# Operating system updates

Symptom: "is my computer up to date?", "install the updates", an update reminder, a proactive patch alert.

1. The employee's device: run_endpoint_script `patch-status` (last installed updates, pending count, reboot pending, the last install run).
2. Pending updates and the employee agrees: run_endpoint_script `install-updates`. It runs in the background (10-30 minutes); tell them they can keep working and that a restart may be needed.
3. schedule_followup in 30 minutes to run `patch-status` again and report the outcome; if a restart is required, agree a time with the employee rather than restarting on your own.
4. Failed updates: note the update names and result from patch-status, then hand off to L2 (see SOP L2/003-stale-patch-laptop).
