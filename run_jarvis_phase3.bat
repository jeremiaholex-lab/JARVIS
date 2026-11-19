@echo off
set "OPENJARVIS_HOME=%~dp0.openjarvis"
set "OPENJARVIS_CONFIG=%~dp0jarvis-phase3.toml"
set "OPENJARVIS_REPO=%~dp0openjarvis_runtime"
"C:\Python314\python.exe" "%~dp0jarvis_voice_loop.py" %*
