#!/bin/sh
# One-shot installer for the Wi-Fi watchdog units. Needs the read-only root
# remounted, so it is run by a human: sh /data/install_wifi_watchdog.sh
# 2026-09-13: timer tightened 2min -> 1min (AccuracySec 30s -> 10s); the
# script now confirms after 15 s and reloads at most once per 2 min.
set -e
[ -x /data/wifi_watchdog.sh ] || { echo "missing /data/wifi_watchdog.sh"; exit 1; }
mount -o remount,rw /
cat > /etc/systemd/system/wifi-watchdog.service <<'UNIT'
[Unit]
Description=Recover a wedged Wi-Fi radio (see /data/wifi_watchdog.sh)
After=network.target

[Service]
Type=oneshot
ExecStart=/data/wifi_watchdog.sh
UNIT
cat > /etc/systemd/system/wifi-watchdog.timer <<'UNIT'
[Unit]
Description=Run the Wi-Fi watchdog every minute

[Timer]
OnBootSec=3min
OnUnitActiveSec=1min
AccuracySec=10s

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now wifi-watchdog.timer
systemctl restart wifi-watchdog.timer
sync; mount -o remount,ro /
echo "--- installed:"; systemctl list-timers --no-pager 2>/dev/null | grep -i wifi-watchdog || systemctl is-active wifi-watchdog.timer
mount | grep " / "
