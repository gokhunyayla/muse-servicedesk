#!/usr/bin/env bash
# Muse ServiceDesk — one-shot server setup on Ubuntu 24.04 (run as a sudo-capable user on the VM).
#   DOMAIN=desk.example.com bash deploy/setup-server.sh
# Installs MariaDB, Python, ffmpeg and Caddy (automatic HTTPS), checks out the repo under /opt/muse,
# creates the venv, the systemd service and the Caddy site. Idempotent: safe to run again to update.
set -euo pipefail
DOMAIN="${DOMAIN:?set DOMAIN=desk.example.com}"
REPO="${REPO:-https://github.com/gokhunyayla/muse-servicedesk.git}"
BRANCH="${BRANCH:-main}"
APP_DIR=/opt/muse
DB_NAME=muse_servicedesk
DB_USER=muse

export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -q
sudo apt-get install -y -q git python3 python3-venv python3-pip mariadb-server ffmpeg debian-keyring debian-archive-keyring apt-transport-https curl

# --- Caddy (official repo) -----------------------------------------------------------------------
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -q && sudo apt-get install -y -q caddy
fi

# --- MariaDB: database + app user with a generated password (kept only in /opt/muse/.env) ---------
sudo systemctl enable --now mariadb
if [ ! -f "$APP_DIR/.env" ]; then
  DB_PASS=$(openssl rand -hex 16)
  sudo mysql -e "CREATE DATABASE IF NOT EXISTS \`$DB_NAME\` CHARACTER SET utf8mb4; CREATE USER IF NOT EXISTS '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASS'; GRANT ALL ON \`$DB_NAME\`.* TO '$DB_USER'@'localhost'; FLUSH PRIVILEGES;"
else
  DB_PASS=$(grep -oP 'mysql\+pymysql://'$DB_USER':\K[^@]+' "$APP_DIR/.env" || true)
fi

# --- code ----------------------------------------------------------------------------------------
sudo mkdir -p "$APP_DIR" && sudo chown "$USER":"$USER" "$APP_DIR"
if [ ! -d "$APP_DIR/.git" ]; then git clone -q -b "$BRANCH" "$REPO" "$APP_DIR"; else git -C "$APP_DIR" pull -q; fi
cd "$APP_DIR"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
mkdir -p data
if [ ! -f .env ]; then
cat > .env <<EOF
DATABASE_URL=mysql+pymysql://$DB_USER:$DB_PASS@127.0.0.1:3306/$DB_NAME
PORT=1002
PUBLIC_URL=https://$DOMAIN
DESK_TZ=${DESK_TZ:-UTC}
META_BASE_URL=https://api.meta.ai/v1
META_MODEL=muse-spark-1.3
META_MODEL_HEAVY=muse-wonder-1.0
META_REASONING_EFFORT=low
EOF
chmod 600 .env
fi

# --- systemd -------------------------------------------------------------------------------------
sudo tee /etc/systemd/system/muse.service >/dev/null <<EOF
[Unit]
Description=Muse ServiceDesk
After=network-online.target mariadb.service
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python run.py
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now muse
sudo systemctl restart muse

# --- Caddy: HTTPS reverse proxy ------------------------------------------------------------------
sudo tee /etc/caddy/Caddyfile >/dev/null <<EOF
$DOMAIN {
    encode gzip
    reverse_proxy 127.0.0.1:1002
}
EOF
sudo systemctl enable --now caddy
sudo systemctl reload caddy || sudo systemctl restart caddy

sleep 3
systemctl is-active muse caddy mariadb
curl -s -o /dev/null -w "local app: %{http_code}\n" http://127.0.0.1:1002/login
echo "Done. Point the DNS A record of $DOMAIN at this server; Caddy fetches the certificate on first request."
