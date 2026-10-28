# Printer not printing

Symptom: "the printer doesn't work", "I can't print", jobs stuck in the queue.

1. Which printer and which device? Find the device with find_user.
2. run_endpoint_script `printer-fix`: clears the print queue and restarts the spooler service.
3. Not fixed: the printer may be unreachable on the network, so hand off to L2 (network_diagnostics).
4. Get confirmation, then close.
