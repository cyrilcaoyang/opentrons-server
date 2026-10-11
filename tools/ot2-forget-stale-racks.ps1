<#
.SYNOPSIS
    Retire tip-rack entries the tracker still holds for slots that no longer
    carry a tip rack.

.DESCRIPTION
    A rack's identity in the tip store is the deck slot it sits in, so moving a
    rack (or swapping a plate in where one used to be) leaves the old slot
    tracked with tips that are not there. That entry is not inert: an
    unqualified pick_up_tip scans tracked racks in slot order and takes the
    first that is addressable, fits the pipette, and reports a free tip -- and
    a slot declared as a deep-well plate passes all three, because the
    tip-size check parses "_<n>ul" from the load_name and returns TRUE for
    anything it cannot classify (service.py `_rack_fits_pipette`). The head is
    then sent into a plate expecting tips.

    Since 2026-10-11 the gateway retires these itself whenever the deck is
    declared (service.py `_retire_racks_off_deck`). This script remains for
    records left from before that, and for drift no declaration has touched.

    This finds every tracked rack whose slot disagrees with the deck -- nothing
    there, or something there that is not a tip rack -- and marks all of its
    wells `empty` via /control/tips/mark, so `next_available` finds nothing and
    the auto-picker skips it with a stated reason.

    Marking also releases any mount whose tips came from those wells
    (`mark_tips` -> `_forget_mounts_from`, matched on the covered wells rather
    than the mount key), which is how a phantom "N tips on the head" record
    from a rack that is long gone gets cleared.

    METADATA ONLY. No motion, no liquid, no tip touched. It writes the
    tracker's map and one audited `tips_marked` event per slot.

    Prints a plan and stops unless -Run is given.

    Unlike ot2-tip-lifecycle-check.ps1 this does NOT refuse port 8020. That
    refusal exists because *actuating* HTE would collide with real campaigns;
    this script cannot move the robot, and HTE is where tracker drift actually
    has to be repaired. Repairing state is not bench testing.

.PARAMETER Slots
    Optional override: repair exactly these slots instead of the detected set.
    Use when you know the deck is mid-edit and the declaration cannot be
    trusted. Refuses a slot the tracker does not know.

.PARAMETER Status
    What the wells actually hold: `empty` (default -- the rack is gone) or
    `new`. Pass `new` only for a slot whose rack is physically present and
    full, which is not the case this script exists for.

.PARAMETER ApiKey
    Optional override. Normally omitted: the key is resolved automatically, in
    order, from $env:OT2_API_KEYS then the gateway service's own NSSM
    environment. tips/mark is claim-gated and claims are login-gated, so some
    key is required -- the point of resolving it here is that a hand-pasted one
    is the step that keeps failing.

    The value is never printed. Only its source and principal name are.

.PARAMETER Service
    NSSM service to read OT2_API_KEYS from. Defaults from -Port.

.PARAMETER Run
    Actually do it. Without this the script only prints what it would do.

.EXAMPLE
    # Look before you leap (HTE)
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\ot2-forget-stale-racks.ps1 -Port 8020

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\ot2-forget-stale-racks.ps1 -Port 8020 -Run
#>
param(
    [int]$Port = 8020,
    [string[]]$Slots,
    [ValidateSet("empty", "new")][string]$Status = "empty",
    [string]$ApiKey,
    [string]$Service,
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
           "$ServiceName's OT2_API_KEYS. tips/mark is claim-gated, so one is required.")
}

$resolved = Resolve-ApiKey -Explicit $ApiKey -ServiceName $Service
$ApiKey = $resolved.key

$base = "http://127.0.0.1:$Port"
$auth = @{ "X-Api-Key" = $ApiKey }

function Get-Details { (Invoke-RestMethod -Uri "$base/status" -TimeoutSec 15).details }

function Get-SlotOwner {
    <# What the deck says is on a slot, as "kind load_name", or $null when the
       slot is empty. Reads the live source first and falls back to the standing
       declaration: once a run occupies a declared slot, `slot_state` stops
       saying "declared" but `declared` is still populated. #>
    param($DeckSlots, [string]$Slot)
    $s = $DeckSlots.$Slot
    if (-not $s) { return $null }
    foreach ($lw in @($s.labware, $s.declared)) {
        if ($lw -and $lw.load_name) {
            return @{ load_name = $lw.load_name; kind = $lw.kind; is_tiprack = [bool]$lw.is_tiprack }
        }
    }
    if ($s.module -or $s.declared_module) {
        $m = if ($s.module) { $s.module } else { $s.declared_module }
        return @{ load_name = $m.module_name; kind = "module"; is_tiprack = $false }
    }
    return $null
}

$d = Get-Details
$deckSlots = $d.snapshot.deck.slots
$tracked = @($d.tip_racks.PSObject.Properties.Name | Sort-Object { [int]$_ })

if (-not $tracked) { Write-Host "No tip racks are tracked; nothing to do."; exit 0 }

Write-Host ("gateway  : {0} (port {1})" -f $d.snapshot.deck.source, $Port)
if ($d.robot.robot_name) { Write-Host ("robot    : {0}" -f $d.robot.robot_name) }
Write-Host ("key      : {0} (principal {1})" -f $resolved.source, $resolved.name)
Write-Host ""

# Classify every tracked rack against the deck. A rack is stale when its slot
# holds nothing, or holds something that is not a tip rack.
$stale = @()
foreach ($slot in $tracked) {
    $r = $d.tip_racks.$slot
    $owner = Get-SlotOwner -DeckSlots $deckSlots -Slot $slot
    $verdict = if (-not $owner) { "STALE - deck declares nothing in this slot" }
               elseif (-not $owner.is_tiprack) { "STALE - deck says {0} ({1}), not a tip rack" -f $owner.load_name, $owner.kind }
               else { "ok" }
    Write-Host ("slot {0,-2} : {1}/{2} fresh, {3} empty, {4} on head" -f `
        $slot, $r.available, $r.total, $r.empty, $r.on_pipette)
    if ($verdict -eq "ok") {
        Write-Host ("          {0} {1}" -f $owner.kind, $owner.load_name) -ForegroundColor DarkGray
    } else {
        Write-Host ("          {0}" -f $verdict) -ForegroundColor Yellow
        $stale += $slot
    }
}

# An explicit -Slots list replaces the detection, but must still name racks the
# tracker knows: marking an unknown slot is a 409, and silently skipping it
# would report success for a repair that did not happen.
if ($Slots) {
    $unknown = @($Slots | Where-Object { $tracked -notcontains $_ })
    if ($unknown) { throw ("Not tracked as a tip rack: {0}. Tracked: {1}." -f ($unknown -join ", "), ($tracked -join ", ")) }
    $stale = @($Slots | Sort-Object { [int]$_ })
    Write-Host ""
    Write-Host ("-Slots given: repairing {0} regardless of the deck." -f ($stale -join ", ")) -ForegroundColor Yellow
}

# Mounts that these slots explain. Marking their wells releases the mount --
# worth naming up front, because the tip may physically still be on the head.
$mounts = @()
foreach ($p in $d.mounted_tips.PSObject.Properties) {
    if ($stale -contains $p.Value.rack) {
        $mounts += ("{0} (from slot {1}, {2} tip(s), picked {3})" -f `
            $p.Name, $p.Value.rack, @($p.Value.wells).Count, $p.Value.picked_at)
    }
}

Write-Host ""
if (-not $stale) { Write-Host "Every tracked rack agrees with the deck. Nothing to do." -ForegroundColor Green; exit 0 }

if (-not $Run) {
    Write-Host "PLAN (nothing done -- pass -Run to execute):" -ForegroundColor Yellow
    Write-Host "  1. claim the gateway"
    $step = 1
    foreach ($slot in $stale) {
        $step++
        Write-Host ("  {0}. POST /control/tips/mark {{slot: {1}, columns: 1..12, status: {2}}}" -f $step, $slot, $Status)
    }
    Write-Host ("  {0}. release the claim" -f ($step + 1))
    if ($mounts) {
        Write-Host ""
        Write-Host "  This also releases these mount records:" -ForegroundColor Yellow
        foreach ($m in $mounts) { Write-Host ("    - {0}" -f $m) }
        Write-Host "  The tracker forgets them; a tip physically on the head STAYS there."
        Write-Host "  Drop it from the operator panel if one is present."
    }
    exit 0
}

# A claim held by someone else -- very often the operator's own panel session --
# makes the POST below a 409 and a raw WebException. Name the holder instead:
# the fix is a click in the panel, not a retry.
$held = (Invoke-RestMethod -Uri "$base/status" -TimeoutSec 10).details.claimed_by
if ($held) {
    $msg = "The gateway is claimed by {0} (expires {1}). Release control in the operator panel, or wait for the claim to expire, then re-run."
    throw ($msg -f $held.owner, $held.expires_at)
}

$claim = Invoke-RestMethod -Uri "$base/control/claim" -Method Post -Headers $auth `
    -ContentType "application/json" -TimeoutSec 15 `
    -Body (@{ owner = "operator:forget-stale-racks"; session_id = [guid]::NewGuid().ToString(); ttl_s = 120 } | ConvertTo-Json)
$hdr = @{ "X-Api-Key" = $ApiKey; "X-Claim-Token" = $claim.claim_token }
Write-Host "claimed" -ForegroundColor Green

try {
    foreach ($slot in $stale) {
        $body = @{ slot = $slot; columns = @(1..12); status = $Status } | ConvertTo-Json
        $after = Invoke-RestMethod -Uri "$base/control/tips/mark" -Method Post -Headers $hdr `
            -ContentType "application/json" -TimeoutSec 30 -Body $body
        $counts = @($after.tips.PSObject.Properties | Group-Object { $_.Value } |
                    ForEach-Object { "{0}={1}" -f $_.Name, $_.Count }) -join ", "
        Write-Host ("slot {0,-2} : marked {1}  -> {2}" -f $slot, $Status, $counts) -ForegroundColor Green
    }

    # Re-read rather than trust the per-call responses: the mount release is a
    # side effect of marking, so /status is where it can actually be confirmed.
    $d = Get-Details
    $left = @($d.mounted_tips.PSObject.Properties | Where-Object { $stale -contains $_.Value.rack })
    if ($left) {
        Write-Warning ("mount(s) still recorded for {0}: {1}" -f ($stale -join ", "),
            (($left | ForEach-Object { $_.Name }) -join ", "))
    } elseif ($mounts) {
        Write-Host ("mounts   : released ({0})" -f (($mounts | ForEach-Object { ($_ -split " ")[0] }) -join ", ")) -ForegroundColor Green
    }
}
finally {
    Invoke-RestMethod -Uri "$base/control/release" -Method Post -Headers $hdr -TimeoutSec 10 | Out-Null
    Write-Host "released"
}
