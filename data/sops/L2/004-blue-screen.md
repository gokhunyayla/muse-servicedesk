# Blue screen (crash) on a laptop

Symptom: a photo of a blue screen, "my computer crashed / restarted by itself", or a proactive [Proactive:crash] alert (the desk saw the crash itself).

1. run_endpoint_script `crash-report` on the device: stop codes with their likely cause, drivers added or changed in the last 14 days, disk health, and a verdict. If the employee sent a photo, compare its stop code with the report.
2. Verdict software (driver or system files):
   - a driver added or changed just before the crash is the suspect: name it to the employee and ask whether they installed something; driver rollback needs their consent;
   - otherwise run_endpoint_script `system-repair` (DISM + SFC, runs in the background, reports back by itself) and tell the employee they can keep working; a restart afterwards finishes the repair.
3. Verdict hardware (disk not Healthy, disk/memory/hardware stop codes):
   - request_manager_approval for a replacement device, naming the failing part and the evidence;
   - once approved: escalate_to_human so IT prepares the replacement (what to prepare, for whom, data to move), and tell the employee to save work to OneDrive now;
   - where vendor warranty and courier tools are available (L4), hand off to L4 instead of escalating.
4. Tell the employee in plain words what happened, what was done and whether anything is left for them (a restart, saving their work).
5. A further crash on the same device after a repair goes straight to the hardware path.
