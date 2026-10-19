param([switch]$SkipBuild)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
python scripts/poc.py prepare
if ($LASTEXITCODE -ne 0) { throw 'Prepare failed' }
if (-not $SkipBuild) {
    python scripts/poc.py build
    if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
}
python scripts/poc.py start --mode k3s
if ($LASTEXITCODE -ne 0) { throw 'k3s start failed' }
foreach ($mode in @('legacy','http','ajp','bluegreen','rolling')) {
    python scripts/variants.py start --mode $mode
    if ($LASTEXITCODE -ne 0) { throw "$mode start failed" }
}
& (Join-Path $PSScriptRoot 'scripts/start-consoles.ps1')
