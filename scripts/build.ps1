$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path $PSScriptRoot -Parent)
python scripts/bootstrap-build.py
if ($LASTEXITCODE -ne 0) { throw 'Tool download or checksum validation failed' }
$env:JAVA_HOME = Join-Path (Get-Location) '.tools/jdk-21.0.12.1+1'
& '.tools/apache-maven-3.9.9/bin/mvn.cmd' -B -ntp '-Dmaven.repo.local=.cache/m2' clean verify
if ($LASTEXITCODE -ne 0) { throw 'Maven verification failed' }
