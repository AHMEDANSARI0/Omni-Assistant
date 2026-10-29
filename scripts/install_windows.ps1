[CmdletBinding()]
param(
    [switch]$Dev,
    [switch]$CreateShortcuts,
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root

if (-not (Test-Path -LiteralPath (Join-Path $Root "pyproject.toml"))) {
    throw "pyproject.toml was not found. Extract the full release ZIP first, then run this script from the scripts folder."
}

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv .venv
    }
    elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv .venv
    }
    else {
        throw "Python 3.11 or newer was not found. Install Python, then run this script again."
    }
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Virtual-environment creation did not produce $VenvPython."
}
& $VenvPython -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "The virtual environment must use Python 3.11 or newer. Remove .venv and rerun with a newer Python."
}

& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }

if ($Dev) {
    & $VenvPython -m pip install -e ".[dev]"
}
else {
    & $VenvPython -m pip install -e .
}
if ($LASTEXITCODE -ne 0) { throw "Package installation failed." }

$PcAgent = Join-Path $Root ".venv\Scripts\pc-agent.exe"
$PcAgentGui = Join-Path $Root ".venv\Scripts\pc-agent-gui.exe"
& $PcAgent doctor
if ($LASTEXITCODE -ne 0) { throw "Doctor failed after installation." }
& $PcAgentGui --check
if ($LASTEXITCODE -ne 0) { throw "GUI dependency check failed after installation." }

if ($Dev -and -not $SkipTests) {
    & $VenvPython -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Test suite failed after installation." }
    & $VenvPython -m ruff check .
    if ($LASTEXITCODE -ne 0) { throw "Ruff failed after installation." }
}

if ($CreateShortcuts) {
    $Shell = New-Object -ComObject WScript.Shell
    $Desktop = [Environment]::GetFolderPath("Desktop")
    $StartMenu = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs"
    foreach ($Folder in @($Desktop, $StartMenu)) {
        $ShortcutPath = Join-Path $Folder "Self-Healing PC Agent.lnk"
        $Shortcut = $Shell.CreateShortcut($ShortcutPath)
        $Shortcut.TargetPath = $PcAgentGui
        $Shortcut.WorkingDirectory = $Root
        $Shortcut.Description = "Self-Healing PC Agent"
        $Shortcut.Save()
    }
}

Write-Host ""
Write-Host "Installation complete."
Write-Host "Run the GUI with: .venv\Scripts\pc-agent-gui.exe"
Write-Host "Run the CLI with: .venv\Scripts\pc-agent.exe doctor"
