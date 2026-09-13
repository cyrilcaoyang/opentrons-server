#!/bin/sh
# /data/wifi_watchdog.sh -- recover a wedged Wi-Fi radio on an Opentrons robot.
# Run by wifi-watchdog.timer every 1 min (docs/OT2_TAILSCALE.md, Traps).
#
# Symptom this exists for: the OT-2's Broadcom firmware (brcmfmac) stops
# talking (-110 timeouts in dmesg); NetworkManager may still say "connected"
# but no packet passes, scans return nothing, and only a driver reload or a
# reboot brings it back. Measured 2026-09-08..13: 23-43 hangs per day on
# EACH OT-2, at any hour. Since 2026-09-12 the Complexation robot is
# CONTROLLED over this Wi-Fi (its USB link died), so every minute of outage
# is a minute the gateway cannot reach the robot -- recovery must be fast.
#
# Rules: always exit 0; never reload on one bad ping (confirm CONFIRM_S later,
# or by a local sign of the hang); never reload a radio that looks healthy
# locally just because the internet is unreachable (ping is the probe; dmesg
# and iw are the judge); never reload more than once per LOCKOUT_S; log every
# decision to /tmp/wifi-watchdog.log (empty while healthy).
export PATH=$PATH:/sbin:/usr/sbin
LOG=/tmp/wifi-watchdog.log
LAST=/tmp/wifi-watchdog.last-reload
CONFIRM_S=15      # wait before the confirming ping
LOCKOUT_S=120     # minimum gap between two reloads
RECENT_S=90       # a brcmfmac -110 kernel error this recent = firmware hung
log() { echo "$(date -u +%FT%TZ) $*" >> "$LOG"; }

# The station interface: wlan0 on the OT-2, mlan0 on the Flex (which also has
# a uap0 access-point interface that must never be picked). Prefer the device
# the wireless profile is bound to; fall back to the first station device.
CON=$(nmcli -t -f NAME,TYPE con show 2>/dev/null | awk -F: '$2=="802-11-wireless"{print $1; exit}')
WIFI_IF=$(nmcli -t -f NAME,DEVICE con show --active 2>/dev/null | awk -F: -v c="$CON" '$1==c && $2!=""{print $2; exit}')
[ -n "$WIFI_IF" ] || WIFI_IF=$(nmcli -t -f DEVICE,TYPE dev status 2>/dev/null | awk -F: '$2=="wifi" && $1 !~ /^(uap|p2p)/{print $1; exit}')
[ -n "$WIFI_IF" ] || exit 0

# Healthy = a packet actually leaves and returns over the Wi-Fi interface.
# (The campus gateway does not answer ping, so probe a public resolver.)
ping_ok() {
    ping -I "$WIFI_IF" -c1 -W3 1.1.1.1 >/dev/null 2>&1 || ping -I "$WIFI_IF" -c1 -W3 8.8.8.8 >/dev/null 2>&1
}
# Local evidence that the firmware is hung, independent of the internet:
# a brcmfmac -110 error in the last RECENT_S seconds of the kernel log ...
recent_110() {
    now=$(cut -d. -f1 /proc/uptime)
    dmesg 2>/dev/null | awk -v now="$now" -v w="$RECENT_S" \
        '/brcmfmac.*-110/ { t = substr($1, 2) + 0; if (now - t <= w) found = 1 } END { exit found ? 0 : 1 }'
}
# ... or the driver no longer answering a link query (it times out when hung).
link_dead() {
    ! timeout 5 iw dev "$WIFI_IF" link 2>/dev/null | grep -q 'signal:'
}

ping_ok && exit 0

log "wifi check failed on $WIFI_IF"
if recent_110; then
    why="kernel -110 errors within ${RECENT_S}s"
else
    sleep "$CONFIRM_S"
    if ping_ok; then log "passed again after ${CONFIRM_S}s; no action"; exit 0; fi
    if recent_110; then
        why="kernel -110 errors after ${CONFIRM_S}s confirm"
    elif link_dead; then
        why="iw link unreadable after ${CONFIRM_S}s confirm"
    else
        log "internet unreachable but radio looks healthy (iw link ok, no -110); not reloading"
        exit 0
    fi
fi

now=$(date +%s); last=$(cat "$LAST" 2>/dev/null || echo 0)
if [ $(( now - last )) -lt "$LOCKOUT_S" ]; then
    log "hung ($why) but reloaded $(( now - last ))s ago; waiting"
    exit 0
fi
echo "$now" > "$LAST"

if lsmod 2>/dev/null | grep -q '^brcmfmac'; then
    log "reloading brcmfmac: $why (dmesg: $(dmesg | grep -c 'brcmfmac.*-110') -110 errors so far)"
    modprobe -r brcmfmac 2>>"$LOG"; sleep 3; modprobe brcmfmac 2>>"$LOG"; sleep 10
else
    log "no brcmfmac; toggling the radio instead ($why)"
    nmcli radio wifi off; sleep 3; nmcli radio wifi on; sleep 10
fi
# Bring the profile back up. Right after a reload the fresh driver has not
# scanned yet, and `nmcli con up` then fails with "The Wi-Fi network could
# not be found" (6 of 44 reloads on 2026-09-13) -- rescan first, retry once.
if [ -n "$CON" ]; then
    i=0; while [ $i -lt 10 ] && ! nmcli -t -f DEVICE dev status 2>/dev/null | grep -qx "$WIFI_IF"; do sleep 2; i=$((i+1)); done
    nmcli dev wifi rescan >/dev/null 2>&1; sleep 5
    if ! nmcli con up "$CON" >>"$LOG" 2>&1; then
        log "nmcli con up failed once; rescanning and retrying"
        sleep 10; nmcli dev wifi rescan >/dev/null 2>&1; sleep 5
        nmcli con up "$CON" >>"$LOG" 2>&1 || log "nmcli con up failed twice; leaving it to NetworkManager autoconnect"
    fi
fi
sleep 5
if ping_ok; then
    log "recovered: $WIFI_IF passes traffic again"
else
    log "still down after reload"
fi
exit 0
