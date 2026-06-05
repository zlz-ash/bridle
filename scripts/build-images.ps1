$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path "$PSScriptRoot\..").Path
docker version 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Error "Docker is not running or not installed. Start Docker Desktop and retry."
    exit 1
}
docker build -t bridle-node-agent:local -f "$repo\docker\node-agent.Dockerfile" $repo
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
docker build -t bridle-main-agent:local -f "$repo\docker\main-agent.Dockerfile" $repo
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "Images built: bridle-node-agent:local, bridle-main-agent:local"
