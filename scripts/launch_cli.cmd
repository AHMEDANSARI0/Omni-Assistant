@echo off
setlocal
set "ROOT=%~dp0.."
set "ACTIVATE=%ROOT%\.venv\Scripts\activate.bat"
if not exist "%ACTIVATE%" (
  echo The application is not installed yet.
  echo Run scripts\install_windows.ps1 from PowerShell first.
  pause
  exit /b 1
)
cd /d "%ROOT%"
cmd /k call "%ACTIVATE%"
endlocal
