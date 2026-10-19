param([ValidateSet('legacy','k3s')][string]$Mode = 'legacy')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
docker info --format '{{.ServerVersion}}'
if ($LASTEXITCODE -ne 0) { throw 'Docker Linux engine is unavailable. See README.md.' }
python scripts/poc.py prepare
if ($LASTEXITCODE -ne 0) { throw 'Prepare failed' }
python scripts/poc.py build
if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
python scripts/poc.py start --mode $Mode
if ($LASTEXITCODE -ne 0) { throw 'Start failed' }
Write-Host 'PoC API: http://127.0.0.1:18080/health/ready'
