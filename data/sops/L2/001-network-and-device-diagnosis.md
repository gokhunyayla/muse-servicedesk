# Network and device diagnosis

For hand-offs from L1:
1. network_diagnostics for reachability and DNS.
2. Broken DNS: run_endpoint_script `dns-reset`. Device offline: pass questions about the cable or Wi-Fi to the employee (through L1).
3. Slowness: run_endpoint_script `perf-report` (CPU, memory, disk, start-up programs) and advise from the report; `disk-cleanup` when the disk is nearly full.
4. Blue screen: if the stop code in the photo points at a driver, run_endpoint_script `driver-rollback`; suspected hardware goes to L4.
5. Return a structured result: finding, action taken, remaining risk.
