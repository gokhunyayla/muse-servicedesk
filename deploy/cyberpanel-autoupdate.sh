#!/usr/bin/env bash
# Pull-based CD for a CyberPanel install: a systemd timer checks the `live` branch every 2 minutes and,
# when it moved, pulls it, installs requirements and restarts the service. Nothing reaches the server
# from GitHub; the server fetches. Touches only /etc/systemd/system/$SVC-update.{service,timer}.
#
#   curl -fsSL https://raw.githubusercontent.com/gokhunyayla/muse-servicedesk/main/deploy/cyberpanel-autoupdate.sh \
#     | sudo DOMAIN=cloudrium.com SITE_USER=cloud7006 bash
set -euo pipefail
DOMAIN="${DOMAIN:?DOMAIN=example.com}"; SITE_USER="${SITE_USER:?SITE_USER=the CyberPanel site user}"
APP=/home/$DOMAIN/app; SVC=${SERVICE_NAME:-${DOMAIN%%.*}}; BRANCH="${BRANCH:-live}"

cat > "$APP/update.sh" <<EOS
#!/usr/bin/env bash
# Runs as root from $SVC-update.timer: pull $BRANCH as $SITE_USER, restart $SVC only when the commit changed.
set -euo pipefail
cd $APP
before=\$(sudo -u $SITE_USER git rev-parse HEAD)
sudo -u $SITE_USER -H git fetch -q origin $BRANCH
after=\$(sudo -u $SITE_USER git rev-parse origin/$BRANCH)
[ "\$before" = "\$after" ] && exit 0
echo "updating \$before -> \$after"
sudo -u $SITE_USER -H git checkout -q -B $BRANCH "\$after"
sudo -u $SITE_USER -H bash -c 'cd '"$APP"' && \$HOME/.local/bin/uv pip install -q -r requirements.txt --python .venv/bin/python'
systemctl restart $SVC
sleep 4
systemctl is-active $SVC && echo "deployed \$after"
EOS
chmod 750 "$APP/update.sh"; chown root:"$SITE_USER" "$APP/update.sh"
sudo -u "$SITE_USER" -H git -C "$APP" fetch -q origin "$BRANCH" && sudo -u "$SITE_USER" -H git -C "$APP" checkout -q -B "$BRANCH" "origin/$BRANCH"

cat > "/etc/systemd/system/$SVC-update.service" <<EOS
[Unit]
Description=Pull $BRANCH for $DOMAIN and restart $SVC when it changed
After=network-online.target

[Service]
Type=oneshot
ExecStart=$APP/update.sh
EOS
cat > "/etc/systemd/system/$SVC-update.timer" <<EOS
[Unit]
Description=Check $DOMAIN $BRANCH branch every 2 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=2min
AccuracySec=20s

[Install]
WantedBy=timers.target
EOS
systemctl daemon-reload
systemctl enable --now "$SVC-update.timer" >/dev/null
systemctl list-timers "$SVC-update.timer" --no-pager | head -3
echo "DONE — $SVC follows origin/$BRANCH; log: journalctl -u $SVC-update -n 20"
