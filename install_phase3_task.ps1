$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$python = Join-Path $root 'openjarvis_runtime\.venv\Scripts\python.exe'
$worker = Join-Path $root 'phase3.py'
if (-not (Test-Path -LiteralPath $python)) { throw "OpenJarvis Python runtime not found: $python" }
$action = New-ScheduledTaskAction -Execute $python -Argument "`"$worker`" worker" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'JARVIS Phase 3 Worker' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Runs the scheduled spoken digest and local 48-hour deadline monitor.' -Force | Out-Null
Start-ScheduledTask -TaskName 'JARVIS Phase 3 Worker'
$morningTime = if ($env:JARVIS_MORNING_TIME) { $env:JARVIS_MORNING_TIME } else { '08:00' }
Write-Host "Installed and started JARVIS Phase 3 Worker. Daily digest time: $morningTime local time."
