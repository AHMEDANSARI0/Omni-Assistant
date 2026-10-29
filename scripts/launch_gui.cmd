@echo off
setlocal
set "ROOT=%~dp0.."
set "GUI=%ROOT%\.venv\Scripts\pc-agent-gui.exe"
if not exist "%GUI%" (
  echo The application is not installed yet.
  echo Run scripts\install_windows.ps1 from PowerShell first.
  pause
  exit /b 1
)
start "Self-Healing PC Agent" "%GUI%" --root "%ROOT%"
endlocal
