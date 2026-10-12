<#
.SYNOPSIS
    Exercise the tip lifecycle on real hardware and print the rack at each step.

.DESCRIPTION
    Verifies end-to-end what unit tests can only assert in isolation: that a
    pick marks its origin well `on_pipette` immediately, that the mount records
    where the tip came from, and that returning it restores the well.

    ACTUATES. It moves the head and consumes one tip's worth of pick/return
    cycles. No liquid is aspirated, so the tip comes back clean and the well
    returns to "new" rather than a sample id.

    Prints a plan and stops unless -Run is given.

    Either robot (-Port 8021 Complexation, the default, or 8020 HTE), per
    AGENT_RULES.md rule 4a: run it only when a human asks, and only while the
    robot is free. With -Run it refuses unless the gateway is idle with no
    plan or automation, no claim, no external run and no tip on a head.

.PARAMETER ApiKey
    Optional override. Normally omitted: the key is resolved automatically, in
    order, from $env:OT2_API_KEYS then the gateway service's own NSSM
    environment. Claims are login-gated, so some key is required -- the point of
    resolving it here is that a hand-pasted one is the step that keeps failing.

    The value is never printed. Only its source and principal name are.

.PARAMETER Service
    NSSM service to read OT2_API_KEYS from. Defaults from -Port.

.PARAMETER Run
    Actually do it. Without this the script only prints what it would do.

.EXAMPLE
    # Look before you leap
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\ot2-tip-lifecycle-check.ps1 -ApiKey KEY

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\ot2-tip-lifecycle-check.ps1 -ApiKey KEY -Run
#>
param(
    [string]$ApiKey,
    [int]$Port = 8021,
    [string]$Service,
    [string]$Pipette = "left",
    [switch]$Run
)

$ErrorActionPreference = "Stop"

if (-not $Service) {
    $Service = switch ($Port) {
        8020 { "ot2-gateway-hte" }
        8021 { "ot2-gateway-complexation" }
        default { $null }
    }
}

function Split-KeyEntry {
    <# OT2_API_KEYS is "name:key" pairs, comma separated; a bare key is also
       legal and reports as the unnamed principal. Splits on the FIRST colon so
       a key containing one survives. #>
    param([string]$Entry)
    $entry = $Entry.Trim()
    if (-not $entry) { return $null }
    $i = $entry.IndexOf(":")
    if ($i -lt 1) { return @{ name = "api:unnamed"; key = $entry } }
    return @{ name = $entry.Substring(0, $i); key = $entry.Substring($i + 1) }
}

function Resolve-ApiKey {
    <# Explicit flag wins, then this shell's env, then the service's own env.
       Returns @{key; name; source} and never emits the key itself. #>
    param([string]$Explicit, [string]$ServiceName)

    if ($Explicit) { return @{ key = $Explicit; name = "(supplied)"; source = "-ApiKey" } }

    if ($env:OT2_API_KEYS) {
        $parsed = Split-KeyEntry (($env:OT2_API_KEYS -split ",")[0])
        if ($parsed) { return @{ key = $parsed.key; name = $parsed.name; source = '$env:OT2_API_KEYS' } }
    }

    if ($ServiceName) {
        $nssm = "C:\SDL_Tools\nssm.exe"
        if (Test-Path $nssm) {
            # `nssm get` reads service config and works unelevated -- but it
            # writes "LsaOpenPolicy(): Access is denied" to stderr while doing
            # so, and under ErrorActionPreference=Stop a native command's
            # stderr is a TERMINATING error even with 2>$null. So relax it for
            # exactly this call: the warning is noise, the stdout is the answer.
            $prev = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            $raw = & $nssm get $ServiceName AppEnvironmentExtra 2>$null
            $ErrorActionPreference = $prev
            $line = @($raw -split "`r?`n" | Where-Object { $_ -like "OT2_API_KEYS=*" })[0]
            if ($line) {
                $parsed = Split-KeyEntry (($line.Substring("OT2_API_KEYS=".Length) -split ",")[0])
                if ($parsed) {
                    return @{ key = $parsed.key; name = $parsed.name; source = "$ServiceName service env" }
                }
            }
        }
    }

    throw ("No API key found. Tried -ApiKey, `$env:OT2_API_KEYS, and " +
           "$ServiceName's OT2_API_KEYS. Claims are login-gated, so one is required.")
}

$resolved = Resolve-ApiKey -Explicit $ApiKey -ServiceName $Service
$ApiKey = $resolved.key

$base = "http://127.0.0.1:$Port"
$auth = @{ "X-Api-Key" = $ApiKey }

function Get-Racks {
    $d = (Invoke-RestMethod -Uri "$base/status" -TimeoutSec 10).details
    return $d
}

$d = Get-Racks
Write-Host ("robot    : {0}" -f $d.robot.robot_name)
Write-Host ("key      : {0} (principal {1})" -f $resolved.source, $resolved.name)
Write-Host ("pipette  : {0} -> {1}" -f $Pipette,
    (($d.robot.instruments | Where-Object { $_.mount -eq $Pipette }).name))
foreach ($k in ($d.tip_racks.PSObject.Properties.Name | Sort-Object { [int]$_ })) {
    $r = $d.tip_racks.$k
    Write-Host ("rack {0,-2}  : {1}/{2} fresh, {3} on head" -f $k, $r.available, $r.total, $r.on_pipette)
}

if (-not $Run) {
    Write-Host ""
    Write-Host "PLAN (nothing done -- pass -Run to execute):" -ForegroundColor Yellow
    Write-Host "  1. claim the gateway"
    Write-Host ("  2. POST /control/pick-up-tip {{pipette: {0}}}  -- gateway auto-selects rack + well" -f $Pipette)
    Write-Host "  3. read the origin well: expect on_pipette, and a mount record"
    Write-Host "  4. POST /control/drop-tip back into that same well"
    Write-Host "  5. read it again: expect new (the tip never touched liquid)"
    Write-Host "  6. home the gantry, then release the claim"
    exit 0
}

# A claim held by someone else -- very often the operator's own panel session --
# makes the POST below a 409 and a raw WebException. Name the holder instead:
# the fix is a click in the panel, not a retry.
$now = Invoke-RestMethod -Uri "$base/status" -TimeoutSec 10
$busy = @()
if ($now.activity -ne "idle") { $busy += "activity is $($now.activity)" }
if ($now.details.automation) { $busy += "a plan or automation is running" }
if ($now.details.robot.run_active -ne $false) { $busy += "the robot reports an active run (or its run list is unreadable)" }
if ($now.details.mounted_tips.PSObject.Properties.Count -gt 0) { $busy += "a tip is already on a head" }
if ($busy.Count -gt 0) {
    throw ("Refusing: the robot is not free -- {0}. Wait until it is idle, then re-run." -f ($busy -join "; "))
}
$held = $now.details.claimed_by
if ($held) {
    # One format string, not a concatenation: `-f` binds to the string
    # immediately left of it, so ("a {0}" + "b {1}" -f $x, $y) formats only the
    # second half and leaves {0} literal.
    $msg = "The gateway is claimed by {0} (expires {1}). Release control in the operator panel, or wait for the claim to expire, then re-run."
    throw ($msg -f $held.owner, $held.expires_at)
}

$claim = Invoke-RestMethod -Uri "$base/control/claim" -Method Post -Headers $auth `
    -ContentType "application/json" -TimeoutSec 15 `
    -Body (@{ owner = "operator:tip-lifecycle-check"; session_id = [guid]::NewGuid().ToString(); ttl_s = 120 } | ConvertTo-Json)
$hdr = @{ "X-Api-Key" = $ApiKey; "X-Claim-Token" = $claim.claim_token }
Write-Host "claimed" -ForegroundColor Green

try {
    Invoke-RestMethod -Uri "$base/control/pick-up-tip" -Method Post -Headers $hdr `
        -ContentType "application/json" -TimeoutSec 120 `
        -Body (@{ pipette = $Pipette } | ConvertTo-Json) | Out-Null

    $d = Get-Racks
    $m = $d.mounted_tips.$Pipette
    if (-not $m) { throw "picked, but no mount was recorded -- that is the bug this checks for." }
    $rack = $m.rack; $well = $m.well
    Write-Host ("picked   : rack {0} well {1} | contacted_liquid={2} | uncertain={3}" -f `
        $rack, $well, $m.contacted_liquid, $m.uncertain) -ForegroundColor Green
    Write-Host ("well now : {0}   <-- expect on_pipette" -f $d.tip_racks.$rack.tips.$well)

    Invoke-RestMethod -Uri "$base/control/drop-tip" -Method Post -Headers $hdr `
        -ContentType "application/json" -TimeoutSec 120 `
        -Body (@{ pipette = $Pipette; labware_nickname = $rack; position = $well } | ConvertTo-Json) | Out-Null

    $d = Get-Racks
    $after = $d.tip_racks.$rack.tips.$well
    if (-not $after) { $after = "new (absent from the map = fresh)" }
    Write-Host ("returned : {0}   <-- expect new" -f $after) -ForegroundColor Green
    $left = "none -- mount cleared"
    # -gt 0, not -ne 0: an empty object's property count comes back $null, and
    # ($null -ne 0) is TRUE -- which printed the raw "{}" instead of the plain
    # answer on the one line the check exists to report.
    if ($d.mounted_tips.PSObject.Properties.Count -gt 0) { $left = ($d.mounted_tips | ConvertTo-Json -Compress) }
    Write-Host ("mounted  : {0}" -f $left)
}
finally {
    # Home before releasing, whatever happened. `drop_tip` leaves the head over
    # the rack it just used -- the gateway does not home on its own, and there is
    # no reason it should mid-protocol -- but a bench tool that hands the robot
    # back parked over a deck slot is a tool that obstructs the next person and
    # starts the next operation from an arbitrary position. Homing is idempotent
    # and retracts Z first, so it is also the right thing after a failure.
    #
    # Guarded so a homing problem cannot mask the error that brought us here,
    # and so the claim is always released.
    try {
        Invoke-RestMethod -Uri "$base/control/home" -Method Post -Headers $hdr -TimeoutSec 120 | Out-Null
        Write-Host "homed"
    } catch {
        Write-Warning ("home failed: {0} -- the head may still be over the deck." -f $_.Exception.Message)
    }
    Invoke-RestMethod -Uri "$base/control/release" -Method Post -Headers $hdr -TimeoutSec 10 | Out-Null
    Write-Host "released"
}
