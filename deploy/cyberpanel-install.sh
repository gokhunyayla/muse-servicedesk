#!/usr/bin/env bash
# Muse ServiceDesk on a CyberPanel / OpenLiteSpeed server, inside one website's own root.
#
#   curl -fsSL https://raw.githubusercontent.com/gokhunyayla/muse-servicedesk/main/deploy/cyberpanel-install.sh \
#     | sudo DOMAIN=cloudrium.com SITE_USER=cloud7006 DB_NAME=clou_muse_servicedesk DB_USER=clou_muse_servicedesk_user DB_PASS='…' bash
#
# What it touches: apt installs ffmpeg (only system package); everything else lives in /home/$DOMAIN/app owned
# by the site user, with an isolated Python 3.12 (uv, under the site user's home); a systemd unit named after the
# domain; a proxy block appended to that site's vhost.conf (backup kept). No other site or system setting changes.
set -euo pipefail
DOMAIN="${DOMAIN:?DOMAIN=example.com}"
SITE_USER="${SITE_USER:?SITE_USER=the CyberPanel site user}"
DB_NAME="${DB_NAME:?}"; DB_USER="${DB_USER:?}"; DB_PASS="${DB_PASS:?}"
SITE=/home/$DOMAIN; APP=$SITE/app; SVC=${SERVICE_NAME:-${DOMAIN%%.*}}
PORT="${PORT:-8102}"   # unprivileged port: the site user may not bind below 1024
REPO="${REPO:-https://github.com/gokhunyayla/muse-servicedesk.git}"

urlencode() { python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"; }

echo "== 1/5 ffmpeg"
command -v ffmpeg >/dev/null || { DEBIAN_FRONTEND=noninteractive apt-get install -y -q ffmpeg >/dev/null; }
ffmpeg -version | head -1

echo "== 2/5 code + isolated Python 3.12 (uv under $SITE_USER's home)"
sudo -u "$SITE_USER" -H env APP="$APP" SITE="$SITE" REPO="$REPO" bash <<'EOS'
set -e
cd "$SITE"
[ -x "$HOME/.local/bin/uv" ] || curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
export PATH="$HOME/.local/bin:$PATH"
if [ ! -d "$APP/.git" ]; then git clone -q "$REPO" "$APP"; else git -C "$APP" pull -q; fi
cd "$APP"
uv python install 3.12 >/dev/null
[ -d .venv ] || uv venv --python 3.12 .venv >/dev/null
uv pip install -q -r requirements.txt --python .venv/bin/python
mkdir -p data && chmod 700 data
.venv/bin/python -c "import app.main; print('app imports ok')"
EOS

echo "== 3/5 .env"
if [ ! -f "$APP/.env" ]; then
  cat > "$APP/.env" <<EOF
DATABASE_URL=mysql+pymysql://$DB_USER:$(urlencode "$DB_PASS")@127.0.0.1:3306/$DB_NAME
PORT=$PORT
HOST=127.0.0.1
PUBLIC_URL=https://$DOMAIN
DESK_TZ=Europe/Istanbul
META_BASE_URL=https://api.meta.ai/v1
META_MODEL=muse-spark-1.3
META_MODEL_HEAVY=muse-wonder-1.0
META_REASONING_EFFORT=low
EOF
  chown "$SITE_USER":"$SITE_USER" "$APP/.env" && chmod 600 "$APP/.env"
  echo "written"
else
  echo "kept existing .env"
fi

echo "== 4/5 $SVC.service"
cat > "/etc/systemd/system/$SVC.service" <<EOF
[Unit]
Description=Muse ServiceDesk for $DOMAIN
After=network-online.target mariadb.service
Wants=network-online.target

[Service]
User=$SITE_USER
Group=$SITE_USER
WorkingDirectory=$APP
ExecStart=$APP/.venv/bin/python run.py
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now "$SVC" >/dev/null
systemctl restart "$SVC"
sleep 5
systemctl is-active "$SVC" || { journalctl -u "$SVC" -n 30 --no-pager; exit 1; }
curl -s -o /dev/null -w "app local: %{http_code}\n" "http://127.0.0.1:$PORT/login"

echo "== 5/5 vhost proxy (backup: vhost.conf.bak-$SVC)"
VH=/usr/local/lsws/conf/vhosts/$DOMAIN/vhost.conf
cp -n "$VH" "$VH.bak-$SVC"
if ! grep -q "extprocessor $SVC" "$VH"; then
cat >> "$VH" <<EOF

extprocessor $SVC {
  type                    proxy
  address                 127.0.0.1:$PORT
  maxConns                100
  pcKeepAliveTimeout      60
  initTimeout             60
  retryTimeout            0
  respBuffer              0
}

context / {
  type                    proxy
  handler                 $SVC
  addDefaultCharset       off
}
EOF
fi
/usr/local/lsws/bin/lswsctrl restart >/dev/null
sleep 3
curl -sk -o /dev/null -w "https via litespeed: %{http_code}\n" --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/login"
echo "DONE — open https://$DOMAIN/setup"
