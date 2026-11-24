Add-Type -AssemblyName System.Media
$s = New-Object System.Media.SoundPlayer((Join-Path $PSScriptRoot 'tts_check.wav'))
$s.PlayLooping()
Start-Sleep -Seconds 4
$s.Stop()
$s.Dispose()
Write-Host "played tts_check.wav (looping 4s)"