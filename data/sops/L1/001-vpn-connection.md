# VPN connection problem

Symptom: "VPN won't connect", "I can't reach our systems from home", the VPN client shows an error.

1. Find the employee's device (find_user) and read its profile state with vpn_status.
2. If the profile is `expired`, renew it with renew_vpn_profile; the device must be online.
3. If the profile is valid but still fails, run_endpoint_script `vpn-reset` (restarts the client and clears the DNS cache).
4. Still failing: hand off to L2 for network_diagnostics.
5. Get a "connected" confirmation from the employee; do not close before it (request_confirmation).
