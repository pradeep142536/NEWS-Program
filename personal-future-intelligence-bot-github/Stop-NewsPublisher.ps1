$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$dataDir = Join-Path $projectRoot 'data'
$pidFile = Join-Path $dataDir 'publisher.pid'
$stopSignal = Join-Path $dataDir 'stop.signal'

$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $venvPython) {
    $python = $venvPython
} else {
    $python = (Get-Command python -ErrorAction Stop).Source
}
$scriptPath = Join-Path $projectRoot 'bot.py'
& $python $scriptPath --disable-monitoring
if ($LASTEXITCODE -ne 0) { throw 'Could not persist monitoring OFF.' }

New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
Set-Content -LiteralPath $stopSignal -Value 'stop' -Encoding ascii
if (-not (Test-Path -LiteralPath $pidFile)) {
    Remove-Item -LiteralPath $stopSignal -Force -ErrorAction SilentlyContinue
    Write-Output 'Monitoring is OFF; no background publisher process was registered.'
    exit 0
}

$savedPid = 0
[void][int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$savedPid)
$processInfo = if ($savedPid -gt 0) { Get-CimInstance Win32_Process -Filter "ProcessId = $savedPid" -ErrorAction SilentlyContinue } else { $null }
if (-not $processInfo -or -not $processInfo.CommandLine -or -not $processInfo.CommandLine.Contains($scriptPath)) {
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $stopSignal -Force -ErrorAction SilentlyContinue
    Write-Output 'Monitoring is OFF; no matching publisher process was running.'
    exit 0
}

for ($i = 0; $i -lt 60 -and (Get-Process -Id $savedPid -ErrorAction SilentlyContinue); $i++) {
    Start-Sleep -Seconds 1
}
if (Get-Process -Id $savedPid -ErrorAction SilentlyContinue) {
    Stop-Process -Id $savedPid -Force
    Write-Output 'Publisher did not exit in time and was stopped; queued items remain stored for recovery.'
} else {
    Write-Output 'Publisher stopped cleanly; monitoring is OFF.'
}
Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $stopSignal -Force -ErrorAction SilentlyContinue
