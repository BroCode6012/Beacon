<#
  Installs (or removes) the "BeaconRadarAutostart" scheduled task — a keep-alive that keeps the
  local background scraper running whenever you're logged in:
    - runs every 5 minutes (interactive/current-user), and
    - calls scraper_autostart.py -> sweep_control.start_worker(), which is idempotent (a no-op
      when a worker is already alive), so it starts the scraper within ~5 min of logging in and
      restarts it within ~5 min if it ever dies, and can NEVER stack up two workers.

  Uses schtasks.exe /SC MINUTE /IT so it installs WITHOUT elevation — the same mechanism the
  BeaconEdgeSync task uses. (/SC ONLOGON would require admin; a 5-min keep-alive doesn't, and is
  more robust because it also recovers from crashes.)

  Usage:
    powershell -ExecutionPolicy Bypass -File .\install_radar_autostart.ps1            # install
    powershell -ExecutionPolicy Bypass -File .\install_radar_autostart.ps1 -Uninstall # remove
    powershell -ExecutionPolicy Bypass -File .\install_radar_autostart.ps1 -Status    # query
#>
param(
    [int]$EveryMinutes = 5,
    [switch]$Uninstall,
    [switch]$Status
)

$ErrorActionPreference = "Stop"
$TaskName  = "BeaconRadarAutostart"
$BeaconDir = $PSScriptRoot
$Script    = Join-Path $BeaconDir "scraper_autostart.py"
$Pyw = Join-Path $BeaconDir ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Pyw)) { throw "Install Beacon requirements in its own .venv first." }

if ($Status) {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($t) { Write-Output ("enabled:" + $t.State) } else { Write-Output "absent" }
    return
}

if ($Uninstall) {
    & schtasks.exe /Delete /F /TN $TaskName 2>$null | Out-Null
    Write-Output "removed"
    return
}

if (-not (Test-Path -LiteralPath $Script)) {
    throw "scraper_autostart.py not found next to this script: $Script"
}

$Interval = [Math]::Max(1, [Math]::Min($EveryMinutes, 60))
$Command = 'wscript.exe "' + (Join-Path $BeaconDir "run_python_hidden.vbs") + '" "' + $Pyw + '" "' + $Script + '"'
& schtasks.exe /Create /F /SC MINUTE /MO $Interval /TN $TaskName /TR $Command /IT
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install scheduled task: $TaskName"
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -Hidden `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Set-ScheduledTask -TaskName $TaskName -Settings $settings | Out-Null
Write-Output ("installed (every $Interval min)")
