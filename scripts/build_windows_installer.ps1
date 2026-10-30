[CmdletBinding()]
param(
    [string]$SourceOutput,
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

$VersionLine = (& $PcAgent --version).Trim()
if ($LASTEXITCODE -ne 0) { throw "Could not read the application version." }
$Version = ($VersionLine -split '\s+')[-1]

if (-not $SourceOutput) {
    $SourceOutput = Join-Path $Root "workspace\windows-installer-v$Version"
}

if (Test-Path -LiteralPath $SourceOutput) {
    throw "Installer source output already exists and will not be overwritten: $SourceOutput"
}

if (-not $SkipTests) {
    & $Python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Tests failed; installer source was not built." }
    & $Python -m ruff check .
    if ($LASTEXITCODE -ne 0) { throw "Ruff failed; installer source was not built." }
}

& $PcAgent release installer-source --output $SourceOutput
if ($LASTEXITCODE -ne 0) { throw "Windows installer source build failed." }
& $PcAgent release installer-verify $SourceOutput
if ($LASTEXITCODE -ne 0) { throw "Windows installer source verification failed." }

$CompilerCandidates = @()
if ($env:INNO_SETUP_COMPILER) {
    $CompilerCandidates += $env:INNO_SETUP_COMPILER
}
$PathCompiler = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if ($PathCompiler) {
    $CompilerCandidates += $PathCompiler.Source
}
$CompilerCandidates += @(
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
)
$Compiler = $CompilerCandidates |
    Where-Object { $_ -and (Test-Path -LiteralPath $_) } |
    Select-Object -First 1
if (-not $Compiler) {
    throw "Inno Setup 6 compiler (ISCC.exe) was not found. Install Inno Setup 6, or set INNO_SETUP_COMPILER to its full path. The verified installer source remains at: $SourceOutput"
}

$InstallerScript = Join-Path $SourceOutput "SelfHealingPCAgent.iss"
& $Compiler $InstallerScript
if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed." }

$SetupExe = Join-Path $SourceOutput "output\SelfHealingPCAgent-Setup-$Version.exe"
if (-not (Test-Path -LiteralPath $SetupExe)) {
    throw "Inno Setup completed but the expected setup executable was not found: $SetupExe"
}
$Hash = (Get-FileHash -LiteralPath $SetupExe -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Host "Unsigned Windows installer ready: $SetupExe"
Write-Host "SHA-256: $Hash"
Write-Host "Authenticode signing was not performed; Windows may show an unknown-publisher warning."
