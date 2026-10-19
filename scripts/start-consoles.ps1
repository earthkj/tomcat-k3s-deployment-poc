$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$pythonPath = (Get-Command python).Source
$pidPath = Join-Path $projectRoot '.runtime/live-pids.json'
$owned = @()
if (Test-Path -LiteralPath $pidPath) { $owned = @(Get-Content -LiteralPath $pidPath -Raw | ConvertFrom-Json) }
$sessions = @(
    @{Port=18700; Script='dashboard.py'; Mode=$null},
    @{Port=18701; Script='dashboard.py'; Mode='legacy'},
    @{Port=18702; Script='dashboard.py'; Mode='http'},
    @{Port=18703; Script='dashboard.py'; Mode='ajp'},
    @{Port=18704; Script='dashboard.py'; Mode='bluegreen'},
    @{Port=18705; Script='dashboard.py'; Mode='rolling'},
    @{Port=18706; Script='external.py'; Mode=$null}
)
foreach ($session in $sessions) {
    $scriptPath = Join-Path $PSScriptRoot $session.Script
    $listener = Get-NetTCPConnection -LocalPort $session.Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) {
        $running = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
        if (-not $running -or -not $running.CommandLine -or -not $running.CommandLine.Contains($scriptPath)) { throw "Port $($session.Port) is occupied by another application." }
        if (-not ($owned | Where-Object { $_.Pid -eq $running.ProcessId })) { $owned += @{Pid=$running.ProcessId;Script=$scriptPath;Port=$session.Port} }
        continue
    }
    $launchArgs = @('"' + $scriptPath + '"')
    if ($session.Mode) { $launchArgs += @('--mode', $session.Mode) }
    $process = Start-Process -FilePath $pythonPath -ArgumentList $launchArgs -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru
    $owned += @{Pid=$process.Id; Script=$scriptPath; Port=$session.Port}
}
$owned | ConvertTo-Json | Set-Content -LiteralPath $pidPath -Encoding UTF8
Write-Host 'Live consoles: http://127.0.0.1:18700/ through http://127.0.0.1:18706/'
