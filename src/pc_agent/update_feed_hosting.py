"""Operational Windows-first hosting for signed update feeds.

Phase 40 produced a verified static update-feed directory but deliberately
stopped before deployment.  Phase 55 adds the operational Windows layer:

* a versioned hosting package around the exact Phase 40 ``site`` tree;
* a built-in bounded HTTPS static host suitable for a Windows scheduled
  task, using an operator-supplied trusted certificate;
* an IIS static-site deployment/rollback package for Windows hosts that
  already run IIS;
* a one-shot live HTTPS verifier that fetches the feed, signature and
  bundle, then independently checks cache policy, size/SHA-256, portable
  bundle structure and the detached Ed25519 signature.

The platform registry intentionally has Linux/macOS slots.  They are marked
``planned`` in this Windows-first phase and fail closed if selected; only
the shared static-site format is cross-platform today.  No hosting account,
DNS record, firewall rule or publicly trusted certificate is fabricated.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from . import __version__
from .config import Settings
from .errors import PcAgentError
from .http_tls import create_tls_context
from .logging_config import redact_text
from .online_updates import (
    MAX_FEED_BYTES,
    MAX_SIGNATURE_DOWNLOAD_BYTES,
    _origin,
    _parse_feed_manifest,
    _SameOriginRedirectHandler,
    _validate_update_url,
)
from .release import ReleaseFileRecord, verify_release_bundle
from .update_feed_publisher import (
    UPDATE_FEED_PUBLISHER_MANIFEST,
    UpdateFeedPackageInfo,
    verify_update_feed_package,
)
from .updates import (
    b64url_decode,
    b64url_encode,
    inspect_release_signature,
    parse_version,
    validate_key_id,
)

UPDATE_FEED_HOSTING_FORMAT = 1
UPDATE_FEED_HOSTING_MANIFEST = "hosting-manifest.json"
UPDATE_FEED_HOSTING_REPORT = "windows-https-update-hosting-report.json"
WINDOWS_HTTPS_TASK_PROFILE = "windows-https-task"
WINDOWS_IIS_PROFILE = "windows-iis"

_MAX_MANIFEST_BYTES = 1_000_000
_MAX_REPORT_BYTES = 1_000_000
_MAX_GENERATED_BYTES = 250_000
_MAX_BUNDLE_BYTES = 25_000_000
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_DEPLOYMENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")
_WINDOWS_PATH_RE = re.compile(
    r"^(?:[A-Za-z]:\\(?:[^\\/:*?\"<>|]+\\)*[^\\/:*?\"<>|]*|"
    r"\\\\[^\\/:*?\"<>|]+\\[^\\/:*?\"<>|]+(?:\\[^\\/:*?\"<>|]+)*)$"
)
_CHECK_NAMES = (
    "https_feed_url",
    "feed_fetch",
    "feed_cache_policy",
    "feed_manifest",
    "same_origin_resources",
    "signature_fetch",
    "bundle_fetch",
    "bundle_size_and_hash",
    "bundle_portable_verification",
    "bundle_signature_verified",
    "release_cache_policy",
    "no_sensitive_data_recorded",
)
_MANIFEST_KEYS = {
    "app",
    "base_url",
    "bundle_sha256",
    "channel",
    "deployment_name",
    "deployment_root",
    "feed_sha256",
    "feed_url",
    "files",
    "format",
    "host_name",
    "key_id",
    "platform",
    "platform_status",
    "port",
    "profile",
    "public_key",
    "publisher_manifest_sha256",
    "site_file_count",
    "tls_required",
    "url_prefix",
    "version",
}


@dataclass(frozen=True, slots=True)
class HostingProfile:
    profile_id: str
    platform: str
    display_name: str
    status: str  # implemented | planned
    description: str

    @property
    def implemented(self) -> bool:
        return self.status == "implemented"

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "display_name": self.display_name,
            "id": self.profile_id,
            "platform": self.platform,
            "status": self.status,
        }


HOSTING_PROFILES: tuple[HostingProfile, ...] = (
    HostingProfile(
        WINDOWS_HTTPS_TASK_PROFILE,
        "windows",
        "Windows built-in HTTPS host (scheduled task)",
        "implemented",
        "Bounded static HTTPS server run by pc-agent-release-admin.",
    ),
    HostingProfile(
        WINDOWS_IIS_PROFILE,
        "windows",
        "Windows IIS static HTTPS site",
        "implemented",
        "Versioned IIS deployment with an operator-supplied certificate.",
    ),
    HostingProfile(
        "linux-nginx",
        "linux",
        "Linux Nginx static HTTPS site",
        "planned",
        "Reserved structure; Linux acceptance is deferred by request.",
    ),
    HostingProfile(
        "linux-caddy",
        "linux",
        "Linux Caddy static HTTPS site",
        "planned",
        "Reserved structure; Linux acceptance is deferred by request.",
    ),
    HostingProfile(
        "macos-nginx",
        "macos",
        "macOS Nginx static HTTPS site",
        "planned",
        "Reserved structure; macOS acceptance is deferred by request.",
    ),
    HostingProfile(
        "macos-caddy",
        "macos",
        "macOS Caddy static HTTPS site",
        "planned",
        "Reserved structure; macOS acceptance is deferred by request.",
    ),
)
_PROFILES_BY_ID = {profile.profile_id: profile for profile in HOSTING_PROFILES}


def list_hosting_profiles() -> tuple[HostingProfile, ...]:
    return HOSTING_PROFILES


def get_hosting_profile(profile_id: str) -> HostingProfile:
    value = profile_id.strip().lower()
    if _PROFILE_RE.fullmatch(value) is None or value not in _PROFILES_BY_ID:
        known = ", ".join(profile.profile_id for profile in HOSTING_PROFILES)
        raise PcAgentError(f"Unknown update-feed hosting profile. Known: {known}.")
    return _PROFILES_BY_ID[value]


def _require_implemented(profile: HostingProfile) -> None:
    if not profile.implemented:
        raise PcAgentError(
            f"Hosting profile {profile.profile_id} is reserved for "
            f"{profile.platform} but is not implemented in this "
            "Windows-first phase."
        )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _safe_text(value: Any, *, limit: int = 300) -> str:
    text = "" if value is None else str(value)
    cleaned = "".join(
        character
        for character in text.strip()
        if ord(character) >= 32 and ord(character) != 127
    )
    return cleaned[:limit]


def _sanitize_error(error: BaseException | str) -> str:
    return _safe_text(redact_text(str(error)), limit=500) or (
        "Windows HTTPS update-feed hosting verification failed."
    )


def normalize_release_public_key(public_key: str | bytes) -> tuple[bytes, str]:
    if isinstance(public_key, bytes):
        if len(public_key) != 32:
            raise PcAgentError("Release public key must decode to exactly 32 bytes.")
        raw = public_key
    else:
        raw = b64url_decode(
            public_key,
            expected_length=32,
            label="Release public key",
        )
    return raw, b64url_encode(raw)


def read_release_public_key_file(path: Path) -> bytes:
    key_file = path.expanduser().resolve()
    if not key_file.is_file() or key_file.is_symlink():
        raise PcAgentError(f"Release public key file was not found: {key_file}.")
    for raw_line in key_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            name, value = line.split("=", 1)
            if name.strip() != "PC_AGENT_RELEASE_PUBLIC_KEY":
                continue
            return b64url_decode(
                value.strip(),
                expected_length=32,
                label="Release public key",
            )
        return b64url_decode(
            line,
            expected_length=32,
            label="Release public key",
        )
    raise PcAgentError("Release public key file contains no key value.")


def _validate_deployment_name(raw: str) -> str:
    value = raw.strip()
    if _DEPLOYMENT_NAME_RE.fullmatch(value) is None:
        raise PcAgentError(
            "Hosting deployment name must be 1-64 safe letters, digits, "
            "spaces, '.', '_' or '-'."
        )
    return value


def _validate_windows_absolute_path(raw: str, *, label: str) -> str:
    value = raw.strip()
    if (
        not value
        or len(value) > 240
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or _WINDOWS_PATH_RE.fullmatch(value) is None
    ):
        raise PcAgentError(f"{label} must be a safe absolute Windows path.")
    if len(value) > 3:
        value = value.rstrip("\\")
    return value


def _default_deployment_name(profile: HostingProfile) -> str:
    if profile.profile_id == WINDOWS_IIS_PROFILE:
        return "SelfHealingPCAgentUpdates"
    return "SelfHealingPCAgentUpdateFeed"


def _default_deployment_root(profile: HostingProfile) -> str:
    if profile.profile_id == WINDOWS_IIS_PROFILE:
        return r"C:\inetpub\SelfHealingPCAgentUpdates"
    return r"C:\ProgramData\SelfHealingPCAgent\UpdateFeedHost"


def _read_bounded(path: Path, *, limit: int, label: str) -> bytes:
    candidate = path.expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise PcAgentError(f"{label} was not found: {candidate}.")
    size = candidate.stat().st_size
    if size > limit:
        raise PcAgentError(f"{label} exceeds the {limit}-byte limit.")
    data = candidate.read_bytes()
    if len(data) != size:
        raise PcAgentError(f"{label} changed while reading: {candidate}.")
    return data


def _records(files: list[tuple[str, bytes]]) -> tuple[ReleaseFileRecord, ...]:
    return tuple(
        ReleaseFileRecord(path, len(data), _sha256(data))
        for path, data in sorted(files, key=lambda item: item[0])
    )


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _base_url_parts(info: UpdateFeedPackageInfo) -> tuple[str, int, str]:
    parsed = urllib.parse.urlsplit(info.base_url)
    if parsed.scheme.lower() != "https":
        raise PcAgentError(
            "Operational update-feed hosting requires an HTTPS base URL; "
            "loopback HTTP packages are test fixtures, not live hosting."
        )
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise PcAgentError("Update-feed base URL has no host name.")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise PcAgentError("Update-feed base URL has an invalid port.") from exc
    prefix = parsed.path.rstrip("/")
    return host, port, prefix


@dataclass(frozen=True, slots=True)
class UpdateFeedHostingPackageInfo:
    path: Path
    profile: str
    platform: str
    deployment_name: str
    deployment_root: str
    base_url: str
    feed_url: str
    channel: str
    version: str
    key_id: str
    public_key: str
    host_name: str
    port: int
    url_prefix: str
    bundle_sha256: str
    feed_sha256: str
    publisher_manifest_sha256: str
    manifest_sha256: str
    site_file_count: int
    files: tuple[ReleaseFileRecord, ...] = ()

    @property
    def file_count(self) -> int:
        return len(self.files) + 1

    def summary(self) -> str:
        return (
            f"profile={self.profile}; version={self.version}; "
            f"files={self.file_count}; host={self.host_name}; "
            f"bundle_sha256={self.bundle_sha256[:16]}..."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "bundle_sha256": self.bundle_sha256,
            "certificate_included": False,
            "channel": self.channel,
            "deployment_name": self.deployment_name,
            "deployment_root": self.deployment_root,
            "feed_sha256": self.feed_sha256,
            "feed_url": self.feed_url,
            "file_count": self.file_count,
            "files": [record.to_dict() for record in self.files],
            "host_name": self.host_name,
            "key_id": self.key_id,
            "live_verified": False,
            "manifest_sha256": self.manifest_sha256,
            "path": str(self.path),
            "platform": self.platform,
            "port": self.port,
            "private_key_included": False,
            "profile": self.profile,
            "public_key": self.public_key,
            "publisher_manifest_sha256": self.publisher_manifest_sha256,
            "site_file_count": self.site_file_count,
            "tls_certificate_required": True,
            "uploaded": False,
            "url_prefix": self.url_prefix,
            "version": self.version,
        }


def _verify_script(info: UpdateFeedHostingPackageInfo) -> bytes:
    feed_url = info.feed_url
    key_id = info.key_id
    profile = info.profile
    text = """[CmdletBinding()]
param(
    [string]$PcAgentExe = 'pc-agent.exe',
    [string]$AppRoot = (Get-Location).Path,
    [Parameter(Mandatory = $true)][string]$PublicKeyFile,
    [string]$ReportPath
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $PublicKeyFile -PathType Leaf)) {
    throw \"Release public key file was not found: $PublicKeyFile\"
}
if ([string]::IsNullOrWhiteSpace($ReportPath)) {
    $Stamp = Get-Date -Format 'yyyyMMddHHmmss'
    $ReportPath = Join-Path (Get-Location).Path `
        \"windows-https-update-hosting-$Stamp.json\"
}

& $PcAgentExe --root $AppRoot update hosting-check `
    --feed-url __FEED_URL__ `
    --public-key-file $PublicKeyFile `
    --key-id __KEY_ID__ `
    --profile __PROFILE__ `
    --report $ReportPath
exit $LASTEXITCODE
"""
    text = text.replace("__FEED_URL__", _ps_quote(feed_url))
    text = text.replace("__KEY_ID__", _ps_quote(key_id))
    text = text.replace("__PROFILE__", _ps_quote(profile))
    return text.encode("utf-8")


def _windows_readme(
    info: UpdateFeedPackageInfo,
    *,
    profile: HostingProfile,
    deployment_name: str,
    deployment_root: str,
) -> bytes:
    lines = [
        "Self-Healing PC Agent - Windows HTTPS update-feed hosting",
        "===========================================================",
        "",
        f"Profile: {profile.profile_id}",
        f"Feed URL: {info.feed_url}",
        f"Channel/version: {info.channel} / {info.version}",
        f"Deployment name: {deployment_name}",
        f"Deployment root: {deployment_root}",
        "",
        "The site folder is the exact verified Phase 40 static package.",
        "No release-signing private key and no TLS private key is included.",
        "A publicly trusted TLS certificate, DNS name, firewall/NAT rule",
        "and the Windows host itself remain operator-supplied infrastructure.",
        "",
        "After deployment, prove the public endpoint with the generated",
        "Verify-LiveUpdateFeed.ps1 script or pc-agent update hosting-check.",
        "A package on disk is not live evidence until that report passes.",
        "Run operational commands one at a time.",
        "",
    ]
    if profile.profile_id == WINDOWS_HTTPS_TASK_PROFILE:
        lines.extend(
            [
                "Built-in host notes:",
                "- Start-UpdateFeedHost.ps1 runs the bounded HTTPS server.",
                "- Register-UpdateFeedHost.ps1 creates the AtStartup task.",
                "- Restrict the TLS private-key file to SYSTEM/Administrators.",
                "- The server serves only the verified site files and healthz.",
            ]
        )
    else:
        lines.extend(
            [
                "IIS notes:",
                "- Deploy-UpdateFeedIis.ps1 requires IIS/WebAdministration.",
                "- It creates a versioned release directory before switching.",
                "- Rollback-UpdateFeedIis.ps1 restores the previous path.",
                "- The certificate is selected by LocalMachine thumbprint.",
            ]
        )
    lines.extend(
        [
            "",
            "Linux/macOS profile names are reserved in the profile registry,",
            "but their deployment and live acceptance are intentionally not",
            "claimed by this Windows-first package.",
            "",
        ]
    )
    return ("\n".join(lines)).encode("utf-8")


def _task_profile_files(
    info: UpdateFeedPackageInfo,
    *,
    profile: HostingProfile,
    deployment_name: str,
    deployment_root: str,
) -> list[tuple[str, bytes]]:
    start = """[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$CertificatePath,
    [Parameter(Mandatory = $true)][string]$PrivateKeyPath,
    [Parameter(Mandatory = $true)][string]$PublicKeyFile,
    [string]$ReleaseAdminExe = 'pc-agent-release-admin.exe',
    [string]$BindAddress = '0.0.0.0',
    [int]$Port = __PORT__
)

$ErrorActionPreference = 'Stop'
$PackageRoot = Split-Path -Parent $PSScriptRoot
& $ReleaseAdminExe serve-hosting $PackageRoot `
    --public-key-file $PublicKeyFile `
    --key-id __KEY_ID__ `
    --certificate $CertificatePath `
    --private-key $PrivateKeyPath `
    --bind $BindAddress `
    --port $Port
exit $LASTEXITCODE
"""
    start = start.replace("__PORT__", str(info.port))
    start = start.replace("__KEY_ID__", _ps_quote(info.key_id))

    register = """#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$CertificatePath,
    [Parameter(Mandatory = $true)][string]$PrivateKeyPath,
    [Parameter(Mandatory = $true)][string]$PublicKeyFile,
    [Parameter(Mandatory = $true)][string]$ReleaseAdminExe,
    [string]$BindAddress = '0.0.0.0',
    [int]$Port = __PORT__,
    [string]$TaskName = __TASK_NAME__
)

$ErrorActionPreference = 'Stop'
function Quote-Argument([string]$Value) {
    return '\"' + ($Value -replace '\"', '\\\"') + '\"'
}

$PackageRoot = Split-Path -Parent $PSScriptRoot
$Arguments = @(
    'serve-hosting',
    (Quote-Argument $PackageRoot),
    '--public-key-file', (Quote-Argument $PublicKeyFile),
    '--key-id', __KEY_ID__,
    '--certificate', (Quote-Argument $CertificatePath),
    '--private-key', (Quote-Argument $PrivateKeyPath),
    '--bind', (Quote-Argument $BindAddress),
    '--port', [string]$Port
) -join ' '

$Action = New-ScheduledTaskAction -Execute $ReleaseAdminExe `
    -Argument $Arguments -WorkingDirectory $PackageRoot
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' `
    -LogonType ServiceAccount -RunLevel Highest
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $Action `
    -Trigger $Trigger -Principal $Principal -Settings $Settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host \"Registered and started scheduled task: $TaskName\"
Write-Host \"Feed URL: __FEED_URL__\"
"""
    register = register.replace("__PORT__", str(info.port))
    register = register.replace("__TASK_NAME__", _ps_quote(deployment_name))
    register = register.replace("__KEY_ID__", _ps_quote(info.key_id))
    register = register.replace("__FEED_URL__", info.feed_url)

    unregister = """#Requires -RunAsAdministrator
[CmdletBinding()]
param([string]$TaskName = __TASK_NAME__)

$ErrorActionPreference = 'Stop'
$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $Task) {
    Write-Host \"Scheduled task is not registered: $TaskName\"
    exit 0
}
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host \"Removed scheduled task: $TaskName\"
"""
    unregister = unregister.replace("__TASK_NAME__", _ps_quote(deployment_name))

    return [
        ("windows/Start-UpdateFeedHost.ps1", start.encode("utf-8")),
        ("windows/Register-UpdateFeedHost.ps1", register.encode("utf-8")),
        ("windows/Unregister-UpdateFeedHost.ps1", unregister.encode("utf-8")),
        ("windows/Verify-LiveUpdateFeed.ps1", _verify_script(info)),
        (
            "README-WINDOWS.txt",
            _windows_readme(
                info,
                profile=profile,
                deployment_name=deployment_name,
                deployment_root=deployment_root,
            ),
        ),
    ]


def _iis_root_config() -> bytes:
    return b"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<configuration>
  <system.webServer>
    <directoryBrowse enabled=\"false\" />
    <defaultDocument enabled=\"false\" />
    <staticContent>
      <remove fileExtension=\".json\" />
      <mimeMap fileExtension=\".json\" mimeType=\"application/json\" />
      <remove fileExtension=\".zip\" />
      <mimeMap fileExtension=\".zip\" mimeType=\"application/zip\" />
      <clientCache cacheControlMode=\"UseMaxAge\"
                   cacheControlMaxAge=\"00:01:00\" />
    </staticContent>
    <httpProtocol>
      <customHeaders>
        <add name=\"X-Content-Type-Options\" value=\"nosniff\" />
        <add name=\"Referrer-Policy\" value=\"no-referrer\" />
        <add name=\"Content-Security-Policy\"
             value=\"default-src 'none'; sandbox\" />
        <add name=\"Strict-Transport-Security\"
             value=\"max-age=31536000\" />
      </customHeaders>
    </httpProtocol>
    <security>
      <requestFiltering>
        <fileExtensions allowUnlisted=\"false\">
          <add fileExtension=\".json\" allowed=\"true\" />
          <add fileExtension=\".zip\" allowed=\"true\" />
          <add fileExtension=\".txt\" allowed=\"true\" />
          <add fileExtension=\".example\" allowed=\"true\" />
        </fileExtensions>
        <denyUrlSequences>
          <add sequence=\"..\" />
        </denyUrlSequences>
      </requestFiltering>
    </security>
  </system.webServer>
</configuration>
"""


def _iis_releases_config() -> bytes:
    return b"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<configuration>
  <system.webServer>
    <staticContent>
      <clientCache cacheControlMode=\"UseMaxAge\"
                   cacheControlMaxAge=\"365.00:00:00\" />
    </staticContent>
  </system.webServer>
</configuration>
"""


def _iis_profile_files(
    info: UpdateFeedPackageInfo,
    *,
    profile: HostingProfile,
    deployment_name: str,
    deployment_root: str,
) -> list[tuple[str, bytes]]:
    deploy = """#Requires -RunAsAdministrator
#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Fa-f0-9]{40}$')]
    [string]$CertificateThumbprint,
    [string]$DeploymentRoot = __DEPLOYMENT_ROOT__,
    [string]$SiteName = __SITE_NAME__,
    [string]$HostName = __HOST_NAME__,
    [int]$Port = __PORT__
)

$ErrorActionPreference = 'Stop'
Import-Module WebAdministration
$PackageRoot = Split-Path -Parent $PSScriptRoot
$ManifestPath = Join-Path $PackageRoot 'hosting-manifest.json'
$Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
foreach ($Record in $Manifest.files) {
    if (-not ([string]$Record.path).StartsWith('site/')) { continue }
    $Path = Join-Path $PackageRoot (($Record.path) -replace '/', '\\')
    $Actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash
    if ($Actual.ToLowerInvariant() -ne $Record.sha256) {
        throw \"Hosting package file failed SHA-256 verification: $Path\"
    }
}

$Stamp = [DateTime]::UtcNow.ToString('yyyyMMddHHmmss')
$ShortHash = ([string]$Manifest.bundle_sha256).Substring(0, 12)
$ReleaseName = \"$($Manifest.version)-$ShortHash-$Stamp\"
$ReleaseRoot = Join-Path $DeploymentRoot \"releases\\$ReleaseName\"
New-Item -ItemType Directory -Path $ReleaseRoot -Force | Out-Null
Copy-Item -Path (Join-Path $PackageRoot 'site\\*') `
    -Destination $ReleaseRoot -Recurse -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'iis\\web.config') `
    -Destination (Join-Path $ReleaseRoot 'web.config') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'iis\\releases.web.config') `
    -Destination (Join-Path $ReleaseRoot 'releases\\web.config') -Force
& icacls.exe $ReleaseRoot /grant:r 'IIS_IUSRS:(OI)(CI)R' /T /Q | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not set IIS read permissions.' }

$StatePath = Join-Path $DeploymentRoot 'hosting-state.json'
$PreviousRelease = $null
$Site = Get-Website -Name $SiteName -ErrorAction SilentlyContinue
if ($null -ne $Site) {
    $PreviousRelease = [string]$Site.PhysicalPath
}
if ($null -eq (Get-WebAppPoolState -Name $SiteName `
        -ErrorAction SilentlyContinue)) {
    New-WebAppPool -Name $SiteName | Out-Null
}
Set-ItemProperty \"IIS:\\AppPools\\$SiteName\" -Name managedRuntimeVersion -Value ''
if ($null -eq $Site) {
    New-Website -Name $SiteName -PhysicalPath $ReleaseRoot `
        -ApplicationPool $SiteName -HostHeader $HostName -Port $Port `
        -Ssl -SslFlags 1 | Out-Null
} else {
    Set-ItemProperty \"IIS:\\Sites\\$SiteName\" -Name physicalPath -Value $ReleaseRoot
    Set-ItemProperty \"IIS:\\Sites\\$SiteName\" -Name applicationPool -Value $SiteName
    $Binding = Get-WebBinding -Name $SiteName -Protocol https `
        -HostHeader $HostName -Port $Port -ErrorAction SilentlyContinue
    if ($null -eq $Binding) {
        New-WebBinding -Name $SiteName -Protocol https `
            -HostHeader $HostName -Port $Port -SslFlags 1 | Out-Null
    }
}

$Certificate = Get-Item -LiteralPath `
    \"Cert:\\LocalMachine\\My\\$CertificateThumbprint\"
$SslPath = \"IIS:\\SslBindings\\0.0.0.0!$Port!$HostName\"
if (Test-Path -LiteralPath $SslPath) {
    Remove-Item -LiteralPath $SslPath -Force
}
New-Item -Path $SslPath -Value $Certificate -SslFlags 1 | Out-Null

$State = [ordered]@{
    site_name = $SiteName
    current_release = $ReleaseRoot
    previous_release = $PreviousRelease
    version = [string]$Manifest.version
    deployed_at = [DateTime]::UtcNow.ToString('o')
}
$State | ConvertTo-Json | Set-Content -LiteralPath $StatePath -Encoding UTF8
if ((Get-WebAppPoolState -Name $SiteName).Value -ne 'Started') {
    Start-WebAppPool -Name $SiteName
}
if ((Get-WebsiteState -Name $SiteName).Value -ne 'Started') {
    Start-Website -Name $SiteName
}
Write-Host \"Deployed update feed release: $ReleaseRoot\"
Write-Host \"Feed URL: $($Manifest.feed_url)\"
"""
    deploy = deploy.replace(
        "__DEPLOYMENT_ROOT__", _ps_quote(deployment_root)
    )
    deploy = deploy.replace("__SITE_NAME__", _ps_quote(deployment_name))
    deploy = deploy.replace("__HOST_NAME__", _ps_quote(info.host_name))
    deploy = deploy.replace("__PORT__", str(info.port))

    rollback = """#Requires -RunAsAdministrator
#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$DeploymentRoot = __DEPLOYMENT_ROOT__,
    [string]$SiteName = __SITE_NAME__
)

$ErrorActionPreference = 'Stop'
Import-Module WebAdministration
$StatePath = Join-Path $DeploymentRoot 'hosting-state.json'
$State = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
if ([string]::IsNullOrWhiteSpace([string]$State.previous_release)) {
    throw 'No previous IIS update-feed release is recorded.'
}
$Current = [string]$State.current_release
$Previous = [string]$State.previous_release
Set-ItemProperty \"IIS:\\Sites\\$SiteName\" -Name physicalPath -Value $Previous
$State.current_release = $Previous
$State.previous_release = $Current
$State | ConvertTo-Json | Set-Content -LiteralPath $StatePath -Encoding UTF8
if ((Get-WebsiteState -Name $SiteName).Value -ne 'Started') {
    Start-Website -Name $SiteName
}
Write-Host \"Rolled back IIS update feed to: $Previous\"
"""
    rollback = rollback.replace(
        "__DEPLOYMENT_ROOT__", _ps_quote(deployment_root)
    )
    rollback = rollback.replace("__SITE_NAME__", _ps_quote(deployment_name))

    return [
        ("windows/iis/web.config", _iis_root_config()),
        ("windows/iis/releases.web.config", _iis_releases_config()),
        ("windows/Deploy-UpdateFeedIis.ps1", deploy.encode("utf-8")),
        ("windows/Rollback-UpdateFeedIis.ps1", rollback.encode("utf-8")),
        ("windows/Verify-LiveUpdateFeed.ps1", _verify_script(info)),
        (
            "README-WINDOWS.txt",
            _windows_readme(
                info,
                profile=profile,
                deployment_name=deployment_name,
                deployment_root=deployment_root,
            ),
        ),
    ]


def _profile_files(
    info: UpdateFeedPackageInfo,
    *,
    profile: HostingProfile,
    deployment_name: str,
    deployment_root: str,
) -> list[tuple[str, bytes]]:
    if profile.profile_id == WINDOWS_HTTPS_TASK_PROFILE:
        return _task_profile_files(
            info,
            profile=profile,
            deployment_name=deployment_name,
            deployment_root=deployment_root,
        )
    if profile.profile_id == WINDOWS_IIS_PROFILE:
        return _iis_profile_files(
            info,
            profile=profile,
            deployment_name=deployment_name,
            deployment_root=deployment_root,
        )
    _require_implemented(profile)
    raise AssertionError("unreachable")


def _package_shell_info(
    feed_info: UpdateFeedPackageInfo,
    *,
    profile: HostingProfile,
    deployment_name: str,
    deployment_root: str,
) -> UpdateFeedHostingPackageInfo:
    host_name, port, prefix = _base_url_parts(feed_info)
    return UpdateFeedHostingPackageInfo(
        path=Path(),
        profile=profile.profile_id,
        platform=profile.platform,
        deployment_name=deployment_name,
        deployment_root=deployment_root,
        base_url=feed_info.base_url,
        feed_url=feed_info.feed_url,
        channel=feed_info.channel,
        version=feed_info.version,
        key_id=feed_info.key_id,
        public_key=feed_info.public_key,
        host_name=host_name,
        port=port,
        url_prefix=prefix,
        bundle_sha256=feed_info.bundle_sha256,
        feed_sha256=feed_info.feed_sha256,
        publisher_manifest_sha256=feed_info.manifest_sha256,
        manifest_sha256="",
        site_file_count=feed_info.file_count,
        files=(),
    )


def build_update_feed_hosting_package(
    feed_package_dir: Path,
    output_dir: Path,
    *,
    public_key: str | bytes,
    key_id: str,
    profile_id: str = WINDOWS_HTTPS_TASK_PROFILE,
    deployment_name: str | None = None,
    deployment_root: str | None = None,
) -> UpdateFeedHostingPackageInfo:
    """Build and verify one Windows operational hosting package."""

    profile = get_hosting_profile(profile_id)
    _require_implemented(profile)
    clean_key_id = validate_key_id(key_id)
    clean_name = _validate_deployment_name(
        deployment_name or _default_deployment_name(profile)
    )
    clean_root = _validate_windows_absolute_path(
        deployment_root or _default_deployment_root(profile),
        label="Hosting deployment root",
    )
    feed_info = verify_update_feed_package(
        feed_package_dir,
        public_key=public_key,
        key_id=clean_key_id,
    )
    shell = _package_shell_info(
        feed_info,
        profile=profile,
        deployment_name=clean_name,
        deployment_root=clean_root,
    )

    destination = output_dir.expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        raise PcAgentError(
            f"Update-feed hosting package output already exists: {destination}."
        )

    site_files: list[tuple[str, bytes]] = []
    for record in feed_info.files:
        limit = _MAX_BUNDLE_BYTES if record.path.endswith(".zip") else _MAX_GENERATED_BYTES
        data = _read_bounded(
            feed_info.path / record.path,
            limit=limit,
            label="Update feed package file",
        )
        if len(data) != record.size or _sha256(data) != record.sha256:
            raise PcAgentError("Update feed package changed during hosting build.")
        site_files.append((f"site/{record.path}", data))
    publisher_data = _read_bounded(
        feed_info.path / UPDATE_FEED_PUBLISHER_MANIFEST,
        limit=_MAX_MANIFEST_BYTES,
        label="Update feed publisher manifest",
    )
    if _sha256(publisher_data) != feed_info.manifest_sha256:
        raise PcAgentError("Update feed package changed during hosting build.")
    site_files.append((f"site/{UPDATE_FEED_PUBLISHER_MANIFEST}", publisher_data))

    generated_files = _profile_files(
        shell,
        profile=profile,
        deployment_name=clean_name,
        deployment_root=clean_root,
    )
    all_files = sorted(
        [*site_files, *generated_files], key=lambda item: item[0]
    )
    if len({path for path, _data in all_files}) != len(all_files):
        raise PcAgentError("Update-feed hosting package paths are duplicated.")
    for path, data in generated_files:
        if len(data) > _MAX_GENERATED_BYTES:
            raise PcAgentError(f"Generated hosting file is too large: {path}.")
        if b"-----BEGIN PRIVATE KEY-----" in data:
            raise PcAgentError("Generated hosting data contains a private key.")

    records = _records(all_files)
    manifest = {
        "app": "self-healing-pc-agent",
        "base_url": feed_info.base_url,
        "bundle_sha256": feed_info.bundle_sha256,
        "channel": feed_info.channel,
        "deployment_name": clean_name,
        "deployment_root": clean_root,
        "feed_sha256": feed_info.feed_sha256,
        "feed_url": feed_info.feed_url,
        "files": [record.to_dict() for record in records],
        "format": UPDATE_FEED_HOSTING_FORMAT,
        "host_name": shell.host_name,
        "key_id": clean_key_id,
        "platform": profile.platform,
        "platform_status": profile.status,
        "port": shell.port,
        "profile": profile.profile_id,
        "public_key": feed_info.public_key,
        "publisher_manifest_sha256": feed_info.manifest_sha256,
        "site_file_count": feed_info.file_count,
        "tls_required": True,
        "url_prefix": shell.url_prefix,
        "version": feed_info.version,
    }
    manifest_data = _json_bytes(manifest)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.tmp-{secrets.token_hex(4)}"
    )
    try:
        for relative, data in all_files:
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (temporary / UPDATE_FEED_HOSTING_MANIFEST).write_bytes(manifest_data)
        verify_update_feed_hosting_package(
            temporary,
            public_key=feed_info.public_key,
            key_id=clean_key_id,
        )
        if destination.exists():
            raise PcAgentError(
                f"Update-feed hosting package output already exists: {destination}."
            )
        temporary.replace(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)

    return verify_update_feed_hosting_package(
        destination,
        public_key=feed_info.public_key,
        key_id=clean_key_id,
    )


def _parse_hosting_manifest(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PcAgentError("Update-feed hosting manifest is not valid JSON.") from exc
    if not isinstance(payload, dict) or set(payload) != _MANIFEST_KEYS:
        raise PcAgentError("Update-feed hosting manifest has an unexpected shape.")
    if (
        payload["format"] != UPDATE_FEED_HOSTING_FORMAT
        or payload["app"] != "self-healing-pc-agent"
        or payload["tls_required"] is not True
    ):
        raise PcAgentError("Update-feed hosting manifest format is unsupported.")
    if not isinstance(payload["files"], list):
        raise PcAgentError("Update-feed hosting manifest files are invalid.")
    return payload


def _hosting_records(payload: dict[str, Any]) -> tuple[ReleaseFileRecord, ...]:
    records: list[ReleaseFileRecord] = []
    seen: set[str] = set()
    for raw in payload["files"]:
        if not isinstance(raw, dict) or set(raw) != {"path", "size", "sha256"}:
            raise PcAgentError("Update-feed hosting manifest has a malformed record.")
        path = raw["path"]
        size = raw["size"]
        digest = raw["sha256"]
        if (
            not isinstance(path, str)
            or not path
            or path in seen
            or path.startswith(("/", "\\"))
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or size > _MAX_BUNDLE_BYTES
            or not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
        ):
            raise PcAgentError("Update-feed hosting manifest has an invalid record.")
        seen.add(path)
        records.append(ReleaseFileRecord(path, size, digest))
    if not records or records != sorted(records, key=lambda record: record.path):
        raise PcAgentError("Update-feed hosting manifest records are not canonical.")
    return tuple(records)


def verify_update_feed_hosting_package(
    package_dir: Path,
    *,
    public_key: str | bytes,
    key_id: str,
    expected_profile: str | None = None,
) -> UpdateFeedHostingPackageInfo:
    """Verify one Windows hosting package and its embedded Phase 40 site."""

    clean_key_id = validate_key_id(key_id)
    _raw_public, public_text = normalize_release_public_key(public_key)
    root_input = package_dir.expanduser()
    if root_input.is_symlink() or not root_input.is_dir():
        raise PcAgentError(f"Update-feed hosting package was not found: {root_input}.")
    root = root_input.resolve()

    manifest_data = _read_bounded(
        root / UPDATE_FEED_HOSTING_MANIFEST,
        limit=_MAX_MANIFEST_BYTES,
        label="Update-feed hosting manifest",
    )
    payload = _parse_hosting_manifest(manifest_data)
    records = _hosting_records(payload)
    record_by_path = {record.path: record for record in records}

    actual: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            relative = path.relative_to(root).as_posix()
            raise PcAgentError(
                f"Update-feed hosting package contains a symbolic link: {relative}."
            )
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != set(record_by_path) | {UPDATE_FEED_HOSTING_MANIFEST}:
        raise PcAgentError(
            "Update-feed hosting package inventory does not match its manifest."
        )

    file_data: dict[str, bytes] = {}
    for record in records:
        limit = _MAX_BUNDLE_BYTES if record.path.endswith(".zip") else _MAX_GENERATED_BYTES
        if record.path == f"site/{UPDATE_FEED_PUBLISHER_MANIFEST}":
            limit = _MAX_MANIFEST_BYTES
        data = _read_bounded(
            root / record.path,
            limit=limit,
            label="Update-feed hosting file",
        )
        if len(data) != record.size or _sha256(data) != record.sha256:
            raise PcAgentError(
                f"Update-feed hosting file failed verification: {record.path}."
            )
        file_data[record.path] = data

    profile = get_hosting_profile(str(payload["profile"]))
    _require_implemented(profile)
    if expected_profile is not None and profile.profile_id != get_hosting_profile(
        expected_profile
    ).profile_id:
        raise PcAgentError("Update-feed hosting profile does not match the expected profile.")
    if (
        payload["platform"] != profile.platform
        or payload["platform_status"] != profile.status
    ):
        raise PcAgentError("Update-feed hosting platform metadata is inconsistent.")
    deployment_name = _validate_deployment_name(str(payload["deployment_name"]))
    deployment_root = _validate_windows_absolute_path(
        str(payload["deployment_root"]),
        label="Hosting deployment root",
    )
    if payload["key_id"] != clean_key_id or payload["public_key"] != public_text:
        raise PcAgentError("Update-feed hosting signing key does not match.")
    version = str(payload["version"])
    parse_version(version)

    feed_info = verify_update_feed_package(
        root / "site",
        public_key=public_text,
        key_id=clean_key_id,
        expected_base_url=str(payload["base_url"]),
        expected_channel=str(payload["channel"]),
    )
    shell = _package_shell_info(
        feed_info,
        profile=profile,
        deployment_name=deployment_name,
        deployment_root=deployment_root,
    )
    comparisons = {
        "base_url": feed_info.base_url,
        "bundle_sha256": feed_info.bundle_sha256,
        "channel": feed_info.channel,
        "feed_sha256": feed_info.feed_sha256,
        "feed_url": feed_info.feed_url,
        "host_name": shell.host_name,
        "port": shell.port,
        "publisher_manifest_sha256": feed_info.manifest_sha256,
        "site_file_count": feed_info.file_count,
        "url_prefix": shell.url_prefix,
        "version": feed_info.version,
    }
    for key, expected in comparisons.items():
        if payload[key] != expected:
            raise PcAgentError(
                f"Update-feed hosting manifest field does not match its site: {key}."
            )

    expected_site_records = {
        f"site/{record.path}": record for record in feed_info.files
    }
    publisher_record = record_by_path.get(
        f"site/{UPDATE_FEED_PUBLISHER_MANIFEST}"
    )
    if publisher_record is None or publisher_record.sha256 != feed_info.manifest_sha256:
        raise PcAgentError("Hosting package publisher manifest record is inconsistent.")
    for relative, expected in expected_site_records.items():
        actual_record = record_by_path.get(relative)
        if actual_record is None or (
            actual_record.size,
            actual_record.sha256,
        ) != (expected.size, expected.sha256):
            raise PcAgentError("Hosting package site inventory is inconsistent.")

    expected_generated = dict(
        _profile_files(
            shell,
            profile=profile,
            deployment_name=deployment_name,
            deployment_root=deployment_root,
        )
    )
    actual_generated = {
        path: data for path, data in file_data.items() if not path.startswith("site/")
    }
    if actual_generated != expected_generated:
        raise PcAgentError("Hosting package generated Windows files are inconsistent.")
    expected_paths = (
        set(expected_site_records)
        | {f"site/{UPDATE_FEED_PUBLISHER_MANIFEST}"}
        | set(expected_generated)
    )
    if set(record_by_path) != expected_paths:
        raise PcAgentError("Hosting package published paths are unexpected.")

    return UpdateFeedHostingPackageInfo(
        path=root,
        profile=profile.profile_id,
        platform=profile.platform,
        deployment_name=deployment_name,
        deployment_root=deployment_root,
        base_url=feed_info.base_url,
        feed_url=feed_info.feed_url,
        channel=feed_info.channel,
        version=feed_info.version,
        key_id=clean_key_id,
        public_key=public_text,
        host_name=shell.host_name,
        port=shell.port,
        url_prefix=shell.url_prefix,
        bundle_sha256=feed_info.bundle_sha256,
        feed_sha256=feed_info.feed_sha256,
        publisher_manifest_sha256=feed_info.manifest_sha256,
        manifest_sha256=_sha256(manifest_data),
        site_file_count=feed_info.file_count,
        files=records,
    )


@dataclass(frozen=True, slots=True)
class _ServedFile:
    data: bytes
    content_type: str
    cache_control: str
    etag: str


def _content_type(path: str) -> str:
    lower = path.lower()
    if lower.endswith(".json"):
        return "application/json"
    if lower.endswith(".zip"):
        return "application/zip"
    return "text/plain; charset=utf-8"


def _cache_control(path: str) -> str:
    if path == "stable.json":
        return "public, max-age=60, must-revalidate"
    if path.startswith("releases/"):
        return "public, max-age=31536000, immutable"
    return "public, max-age=300"


def _parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    if header is None:
        return None
    value = header.strip()
    if not value.startswith("bytes=") or "," in value:
        raise PcAgentError("Only one byte range is supported.")
    raw_start, separator, raw_end = value[6:].partition("-")
    if not separator or (not raw_start and not raw_end):
        raise PcAgentError("Invalid byte range.")
    try:
        if raw_start:
            start = int(raw_start)
            end = int(raw_end) if raw_end else size - 1
        else:
            suffix = int(raw_end)
            if suffix <= 0:
                raise ValueError
            start = max(0, size - suffix)
            end = size - 1
    except ValueError as exc:
        raise PcAgentError("Invalid byte range.") from exc
    if start < 0 or start >= size or end < start:
        raise PcAgentError("Byte range is not satisfiable.")
    return start, min(end, size - 1)


def _hosting_handler(
    files: Mapping[str, _ServedFile],
    *,
    prefix: str,
    health_data: bytes,
    https_enabled: bool,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "SelfHealingPCAgentUpdateHost/1"
        sys_version = ""

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def do_GET(self) -> None:
            self._handle(head=False)

        def do_HEAD(self) -> None:
            self._handle(head=True)

        def do_POST(self) -> None:
            self._method_not_allowed()

        def do_PUT(self) -> None:
            self._method_not_allowed()

        def do_PATCH(self) -> None:
            self._method_not_allowed()

        def do_DELETE(self) -> None:
            self._method_not_allowed()

        def do_OPTIONS(self) -> None:
            self._method_not_allowed()

        def _method_not_allowed(self) -> None:
            self._send(
                405,
                b'{"error":"method-not-allowed"}\n',
                "application/json",
                "no-store",
                head=False,
                extra={"Allow": "GET, HEAD"},
            )

        def _handle(self, *, head: bool) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
                self._send_error_json(400, "invalid-request", head=head)
                return
            path = urllib.parse.unquote(parsed.path)
            if (
                "\\" in path
                or any(ord(char) < 32 or ord(char) == 127 for char in path)
                or any(part in {".", ".."} for part in path.split("/"))
            ):
                self._send_error_json(400, "invalid-path", head=head)
                return
            if prefix:
                if path != prefix and not path.startswith(prefix + "/"):
                    self._send_error_json(404, "not-found", head=head)
                    return
                relative = path[len(prefix) :].lstrip("/")
            else:
                relative = path.lstrip("/")

            if relative == "healthz":
                self._send(
                    200,
                    health_data,
                    "application/json",
                    "no-store",
                    head=head,
                )
                return
            served = files.get(relative)
            if served is None:
                self._send_error_json(404, "not-found", head=head)
                return
            if self.headers.get("If-None-Match") == served.etag:
                self._send(
                    304,
                    b"",
                    served.content_type,
                    served.cache_control,
                    head=True,
                    etag=served.etag,
                )
                return

            body = served.data
            status = 200
            extra: dict[str, str] = {"Accept-Ranges": "bytes"}
            if not head:
                try:
                    byte_range = _parse_range(
                        self.headers.get("Range"), len(body)
                    )
                except PcAgentError:
                    self._send_error_json(
                        416,
                        "range-not-satisfiable",
                        head=False,
                        extra={"Content-Range": f"bytes */{len(body)}"},
                    )
                    return
                if byte_range is not None:
                    start, end = byte_range
                    body = body[start : end + 1]
                    status = 206
                    extra["Content-Range"] = (
                        f"bytes {start}-{end}/{len(served.data)}"
                    )
            self._send(
                status,
                body,
                served.content_type,
                served.cache_control,
                head=head,
                etag=served.etag,
                extra=extra,
            )

        def _send_error_json(
            self,
            status: int,
            code: str,
            *,
            head: bool,
            extra: dict[str, str] | None = None,
        ) -> None:
            self._send(
                status,
                (json.dumps({"error": code}, separators=(",", ":")) + "\n").encode(),
                "application/json",
                "no-store",
                head=head,
                extra=extra,
            )

        def _send(
            self,
            status: int,
            body: bytes,
            content_type: str,
            cache_control: str,
            *,
            head: bool,
            etag: str | None = None,
            extra: dict[str, str] | None = None,
        ) -> None:
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", cache_control)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy", "default-src 'none'; sandbox"
                )
                if https_enabled:
                    self.send_header(
                        "Strict-Transport-Security", "max-age=31536000"
                    )
                if etag is not None:
                    self.send_header("ETag", etag)
                for name, value in (extra or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                if not head and body:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

    return Handler


class _HostingServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_update_feed_hosting_server(
    hosting_package_dir: Path,
    *,
    public_key: str | bytes,
    key_id: str,
    bind_host: str = "127.0.0.1",
    port: int = 0,
    tls_context: ssl.SSLContext | None = None,
    allow_http_for_tests: bool = False,
) -> ThreadingHTTPServer:
    """Create the bounded static server for a Windows task hosting package."""

    info = verify_update_feed_hosting_package(
        hosting_package_dir,
        public_key=public_key,
        key_id=key_id,
        expected_profile=WINDOWS_HTTPS_TASK_PROFILE,
    )
    if tls_context is None and not allow_http_for_tests:
        raise PcAgentError(
            "The built-in update-feed host requires a TLS context in production."
        )
    if not bind_host.strip() or len(bind_host) > 253:
        raise PcAgentError("Update-feed host bind address is invalid.")
    if isinstance(port, bool) or not 0 <= port <= 65535:
        raise PcAgentError("Update-feed host port is invalid.")

    feed_info = verify_update_feed_package(
        info.path / "site",
        public_key=info.public_key,
        key_id=info.key_id,
    )
    served: dict[str, _ServedFile] = {}
    paths = [record.path for record in feed_info.files]
    paths.append(UPDATE_FEED_PUBLISHER_MANIFEST)
    for relative in paths:
        limit = _MAX_BUNDLE_BYTES if relative.endswith(".zip") else _MAX_MANIFEST_BYTES
        data = _read_bounded(
            info.path / "site" / relative,
            limit=limit,
            label="Hosted update-feed file",
        )
        served[relative] = _ServedFile(
            data=data,
            content_type=_content_type(relative),
            cache_control=_cache_control(relative),
            etag=f'"{_sha256(data)}"',
        )
    health = _json_bytes(
        {
            "app": "self-healing-pc-agent",
            "channel": info.channel,
            "status": "ok",
            "version": info.version,
        }
    )
    handler = _hosting_handler(
        served,
        prefix=info.url_prefix,
        health_data=health,
        https_enabled=tls_context is not None,
    )
    try:
        server = _HostingServer((bind_host, port), handler)
    except OSError as exc:
        raise PcAgentError(f"Could not bind the update-feed host: {exc}") from exc
    if tls_context is not None:
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    return server


def serve_update_feed_hosting_package(
    hosting_package_dir: Path,
    *,
    public_key: str | bytes,
    key_id: str,
    certificate_path: Path,
    private_key_path: Path,
    bind_host: str = "0.0.0.0",
    port: int | None = None,
) -> int:
    """Serve one verified hosting package until interrupted."""

    info = verify_update_feed_hosting_package(
        hosting_package_dir,
        public_key=public_key,
        key_id=key_id,
        expected_profile=WINDOWS_HTTPS_TASK_PROFILE,
    )
    certificate = certificate_path.expanduser().resolve()
    private_key = private_key_path.expanduser().resolve()
    for label, path in (
        ("TLS certificate", certificate),
        ("TLS private key", private_key),
    ):
        if path.is_symlink() or not path.is_file():
            raise PcAgentError(f"{label} file was not found: {path}.")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(certfile=certificate, keyfile=private_key)
    except (OSError, ssl.SSLError) as exc:
        raise PcAgentError(f"Could not load the TLS certificate/key pair: {exc}") from exc

    server = create_update_feed_hosting_server(
        info.path,
        public_key=info.public_key,
        key_id=info.key_id,
        bind_host=bind_host,
        port=info.port if port is None else port,
        tls_context=context,
    )
    print("Self-Healing PC Agent Windows HTTPS update-feed host")
    print(f"Feed URL: {info.feed_url}")
    print(f"Version: {info.version}")
    print(f"Listening: {bind_host}:{server.server_address[1]}")
    print("TLS certificate/key contents: not printed or copied")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("Update-feed host stopped by operator.")
    finally:
        server.server_close()
    return 0


@dataclass(frozen=True, slots=True)
class HostingHttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    final_url: str
    tls_verified: bool | None = None


HostingTransport = Callable[
    [str, int, float], HostingHttpResponse | bytes | bytearray
]


@dataclass(slots=True)
class HostingVerificationCheck:
    name: str
    status: str = "pending"
    detail: str = ""

    def set(self, status: str, detail: str) -> None:
        self.status = status
        self.detail = _safe_text(detail)

    def to_dict(self) -> dict[str, str]:
        return {"detail": self.detail, "name": self.name, "status": self.status}


@dataclass(slots=True)
class UpdateFeedHostingReport:
    report_path: Path
    started_at: str
    profile: str
    key_id: str
    client_platform: str
    status: str = "running"
    finished_at: str | None = None
    elapsed_ms: float = 0.0
    error: str | None = None
    feed_manifest: dict[str, Any] | None = None
    resources: dict[str, dict[str, Any] | None] = field(
        default_factory=lambda: {
            "bundle": None,
            "feed": None,
            "signature": None,
        }
    )
    tls_scheme: str = "unknown"
    tls_verified: bool | None = None
    urls: dict[str, str | None] = field(
        default_factory=lambda: {
            "bundle": None,
            "feed": None,
            "signature": None,
        }
    )
    checks: list[HostingVerificationCheck] = field(
        default_factory=lambda: [
            HostingVerificationCheck(name) for name in _CHECK_NAMES
        ]
    )

    def check(self, name: str) -> HostingVerificationCheck:
        for check in self.checks:
            if check.name == name:
                return check
        raise KeyError(name)

    def summary(self) -> str:
        return (
            f"status={self.status}; profile={self.profile}; "
            f"feed={self.urls.get('feed')}; report={self.report_path}"
        )

    def _base_dict(self) -> dict[str, Any]:
        return {
            "app": "Self-Healing PC Agent",
            "checks": [check.to_dict() for check in self.checks],
            "client_platform": self.client_platform,
            "elapsed_ms": round(float(self.elapsed_ms), 3),
            "error": self.error,
            "feed": self.feed_manifest,
            "finished_at": self.finished_at,
            "format": UPDATE_FEED_HOSTING_FORMAT,
            "hosting": {
                "platform": "windows",
                "profile": self.profile,
                "profile_proof": "operator-declared",
            },
            "key_id": self.key_id,
            "resources": self.resources,
            "started_at": self.started_at,
            "status": self.status,
            "tls": {
                "certificate_verified": self.tls_verified,
                "scheme": self.tls_scheme,
            },
            "urls": self.urls,
            "version": __version__,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._base_dict()
        payload["report_sha256"] = _sha256(_canonical_json_bytes(payload))
        return payload

    def write(self) -> None:
        data = _json_bytes(self.to_dict())
        if len(data) > _MAX_REPORT_BYTES:
            raise PcAgentError("Windows HTTPS hosting report is too large.")
        if self.report_path.exists():
            raise PcAgentError(
                f"Windows HTTPS hosting report already exists: {self.report_path.name}."
            )
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.report_path.with_name(
            f".{self.report_path.name}.tmp-{secrets.token_hex(4)}"
        )
        try:
            temporary.write_bytes(data)
            if self.report_path.exists():
                raise PcAgentError(
                    "Windows HTTPS hosting report already exists: "
                    f"{self.report_path.name}."
                )
            temporary.replace(self.report_path)
        finally:
            if temporary.exists():
                temporary.unlink()


@dataclass(frozen=True, slots=True)
class UpdateFeedHostingReportVerification:
    report_path: Path
    version: str
    status: str
    profile: str
    feed_url: str
    feed_version: str
    bundle_sha256: str
    report_sha256: str

    def summary(self) -> str:
        return (
            f"status={self.status}; version={self.version}; "
            f"profile={self.profile}; feed_version={self.feed_version}; "
            f"report_sha256={self.report_sha256[:16]}..."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_sha256": self.bundle_sha256,
            "feed_url": self.feed_url,
            "feed_version": self.feed_version,
            "profile": self.profile,
            "report": str(self.report_path),
            "report_sha256": self.report_sha256,
            "status": self.status,
            "version": self.version,
        }


def _client_platform(raw: str | None = None) -> str:
    value = (raw or sys.platform).lower()
    if value.startswith("win"):
        return "windows"
    if value.startswith("linux"):
        return "linux"
    if value == "darwin":
        return "macos"
    return "unknown"


def _resolve_report_path(
    settings: Settings | None, report_path: Path | None
) -> Path:
    if report_path is None:
        stamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
        base = (
            settings.data_dir / "update-hosting"
            if settings is not None
            else Path.cwd() / "update-hosting-reports"
        )
        candidate = base / f"windows-https-hosting-{stamp}.json"
    else:
        candidate = report_path.expanduser()
    if candidate.suffix.lower() != ".json":
        raise PcAgentError("Windows HTTPS hosting report output must end with .json.")
    if candidate.exists() or candidate.is_symlink():
        raise PcAgentError(
            f"Windows HTTPS hosting report already exists: {candidate.name}."
        )
    return candidate.resolve()


def _normalize_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for name, value in headers.items():
        key = str(name).strip().lower()
        if key:
            normalized[key] = _safe_text(value, limit=500)
    return normalized


def _default_hosting_fetch(
    validated: Any,
    *,
    label: str,
    max_bytes: int,
    timeout_seconds: float,
) -> HostingHttpResponse:
    origin = _origin(validated)
    opener = urllib.request.build_opener(
        _SameOriginRedirectHandler(origin),
        urllib.request.HTTPSHandler(context=create_tls_context()),
    )
    request = urllib.request.Request(
        validated.url,
        headers={
            "Accept": "application/json,application/zip,application/octet-stream",
            "Accept-Encoding": "identity",
            "User-Agent": (
                f"Self-Healing-PC-Agent/{__version__} "
                "(operator-requested Windows HTTPS hosting check)"
            ),
        },
        method="GET",
    )
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            status = int(getattr(response, "status", response.getcode()))
            final = _validate_update_url(
                str(response.geturl() or validated.url),
                label=f"Final {label} URL",
                expected_origin=origin,
                resolve=True,
            )
            headers = _normalize_headers(dict(response.headers.items()))
            raw_length = headers.get("content-length")
            if raw_length:
                try:
                    declared = int(raw_length)
                except ValueError as exc:
                    raise PcAgentError(
                        f"{label} response has an invalid Content-Length."
                    ) from exc
                if declared < 0 or declared > max_bytes:
                    raise PcAgentError(
                        f"{label} response exceeds the {max_bytes}-byte limit."
                    )
            body = bytearray()
            while True:
                remaining = max_bytes + 1 - len(body)
                if remaining <= 0:
                    raise PcAgentError(
                        f"{label} response exceeds the {max_bytes}-byte limit."
                    )
                chunk = response.read(min(65_536, remaining))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise PcAgentError(
                        f"{label} response exceeds the {max_bytes}-byte limit."
                    )
            return HostingHttpResponse(
                status=status,
                headers=headers,
                body=bytes(body),
                final_url=final.url,
                tls_verified=final.scheme == "https",
            )
    except PcAgentError:
        raise
    except urllib.error.HTTPError as exc:
        raise PcAgentError(
            f"{label} download failed with HTTP {exc.code} for host {validated.host}."
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLCertVerificationError) or (
            "certificate verify failed" in str(reason).lower()
        ):
            raise PcAgentError(
                f"TLS certificate verification failed for host {validated.host}. "
                "Certificate verification was not disabled."
            ) from exc
        raise PcAgentError(
            f"{label} download failed for host {validated.host}: {reason}"
        ) from exc


def _fetch_hosting_resource(
    raw_url: str,
    *,
    label: str,
    max_bytes: int,
    timeout_seconds: float,
    expected_origin: tuple[str, str, int] | None = None,
    transport: HostingTransport | None = None,
) -> tuple[HostingHttpResponse, Any]:
    validated = _validate_update_url(
        raw_url,
        label=label,
        expected_origin=expected_origin,
        resolve=transport is None,
    )
    if transport is None:
        response = _default_hosting_fetch(
            validated,
            label=label,
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
        )
    else:
        try:
            result = transport(validated.url, max_bytes, timeout_seconds)
        except PcAgentError:
            raise
        except Exception as exc:
            raise PcAgentError(
                f"{label} download failed for host {validated.host}: {exc}"
            ) from exc
        if isinstance(result, HostingHttpResponse):
            response = result
        elif isinstance(result, (bytes, bytearray)):
            response = HostingHttpResponse(
                status=200,
                headers={},
                body=bytes(result),
                final_url=validated.url,
                tls_verified=None,
            )
        else:
            raise PcAgentError(f"{label} transport did not return an HTTP response.")
    if len(response.body) > max_bytes:
        raise PcAgentError(f"{label} response exceeds the {max_bytes}-byte limit.")
    final = _validate_update_url(
        response.final_url,
        label=f"Final {label} URL",
        expected_origin=_origin(validated),
        resolve=False,
    )
    normalized = HostingHttpResponse(
        status=int(response.status),
        headers=_normalize_headers(response.headers),
        body=bytes(response.body),
        final_url=final.url,
        tls_verified=response.tls_verified,
    )
    return normalized, final


def _resource_record(response: HostingHttpResponse) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
    return {
        "bytes": len(response.body),
        "cache_control": response.headers.get("cache-control", ""),
        "content_type": content_type,
        "etag": response.headers.get("etag") or None,
        "final_url": response.final_url,
        "sha256": _sha256(response.body),
        "status": response.status,
    }


def _cache_directives(raw: str) -> dict[str, str | None]:
    directives: dict[str, str | None] = {}
    for item in raw.split(","):
        token = item.strip().lower()
        if not token:
            continue
        if "=" in token:
            name, value = token.split("=", 1)
            directives[name.strip()] = value.strip().strip('"')
        else:
            directives[token] = None
    return directives


def _cache_max_age(directives: Mapping[str, str | None]) -> int | None:
    raw = directives.get("s-maxage") or directives.get("max-age")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _feed_cache_ok(raw: str) -> bool:
    directives = _cache_directives(raw)
    if "no-store" in directives or "no-cache" in directives:
        return True
    max_age = _cache_max_age(directives)
    return max_age is not None and 0 <= max_age <= 300


def _release_cache_ok(raw: str) -> bool:
    directives = _cache_directives(raw)
    if "no-store" in directives or "no-cache" in directives:
        return False
    max_age = _cache_max_age(directives)
    return max_age is not None and max_age >= 86_400


def _finish_hosting_report(
    report: UpdateFeedHostingReport,
    *,
    started_monotonic: float,
) -> UpdateFeedHostingReport:
    for check in report.checks:
        if check.status == "pending":
            check.set("skipped", "This check was not reached.")
    report.finished_at = _format_time(_utc_now())
    report.elapsed_ms = max(
        0.0, (time.monotonic() - started_monotonic) * 1_000
    )
    if report.check("no_sensitive_data_recorded").status == "skipped":
        report.check("no_sensitive_data_recorded").set(
            "passed",
            "The report records hashes and public HTTP metadata only; "
            "response bodies and keys are not stored.",
        )
    all_passed = all(check.status == "passed" for check in report.checks)
    report.status = "passed" if all_passed and report.error is None else "failed"
    if report.status != "passed" and report.error is None:
        report.error = "One or more Windows HTTPS hosting checks failed."
    report.write()
    return report


def run_update_feed_hosting_live_verification(
    settings: Settings | None = None,
    report_path: Path | None = None,
    *,
    feed_url: str | None = None,
    public_key: str | bytes | None = None,
    key_id: str | None = None,
    channel: str | None = None,
    profile_id: str = WINDOWS_HTTPS_TASK_PROFILE,
    expected_version: str | None = None,
    timeout_seconds: float | None = None,
    max_bundle_bytes: int | None = None,
    transport: HostingTransport | None = None,
    client_platform: str | None = None,
) -> UpdateFeedHostingReport:
    """Run one explicit live HTTPS hosting verification and retain its report."""

    profile = get_hosting_profile(profile_id)
    _require_implemented(profile)
    destination = _resolve_report_path(settings, report_path)
    raw_feed_url = (
        feed_url
        or (settings.update_feed_url if settings is not None else None)
        or ""
    ).strip()
    raw_public = (
        public_key
        if public_key is not None
        else (settings.release_public_key if settings is not None else None)
    )
    raw_key_id = (
        key_id
        if key_id is not None
        else (settings.release_key_id if settings is not None else None)
    )
    clean_key_id = validate_key_id(raw_key_id or "")
    if raw_public is None:
        raise PcAgentError(
            "A release public key is required for live hosting verification."
        )
    _public_raw, public_text = normalize_release_public_key(raw_public)
    clean_channel = (
        channel
        or (settings.update_channel if settings is not None else "stable")
    ).strip().lower()
    timeout = float(
        timeout_seconds
        if timeout_seconds is not None
        else (
            settings.online_update_timeout_seconds
            if settings is not None
            else 30.0
        )
    )
    if not 0 < timeout <= 300:
        raise PcAgentError("Hosting verification timeout must be 0-300 seconds.")
    max_bundle = int(
        max_bundle_bytes
        if max_bundle_bytes is not None
        else (
            settings.online_update_max_bytes
            if settings is not None
            else _MAX_BUNDLE_BYTES
        )
    )
    if not 1 <= max_bundle <= _MAX_BUNDLE_BYTES:
        raise PcAgentError("Hosting verification bundle limit is invalid.")
    if expected_version is not None:
        parse_version(expected_version)

    started_monotonic = time.monotonic()
    report = UpdateFeedHostingReport(
        report_path=destination,
        started_at=_format_time(_utc_now()),
        profile=profile.profile_id,
        key_id=clean_key_id,
        client_platform=_client_platform(client_platform),
    )

    try:
        feed_validated = _validate_update_url(
            raw_feed_url,
            label="Online update feed URL",
            resolve=False,
        )
    except PcAgentError as exc:
        report.check("https_feed_url").set(
            "failed", "The configured feed URL is not a safe HTTPS URL."
        )
        report.error = _sanitize_error(exc)
        return _finish_hosting_report(
            report, started_monotonic=started_monotonic
        )
    report.tls_scheme = feed_validated.scheme
    report.urls["feed"] = feed_validated.url
    https_ok = feed_validated.scheme == "https"
    report.check("https_feed_url").set(
        "passed" if https_ok else "failed",
        "The live feed URL uses HTTPS."
        if https_ok
        else "Operational hosting evidence requires HTTPS; loopback HTTP is not enough.",
    )
    if not https_ok:
        report.tls_verified = False
        report.error = "The update feed is not hosted over HTTPS."
        return _finish_hosting_report(
            report, started_monotonic=started_monotonic
        )

    try:
        feed_response, final_feed = _fetch_hosting_resource(
            feed_validated.url,
            label="Online update feed",
            max_bytes=MAX_FEED_BYTES,
            timeout_seconds=timeout,
            transport=transport,
        )
    except PcAgentError as exc:
        report.check("feed_fetch").set(
            "failed", "The HTTPS feed could not be fetched successfully."
        )
        report.error = _sanitize_error(exc)
        return _finish_hosting_report(
            report, started_monotonic=started_monotonic
        )
    report.tls_verified = feed_response.tls_verified
    report.urls["feed"] = feed_response.final_url
    report.resources["feed"] = _resource_record(feed_response)
    feed_type_ok = report.resources["feed"]["content_type"] in {
        "application/json",
        "application/octet-stream",
    }
    feed_fetch_ok = feed_response.status == 200 and feed_type_ok
    report.check("feed_fetch").set(
        "passed" if feed_fetch_ok else "failed",
        "The feed returned HTTP 200 with a JSON content type."
        if feed_fetch_ok
        else "The feed did not return a usable HTTP 200 JSON response.",
    )
    if not feed_fetch_ok:
        report.error = "The live update feed fetch failed."
        return _finish_hosting_report(
            report, started_monotonic=started_monotonic
        )

    feed_cache_ok = _feed_cache_ok(
        str(report.resources["feed"]["cache_control"])
    )
    report.check("feed_cache_policy").set(
        "passed" if feed_cache_ok else "failed",
        "The feed uses no-cache/no-store or a cache lifetime of at most 300 seconds."
        if feed_cache_ok
        else "The feed cache policy could retain stale update metadata too long.",
    )

    try:
        manifest = _parse_feed_manifest(
            feed_response.body,
            feed=final_feed,
            expected_channel=clean_channel,
            max_bundle_bytes=max_bundle,
        )
        if expected_version is not None and manifest.version != expected_version:
            raise PcAgentError(
                f"Live feed version is {manifest.version}, expected {expected_version}."
            )
        if manifest.signature_url is None:
            raise PcAgentError("Live feed does not advertise a signature URL.")
    except PcAgentError as exc:
        report.check("feed_manifest").set(
            "failed", "The live feed is not a valid signed-update manifest."
        )
        report.error = _sanitize_error(exc)
        return _finish_hosting_report(
            report, started_monotonic=started_monotonic
        )
    report.feed_manifest = manifest.to_dict()
    report.urls["bundle"] = manifest.bundle_url
    report.urls["signature"] = manifest.signature_url
    report.check("feed_manifest").set(
        "passed",
        "The strict Phase 24 feed parser accepted the live manifest.",
    )

    origin = _origin(final_feed)
    try:
        bundle_validated = _validate_update_url(
            manifest.bundle_url,
            label="Online update bundle URL",
            expected_origin=origin,
            resolve=False,
        )
        signature_validated = _validate_update_url(
            str(manifest.signature_url),
            label="Online update signature URL",
            expected_origin=origin,
            resolve=False,
        )
        same_origin_ok = (
            _origin(bundle_validated) == origin
            and _origin(signature_validated) == origin
        )
    except PcAgentError:
        same_origin_ok = False
    report.check("same_origin_resources").set(
        "passed" if same_origin_ok else "failed",
        "Feed, bundle and signature remain on one HTTPS origin."
        if same_origin_ok
        else "A bundle or signature URL left the feed origin.",
    )
    if not same_origin_ok:
        report.error = "The live update resources are not same-origin."
        return _finish_hosting_report(
            report, started_monotonic=started_monotonic
        )

    try:
        signature_response, _final_signature = _fetch_hosting_resource(
            str(manifest.signature_url),
            label="Online update signature",
            max_bytes=MAX_SIGNATURE_DOWNLOAD_BYTES,
            timeout_seconds=timeout,
            expected_origin=origin,
            transport=transport,
        )
    except PcAgentError as exc:
        report.check("signature_fetch").set(
            "failed", "The detached bundle signature could not be fetched."
        )
        report.error = _sanitize_error(exc)
        return _finish_hosting_report(
            report, started_monotonic=started_monotonic
        )
    report.urls["signature"] = signature_response.final_url
    report.resources["signature"] = _resource_record(signature_response)
    signature_ok = signature_response.status == 200 and report.resources[
        "signature"
    ]["content_type"] in {"application/json", "application/octet-stream"}
    report.check("signature_fetch").set(
        "passed" if signature_ok else "failed",
        "The signature returned HTTP 200 with a JSON content type."
        if signature_ok
        else "The signature did not return a usable HTTP 200 JSON response.",
    )
    if not signature_ok:
        report.error = "The live update signature fetch failed."
        return _finish_hosting_report(
            report, started_monotonic=started_monotonic
        )

    try:
        bundle_response, _final_bundle = _fetch_hosting_resource(
            manifest.bundle_url,
            label="Online update bundle",
            max_bytes=max_bundle,
            timeout_seconds=timeout,
            expected_origin=origin,
            transport=transport,
        )
    except PcAgentError as exc:
        report.check("bundle_fetch").set(
            "failed", "The portable release bundle could not be fetched."
        )
        report.error = _sanitize_error(exc)
        return _finish_hosting_report(
            report, started_monotonic=started_monotonic
        )
    report.urls["bundle"] = bundle_response.final_url
    report.resources["bundle"] = _resource_record(bundle_response)
    bundle_ok = bundle_response.status == 200 and report.resources["bundle"][
        "content_type"
    ] in {"application/zip", "application/octet-stream"}
    report.check("bundle_fetch").set(
        "passed" if bundle_ok else "failed",
        "The bundle returned HTTP 200 with a ZIP content type."
        if bundle_ok
        else "The bundle did not return a usable HTTP 200 ZIP response.",
    )
    if not bundle_ok:
        report.error = "The live update bundle fetch failed."
        return _finish_hosting_report(
            report, started_monotonic=started_monotonic
        )

    size_hash_ok = (
        len(bundle_response.body) == manifest.size_bytes
        and _sha256(bundle_response.body) == manifest.bundle_sha256
    )
    report.check("bundle_size_and_hash").set(
        "passed" if size_hash_ok else "failed",
        "Bundle size and SHA-256 match the feed exactly."
        if size_hash_ok
        else "Bundle size or SHA-256 does not match the feed.",
    )

    portable_ok = False
    signature_verified = False
    if size_hash_ok:
        try:
            with tempfile.TemporaryDirectory(
                prefix="pc-agent-hosting-check-"
            ) as temporary:
                root = Path(temporary)
                bundle_path = root / f"release-{manifest.version}.zip"
                signature_path = root / f"release-{manifest.version}.zip.sig.json"
                bundle_path.write_bytes(bundle_response.body)
                signature_path.write_bytes(signature_response.body)
                bundle = verify_release_bundle(bundle_path)
                portable_ok = (
                    bundle.version == manifest.version
                    and bundle.bundle_sha256 == manifest.bundle_sha256
                )
                if portable_ok:
                    signature = inspect_release_signature(
                        bundle,
                        SimpleNamespace(  # type: ignore[arg-type]
                            release_public_key=public_text,
                            release_key_id=clean_key_id,
                        ),
                        signature_path,
                    )
                    signature_verified = signature.verified
        except (OSError, PcAgentError):
            portable_ok = False
            signature_verified = False
    report.check("bundle_portable_verification").set(
        "passed" if portable_ok else "failed",
        "The downloaded ZIP passes portable release verification."
        if portable_ok
        else "The downloaded ZIP failed portable release verification.",
    )
    report.check("bundle_signature_verified").set(
        "passed" if signature_verified else "failed",
        "The detached Ed25519 signature verifies for the exact bundle."
        if signature_verified
        else "The detached Ed25519 signature did not verify.",
    )

    release_cache_ok = _release_cache_ok(
        str(report.resources["bundle"]["cache_control"])
    )
    report.check("release_cache_policy").set(
        "passed" if release_cache_ok else "failed",
        "Versioned release files use a long-lived cache policy."
        if release_cache_ok
        else "Versioned release files do not use a long-lived cache policy.",
    )
    report.check("no_sensitive_data_recorded").set(
        "passed",
        "The report records hashes and public HTTP metadata only; "
        "response bodies and keys are not stored.",
    )
    return _finish_hosting_report(
        report, started_monotonic=started_monotonic
    )


def _parse_report_time(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise PcAgentError(f"Windows HTTPS hosting report {label} is invalid.")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise PcAgentError(
            f"Windows HTTPS hosting report {label} is invalid."
        ) from exc


def _validate_report_resource(
    value: Any,
    *,
    label: str,
    expected_types: set[str],
) -> dict[str, Any]:
    expected = {
        "bytes",
        "cache_control",
        "content_type",
        "etag",
        "final_url",
        "sha256",
        "status",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise PcAgentError(f"Windows HTTPS hosting report {label} is malformed.")
    if (
        value["status"] != 200
        or isinstance(value["bytes"], bool)
        or not isinstance(value["bytes"], int)
        or not 1 <= value["bytes"] <= _MAX_BUNDLE_BYTES
        or value["content_type"] not in expected_types
        or not isinstance(value["cache_control"], str)
        or len(value["cache_control"]) > 500
        or not isinstance(value["sha256"], str)
        or _SHA256_RE.fullmatch(value["sha256"]) is None
        or not isinstance(value["final_url"], str)
    ):
        raise PcAgentError(f"Windows HTTPS hosting report {label} is invalid.")
    etag = value["etag"]
    if etag is not None and (
        not isinstance(etag, str)
        or len(etag) > 128
        or any(ord(char) < 32 or ord(char) == 127 for char in etag)
    ):
        raise PcAgentError(f"Windows HTTPS hosting report {label} ETag is invalid.")
    return value


def verify_update_feed_hosting_report(
    report_path: Path,
) -> UpdateFeedHostingReportVerification:
    """Strictly verify one passed live Windows HTTPS hosting report offline."""

    path = report_path.expanduser()
    if path.suffix.lower() != ".json":
        raise PcAgentError("Windows HTTPS hosting report must end with .json.")
    data = _read_bounded(
        path,
        limit=_MAX_REPORT_BYTES,
        label="Windows HTTPS hosting report",
    )
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PcAgentError("Windows HTTPS hosting report is not valid JSON.") from exc
    expected_keys = {
        "app",
        "checks",
        "client_platform",
        "elapsed_ms",
        "error",
        "feed",
        "finished_at",
        "format",
        "hosting",
        "key_id",
        "report_sha256",
        "resources",
        "started_at",
        "status",
        "tls",
        "urls",
        "version",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise PcAgentError("Windows HTTPS hosting report has an unexpected shape.")
    recorded_hash = payload["report_sha256"]
    if not isinstance(recorded_hash, str) or _SHA256_RE.fullmatch(recorded_hash) is None:
        raise PcAgentError("Windows HTTPS hosting report hash is invalid.")
    unsigned = dict(payload)
    unsigned.pop("report_sha256")
    if _sha256(_canonical_json_bytes(unsigned)) != recorded_hash:
        raise PcAgentError("Windows HTTPS hosting report hash does not match.")
    if (
        payload["app"] != "Self-Healing PC Agent"
        or payload["format"] != UPDATE_FEED_HOSTING_FORMAT
        or payload["status"] != "passed"
        or payload["error"] is not None
        or payload["version"] != __version__
    ):
        raise PcAgentError(
            "Windows HTTPS hosting report does not record a passed current run."
        )
    if payload["client_platform"] not in {"windows", "linux", "macos", "unknown"}:
        raise PcAgentError("Windows HTTPS hosting report client platform is invalid.")
    clean_key_id = validate_key_id(str(payload["key_id"]))
    if payload["key_id"] != clean_key_id:
        raise PcAgentError("Windows HTTPS hosting report key ID is invalid.")

    hosting = payload["hosting"]
    if (
        not isinstance(hosting, dict)
        or set(hosting) != {"platform", "profile", "profile_proof"}
        or hosting["platform"] != "windows"
        or hosting["profile_proof"] != "operator-declared"
    ):
        raise PcAgentError("Windows HTTPS hosting report hosting data is invalid.")
    profile = get_hosting_profile(str(hosting["profile"]))
    _require_implemented(profile)
    if profile.platform != "windows":
        raise PcAgentError("Windows HTTPS hosting report profile is not Windows.")

    checks = payload["checks"]
    if not isinstance(checks, list) or len(checks) != len(_CHECK_NAMES):
        raise PcAgentError("Windows HTTPS hosting report checks are invalid.")
    for expected_name, check in zip(_CHECK_NAMES, checks, strict=True):
        if (
            not isinstance(check, dict)
            or set(check) != {"detail", "name", "status"}
            or check["name"] != expected_name
            or check["status"] != "passed"
            or not isinstance(check["detail"], str)
            or not check["detail"]
            or len(check["detail"]) > 300
        ):
            raise PcAgentError("Windows HTTPS hosting report checks are invalid.")

    started = _parse_report_time(payload["started_at"], label="start time")
    finished = _parse_report_time(payload["finished_at"], label="finish time")
    if finished < started:
        raise PcAgentError("Windows HTTPS hosting report times are inconsistent.")
    elapsed = payload["elapsed_ms"]
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or elapsed < 0:
        raise PcAgentError("Windows HTTPS hosting report elapsed time is invalid.")

    if payload["tls"] != {"certificate_verified": True, "scheme": "https"}:
        raise PcAgentError("Windows HTTPS hosting report TLS evidence is invalid.")

    urls = payload["urls"]
    if not isinstance(urls, dict) or set(urls) != {"bundle", "feed", "signature"}:
        raise PcAgentError("Windows HTTPS hosting report URLs are invalid.")
    validated: dict[str, Any] = {}
    for name in ("feed", "bundle", "signature"):
        validated[name] = _validate_update_url(
            str(urls[name]),
            label=f"Recorded {name} URL",
            resolve=False,
        )
        if validated[name].scheme != "https" or urls[name] != validated[name].url:
            raise PcAgentError("Windows HTTPS hosting report URLs are not canonical HTTPS.")
    origin = _origin(validated["feed"])
    if _origin(validated["bundle"]) != origin or _origin(validated["signature"]) != origin:
        raise PcAgentError("Windows HTTPS hosting report URLs are not same-origin.")
    if not urllib.parse.urlsplit(validated["bundle"].url).path.lower().endswith(".zip"):
        raise PcAgentError("Windows HTTPS hosting report bundle URL is invalid.")
    if not urllib.parse.urlsplit(validated["signature"].url).path.lower().endswith(
        (".sig.json", ".json")
    ):
        raise PcAgentError("Windows HTTPS hosting report signature URL is invalid.")

    resources = payload["resources"]
    if not isinstance(resources, dict) or set(resources) != {
        "bundle",
        "feed",
        "signature",
    }:
        raise PcAgentError("Windows HTTPS hosting report resources are invalid.")
    feed_resource = _validate_report_resource(
        resources["feed"],
        label="feed resource",
        expected_types={"application/json", "application/octet-stream"},
    )
    signature_resource = _validate_report_resource(
        resources["signature"],
        label="signature resource",
        expected_types={"application/json", "application/octet-stream"},
    )
    bundle_resource = _validate_report_resource(
        resources["bundle"],
        label="bundle resource",
        expected_types={"application/zip", "application/octet-stream"},
    )
    for name, resource in (
        ("feed", feed_resource),
        ("signature", signature_resource),
        ("bundle", bundle_resource),
    ):
        if resource["final_url"] != urls[name]:
            raise PcAgentError("Windows HTTPS hosting report final URLs are inconsistent.")
    if not _feed_cache_ok(str(feed_resource["cache_control"])):
        raise PcAgentError("Windows HTTPS hosting report feed cache policy is invalid.")
    if not _release_cache_ok(str(bundle_resource["cache_control"])):
        raise PcAgentError("Windows HTTPS hosting report release cache policy is invalid.")

    feed = payload["feed"]
    expected_feed_keys = {
        "app",
        "bundle_sha256",
        "bundle_url",
        "channel",
        "format",
        "published_at",
        "signature_url",
        "size_bytes",
        "version",
    }
    if not isinstance(feed, dict) or set(feed) != expected_feed_keys:
        raise PcAgentError("Windows HTTPS hosting report feed data is invalid.")
    if (
        feed["app"] != "self-healing-pc-agent"
        or feed["format"] != 1
        or feed["bundle_url"] != urls["bundle"]
        or feed["signature_url"] != urls["signature"]
        or feed["bundle_sha256"] != bundle_resource["sha256"]
        or feed["size_bytes"] != bundle_resource["bytes"]
    ):
        raise PcAgentError("Windows HTTPS hosting report feed data is inconsistent.")
    parse_version(str(feed["version"]))
    if feed["published_at"] is not None and not isinstance(feed["published_at"], str):
        raise PcAgentError("Windows HTTPS hosting report publication time is invalid.")

    return UpdateFeedHostingReportVerification(
        report_path=path.resolve(),
        version=str(payload["version"]),
        status="passed",
        profile=profile.profile_id,
        feed_url=str(urls["feed"]),
        feed_version=str(feed["version"]),
        bundle_sha256=str(feed["bundle_sha256"]),
        report_sha256=recorded_hash,
    )


__all__ = [
    "HOSTING_PROFILES",
    "UPDATE_FEED_HOSTING_FORMAT",
    "UPDATE_FEED_HOSTING_MANIFEST",
    "UPDATE_FEED_HOSTING_REPORT",
    "WINDOWS_HTTPS_TASK_PROFILE",
    "WINDOWS_IIS_PROFILE",
    "HostingHttpResponse",
    "HostingProfile",
    "HostingTransport",
    "UpdateFeedHostingPackageInfo",
    "UpdateFeedHostingReport",
    "UpdateFeedHostingReportVerification",
    "build_update_feed_hosting_package",
    "create_update_feed_hosting_server",
    "get_hosting_profile",
    "list_hosting_profiles",
    "normalize_release_public_key",
    "read_release_public_key_file",
    "run_update_feed_hosting_live_verification",
    "serve_update_feed_hosting_package",
    "verify_update_feed_hosting_package",
    "verify_update_feed_hosting_report",
]
