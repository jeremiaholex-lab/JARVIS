# Register the local brain to start at sign-in.
#
# Why this exists: Ollama was not registered anywhere in the HKCU Run key and
# had no scheduled task, so after every reboot the local model was simply
# absent - "ollama not reachable" for the orb conversation and for
# "phase4.py skill" until someone opened the app by hand. The orb shell and the
# Phase 3 worker both start at logon; the brain has to as well, or the
# local-first design only works on days you remember.
#
# A scheduled task (not the Run key) is used for the same reason as
# install_phase3_task.ps1: "ollama serve" is a console program, and Start-Process
# at logon would leave a terminal window on the desktop.
#
# Keep this file pure ASCII: Windows PowerShell 5.1 reads .ps1 as the system
# ANSI codepage when there is no BOM, so a UTF-8 em dash becomes a control
# character and the script fails to parse.
$ErrorActionPreference = 'Stop'

$ollama = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
if (-not (Test-Path -LiteralPath $ollama)) {
    throw "Ollama not found at $ollama - install it from https://ollama.com/download/windows first."
}

$action = New-ScheduledTaskAction -Execute $ollama -Argument 'serve'
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName 'JARVIS Local Brain (Ollama)' -Action $action `
    -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'Starts the Ollama server that JARVIS uses as its local-first brain.' -Force | Out-Null

# Start it now as well, so the current session gets the fix without a reboot.
$portOpen = $false
try {
    Invoke-RestMethod -Uri 'http://localhost:11434/api/tags' -TimeoutSec 3 | Out-Null
    $portOpen = $true
} catch {
    $portOpen = $false
}

if (-not $portOpen) {
    Start-ScheduledTask -TaskName 'JARVIS Local Brain (Ollama)'
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 2
        try {
            Invoke-RestMethod -Uri 'http://localhost:11434/api/tags' -TimeoutSec 3 | Out-Null
            break
        } catch {}
    }
}

try {
    $models = (Invoke-RestMethod -Uri 'http://localhost:11434/api/tags' -TimeoutSec 5).models
    Write-Host "Ollama is serving $($models.Count) model(s); it will start again at every sign-in."
} catch {
    Write-Warning 'Ollama still is not answering on :11434 - check the task in Task Scheduler.'
}
