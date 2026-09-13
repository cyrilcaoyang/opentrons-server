# Tailscale on the Opentrons robots

How `tailscaled` is installed and kept running on the robots' Buildroot /
OT3 system, and how to upgrade it. Both the OT-2 and the Flex have a **read-only root filesystem**,
no package manager, no `nano` (use `vi`), and no TUN driver, so Tailscale runs
as a static binary from the persistent `/data` partition in
**userspace-networking** mode, started by a small systemd unit.

Both robots follow this layout. Neither gateway *depends* on it for control
any more (see `DEVICE_BRINGUP.md` *Network paths*): HTE is reached by wire,
Complexation through the UPLC PC USB bridge. Tailscale on the robot is for
SSH access (`tailscale up --ssh`) and for the Opentrons App.

## Layout

| Path | Purpose |
|---|---|
| `/data/tailscale_<ver>_arm/` | one directory per installed version (`tailscaled`, `tailscale`) |
| `/data/tailscale` | **symlink to the active version** — everything else refers to this, so an upgrade is a symlink swap |
| `/data/start_tailscale.sh` | boot script: starts `tailscaled` if not running, then `tailscale up --ssh` |
| `/etc/systemd/system/tailscale-autostart.service` | oneshot unit that runs the script after `network-online.target` |
| `/var/lib/tailscale/tailscaled.state` | node identity + prefs (persists across reboots; **never delete** or the robot becomes a new machine and needs re-auth + re-tagging) |
| `/tmp/tailscaled.log`, `/tmp/tailscale-up.log`, `/tmp/tailscale-watch.log` | logs, volatile |

Architecture: the **OT-2** (Raspberry Pi 3B+, `armv7l`) takes the 32-bit
**`arm`** build; the **Flex** (`aarch64`) takes **`arm64`**. Check `uname -m`
before downloading.

Installs before 2026-09 hard-coded `/data/tailscale_1.82.0_arm/` in the
script; the symlink layout replaces that. If a robot still has the old
script, the upgrade below rewrites it.

## Fresh install

1. On your computer, fetch the latest static build and verify it:

   ```bash
   V=$(curl -s https://pkgs.tailscale.com/stable/ | grep -o 'tailscale_[0-9.]*_arm\.tgz' | sort -uV | tail -1 | sed 's/tailscale_\(.*\)_arm.tgz/\1/')
   curl -O https://pkgs.tailscale.com/stable/tailscale_${V}_arm.tgz
   curl -s https://pkgs.tailscale.com/stable/tailscale_${V}_arm.tgz.sha256; echo; sha256sum tailscale_${V}_arm.tgz
   scp -i ~/.ssh/ot2_ssh_key -O tailscale_${V}_arm.tgz root@<robot>:/data/
   ```

2. On the robot (`ssh -i ~/.ssh/ot2_ssh_key root@<robot>`):

   ```sh
   cd /data && tar -xzf tailscale_${V}_arm.tgz && ln -sfn /data/tailscale_${V}_arm /data/tailscale
   /data/tailscale/tailscaled --tun=userspace-networking > /tmp/tailscaled.log 2>&1 &
   sleep 5
   /data/tailscale/tailscale up --ssh
   ```

   Open the printed URL, authenticate, then in the Tailscale admin console
   tag the new machine `tag:tailscale-SSH` (and `tagged-devices` like its
   siblings). Test `ssh root@<tailnet-ip>` before continuing.

3. Boot script, `/data/start_tailscale.sh` (`/data` is writable, no remount).
   This is the version deployed on both robots on 2026-09-06; every line of
   the comment block was earned by an outage (see *Traps*).

   ```sh
   #!/bin/sh
   # Started by tailscale-autostart.service (Type=oneshot, RemainAfterExit).
   #  * pidof, not ps|grep: any shell whose argv mentions the daemon name would
   #    otherwise trip the check and the daemon would never start.
   #  * This script must ALWAYS exit 0. A failed oneshot makes systemd kill the
   #    unit's whole cgroup -- the daemon included.
   #  * `tailscale up` is only needed for the first login; prefs (WantRunning,
   #    --ssh) live in /var/lib/tailscale/tailscaled.state and the daemon
   #    reconnects on its own. So its outcome is logged, never fatal.
   TS=/data/tailscale
   LOG=/tmp/tailscale-watch.log
   if ! pidof tailscaled > /dev/null; then
       echo "$(date) starting tailscaled" >> $LOG
       nohup $TS/tailscaled --tun=userspace-networking > /tmp/tailscaled.log 2>&1 &
       i=0
       while [ $i -lt 30 ] && [ ! -S /var/run/tailscale/tailscaled.sock ]; do i=$((i+1)); sleep 1; done
       if $TS/tailscale up --ssh --timeout=60s >> /tmp/tailscale-up.log 2>&1; then
           echo "$(date) tailscale up ok" >> $LOG
       else
           echo "$(date) tailscale up rc=$? -- daemon left running, will reconnect on its own" >> $LOG
       fi
   else
       echo "$(date) tailscaled already running" >> $LOG
   fi
   exit 0
   ```

   `chmod +x /data/start_tailscale.sh`

4. systemd unit. `/etc` is on the read-only root, so remount once; a reboot
   restores read-only automatically.

   ```sh
   mount -o remount,rw /
   vi /etc/systemd/system/tailscale-autostart.service
   ```

   ```ini
   [Unit]
   Description=Start tailscaled on boot
   Wants=network-online.target
   After=network-online.target

   [Service]
   Type=oneshot
   ExecStart=/data/start_tailscale.sh
   RemainAfterExit=yes

   [Install]
   WantedBy=multi-user.target
   ```

   ```sh
   systemctl daemon-reload
   systemctl enable tailscale-autostart.service
   systemctl start tailscale-autostart.service
   mount -o remount,ro /
   ```

5. Verify: `/data/tailscale/tailscale status`,
   `systemctl status tailscale-autostart.service`, then reboot and watch the
   machine go offline → connected in the admin console.

## Upgrade (keeps the node identity, no re-auth)

Identity lives in `/var/lib/tailscale/tailscaled.state`, not in the binary
directory, so swapping binaries is safe. Order matters: stop the daemon
before replacing the symlink; the new daemon reads the same state file.

```sh
# on the robot, after scp'ing the new tgz to /data/
cd /data && tar -xzf tailscale_${V}_arm.tgz
killall tailscaled; sleep 2
ln -sfn /data/tailscale_${V}_arm /data/tailscale
/data/start_tailscale.sh          # restarts the daemon via the symlink
/data/tailscale/tailscale version
/data/tailscale/tailscale status | head -3
rm -rf /data/tailscale_<old>_arm /data/tailscale_${V}_arm.tgz   # once the new one is confirmed up
```

The SSH session you are in survives if you came in over the wired or USB
path. If you came in over Tailscale SSH, expect the session to drop at
`pkill` and reconnect ~10 s later; the boot unit is not involved, but running
the script by hand is exactly what it would do.

## Wi-Fi watchdog (all three robots, since 2026-09-06)

Because the radio wedge recurs, each robot now checks its own Wi-Fi every
minute and reloads the driver when it has died — so the tailnet and SSH come
back on their own instead of waiting for someone to notice a grey tile.

**How often it fires (measured 2026-09-08 → 13, kernel journal):** the
firmware hangs **23–43 times a day on each OT-2**, at every hour of the day —
Complexation 28 / 23 / 26 / 27 / 43 reloads on 09-09 … 09-13, HTE 41 / 38 /
43 / 39. Not power (no throttling or under-voltage), not heat (49 °C), not the
AP (every disconnect is locally generated), not scans (none requested), signal
−63 dBm with power save off. It is the Broadcom 43430 firmware on the 4.14
kernel, and nothing short of bypassing that radio stops it. This was invisible
while both control paths were wired/USB; it became the Complexation robot's
availability the night its USB link died (2026-09-12) and control moved to
Wi-Fi + Tailscale.

| Path | Purpose |
|---|---|
| `/data/wifi_watchdog.sh` | the check and the recovery (below) |
| `/data/install_wifi_watchdog.sh` | one-shot installer for the two units (remounts `/` rw, then ro) |
| `/etc/systemd/system/wifi-watchdog.{service,timer}` | oneshot service + timer (`OnBootSec=3min`, `OnUnitActiveSec=1min`, `AccuracySec=10s` — was 2 min / 30 s until 2026-09-13) |
| `/tmp/wifi-watchdog.log` | every decision; empty while healthy |

The source of truth for both files is `tools/ot2_wifi_watchdog/` in this repo
(deployed 2026-09-13 to Complexation; HTE still runs the 2026-09-06 version).
To (re)install: copy both to `/data/` on the robot, then
`sh /data/install_wifi_watchdog.sh`.

How it decides: **healthy** means a ping to `1.1.1.1` (or `8.8.8.8`) leaves
and returns over the station interface (`wlan0` on the OT-2, `mlan0` on the
Flex — the Flex's `uap0` access-point interface is never picked). The campus
gateway does not answer ping, so it is not the probe. Ping is only the
*probe*, though — since 2026-09-13 (Complexation; HTE still runs the 09-06
version) the *judge* is local evidence that the firmware is hung, so an
internet blip never reloads a healthy radio: a failed ping is confirmed
**15 s later** and acted on only if `dmesg` shows a `brcmfmac … -110` error in
the last 90 s or `iw dev wlan0 link` no longer answers; otherwise it logs
"radio looks healthy" and does nothing. Reloads are at most **once per
2 min** (was 10 min), and the script always exits 0. Recovery is
`modprobe -r brcmfmac; modprobe brcmfmac` where that driver is loaded (the
OT-2s), or `nmcli radio wifi off/on` otherwise (the Flex has an NXP chip),
followed by a rescan and `nmcli con up <profile>`, retried once — brought up
too early, the fresh driver has not scanned and the profile fails with "The
Wi-Fi network could not be found" (6 of 44 reloads on 2026-09-13 did).

Why the 2026-09-13 tightening: with a 2-min timer, two-failure rule and 10-min
lockout, each hang cost the gateway 2–17 min of "robot unreachable" (median
4 min; back-to-back hangs hit the lockout — 40 lockout waits against 43
reloads that day) — about 3.2 h of the 12.6 h after the repoint, and one PyPoe
down/recovered pair every 30–50 min. The new cycle bounds a hang at roughly
1–2 min, under the dashboard's two-sweep alert threshold for most events.
**Verified live on the first hang after the deploy (18:15 UTC the same day):**
the `-110` detector fired on the first failed check with no confirm wait, the
driver was reloaded 8 s after the gateway lost the robot, and the gateway had
it back after **21 s** (watchdog: check failed 18:15:41, reload 18:15:42,
recovered 18:16:07). The dashboard's 60 s sweep never saw it. The
previous script and installer are kept on the robot as
`/data/*.bak-20260913`; restoring them and re-running the installer reverts.

Like the Tailscale unit, the units live in `/etc` and vanish with an OS
update; `sh /data/install_wifi_watchdog.sh` puts them back. Check it with
`systemctl list-timers | grep wifi-watchdog` and `tail /tmp/wifi-watchdog.log`.

## Per-robot `up` flags

The `tailscale up` line is not identical on every robot, and `up` refuses to
run if you drop a flag that is already in the saved prefs (it asks for
`--reset`). Keep each robot's line when rewriting the script:

| robot | `up` flags |
|---|---|
| HTE, Flex | `--ssh` |
| Complexation | `--ssh --advertise-tags=tag:sdl2-devices,tag:tailscale-ssh --accept-dns=false --hostname=sdl2-ot2-training` |

`--accept-dns=false` is worth adopting everywhere at the next touch: in
userspace-networking mode the robot gains nothing from MagicDNS and it keeps
Tailscale out of `resolv.conf`. `--hostname` is why the tailnet shows
`sdl2-ot2-training` while the registry calls it `sdl2-ot2-complexation`.

## Traps

- **The OT-2's Wi-Fi chip wedges, and it is a driver fault, not the campus
  network.** Observed directly on HTE 2026-09-06: NetworkManager still says
  `wlan0: connected`, but `dmesg` fills with
  `brcmfmac: brcmf_proto_bcdc_msg failed w/status -110`, `iw dev wlan0 link`
  times out, ARP to the gateway stays `INCOMPLETE`, and no packet passes.
  Complexation shows the end stage of the same fault (`wlan0` disconnected,
  empty scan). **Recovery without a reboot**, over the wired/USB path:

  ```sh
  modprobe -r brcmfmac; sleep 3; modprobe brcmfmac; sleep 10
  nmcli con up compsci
  ```

  Wi-Fi was back and `tailscaled` reconnected on its own within a minute
  (and again on Complexation from the UPLC PC later the same night). The
  watchdog above now does this automatically; do it by hand only if the log
  shows it gave up. Do **not** `nmcli con down compsci` first — it is not needed, and it
  leaves the profile inactive so the following `up` has to name it
  explicitly.

- **A non-zero exit from the boot script kills the daemon.** When a
  `Type=oneshot` unit's `ExecStart` fails, systemd stops the unit and kills
  everything left in its cgroup — the `nohup`'d `tailscaled` included. The
  original script ended with a bare `tailscale up --ssh`, so any `up` failure
  (control plane unreachable at boot, the 60 s timeout, a DNS blip) took the
  daemon down with it. That is what killed HTE's Tailscale at every boot from
  2026-08-14 to 2026-09-06. The script now always exits 0 and only logs the
  `up` result; the daemon reconnects on its own once it has a route.
- **Start the daemon through the unit, never from your SSH session.** A
  `tailscaled` launched by hand (even with `nohup`) belongs to the login
  session and dies when that session ends. Use
  `systemctl restart tailscale-autostart.service` and confirm
  `systemctl status` lists `tailscaled` under the unit's `CGroup`.
- **The running-check must be `pidof`.** The original `ps | grep
  '[t]ailscaled'` also matched any shell whose command line mentioned the
  daemon (e.g. the very `ssh … 'killall tailscaled; systemctl restart …'`
  that was trying to fix it), so the script logged "already running" and
  started nothing. Busybox `ps` shows every process; this bit HTE, not the
  Flex.

- **A wired static config can black-hole the robot's internet.** HTE's
  `/etc/network/interfaces` (Buildroot `ifupdown`, runs before
  NetworkManager) gave `eth0` a `gateway 192.168.254.230` that does not
  exist on the lab switch. Being metric 0 it outranked the Wi-Fi default
  (metric 600), so *every* internet-bound packet was dropped: no NTP, no
  Tailscale control plane, `tailscale up` hanging at boot. The lab switch is
  on-link only — the gateway needs no default route there. Fixed 2026-09-06
  by deleting the `gateway` line (backup at `/data/interfaces.bak-20260906`);
  the robot's internet is its Wi-Fi. Symptom to recognise: `ping 1.1.1.1`
  fails but `ping -I wlan0 1.1.1.1` works.
- **`/etc` edits do not survive an Opentrons OS update** (A/B root
  partitions). After an update, re-create the unit (§Fresh install step 4)
  and re-check `/etc/network/interfaces`. `/data` and `/var/lib/tailscale`
  persist.
- **Tailscale SSH is gated by the tailnet ACL, not by `--ssh`.** `RunSSH:
  true` on the node is necessary but the admin console must also tag the
  machine (`tag:tailscale-ssh`) or the connection is refused with `tailnet
  policy does not permit you to SSH to this node`. All three robots were
  tagged 2026-09-06 — and the refusal persisted from the Cytation PC, whose
  daemon log line reads `failed to evaluate policy, result: rejected`. The
  **source** matters too: the Cytation PC is itself a tagged device
  (`sdl2-pc-03-cytation`, `tag:sdl2-devices`), and an `ssh` ACL rule whose
  `src` is `autogroup:member` admits users, not tagged machines. To SSH
  robot-to-robot or from a device PC, the rule needs that tag in `src`;
  from a person's laptop it should already work. Key-based `sshd` on port 22
  over the tailnet address works regardless and is what the `*_tailscale`
  aliases in `~/.ssh/config` use.
- No `pkill` and no `timeout` on the OT-2; use `killall`, and run long probes
  under a client-side `timeout` in the `ssh` command. On the Flex, `route` and
  `ip` live in `/sbin`, which a non-login `ssh` command does not have on `PATH`.
- **The live route can differ from the file.** The Flex had `gateway
  192.168.254.230` in `/etc/network/interfaces` but `192.168.254.1` in the
  kernel table (the file had been edited after a 162-day-old boot). Always
  read `route -n` / `ip route` and delete the route that is actually there.
  The Flex's Wi-Fi interface is `mlan0`, not `wlan0`.

## Reaching each robot for this

| robot | path that does not depend on Tailscale | notes |
|---|---|---|
| `ot2_hte` (`ot2cytation`) | `ssh ot2_local` → `root@192.168.254.50`, lab switch | key `~/.ssh/ot2_ssh_key` (passphrase-protected; load it into `ssh-agent` first) |
| Flex (`sdl2-otflex-01`) | `ssh otflex_local` → `root@192.168.254.81`, lab switch | same key |
| `ot2_complexation` (`ot2training`) | `ssh -J sdl2@100.64.254.19 root@169.254.40.81` — the USB-B link, jumping through the UPLC PC | the robot key is authorized on the UPLC PC (`administrators_authorized_keys`, 2026-09-06). Direct from the Cytation PC there is no route; the bridge forwards port 31950 only. |

## Installed versions

| robot | tailscale | installed | notes |
|---|---|---|---|
| `ot2_hte` | **1.102.3** | 2026-09-06 (was 1.92.5 from 2026-01-15) | symlink layout; boot script has `--timeout`; bogus eth0 gateway removed the same day — online on the tailnet again after 3 weeks |
| Flex `sdl2-otflex-01` (100.64.254.92) | **1.102.3** arm64 | 2026-09-06 (was 1.84.0 from 2025-06-26, never auto-started: no unit existed) | unit created, bogus eth0 gateway removed; back online after ~200 days offline. Its `opentrons-robot-server` has been stopped since 2026-03-30 — separate issue, untouched |
| `ot2_complexation` `sdl2-ot2-complexation` (100.64.254.91) | **1.102.3** arm | 2026-09-06 (was 1.92.5 from 2026-01-19) | reached via the UPLC PC jump host; Wi-Fi recovered with the `brcmfmac` reload, no reboot. Its `up` line carries extra flags (see *Per-robot `up` flags*) |
