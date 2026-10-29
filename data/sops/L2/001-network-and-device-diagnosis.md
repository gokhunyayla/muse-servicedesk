# Network and device diagnosis

For hand-offs from L1:
1. network_diagnostics for reachability and DNS.
2. Broken DNS: run_endpoint_script `dns-reset`. Device offline: pass questions about the cable or Wi-Fi to the employee (through L1).
3. Slowness: run_endpoint_script `perf-report` (CPU, memory, disk, start-up programs) and advise from the report; `disk-cleanup` when the disk is nearly full.
4. Blue screen or a crash: follow the blue screen SOP (L2/004-blue-screen).
5. Return a structured result: finding, action taken, remaining risk.
