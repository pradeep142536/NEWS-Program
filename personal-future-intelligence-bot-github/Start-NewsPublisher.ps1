$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$dataDir = Join-Path $projectRoot 'data'
$logDir = Join-Path $projectRoot 'logs'
$pidFile = Join-Path $dataDir 'publisher.pid'
$stopSignal = Join-Path $dataDir 'stop.signal'
$stdout = Join-Path $logDir 'publisher.log'
$stderr = Join-Path $logDir 'publisher-error.log'

New-Item -ItemType Directory -Path $dataDir, $logDir -Force | Out-Null

if (Test-Path -LiteralPath $pidFile) {
    $savedPid = 0
    [void][int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$savedPid)
    if ($savedPid -gt 0) {
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $savedPid" -ErrorAction SilentlyContinue
        if ($processInfo -and $processInfo.CommandLine -and $processInfo.CommandLine.Contains((Join-Path $projectRoot 'bot.py'))) {
            Write-Output "Publisher is already running (PID $savedPid)."
            exit 0
        }
    }
    Remove-Item -LiteralPath $pidFile -Force
}

$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $venvPython) {
    $python = $venvPython
} else {
    $python = (Get-Command python -ErrorAction Stop).Source
}
$scriptPath = Join-Path $projectRoot 'bot.py'

& $python $scriptPath --verify-destination
if ($LASTEXITCODE -ne 0) { throw 'Destination verification failed; publisher was not started.' }
Remove-Item -LiteralPath $stopSignal -Force -ErrorAction SilentlyContinue
& $python $scriptPath --enable-monitoring
if ($LASTEXITCODE -ne 0) { throw 'Could not enable monitoring; publisher was not started.' }

$arguments = @("`"$scriptPath`"")
$process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
for ($i = 0; $i -lt 30 -and -not (Test-Path -LiteralPath $pidFile); $i++) {
    if ($process.HasExited) { break }
    Start-Sleep -Seconds 1
}
if (Test-Path -LiteralPath $pidFile) {
    $publisherPid = (Get-Content -LiteralPath $pidFile -Raw).Trim()
    Write-Output "Publisher started in the background (PID $publisherPid). Logs: $logDir"
} else {
    Write-Output "Publisher launch was attempted (launcher PID $($process.Id)); check $stderr for startup errors."
}
