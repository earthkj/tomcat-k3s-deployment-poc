$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pidPath = Join-Path $PSScriptRoot '.runtime/live-pids.json'
if (Test-Path -LiteralPath $pidPath) {
    $sessions = @(Get-Content -LiteralPath $pidPath -Raw | ConvertFrom-Json)
    foreach ($session in $sessions) {
        $ownedProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($session.Pid)"
        if (-not $ownedProcess -or -not $ownedProcess.CommandLine -or -not $ownedProcess.CommandLine.Contains($session.Script)) { continue }
        $baseUrl = "http://127.0.0.1:$($session.Port)"
        try {
            $page = (Invoke-WebRequest -Uri "$baseUrl/" -TimeoutSec 3 -UseBasicParsing).Content
            $match = [regex]::Match($page, "const token='([^']+)'")
            if (-not $match.Success) { continue }
            $headers = @{'X-PoC-Token'=$match.Groups[1].Value; Origin=$baseUrl}
            if ($session.Port -eq 18706) {
                $state = Invoke-RestMethod -Uri "$baseUrl/state" -TimeoutSec 3
                if (-not $state.done) { Invoke-RestMethod -Uri "$baseUrl/stop" -Method Post -Headers $headers -ContentType 'application/json' -Body '{}' | Out-Null }
            } else {
                $state = Invoke-RestMethod -Uri "$baseUrl/state" -TimeoutSec 3
                $body = @{action='send-stop'; mode=$state.mode} | ConvertTo-Json
                Invoke-RestMethod -Uri "$baseUrl/action" -Method Post -Headers $headers -ContentType 'application/json' -Body $body | Out-Null
            }
            $deadline = (Get-Date).AddSeconds(65)
            do {
                $state = Invoke-RestMethod -Uri "$baseUrl/state" -TimeoutSec 3
                $running = if ($session.Port -eq 18706) { -not $state.done } else { $state.measureBusy -or $state.busy }
                if ($running) { Start-Sleep -Milliseconds 500 }
            } while ($running -and (Get-Date) -lt $deadline)
            if ($running) { throw 'An operation is still active; preserve processes and inspect the console.' }
        } catch { throw "Console $($session.Port): $_" }
    }
}
foreach ($mode in @('legacy','http','ajp','bluegreen','rolling')) {
    python scripts/variants.py stop --mode $mode
    if ($LASTEXITCODE -ne 0) { throw "$mode stop failed" }
}
python scripts/poc.py stop
if ($LASTEXITCODE -ne 0) { throw 'Core stop failed' }
if (Test-Path -LiteralPath $pidPath) {
    foreach ($session in $sessions) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($session.Pid)"
        if ($process -and $process.CommandLine -and $process.CommandLine.Contains($session.Script)) { Stop-Process -Id $session.Pid }
    }
}
Write-Host 'Project services stopped; database volumes and results retained.'
