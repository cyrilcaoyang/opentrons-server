<#
.SYNOPSIS
    One-screen preflight for the OT-2 gateways before a bench session.

.DESCRIPTION
    Reads /status from each gateway and prints the handful of fields an operator
    actually checks before touching a robot: health, whether anything is on a
    head, who holds the claim, and what the tip tracker believes about each
    rack. Read-only -- it issues nothing but GETs, so it is safe at any time,
    including mid-run.

    It exists because those fields are scattered across a deep `details` blob,
    and the two that matter most are easy to miss:

    * `mounted tips` -- a tip left on a head by an earlier run. The gateway now
      records this across restarts, so "none" here is a real answer rather than
      an absence of memory.
    * `channels` / `volumes` -- the two pipette bindings, judged SEPARATELY
      because they resolve differently. Both come from the robot's instrument
      report, via a /control/setup recipe or (declared-deck flow) per-call by
      mount. `volumes` empty means the live per-pipette guard is genuinely
      inactive and only the 0-1000 uL schema bound applies. `channels` empty
      with a reachable probe is normal on a declared deck -- mount-addressed
      picks still resolve; only a nickname-addressed pipette with no recipe
      falls back to 1 tip, which would track an 8-channel pick as one.
      The script spells out which case it is, because both are silent.

    Tip-rack counts are the tracker's memory, not an observation. The gateway
    cannot see a refill, so a count only means something if picks/drops or an
    explicit tips/reset put it there.

.PARAMETER Ports
    Gateway ports to probe. Defaults to both robots: 8020 (hte), 8021
    (complexation).

.PARAMETER BaseHost
    Host serving the gateways. Loopback by default, since this normally runs on
    the device PC itself.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\ot2-preflight.ps1

.EXAMPLE
    # Just the robot you are working on
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\ot2-preflight.ps1 -Ports 8021
#>
param(
    [int[]]$Ports = @(8020, 8021),
    [string]$BaseHost = "127.0.0.1"
)

foreach ($p in $Ports) {
    try {
        $s = Invoke-RestMethod -Uri "http://${BaseHost}:$p/status" -TimeoutSec 10
    } catch {
        Write-Output ""
        Write-Output ("=== port {0} UNREACHABLE -- {1}" -f $p, $_.Exception.Message)
        continue
    }
    $d = $s.details

    $err   = if ($s.last_error) { $s.last_error.code } else { "none" }
    $claim = if ($d.claimed_by) { $d.claimed_by.owner } else { "free" }
    $mnt   = if ($d.mounted_tips.PSObject.Properties.Count -gt 0) {
                 ($d.mounted_tips | ConvertTo-Json -Compress) } else { "none" }
    $plate = if ($d.loaded_plate) { $d.loaded_plate.plate_id } else { "none loaded" }

    $bound = ($d.pipette_channels.PSObject.Properties.Count -gt 0)
    $chan  = if ($bound) { ($d.pipette_channels | ConvertTo-Json -Compress) } else { "UNBOUND" }
    $vols  = if ($d.pipette_volumes.PSObject.Properties.Count -gt 0) {
                 ($d.pipette_volumes | ConvertTo-Json -Compress) } else { "UNBOUND" }

    Write-Output ""
    Write-Output ("=== {0}  (port {1})" -f $s.equipment_id, $p)
    Write-Output ("  state    : {0} / activity {1} / service {2}" -f `
        $s.equipment_status, $s.activity, $d.service_state)
    Write-Output ("  up       : {0} min | last_error: {1}" -f `
        [math]::Round($s.uptime_seconds / 60, 1), $err)
    Write-Output ("  claim    : {0}" -f $claim)
    Write-Output ("  mounted  : {0}" -f $mnt)
    Write-Output ("  channels : {0}" -f $chan)
    Write-Output ("  volumes  : {0}" -f $vols)

    foreach ($k in ($d.tip_racks.PSObject.Properties.Name | Sort-Object { [int]$_ })) {
        $r = $d.tip_racks.$k
        Write-Output ("  rack {0,-2}  : {1}/{2} fresh, {3} empty, {4} on head, {5} touched" -f `
            $k, $r.available, $r.total, $r.empty, $r.on_pipette, $r.touched)
    }
    Write-Output ("  plate    : {0}" -f $plate)

    # Interpretation, not just fields. The two bindings are independent and must
    # be judged separately: keying both off `channels` claimed the volume guard
    # was inactive on a declared-deck robot where it is in fact live off the
    # probe -- the exact over-warning this script exists to prevent.
    $volBound   = ($d.pipette_volumes.PSObject.Properties.Count -gt 0)
    $haveProbe  = (@($d.robot.instruments).Count -gt 0)

    if (-not $volBound) {
        Write-Output "  NOTE     : per-pipette volume guard INACTIVE -- only the schema bound"
        Write-Output "             (0-1000 uL) applies, so an over-volume aspirate reaches the robot."
        if ($haveProbe) {
            # The probe knows the limits, so the gateway is the part that cannot
            # read them: a build older than the mount-addressed fallback.
            Write-Output "             The robot probe DOES report limits, so this gateway predates"
            Write-Output "             the probe fallback -- pull and restart the service to fix it."
        } else {
            Write-Output "             No robot probe to read limits from; resolves when the robot"
            Write-Output "             is reachable."
        }
    }
    if (-not $bound) {
        if ($haveProbe) {
            Write-Output "  NOTE     : no /control/setup, so channel counts are resolved per-call"
            Write-Output "             from the robot probe (declared-deck flow). Mount-addressed"
            Write-Output "             picks track correctly; a nickname-addressed pipette with no"
            Write-Output "             recipe would fall back to 1 tip."
        } else {
            Write-Output "  NOTE     : channel counts UNKNOWN and no robot probe to fall back on."
            Write-Output "             A multi-channel pick would be tracked as 1 tip. Do not run"
            Write-Output "             an 8-channel protocol until this resolves."
        }
    }
    $onHead = 0
    foreach ($k in $d.tip_racks.PSObject.Properties.Name) { $onHead += [int]$d.tip_racks.$k.on_pipette }
    if ($onHead -gt 0) {
        Write-Output ("  NOTE     : {0} well(s) read on_pipette -- a tip is off the rack. Resolve it" -f $onHead)
        Write-Output "             (return to its own well, or drop to trash) before a new run."
    }
}
