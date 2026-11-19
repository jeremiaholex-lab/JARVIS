@echo off
rem JARVIS orb launcher. The shell host starts the backend on 127.0.0.1:8010 if
rem nothing is serving there yet, and owns its lifetime.
rem Usage: launch_orb.bat          (full UI window)
rem        launch_orb.bat hud      (floating HUD only, Ctrl+Alt+J to toggle)
setlocal
set "ORB=%~dp0"

pushd "%ORB%"
if /i "%~1"=="hud" (
  dotnet run -c Release --project JARVIS.Orb.csproj -- --hud
) else (
  dotnet run -c Release --project JARVIS.Orb.csproj
)
popd
