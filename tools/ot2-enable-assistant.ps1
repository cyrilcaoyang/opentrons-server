<#
.SYNOPSIS
    Turn the panel assistant on (or off) for one OT-2 gateway service.

.DESCRIPTION
    Flips OT2_ASSISTANT_ENABLED in the service's NSSM environment, preserving
    every other variable.

    That preservation is the whole point of the script. `nssm set
    AppEnvironmentExtra` REPLACES the entire variable block rather than adding
    to it, so the naive one-liner silently drops OT2_EQUIPMENT_ID, the three
    state paths, the SSH password and the edge secret -- a gateway that then
    comes back pointed at the wrong robot with no state. This reads the current
    block, changes exactly one entry, writes it back, and refuses to proceed if
    the variable count would change unexpectedly.

    Requires elevation (service configuration). UAC prompts only render inside
    an interactive RDP session on this PC.

.PARAMETER Service
    NSSM service name, e.g. ot2-gateway-hte.

.PARAMETER Enabled
    "1" to enable, "0" to disable. Default "1".

.PARAMETER Restart
    Restart the service afterwards so the change takes effect. Default $true.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\ot2-enable-assistant.ps1 -Service ot2-gateway-hte
#>
param(
    [Parameter(Mandatory = $true)][string]$Service,
    [ValidateSet("0", "1")][string]$Enabled = "1",
    [bool]$Restart = $true
)

$ErrorActionPreference = "Stop"

# Say the requirement out loud. Without this, an unelevated run dies inside
# `nssm get` with "LsaOpenPolicy(): Access is denied" and a PowerShell stack
# trace, which reads like a broken script rather than a missing privilege.
$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw ("This script reconfigures a Windows service and must run ELEVATED. " +
           "UAC prompts render only inside an interactive RDP session on this PC -- " +
           "open an elevated PowerShell there and re-run.")
}

$nssm = "C:\SDL_Tools\nssm.exe"
$VAR = "OT2_ASSISTANT_ENABLED"

$raw = & $nssm get $Service AppEnvironmentExtra 2>$null
$vars = @($raw -split "`r?`n" | Where-Object { $_ -match "=" })
if ($vars.Count -eq 0) { throw "Read no environment for $Service. Is the name right, and is this shell elevated?" }

Write-Host ("before: {0} variables" -f $vars.Count) -ForegroundColor Cyan
$current = ($vars | Where-Object { $_ -like "$VAR=*" })
if (-not $current) { $current = "absent" }
Write-Host ("  $VAR is currently: {0}" -f $current)

$updated = @()
$found = $false
foreach ($v in $vars) {
    if ($v -like "$VAR=*") { $updated += "$VAR=$Enabled"; $found = $true }
    else { $updated += $v }
}
if (-not $found) { $updated += "$VAR=$Enabled" }

# The only sanctioned change in count is appending the flag when it was absent.
$expected = if ($found) { $vars.Count } else { $vars.Count + 1 }
if ($updated.Count -ne $expected) {
    throw ("Refusing to write: {0} variables in, {1} out (expected {2})." -f $vars.Count, $updated.Count, $expected)
}

& $nssm set $Service AppEnvironmentExtra @updated | Out-Null

$after = @((& $nssm get $Service AppEnvironmentExtra 2>$null) -split "`r?`n" | Where-Object { $_ -match "=" })
Write-Host ("after : {0} variables" -f $after.Count) -ForegroundColor Cyan
if ($after.Count -ne $expected) { throw "Write-back changed the variable count -- inspect before restarting." }
Write-Host ("  $VAR is now: {0}" -f ($after | Where-Object { $_ -like "$VAR=*" })) -ForegroundColor Green

# Names only, so a dropped variable is visible without printing any secret.
Write-Host "  variables present:" -ForegroundColor Cyan
($after | ForEach-Object { ($_ -split "=")[0] } | Sort-Object) -join ", " | Write-Host

if ($Restart) {
    & $nssm restart $Service | Out-Null
    & sc.exe continue $Service | Out-Null
    Start-Sleep -Seconds 4
    Write-Host "  restarted" -ForegroundColor Green
}
