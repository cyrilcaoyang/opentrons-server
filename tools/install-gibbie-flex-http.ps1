<#
.SYNOPSIS
    Install the dedicated Flex HTTP gateway on the Gibbie PC.

.DESCRIPTION
    Plan-and-stop by default. With -Run, creates one isolated NSSM service on
    port 8071. It never calls /control/startup and sets
    OT2_AUTO_RECONNECT=false, so installation performs health reads only and
    cannot create a run or move the robot.

    Run this script from the opentrons_flex_http checkout. It refuses another
    branch, a non-Flex robot identity, a busy port, an existing service, or a
    missing prepared virtual environment. Dependency installation must happen
    separately in a non-elevated shell before -Run.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\install-gibbie-flex-http.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\install-gibbie-flex-http.ps1 -Run
#>
param(
    [switch]$Run,
    [string]$Service = "opentrons-flex-http",
    [ValidateRange(1024, 65535)][int]$Port = 8071,
    [string]$RobotUrl = "http://192.168.254.81:31950",
    [string]$StateDir = "C:\SDL_State\opentrons-flex-http",
    [string]$LogDir = "C:\SDL_Logs"
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Uv = "C:\SDL_Tools\uv.exe"
$Nssm = "C:\SDL_Tools\nssm.exe"
$AssistantEnv = Join-Path $StateDir "assistant.env"
$FirewallRule = "$Service $Port"
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"

function Assert-Command([string]$Path) {
    if (-not (Test-Path $Path -PathType Leaf)) { throw "Required executable not found: $Path" }
}

function Invoke-Nssm {
    # NSSM may write successful output to stderr. Native exit status is the
    # authority; do not continue installing after a failed setting.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Nssm @args 2>&1 | Out-Null
        $nativeExitCode = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $previousPreference }
    if ($nativeExitCode -ne 0) {
        throw "NSSM operation failed with exit code $nativeExitCode; service setup is incomplete"
    }
}

Assert-Command $Uv
Assert-Command $Nssm

$branch = (& git -C $RepoRoot branch --show-current).Trim()
if ($branch -ne "opentrons_flex_http") {
    throw "Refusing branch '$branch'; install from opentrons_flex_http"
}

if (Get-Service -Name $Service -ErrorAction SilentlyContinue) {
    throw "Service '$Service' already exists; this installer is first-install only"
}
if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
    throw "TCP port $Port is already listening"
}

$headers = @{ "Opentrons-Version" = "3" }
$health = Invoke-RestMethod -Method Get -Uri ($RobotUrl.TrimEnd('/') + "/health") `
    -Headers $headers -TimeoutSec 8
$robotModel = [string]$health.robot_model
if ($robotModel -notmatch "(?i)(flex|ot-?3)") {
    throw "Expected a Flex/OT-3 at $RobotUrl, observed robot_model='$robotModel'"
}

$envVars = @(
    "OT2_EQUIPMENT_ID=gibbie_flex_http",
    "OT2_EQUIPMENT_NAME=Opentrons Flex (Gibbie HTTP)",
    "OT2_ROBOT_MODEL=Flex",
    "OT2_TRANSPORT=http",
    "OT2_HTTP_BASE_URL=$RobotUrl",
    "OT2_HOST_ALIAS=$(([uri]$RobotUrl).Host)",
    "OT2_PLATE_STATE_PATH=$($StateDir.Replace('\', '/'))/plate.json",
    "OT2_DECK_STATE_PATH=$($StateDir.Replace('\', '/'))/deck.json",
    "OT2_TIP_STATE_PATH=$($StateDir.Replace('\', '/'))/tips.json",
    "OT2_STOP_STATE_PATH=$($StateDir.Replace('\', '/'))/stop.json",
    "OT2_DRY_RUN=false",
    "OT2_AUTO_RECONNECT=false",
    "OT2_LOG_LEVEL=INFO",
    "OT2_UI=true",
    "OT2_TRUST_LOCAL_UI=false",
    "OT2_REQUIRE_LOGIN=true",
    "OT2_ASSISTANT_ENABLED=true",
    "OT2_ENV_FILE=$($AssistantEnv.Replace('\', '/'))"
)

Write-Host "Flex gateway installation plan" -ForegroundColor Cyan
Write-Host "  checkout:  $RepoRoot"
Write-Host "  branch:    $branch"
Write-Host "  robot:     $RobotUrl ($robotModel)"
Write-Host "  service:   $Service"
Write-Host "  backend:   http://$env:COMPUTERNAME`:$Port (UI requires authenticated proxy)"
Write-Host "  state:     $StateDir"
Write-Host "  assistant: expected at $AssistantEnv (contents not inspected)"
Write-Host "  startup:   disabled; no robot run will be created"

if (-not $Run) {
    Write-Host "PLAN ONLY. Re-run from an elevated shell with -Run to apply." -ForegroundColor Yellow
    exit 0
}

$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "-Run requires an elevated PowerShell session"
}

Assert-Command $Python
if ([string]::IsNullOrWhiteSpace($env:OT2_EDGE_SECRET)) {
    throw "Provision the approved proxy secret as OT2_EDGE_SECRET in this installation process before -Run"
}
$envVars += "OT2_EDGE_SECRET=$env:OT2_EDGE_SECRET"
if (-not (Test-Path $AssistantEnv -PathType Leaf)) {
    throw "Operator must provision the approved provider configuration at $AssistantEnv before -Run"
}

New-Item -ItemType Directory -Force $StateDir, $LogDir | Out-Null

# Dependencies were prepared without elevation. Execute that environment
# directly so a service start cannot sync packages or change its build.
$parameters = "-m uvicorn opentrons_server.gateway.api:app --host 0.0.0.0 --port $Port"
Invoke-Nssm install $Service $Python $parameters
Invoke-Nssm set $Service AppDirectory $RepoRoot
Invoke-Nssm set $Service DisplayName "Opentrons Flex (Gibbie HTTP) gateway"
Invoke-Nssm set $Service Description "Typed Flex robot-server gateway; operator UI and proposal-only assistant"
Invoke-Nssm set $Service Start SERVICE_DEMAND_START
Invoke-Nssm set $Service AppStdout (Join-Path $LogDir "$Service.out.log")
Invoke-Nssm set $Service AppStderr (Join-Path $LogDir "$Service.err.log")
Invoke-Nssm set $Service AppRotateFiles 1
Invoke-Nssm set $Service AppRotateBytes 10485760
Invoke-Nssm set $Service AppEnvironmentExtra @envVars

# Verify before starting: a truncated environment could restore default
# reconnect or authentication behavior. Never print values (including secrets).
$storedEnv = @((Get-ItemProperty -LiteralPath (
    "HKLM:\SYSTEM\CurrentControlSet\Services\$Service\Parameters"
) -Name AppEnvironmentExtra).AppEnvironmentExtra)
if ($storedEnv.Count -ne $envVars.Count -or
    @(Compare-Object -ReferenceObject $envVars -DifferenceObject $storedEnv -CaseSensitive).Count -ne 0) {
    throw "Service environment read-back differs from the installation plan; refusing to start"
}

if (-not (Get-NetFirewallRule -DisplayName $FirewallRule -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName $FirewallRule -Direction Inbound -Action Allow `
        -Protocol TCP -LocalPort $Port -RemoteAddress "100.64.0.0/10" | Out-Null
}

Invoke-Nssm start $Service

$base = "http://127.0.0.1:$Port"
$deadline = (Get-Date).AddSeconds(60)
do {
    Start-Sleep -Seconds 1
    try { $gateway = Invoke-RestMethod -Method Get -Uri "$base/health" -TimeoutSec 3 }
    catch { $gateway = $null }
} until ($gateway -or (Get-Date) -ge $deadline)
if (-not $gateway) { throw "Gateway did not become healthy within 60 seconds; inspect $LogDir" }
if ($gateway.status -ne "healthy") { throw "Unexpected gateway health response" }

$status = Invoke-RestMethod -Method Get -Uri "$base/status" -TimeoutSec 5
$openapi = Invoke-RestMethod -Method Get -Uri "$base/openapi.json" -TimeoutSec 5
$ui = Invoke-WebRequest -UseBasicParsing -Method Get -Uri "$base/ui/" `
    -Headers @{ "X-Edge-Key" = $env:OT2_EDGE_SECRET } -TimeoutSec 5
# The first read imports the provider SDK on Windows; allow cold-start time.
# This validates configuration only and does not call the provider.
$assistant = Invoke-RestMethod -Method Get -Uri "$base/assistant/health" -TimeoutSec 30
if ($status.equipment_id -ne "gibbie_flex_http") {
    throw "Unexpected gateway identity after installation"
}
if ($status.details.control_auth -ne "identity" -or $status.details.ui_mode -ne "edge") {
    throw "Gateway authentication acceptance failed"
}
if (-not $openapi.paths.'/control/gripper-move-to-absolute' -or
    $ui.StatusCode -ne 200 -or $ui.Content -notmatch '<div id="root">') {
    throw "Flex OpenAPI or operator UI acceptance failed"
}
if ($status.equipment_status -notin @("requires_init", "unknown")) {
    throw "Expected an uninitialized state after installation, got '$($status.equipment_status)'"
}
if (-not $assistant.configured) {
    throw "Assistant is unavailable: $($assistant.reason)"
}

Invoke-Nssm set $Service Start SERVICE_AUTO_START

Write-Host "Installed without initializing or moving the robot." -ForegroundColor Green
Write-Host "Open the separately approved authenticated proxy URL ending in /ui/."
Write-Host "Rollback: nssm stop $Service; nssm remove $Service confirm"
