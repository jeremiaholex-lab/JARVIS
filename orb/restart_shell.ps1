$ErrorActionPreference = 'SilentlyContinue'

# Stop the shell AND the backend it owns.
#
# This matters more than it looks: ShellHost attaches when port 8010 is already
# open, and killing OrbShell with -Force does not take down its pythonw child.
# The next start then re-attaches to the OLD backend, so the window comes up
# healthy while still running superseded server code - which is exactly how a
# correct fix appears to "not work". Restarting the orb must restart both.

Write-Host '--- stopping OrbShell ---'
$shells = Get-Process OrbShell
if ($shells) { $shells | ForEach-Object { Write-Host ("kill shell PID " + $_.Id) } }
Get-Process OrbShell | Stop-Process -Force

Write-Host '--- stopping orphaned backends on 8010 ---'
$backends = Get-CimInstance Win32_Process |
    Where-Object { $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -match 'orb[\\/]+server\.py' }
foreach ($b in $backends) {
    Write-Host ("kill backend PID " + $b.ProcessId)
    Stop-Process -Id $b.ProcessId -Force
}
Start-Sleep -Seconds 2

Write-Host '--- starting orb shell ---'
Start-Process -FilePath 'C:\Users\HP\Desktop\JARVIS\orb\bin\Release\net8.0-windows\OrbShell.exe' `
  -WorkingDirectory 'C:\Users\HP\Desktop\JARVIS\orb'

# Poll for the port rather than sleeping a fixed 6s: the backend imports torch,
# faster-whisper and Kokoro before binding 8010, which takes 15-30s here, and a
# fixed sleep reported DOWN for backends that came up seconds later.
$up = $false
for ($i = 0; $i -lt 90; $i++) {
    Start-Sleep -Seconds 2
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $c.Connect('127.0.0.1', 8010)
        $c.Close()
        $up = $true
        break
    } catch {
        if (-not (Get-Process OrbShell)) { break }
    }
}

Write-Host '--- OrbShell ---'
Get-Process OrbShell | Select-Object ProcessName, Id, MainWindowTitle | Format-List
if (-not $up) {
    Write-Host '--- 8010: DOWN after 180s - see orb\backend.err.log ---'
    exit 1
}
Write-Host '--- 8010: UP ---'

# The port opens before the models finish loading. Report the boot stage so a
# slow load is not mistaken for a failed restart, and confirm the backend is
# actually the new code (stage/voice_ready only exist in the current server.py).
$stage = ''
for ($i = 0; $i -lt 60; $i++) {
    try {
        $status = Invoke-RestMethod -Uri 'http://localhost:8010/api/status' -TimeoutSec 4
        $stage = $status.stage
        if ($status.models_ready) {
            Write-Host ('--- models: READY (code build reports stage="' + $stage + '") ---')
            break
        }
        if ($i % 10 -eq 9) { Write-Host ('   loading, stage=' + $stage + ' voice_ready=' + $status.voice_ready) }
    } catch { }
    Start-Sleep -Seconds 2
}
