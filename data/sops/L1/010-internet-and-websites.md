# Internet slow or some websites don't open

Symptom: "some sites don't open", "the internet is slow", "pages keep loading", a browser DNS error.

1. Find the employee's device (find_user / get_employee) and read its state with network_diagnostics: online, DNS, CPU, memory.
2. DNS not OK, or only some sites fail: run_endpoint_script `dns-reset` (clears the DNS cache and re-registers; safe, no restart). Tell the employee what the test lookup returned.
3. Ask them to try the same site again, in a private window as well (rules out the browser cache).
4. Still failing for one site only: get the exact address and error text; it may be the site itself or a block, so add_note and hand off to L2.
5. Everything is slow: run_endpoint_script `perf-report`; high CPU or memory points at the device, otherwise hand off to L2 for the network.
6. Get confirmation before closing (request_confirmation).
