$ErrorActionPreference = "Continue"

$projectRoot   = "D:\Bridle"
$workspace     = "D:\Bridle-workspace"
$venvPy        = Join-Path $projectRoot ".venv\Scripts\python.exe"
$frontendDir   = Join-Path $projectRoot "frontend"
$appUrl        = "http://localhost:5173"
$backendHost   = "127.0.0.1"
$backendPort   = 8900
$chrome        = "C:\Program Files\Google\Chrome\Application\chrome.exe"
$logDir        = Join-Path $projectRoot "scripts\.app-logs"
$chromeProfile = Join-Path $logDir "chrome-profile"

if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}

# Per-session timestamped log set. Keeps history without one file growing unbounded.
$sessionStamp = [DateTime]::Now.ToString("yyyyMMdd_HHmmss")
$launcherLog = Join-Path $logDir "launcher_$sessionStamp.log"
$backendOut  = Join-Path $logDir "backend_$sessionStamp.out.log"
$backendErr  = Join-Path $logDir "backend_$sessionStamp.err.log"
$frontendOut = Join-Path $logDir "frontend_$sessionStamp.out.log"
$frontendErr = Join-Path $logDir "frontend_$sessionStamp.err.log"

# Rotate: keep only the most recent N sessions worth of logs.
$keepSessions = 10
$patterns = @("launcher_*.log", "backend_*.out.log", "backend_*.err.log", "frontend_*.out.log", "frontend_*.err.log")
foreach ($pat in $patterns) {
    Get-ChildItem -Path $logDir -Filter $pat -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip $keepSessions |
        Remove-Item -Force -ErrorAction SilentlyContinue
}


function Write-Log {
    param([string]$msg)
    $line = "{0} {1}" -f ([DateTime]::Now.ToString("yyyy-MM-dd HH:mm:ss")), $msg
    Add-Content -Path $launcherLog -Value $line -Encoding utf8
}

function Kill-Tree {
    param([int]$rootPid)
    if (-not $rootPid) { return }
    try {
        & taskkill.exe /T /F /PID $rootPid 2>$null | Out-Null
    } catch {}
}

Write-Log "=== launcher start ==="

foreach ($p in @($venvPy, (Join-Path $frontendDir "package.json"), $chrome, $workspace)) {
    if (-not (Test-Path $p)) {
        Write-Log "missing prerequisite: $p"
        exit 1
    }
}

try {
    $backend = Start-Process -FilePath $venvPy `
        -ArgumentList @("-m", "bridle", "serve", "-w", $workspace) `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $backendOut `
        -RedirectStandardError $backendErr
    Write-Log "backend started pid=$($backend.Id) workspace=$workspace"
} catch {
    Write-Log "failed to start backend: $_"
    exit 1
}

try {
    $frontend = Start-Process -FilePath "cmd.exe" `
        -ArgumentList "/c npm run dev" `
        -WorkingDirectory $frontendDir `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $frontendOut `
        -RedirectStandardError $frontendErr
    Write-Log "frontend started pid=$($frontend.Id)"
} catch {
    Write-Log "failed to start frontend: $_"
    Kill-Tree $backend.Id
    exit 1
}

Write-Log "waiting for frontend on $appUrl (up to 180s)"
$ready = $false
for ($i = 1; $i -le 180; $i++) {
    try {
        $resp = Invoke-WebRequest -UseBasicParsing -Uri $appUrl -TimeoutSec 1
        if ($resp.StatusCode) { $ready = $true; break }
    } catch {}
    Start-Sleep -Seconds 1
    if ($i % 10 -eq 0) { Write-Log "still waiting, ${i}s elapsed" }
}

if (-not $ready) {
    Write-Log "frontend did not come up within 180s; cleaning up"
    Kill-Tree $backend.Id
    Kill-Tree $frontend.Id
    exit 1
}

Write-Log "frontend ready, waiting for backend on ${backendHost}:${backendPort} (up to 300s; first run may build docker image)"
$backendReady = $false
for ($i = 1; $i -le 300; $i++) {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect($backendHost, $backendPort, $null, $null)
        if ($iar.AsyncWaitHandle.WaitOne(500)) {
            $client.EndConnect($iar)
            if ($client.Connected) { $backendReady = $true; $client.Close(); break }
        }
        $client.Close()
    } catch {}
    if ($backend.HasExited) {
        Write-Log "backend process exited before becoming ready (see backend.err log)"
        Kill-Tree $frontend.Id
        exit 1
    }
    Start-Sleep -Seconds 1
    if ($i % 15 -eq 0) { Write-Log "backend still starting, ${i}s elapsed" }
}

if (-not $backendReady) {
    Write-Log "backend did not come up within 300s; cleaning up"
    Kill-Tree $backend.Id
    Kill-Tree $frontend.Id
    exit 1
}

Write-Log "backend ready, launching chrome app window"
try {
    Start-Process -FilePath $chrome `
        -ArgumentList @(
            "--app=$appUrl",
            "--user-data-dir=$chromeProfile",
            "--start-fullscreen"
        ) `
        -Wait
    Write-Log "chrome app window closed"
} catch {
    Write-Log "chrome launch error: $_"
}

Write-Log "shutting down backend + frontend"
Kill-Tree $backend.Id
Kill-Tree $frontend.Id
Write-Log "=== launcher exit ==="
