# Mass outage

Several tickets with the same symptom in a short time (e.g. "e-mail won't open" × 5):
1. find_similar_tickets for open similar tickets; link them all to one incident (link_tickets).
2. Look for the root cause on the service side (server health, the latest change).
3. Send every linked ticket's owner a single status message (broadcast_incident_update); update every 30 minutes.
4. When fixed, close them together and ask everyone to confirm.
