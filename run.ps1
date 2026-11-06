# OpenReSense launcher: sets up .venv and node_modules when needed, then starts the app
# and opens http://127.0.0.1:8765/. Extra args go to the server (e.g. -- --port 9000 --no-browser).
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$py = '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    Write-Host 'First run: creating Python environment...'
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3.12 -m venv .venv } else { python -m venv .venv }
}
# Reinstall only when requirements.txt changed since the last successful install.
$marker = '.venv\.requirements.installed'
if (-not (Test-Path $marker) -or (Get-Item requirements.txt).LastWriteTime -gt (Get-Item $marker).LastWriteTime) {
    & $py -m pip install --disable-pip-version-check -q -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
    New-Item -ItemType File -Force $marker | Out-Null
}
if (-not (Test-Path 'node_modules\ol\dist\ol.js') -or -not (Test-Path 'node_modules\cesium\Build\Cesium\Cesium.js')) {
    Write-Host 'Installing map/globe libraries (npm)...'
    npm install --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw 'npm install failed (is Node.js installed?)' }
}
& $py -m server @args
