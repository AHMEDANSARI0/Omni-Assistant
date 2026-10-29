[CmdletBinding()]
param(
    [string]$Output,
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$PcAgent = Join-Path $Root ".venv\Scripts\pc-agent.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "The project virtual environment was not found. Run scripts\install_windows.ps1 -Dev first."
}

if (-not $Output) {
    $VersionLine = (& $PcAgent --version).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Could not read the application version." }
    $Version = ($VersionLine -split '\s+')[-1]
    $Output = Join-Path $Root "workspace\self-healing-pc-agent-v$Version-portable.zip"
}

if (-not $SkipTests) {
    & $Python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Tests failed; release bundle was not built." }
    & $Python -m ruff check .
    if ($LASTEXITCODE -ne 0) { throw "Ruff failed; release bundle was not built." }
}

& $PcAgent release build --output $Output
if ($LASTEXITCODE -ne 0) { throw "Release bundle build failed." }
& $PcAgent release verify $Output
if ($LASTEXITCODE -ne 0) { throw "Release bundle verification failed." }

Write-Host "Portable release bundle ready: $Output"
