# Deploying the desk on a server (fixed domain, HTTPS, no tunnels)

One Ubuntu 24.04 VM (2 vCPU / 4 GB is plenty), a DNS A record, and one script.

```bash
# on the VM, as a sudo-capable user
curl -fsSL https://raw.githubusercontent.com/gokhunyayla/muse-servicedesk/main/deploy/setup-server.sh -o setup-server.sh
DOMAIN=desk.example.com bash setup-server.sh
```

What it does: installs MariaDB, Python, ffmpeg and Caddy; clones the repo to `/opt/muse`; creates the
database and an app user with a generated password (only in `/opt/muse/.env`); installs a `muse`
systemd service on port 1002; configures Caddy as the HTTPS reverse proxy for `DOMAIN` (Let's Encrypt,
automatic). Re-running the script pulls the latest code and restarts the service.

Afterwards:
1. Open `https://DOMAIN/setup` and create the first admin.
2. Settings → Models: Meta API key. Settings → Channels: WhatsApp IDs/tokens. Settings → E-mail: SMTP/IMAP.
3. Meta → WhatsApp → Configuration: callback URL `https://DOMAIN/webhook` with the verify token. Set once.

Operations: `sudo systemctl status muse`, `journalctl -u muse -f`, update with `cd /opt/muse && git pull && sudo systemctl restart muse`.
Secrets never live in the repo or in `.env`; they are entered in the console and stored encrypted (`data/.secret_key` on the server).

## CyberPanel / OpenLiteSpeed server (one site among many)

`deploy/cyberpanel-install.sh` installs the desk inside one website's own root (`/home/DOMAIN/app`, the site
user, an isolated Python 3.12 via uv, a systemd unit named after the domain, a proxy block in that site's
vhost.conf). It never changes global server settings. See the header of the script for the one-liner.

## Continuous deployment without giving GitHub access to the server

`.github/workflows/ci.yml` runs the test suite on every push to `main`; when it passes, the workflow
fast-forwards the `live` branch. The server pulls `live` itself:

```bash
curl -fsSL https://raw.githubusercontent.com/gokhunyayla/muse-servicedesk/main/deploy/cyberpanel-autoupdate.sh \
  | sudo DOMAIN=cloudrium.com SITE_USER=cloud7006 bash
```

That installs `DOMAIN-update.timer` (every 2 minutes: fetch `live`; if it moved, pull, install requirements,
restart the service). No deploy keys, no inbound SSH, nothing reaches `live` without green tests.
Log: `journalctl -u cloudrium-update -n 20`.
