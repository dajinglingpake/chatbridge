param(
  [Parameter(Mandatory = $true, Position = 0)]
  [ValidateSet('start-hub', 'start-bridge', 'start-all', 'stop-hub', 'stop-bridge', 'stop-all', 'restart-all', 'status')]
  [string]$Action
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeDir = Join-Path $Root ".runtime"
$LogDir = Join-Path $RuntimeDir "logs"
New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$HubScript = Join-Path $Root "multi_codex_hub.py"
$BridgeScript = Join-Path $Root "weixin_hub_bridge.py"

$HubPidFile = Join-Path $RuntimeDir "multi_codex_hub.pid"
$BridgePidFile = Join-Path $RuntimeDir "weixin_hub_bridge.pid"

$HubOutLog = Join-Path $LogDir "multi_codex_hub.out.log"
$HubErrLog = Join-Path $LogDir "multi_codex_hub.err.log"
$BridgeOutLog = Join-Path $LogDir "weixin_hub_bridge.out.log"
$BridgeErrLog = Join-Path $LogDir "weixin_hub_bridge.err.log"

function Get-PythonCommand {
  $pythonw = Get-Command pythonw -ErrorAction SilentlyContinue
  if ($pythonw) { return $pythonw.Source }
  $python = Get-Command python -ErrorAction SilentlyContinue
  if ($python) { return $python.Source }
  throw "python/pythonw not found"
}

function Read-PidFile {
  param([string]$PidFile)
  if (-not (Test-Path -LiteralPath $PidFile)) { return $null }
  $raw = (Get-Content -LiteralPath $PidFile -Raw -Encoding UTF8).Trim()
  if ([string]::IsNullOrWhiteSpace($raw)) { return $null }
  return [int]$raw
}

function Get-ProcessCommandLine {
  param([int]$Pid)
  try {
    return (Get-CimInstance Win32_Process -Filter "ProcessId = $Pid").CommandLine
  }
  catch {
    return $null
  }
}

function Find-ManagedProcessByScript {
  param([string]$ScriptPath)
  $escaped = $ScriptPath.Replace('\', '\\')
  $candidates = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine -like "*$ScriptPath*"
  }
  if ($candidates) {
    $pidValue = [int]$candidates[0].ProcessId
    try {
      return Get-Process -Id $pidValue -ErrorAction Stop
    }
    catch {
      return $null
    }
  }
  return $null
}

function Test-ManagedProcess {
  param(
    [string]$PidFile,
    [string]$ScriptPath
  )
  $managedPid = Read-PidFile -PidFile $PidFile
  if (-not $managedPid) { return $null }
  try {
    $proc = Get-Process -Id $managedPid -ErrorAction Stop
    $cmd = Get-ProcessCommandLine -Pid $managedPid
    if ($cmd -and $cmd -like "*$ScriptPath*") {
      return $proc
    }
  }
  catch {
  }
  $discovered = Find-ManagedProcessByScript -ScriptPath $ScriptPath
  if ($discovered) {
    Set-Content -LiteralPath $PidFile -Value $discovered.Id -Encoding UTF8
    return $discovered
  }
  Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
  return $null
}

function Start-ManagedProcess {
  param(
    [string]$Name,
    [string]$ScriptPath,
    [string]$PidFile,
    [string]$StdOutLog,
    [string]$StdErrLog
  )
  $existing = Test-ManagedProcess -PidFile $PidFile -ScriptPath $ScriptPath
  if ($existing) {
    Write-Output "$Name already running (PID $($existing.Id))"
    return
  }

  $pythonCmd = Get-PythonCommand
  $commandLine = '"{0}" "{1}"' -f $pythonCmd, $ScriptPath
  $proc = Start-Process -FilePath "cmd.exe" `
    -ArgumentList @("/d", "/c", $commandLine) `
    -WorkingDirectory $Root `
    -RedirectStandardOutput $StdOutLog `
    -RedirectStandardError $StdErrLog `
    -WindowStyle Hidden `
    -PassThru

  Set-Content -LiteralPath $PidFile -Value $proc.Id -Encoding UTF8
  Write-Output "$Name started (PID $($proc.Id))"
}

function Stop-ManagedProcess {
  param(
    [string]$Name,
    [string]$ScriptPath,
    [string]$PidFile
  )
  $proc = Test-ManagedProcess -PidFile $PidFile -ScriptPath $ScriptPath
  if (-not $proc) {
    Write-Output "$Name is not running"
    return
  }

  $null = Start-Process -FilePath "taskkill.exe" `
    -ArgumentList @("/PID", "$($proc.Id)", "/T", "/F") `
    -WindowStyle Hidden `
    -Wait `
    -PassThru
  Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
  Write-Output "$Name stopped (PID $($proc.Id))"
}

function Show-ManagedStatus {
  param(
    [string]$Name,
    [string]$ScriptPath,
    [string]$PidFile
  )
  $proc = Test-ManagedProcess -PidFile $PidFile -ScriptPath $ScriptPath
  if ($proc) {
    Write-Output "${Name}: running (PID $($proc.Id))"
  }
  else {
    Write-Output "${Name}: stopped"
  }
}

switch ($Action) {
  'start-hub' {
    Start-ManagedProcess -Name 'Hub' -ScriptPath $HubScript -PidFile $HubPidFile -StdOutLog $HubOutLog -StdErrLog $HubErrLog
  }
  'start-bridge' {
    Start-ManagedProcess -Name 'Bridge' -ScriptPath $BridgeScript -PidFile $BridgePidFile -StdOutLog $BridgeOutLog -StdErrLog $BridgeErrLog
  }
  'start-all' {
    Start-ManagedProcess -Name 'Hub' -ScriptPath $HubScript -PidFile $HubPidFile -StdOutLog $HubOutLog -StdErrLog $HubErrLog
    Start-Sleep -Seconds 2
    Start-ManagedProcess -Name 'Bridge' -ScriptPath $BridgeScript -PidFile $BridgePidFile -StdOutLog $BridgeOutLog -StdErrLog $BridgeErrLog
  }
  'stop-hub' {
    Stop-ManagedProcess -Name 'Hub' -ScriptPath $HubScript -PidFile $HubPidFile
  }
  'stop-bridge' {
    Stop-ManagedProcess -Name 'Bridge' -ScriptPath $BridgeScript -PidFile $BridgePidFile
  }
  'stop-all' {
    Stop-ManagedProcess -Name 'Bridge' -ScriptPath $BridgeScript -PidFile $BridgePidFile
    Stop-ManagedProcess -Name 'Hub' -ScriptPath $HubScript -PidFile $HubPidFile
  }
  'restart-all' {
    Stop-ManagedProcess -Name 'Bridge' -ScriptPath $BridgeScript -PidFile $BridgePidFile
    Stop-ManagedProcess -Name 'Hub' -ScriptPath $HubScript -PidFile $HubPidFile
    Start-Sleep -Seconds 1
    Start-ManagedProcess -Name 'Hub' -ScriptPath $HubScript -PidFile $HubPidFile -StdOutLog $HubOutLog -StdErrLog $HubErrLog
    Start-Sleep -Seconds 2
    Start-ManagedProcess -Name 'Bridge' -ScriptPath $BridgeScript -PidFile $BridgePidFile -StdOutLog $BridgeOutLog -StdErrLog $BridgeErrLog
  }
  'status' {
    Show-ManagedStatus -Name 'Hub' -ScriptPath $HubScript -PidFile $HubPidFile
    Show-ManagedStatus -Name 'Bridge' -ScriptPath $BridgeScript -PidFile $BridgePidFile
    $codexProcs = Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^codex(\.cmd)?$|^cmd\.exe$' -and $_.CommandLine -and $_.CommandLine -like '*codex*' }
    if ($codexProcs) {
      Write-Output "Codex child processes:"
      $codexProcs | ForEach-Object { Write-Output ("  PID " + $_.ProcessId + " :: " + $_.CommandLine) }
    }
    else {
      Write-Output "Codex child processes: none"
    }
    Write-Output "Logs: $LogDir"
  }
}
