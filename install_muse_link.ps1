<#
  Installs (or removes) the "BeaconMuseLink" scheduled task — keeps the Muse link alive
  whenever you're logged in:
    - runs muse_link.py every 2 minutes (interactive/current-user), which checks Beacon's data
      service, the read-only door (beacon_gateway.py) and the public road (cloudflared), and
      restarts ONLY what is down;
    - after the laptop wakes, the next 2-minute round does the check (measured 2026-09-28:
      woke 22:17:32, next run 22:21:00 — Windows does not fire a missed repetition at once);
    - it never stacks copies: each run finishes in seconds, and IgnoreNew skips a run if the
      previous one is still going.

  Same mechanism as BeaconRadarAutostart: schtasks /SC MINUTE /IT installs WITHOUT admin.

  Usage:
    powershell -ExecutionPolicy Bypass -File .\install_muse_link.ps1            # install
    powershell -ExecutionPolicy Bypass -File .\install_muse_link.ps1 -Uninstall # remove
    powershell -ExecutionPolicy Bypass -File .\install_muse_link.ps1 -Status    # query
#>
param(
    [int]$EveryMinutes = 2,
    [switch]$Uninstall,
    [switch]$Status
)

$ErrorActionPreference = "Stop"
$TaskName  = "BeaconMuseLink"
$BeaconDir = $PSScriptRoot
$Script    = Join-Path $BeaconDir "muse_link.py"
$Pyw       = "D:\Youtube\Quill\.venv\Scripts\pythonw.exe"
if (-not (Test-Path -LiteralPath $Pyw)) { $Pyw = "pythonw.exe" }

if ($Status) {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($t) { Write-Output ("enabled:" + $t.State) } else { Write-Output "absent" }
    return
}

if ($Uninstall) {
    & schtasks.exe /Delete /F /TN $TaskName 2>$null | Out-Null
    Write-Output "removed (the link keeps running until the laptop restarts)"
    return
}

if (-not (Test-Path -LiteralPath $Script)) {
    throw "muse_link.py not found next to this script: $Script"
}

$Interval = [Math]::Max(1, [Math]::Min($EveryMinutes, 60))
$Command  = '"' + $Pyw + '" "' + $Script + '"'
& schtasks.exe /Create /F /SC MINUTE /MO $Interval /TN $TaskName /TR $Command /IT
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install scheduled task: $TaskName"
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -Hidden `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Set-ScheduledTask -TaskName $TaskName -Settings $settings | Out-Null
Write-Output ("installed (every $Interval min)")
