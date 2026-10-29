[CmdletBinding()]
param(
    [switch]$RemoveVirtualEnvironment,
    [switch]$RemoveUserData,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

function Assert-ExactChild {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedName
    )
    $FullPath = [System.IO.Path]::GetFullPath($Path)
    $Expected = [System.IO.Path]::GetFullPath((Join-Path $Root $ExpectedName))
    if (-not $FullPath.Equals($Expected, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove an unexpected path: $FullPath"
    }
    return $FullPath
}

$DesktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "Self-Healing PC Agent.lnk"
$StartMenuShortcut = Join-Path (Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs") "Self-Healing PC Agent.lnk"
foreach ($Shortcut in @($DesktopShortcut, $StartMenuShortcut)) {
    if (Test-Path -LiteralPath $Shortcut) {
        Remove-Item -LiteralPath $Shortcut -Force
        Write-Host "Removed shortcut: $Shortcut"
    }
}

if ($RemoveVirtualEnvironment) {
    $VenvPath = Assert-ExactChild -Path (Join-Path $Root ".venv") -ExpectedName ".venv"
    if (Test-Path -LiteralPath $VenvPath) {
        Remove-Item -LiteralPath $VenvPath -Recurse -Force
        Write-Host "Removed virtual environment: $VenvPath"
    }
}

if ($RemoveUserData) {
    if (-not $Yes) {
        throw "Refusing to remove data and workspace without the explicit -Yes switch."
    }
    foreach ($Name in @("data", "workspace")) {
        $Path = Assert-ExactChild -Path (Join-Path $Root $Name) -ExpectedName $Name
        if (Test-Path -LiteralPath $Path) {
            Remove-Item -LiteralPath $Path -Recurse -Force
            Write-Host "Removed user folder: $Path"
        }
    }
}

Write-Host "Uninstall helper complete. By default, project files, data, and workspace are preserved."
Write-Host "To remove the application source itself, close the app and delete this exact folder manually: $Root"
Write-Host "Windows Credential Manager entries are not removed automatically; use pc-agent credentials clear for each provider first if desired."
