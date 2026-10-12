<#
.SYNOPSIS
    Repoint one OT-2 gateway at its robot, preserving the rest of its service
    environment.

.DESCRIPTION
    Sets OT2_HTTP_BASE_URL (the run-engine HTTP path) and optionally
    OT2_HOST_ALIAS (the SSH path) in the service's NSSM environment.

    Preservation is the whole point, and the reason this is a script rather
    than a one-liner: `nssm set AppEnvironmentExtra` REPLACES the entire
    variable block rather than adding to it, so the naive call silently drops
    OT2_EQUIPMENT_ID, the three state paths, the SSH password and the edge
    secret -- leaving a gateway that comes back pointed at nothing with no
    tracked state. This reads the current block, changes only the named
    variables, writes it back, and refuses if the variable count moves.

    "Where is the robot" is per-host config that has drifted before: a gateway
    holds a live session across a network change, keeps reporting `ready` off
    it, and the stale address only surfaces on the next restart -- as
    `requires_init` with "Robot unreachable", hours or weeks later. So this
    re-probes after restarting and reports whether the robot actually answered,
    rather than reporting that the write succeeded.

    Prints a plan and stops unless -Run is given. -Run reconfigures a Windows
    service and must be ELEVATED; UAC prompts render only inside an interactive
    RDP session on this PC.

.PARAMETER Service
    NSSM service name: ot2-gateway-hte or ot2-gateway-complexation.

.PARAMETER Url
    New OT2_HTTP_BASE_URL, e.g. http://100.64.254.91:31950 (the robot's
    tailnet address). The OT-2's robot-server listens on 31950.

.PARAMETER HostAlias
    New OT2_HOST_ALIAS -- the SSH host for snapshot reads. Omit to leave it
    alone. Worth setting whenever -Url moves the robot to a different network:
    the two are separate addresses for the same machine, and an alias left on
    an unreachable path breaks the SSH-backed reads while HTTP control works,
    which is a confusing half-failure.

.PARAMETER Restart
    Restart the service after writing, so the change takes effect. Default true.

.PARAMETER Run
    Actually do it. Without this the script only prints what it would do.

.EXAMPLE
    # Look before you leap -- works unelevated, reads only
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\ot2-set-robot-url.ps1 `
        -Service ot2-gateway-complexation -Url http://100.64.254.91:31950

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\ot2-set-robot-url.ps1 `
        -Service ot2-gateway-complexation -Url http://100.64.254.91:31950 -Run
#>
param(
    [Parameter(Mandatory = $true)][ValidateSet("ot2-gateway-hte", "ot2-gateway-complexation")][string]$Service,
    [Parameter(Mandatory = $true)][string]$Url,
    [string]$HostAlias,
    [bool]$Restart = $true,
    [switch]$Run
)

$ErrorActionPreference = "Stop"

if ($Url -notmatch '^https?://[^/\s]+$') {
    throw ("-Url must be scheme://host:port with no trailing path, got '{0}'." -f $Url)
}

$nssm = "C:\SDL_Tools\nssm.exe"
$port = if ($Service -eq "ot2-gateway-hte") { 8020 } else { 8021 }

function Get-ServiceEnv {
    <# `nssm get` reads service config and works unelevated -- but it writes
       "LsaOpenPolicy(): Access is denied" to stderr while doing so, and under
       ErrorActionPreference=Stop a native command's stderr is a TERMINATING
       error even with 2>$null. So relax it for exactly this call. #>
    param([string]$Name)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $raw = & $nssm get $Name AppEnvironmentExtra 2>$null
    $ErrorActionPreference = $prev
    return @($raw -split "`r?`n" | Where-Object { $_ -match "=" })
}

$desired = @{ "OT2_HTTP_BASE_URL" = $Url }
if ($HostAlias) { $desired["OT2_HOST_ALIAS"] = $HostAlias }

$vars = Get-ServiceEnv -Name $Service
if ($vars.Count -eq 0) { throw "Read no environment for $Service. Is the name right?" }

Write-Host ("service  : {0} (port {1})" -f $Service, $port)
Write-Host ("env      : {0} variables" -f $vars.Count)
foreach ($k in ($desired.Keys | Sort-Object)) {
    $cur = @($vars | Where-Object { $_ -like "$k=*" })[0]
    $cur = if ($cur) { $cur.Substring($k.Length + 1) } else { "(absent)" }
    Write-Host ("  {0,-18} {1}  ->  {2}" -f $k, $cur, $desired[$k])
}

# Build the new block. Only the named variables change; everything else is
# carried through byte-for-byte, including values never printed here.
$updated = @()
$seen = @{}
foreach ($v in $vars) {
    $name = ($v -split "=", 2)[0]
    if ($desired.ContainsKey($name)) { $updated += ("{0}={1}" -f $name, $desired[$name]); $seen[$name] = $true }
    else { $updated += $v }
}
$appended = @($desired.Keys | Where-Object { -not $seen.ContainsKey($_) } | Sort-Object)
foreach ($k in $appended) { $updated += ("{0}={1}" -f $k, $desired[$k]) }

# The only sanctioned change in count is appending a variable that was absent.
$expected = $vars.Count + $appended.Count
if ($updated.Count -ne $expected) {
    throw ("Refusing to write: {0} variables in, {1} out (expected {2})." -f $vars.Count, $updated.Count, $expected)
}

if (-not $Run) {
    Write-Host ""
    Write-Host "PLAN (nothing done -- pass -Run to execute):" -ForegroundColor Yellow
    Write-Host ("  1. nssm set {0} AppEnvironmentExtra <{1} variables, {2} changed>" -f `
        $Service, $expected, $desired.Count)
    if ($appended) { Write-Host ("     (appending: {0})" -f ($appended -join ", ")) }
    if ($Restart) {
        Write-Host ("  2. nssm restart {0}; sc continue {0}" -f $Service)
        Write-Host ("  3. GET http://127.0.0.1:{0}/status -- report whether the robot answered" -f $port)
    }
    Write-Host ""
    Write-Host "  -Run must be ELEVATED (service configuration)." -ForegroundColor Yellow
    exit 0
}

# Say the requirement out loud. Without this, an unelevated write dies inside
# `nssm set` with "OpenService(): Access is denied" and a PowerShell stack
# trace, which reads like a broken script rather than a missing privilege.
$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw ("This script reconfigures a Windows service and must run ELEVATED. " +
           "UAC prompts render only inside an interactive RDP session on this PC -- " +
           "open an elevated PowerShell there and re-run.")
}

& $nssm set $Service AppEnvironmentExtra @updated | Out-Null

$after = Get-ServiceEnv -Name $Service
if ($after.Count -ne $expected) { throw "Write-back changed the variable count -- inspect before restarting." }
foreach ($k in ($desired.Keys | Sort-Object)) {
    $now = @($after | Where-Object { $_ -like "$k=*" })[0]
    if ($now -ne ("{0}={1}" -f $k, $desired[$k])) { throw ("{0} did not take: {1}" -f $k, $now) }
    Write-Host ("wrote    : {0}" -f $now) -ForegroundColor Green
}
# Names only, so a dropped variable is visible without printing any secret.
Write-Host ("env      : {0} variables -- {1}" -f $after.Count,
    ((($after | ForEach-Object { ($_ -split "=")[0] }) | Sort-Object) -join ", "))

if (-not $Restart) {
    Write-Host "not restarted -- the running process still holds the old address." -ForegroundColor Yellow
    exit 0
}

& $nssm restart $Service | Out-Null
& sc.exe continue $Service | Out-Null
Write-Host "restarted" -ForegroundColor Green

# The write succeeding proves nothing about the address being right. Give the
# service a few seconds to probe, then say what the robot actually did.
$deadline = (Get-Date).AddSeconds(45)
do {
    Start-Sleep -Seconds 5
    try { $s = Invoke-RestMethod -Uri "http://127.0.0.1:$port/status" -TimeoutSec 20 } catch { $s = $null }
} while ($s -and -not $s.details.robot.reachable -and (Get-Date) -lt $deadline)

if (-not $s) { throw "The service did not answer /status after the restart. Check C:\SDL_Logs\$Service.err.log." }
Write-Host ("status   : {0} / {1}" -f $s.equipment_status, $s.activity)
Write-Host ("message  : {0}" -f $s.message)
if ($s.details.robot.reachable) {
    Write-Host ("robot    : reachable -- {0}, API {1}" -f $s.details.robot.robot_name, $s.details.robot.api_version) -ForegroundColor Green
} else {
    Write-Warning ("robot    : NOT reachable at {0}. The address is written but wrong, or the robot is down." -f $Url)
}
