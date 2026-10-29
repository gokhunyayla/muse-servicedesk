# VPN connection problem

Symptom: "VPN won't connect", "I can't reach our systems from home", the VPN client shows an error.

1. Find the employee's device (find_user) and read its VPN state with vpn_status: connected or not, the server, and whether an internal address answers through it.
2. Disconnected, or connected but the internal network does not answer: run_endpoint_script `vpn-reset` (drops and redials the tunnel, restarts the remote-access services, clears DNS, tests the internal address). No consent needed; tell the employee it may blink for a few seconds.
3. Simulated devices with an `expired` profile: renew_vpn_profile (the device must be online).
4. Still failing after the reset: hand off to L2 for network_diagnostics with the reset output.
5. Report what the tool says now (connected, internal address answers) and get the employee's confirmation before closing (request_confirmation).
