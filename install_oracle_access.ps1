<#
  Installs (or removes) the "BeaconOracleAccess" scheduled task — keeps THIS laptop allowed to
  reach the Oracle server over SSH while its home internet address changes.

  Why it exists: Oracle's firewall lets SSH in only from the laptop's current public address
  (one managed /32 rule). Until the switch-over, the cloud-import task (BeaconEdgeSync) refreshed
  that rule before every sync. Once Beacon runs ON Oracle, that task is switched off — and without
  this one, the next change of the laptop's address would lock the laptop out of its own server
  (no way to open Beacon's screens, update it, or fix anything).

  It runs the engine's standalone updater (beacon_engine/edge/oci_allowlist.py, its own small venv,
  settings from the engine's .env) every 5 minutes. The updater only rewrites that one rule, and
  only when the address actually changed ("cached" otherwise).

  Same mechanism as BeaconMuseLink: schtasks /SC MINUTE /IT installs WITHOUT admin; pythonw means
  no window ever appears.

  Usage:
    powershell -ExecutionPolicy Bypass -File .\install_oracle_access.ps1            # install
    powershell -ExecutionPolicy Bypass -File .\install_oracle_access.ps1 -Uninstall # remove
    powershell -ExecutionPolicy Bypass -File .\install_oracle_access.ps1 -Status    # query
#>
param(
    [int]$EveryMinutes = 5,
    [string]$EngineDir = $PSScriptRoot,
    [switch]$Uninstall,
    [switch]$Status
)

$ErrorActionPreference = "Stop"
$TaskName = "BeaconOracleAccess"

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

$Updater = Join-Path $EngineDir "beacon_engine\edge\oci_allowlist.py"
$StatusFile = Join-Path $EngineDir "storage\edge_sync\oci_allowlist_status.json"
$Pyw = Join-Path $EngineDir "storage\oci_updater_venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Pyw)) { $Pyw = Join-Path $EngineDir ".venv\Scripts\python.exe" }
foreach ($p in @($Updater, $Pyw)) {
    if (-not (Test-Path -LiteralPath $p)) { throw "not found: $p" }
}

$Interval = [Math]::Max(1, [Math]::Min($EveryMinutes, 60))
$Command  = 'wscript.exe "' + (Join-Path $EngineDir "run_python_hidden.vbs") + '" "' + $Pyw + '" "' + $Updater + '" --status "' + $StatusFile + '"'
& schtasks.exe /Create /F /SC MINUTE /MO $Interval /TN $TaskName /TR $Command /IT
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install scheduled task: $TaskName"
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -Hidden `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Set-ScheduledTask -TaskName $TaskName -Settings $settings | Out-Null
Write-Output ("installed (every $Interval min)")
