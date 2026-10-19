# Run in an Administrator PowerShell. This script never reboots automatically.
# Microsoft: https://learn.microsoft.com/windows/wsl/troubleshooting
# Microsoft: https://learn.microsoft.com/windows/wsl/install
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Open PowerShell as Administrator, then run this script.'
}
Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Windows-Subsystem-Linux -All -NoRestart
Enable-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -All -NoRestart
bcdedit /set hypervisorlaunchtype auto
if ($LASTEXITCODE -ne 0) { throw 'Failed to enable hypervisor launch' }
Write-Host 'Save your work, reboot Windows, and start Docker Desktop.'
