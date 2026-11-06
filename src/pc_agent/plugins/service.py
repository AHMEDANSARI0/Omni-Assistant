"""PluginService: the only gate keeper for plugin install and execution.

Flow for an install (every step is a refusal point, not a warning):

1. master gate ``PC_AGENT_ALLOW_PLUGINS``;
2. bundle size, file count, per-file size, path shape (no traversal, no
   absolute paths, no symlink entries);
3. ``manifest.json`` strict validation (known fields, slug ids, semver, bounded
   text, declared tools/skills/files, declared capabilities);
4. content checksum: every file's SHA-256 must match the manifest's file map;
5. Ed25519 signature over the exact manifest bytes against a trusted publisher
   key (unsigned bundles are refused unless the user explicitly allows them);
6. capabilities that this build cannot grant (``network``, ``subprocess``) are
   refused at install time rather than silently ignored;
7. extraction into ``data/plugins/installed/<id>/<version>/`` only.

Execution is a second, independent gate: the plugin must be *enabled*, the tool
must be *declared*, the caller must pass the ``confirm`` token, and the call
runs in a separate process with OS limits and a hard timeout. Skills are text
only and never executed.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..errors import PcAgentError, PluginError
from ..storage.db import Database
from .manifest import (
    CAPABILITIES,
    DENIED_CAPABILITIES,
    ENTRY_NAME,
    MANIFEST_NAME,
    SIGNATURE_NAME,
    PluginManifest,
    bundle_checksum,
    parse_public_key,
    safe_relative_path,
    sha256_hex,
    validate_manifest,
    verify_signature,
)
from .sandbox import SandboxLimits, describe_sandbox, run_sandboxed
from .store import PluginStore

__all__ = ["PluginService"]

_LOG = logging.getLogger(__name__)

MAX_MANIFEST_BYTES = 256_000
MAX_SKILL_CHARS = 20_000
WORKER = Path(__file__).with_name("worker.py")


def _stamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _decode(value: Any, fallback: Any) -> Any:
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        return fallback
    return parsed if isinstance(parsed, type(fallback)) else fallback


class PluginService:
    """Install, inspect, run and remove signed plugins."""

    def __init__(self, settings: Any, database: Database, *, events: Any | None = None) -> None:
        self._settings = settings
        self._database = database
        self._events = events
        self._store = PluginStore(database)

    # -- gates and paths ----------------------------------------------------
    def store(self) -> PluginStore:
        return self._store

    @property
    def allowed(self) -> bool:
        return bool(getattr(self._settings, "allow_plugins", False))

    @property
    def signature_required(self) -> bool:
        return not bool(getattr(self._settings, "plugin_allow_unsigned", False))

    def _require(self) -> None:
        if not self.allowed:
            raise PluginError(
                "plugins are disabled; set PC_AGENT_ALLOW_PLUGINS=1 to use the plugin "
                "ecosystem"
            )

    def _root(self) -> Path:
        root = Path(str(getattr(self._settings, "data_dir", "."))) / "plugins"
        return root

    def _installed_root(self) -> Path:
        return self._root() / "installed"

    def _staging_root(self) -> Path:
        return self._root() / "staging"

    def _run_root(self) -> Path:
        return self._root() / "run"

    def trusted_keys(self) -> dict[str, bytes]:
        raw = str(getattr(self._settings, "plugin_trusted_keys", "") or "").strip()
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise PluginError("PC_AGENT_PLUGIN_TRUSTED_KEYS must be a JSON object.") from exc
        if not isinstance(payload, dict):
            raise PluginError("PC_AGENT_PLUGIN_TRUSTED_KEYS must be a JSON object.")
        keys: dict[str, bytes] = {}
        for key_id, value in payload.items():
            name = str(key_id or "").strip()
            if not name or len(name) > 120:
                raise PluginError("Trusted plugin key ids must be 1-120 characters.")
            keys[name] = parse_public_key(value)
        return keys

    def limits(self) -> SandboxLimits:
        settings = self._settings
        return SandboxLimits(
            timeout_seconds=float(getattr(settings, "plugin_timeout_seconds", 30.0)),
            memory_limit_mb=int(getattr(settings, "plugin_memory_limit_mb", 512)),
            cpu_seconds=int(getattr(settings, "plugin_cpu_limit_seconds", 30)),
            max_processes=1,
            allow_network=False,
            allow_subprocess=False,
        )

    # -- status -------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        counts = self._store.count()
        keys = []
        try:
            keys = sorted(self.trusted_keys())
        except PcAgentError:
            keys = []
        return {
            "enabled": self.allowed,
            "gate": "PC_AGENT_ALLOW_PLUGINS",
            "signature_required": self.signature_required,
            "unsigned_allowed": not self.signature_required,
            "trusted_keys": keys,
            "capabilities": list(CAPABILITIES),
            "denied_capabilities": list(DENIED_CAPABILITIES),
            "sandbox": describe_sandbox(),
            "limits": self.limits().to_dict(),
            "counts": counts,
            "plugins": [self._entry(row) for row in self._store.list_plugins()],
            "detail": (
                "plugins are disabled; set PC_AGENT_ALLOW_PLUGINS=1 and add a trusted "
                "publisher key to PC_AGENT_PLUGIN_TRUSTED_KEYS"
                if not self.allowed
                else "plugin ecosystem is enabled; every tool call runs sandboxed"
            ),
        }

    def summary(self) -> dict[str, Any]:
        counts = self._store.count()
        return {
            "enabled": self.allowed,
            "installed": counts["plugins"],
            "enabled_plugins": counts["enabled"],
            "runs": counts["runs"],
            "signatures": "required" if self.signature_required else "optional",
            "sandbox": describe_sandbox()["mechanism"],
        }

    def _entry(self, row: dict[str, Any]) -> dict[str, Any]:
        manifest = {
            "id": row["id"],
            "name": row["name"],
            "publisher": row["publisher"],
            "description": row["description"],
            "capabilities": _decode(row["capabilities_json"], []),
            "tools": _decode(row["tools_json"], []),
            "skills": _decode(row["skills_json"], []),
        }
        return {
            **manifest,
            "version": row["version"],
            "status": row["status"],
            "checksum": row["checksum"],
            "signature_key": row["signature_key"],
            "signature_status": row["signature_status"],
            "file_count": int(row["file_count"]),
            "run_count": int(row["run_count"]),
            "installed_at": row["installed_at"],
            "updated_at": row["updated_at"],
            "last_run_at": row["last_run_at"],
            "install_dir": row["install_dir"],
        }

    def list_plugins(self) -> dict[str, Any]:
        rows = self._store.list_plugins()
        return {
            "plugins": [self._entry(row) for row in rows],
            "total": len(rows),
            "enabled": self.allowed,
        }

    def show(self, plugin_id: str) -> dict[str, Any]:
        row = self._store.get_plugin(str(plugin_id))
        if row is None:
            raise PluginError(f"Plugin '{plugin_id}' is not installed.")
        entry = self._entry(row)
        entry["versions"] = [
            {
                "version": item["version"],
                "installed_at": item["installed_at"],
                "checksum": item["checksum"],
                "signature_key": item["signature_key"],
            }
            for item in self._store.versions(row["id"])
        ]
        entry["sandbox"] = describe_sandbox()
        return entry

    # -- install ------------------------------------------------------------
    def verify_bundle(self, bundle: str | Path) -> dict[str, Any]:
        """Validate a bundle without installing anything."""

        path = Path(str(bundle)).expanduser()
        if not path.is_file():
            raise PluginError(f"Plugin bundle '{path.name}' does not exist.")
        manifest, files, signature = self._read_bundle(path)
        return {
            "bundle": path.name,
            "manifest": manifest.to_dict(),
            "file_count": len(files),
            "checksum": manifest.checksum,
            "signature_key": signature["key_id"],
            "signature_status": "verified" if signature["verified"] else "unsigned",
            "installable": True,
        }

    def install(self, bundle: str | Path) -> dict[str, Any]:
        self._require()
        path = Path(str(bundle)).expanduser()
        if not path.is_file():
            raise PluginError(f"Plugin bundle '{path.name}' does not exist.")
        manifest, files, signature = self._read_bundle(path)
        if manifest.denied_capabilities:
            raise PluginError(
                "Plugin requests capability "
                f"'{manifest.denied_capabilities[0]}', which this build cannot grant "
                f"(not grantable: {', '.join(DENIED_CAPABILITIES)})."
            )

        target = self._installed_root() / manifest.id / manifest.version
        staging = self._staging_root() / uuid.uuid4().hex
        staging.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(path) as archive:
                for name in files:
                    destination = staging / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(name) as source, destination.open("wb") as sink:
                        shutil.copyfileobj(source, sink, length=64 * 1024)
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staging), str(target))
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise PluginError(f"Plugin files could not be written: {exc.strerror or exc}") from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        row = self._store.upsert_plugin(
            manifest=manifest.raw,
            version=manifest.version,
            install_dir=str(target),
            checksum=manifest.checksum,
            signature_key=signature["key_id"],
            signature_status=signature["status"],
            file_count=len(files),
        )
        self._store.add_version(
            plugin_id=manifest.id,
            version=manifest.version,
            manifest=manifest.raw,
            install_dir=str(target),
            checksum=manifest.checksum,
            signature_key=signature["key_id"],
        )
        self._emit(
            "plugin.installed",
            summary=f"Plugin '{manifest.name}' {manifest.version} installed.",
            payload={
                "plugin": manifest.id,
                "version": manifest.version,
                "signature": signature["status"],
                "capabilities": list(manifest.capabilities),
            },
        )
        entry = self._entry(row) if row else self.show(manifest.id)
        entry["install_dir"] = str(target)
        return entry

    def _read_bundle(
        self, path: Path
    ) -> tuple[PluginManifest, dict[str, str], dict[str, Any]]:
        """Validate the archive, manifest, checksum and signature."""

        settings = self._settings
        max_bundle = int(getattr(settings, "plugin_max_bundle_bytes", 8_000_000))
        max_files = int(getattr(settings, "plugin_max_files", 200))
        max_file = int(getattr(settings, "plugin_max_file_bytes", 2_000_000))

        size = path.stat().st_size
        if size > max_bundle:
            raise PluginError(
                f"Plugin bundle is {size} bytes, larger than the "
                f"PC_AGENT_PLUGIN_MAX_BUNDLE_BYTES limit of {max_bundle}."
            )
        try:
            archive = zipfile.ZipFile(path)
        except zipfile.BadZipFile as exc:
            raise PluginError("Plugin bundle must be a .zip archive.") from exc

        with archive:
            infos = archive.infolist()
            if len(infos) > max_files:
                raise PluginError(
                    f"Plugin bundle has {len(infos)} entries, more than the limit of {max_files}."
                )
            names: list[str] = []
            for info in infos:
                if info.is_dir():
                    continue
                name = safe_relative_path(info.filename, field_name="file")
                if info.file_size > max_file:
                    raise PluginError(
                        f"Plugin file '{name}' is larger than the {max_file}-byte limit."
                    )
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise PluginError(f"Plugin file '{name}' is a symlink, which is refused.")
                names.append(name)
            if MANIFEST_NAME not in names:
                raise PluginError("Plugin bundle is missing manifest.json.")
            if not any(name.endswith(".py") for name in names):
                raise PluginError("Plugin bundle contains no Python entry file.")

            raw_manifest = archive.read(MANIFEST_NAME)
            if len(raw_manifest) > MAX_MANIFEST_BYTES:
                raise PluginError("Plugin manifest is larger than 256 KB.")
            try:
                payload = json.loads(raw_manifest.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise PluginError("Plugin manifest must be UTF-8 JSON.") from exc
            manifest = validate_manifest(payload)

            content = sorted(name for name in names if name not in {MANIFEST_NAME, SIGNATURE_NAME})
            if manifest.entry not in content:
                raise PluginError(f"Plugin entry '{manifest.entry}' is missing from the bundle.")
            for skill in manifest.skills:
                if skill not in content:
                    raise PluginError(f"Plugin skill '{skill}' is missing from the bundle.")
            if manifest.files:
                declared = set(manifest.files)
                if declared != set(content):
                    missing = sorted(set(content) - declared)
                    extra = sorted(declared - set(content))
                    raise PluginError(
                        "Plugin manifest file list does not match the bundle"
                        + (f"; undeclared: {', '.join(missing)}" if missing else "")
                        + (f"; missing: {', '.join(extra)}" if extra else "")
                        + "."
                    )

            digests = {name: sha256_hex(archive.read(name)) for name in content}
            computed = bundle_checksum(digests)
            if manifest.checksum and manifest.checksum != computed:
                raise PluginError(
                    "Plugin content checksum does not match the manifest; the bundle was "
                    "modified after it was built."
                )

            signature_info = self._verify_bundle_signature(
                archive, raw_manifest, payload, computed
            )
        return manifest, digests, signature_info

    def _verify_bundle_signature(
        self,
        archive: zipfile.ZipFile,
        raw_manifest: bytes,
        payload: dict[str, Any],
        computed_checksum: str,
    ) -> dict[str, Any]:
        names = {info.filename for info in archive.infolist() if not info.is_dir()}
        has_signature = SIGNATURE_NAME in names
        if not has_signature:
            if self.signature_required:
                raise PluginError(
                    "Plugin bundle is unsigned and PC_AGENT_PLUGIN_ALLOW_UNSIGNED is not set; "
                    "install a signed bundle or opt in to unsigned plugins explicitly."
                )
            return {"key_id": "", "status": "unsigned", "verified": False}

        raw_signature = archive.read(SIGNATURE_NAME)
        if len(raw_signature) > 8_192:
            raise PluginError("Plugin signature file is larger than 8 KB.")
        try:
            signature = json.loads(raw_signature.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise PluginError("Plugin signature file must be UTF-8 JSON.") from exc
        if not isinstance(signature, dict):
            raise PluginError("Plugin signature file must be a JSON object.")
        key_id = str(signature.get("key_id") or "").strip()
        keys = self.trusted_keys()
        if not keys:
            raise PluginError(
                "No trusted plugin publisher key is configured; set "
                "PC_AGENT_PLUGIN_TRUSTED_KEYS to a JSON object of key id to public key."
            )
        if key_id not in keys:
            raise PluginError(
                f"Plugin was signed by key '{key_id or '(missing)'}', which is not in the "
                "trusted key set."
            )
        # The signature covers the exact manifest bytes; the manifest names the
        # content checksum, so verifying both binds code to publisher.
        declared_checksum = str(signature.get("checksum") or "").strip().lower()
        if declared_checksum and declared_checksum != computed_checksum:
            raise PluginError("Plugin signature names a different content checksum.")
        verify_signature(raw_manifest, signature, keys[key_id], key_id=key_id)
        if not str(payload.get("checksum") or "").strip():
            raise PluginError(
                "A signed plugin manifest must include the content checksum it signs."
            )
        return {"key_id": key_id, "status": "verified", "verified": True}

    # -- lifecycle ----------------------------------------------------------
    def _row(self, plugin_id: str) -> dict[str, Any]:
        row = self._store.get_plugin(str(plugin_id))
        if row is None:
            raise PluginError(f"Plugin '{plugin_id}' is not installed.")
        return row

    def set_enabled(self, plugin_id: str, *, enabled: bool) -> dict[str, Any]:
        self._require()
        row = self._row(plugin_id)
        status = "enabled" if enabled else "disabled"
        self._store.set_status(row["id"], status)
        self._emit(
            "plugin.enabled" if enabled else "plugin.disabled",
            summary=f"Plugin '{row['name']}' {status}.",
            payload={"plugin": row["id"], "status": status},
        )
        return self.show(row["id"])

    def uninstall(self, plugin_id: str) -> dict[str, Any]:
        self._require()
        row = self._row(plugin_id)
        for version in self._store.versions(row["id"]):
            shutil.rmtree(Path(version["install_dir"]), ignore_errors=True)
        shutil.rmtree(self._installed_root() / row["id"], ignore_errors=True)
        self._store.delete_plugin(row["id"])
        self._emit(
            "plugin.uninstalled",
            summary=f"Plugin '{row['name']}' uninstalled.",
            payload={"plugin": row["id"], "version": row["version"]},
        )
        return {"deleted": row["id"], "name": row["name"], "total": self._store.count()["plugins"]}

    def rollback(self, plugin_id: str, *, version: str | None = None) -> dict[str, Any]:
        self._require()
        row = self._row(plugin_id)
        versions = self._store.versions(row["id"])
        candidates = [item for item in versions if item["version"] != row["version"]]
        if version:
            candidates = [item for item in candidates if item["version"] == version]
        if not candidates:
            raise PluginError(
                f"No earlier version of '{row['name']}' is available to roll back to."
            )
        target = candidates[0]
        install_dir = Path(target["install_dir"])
        if not install_dir.is_dir():
            raise PluginError(
                f"Rollback target for '{row['name']}' {target['version']} is missing from disk."
            )
        self._store.set_active_version(
            row["id"], target["version"], str(install_dir), target["checksum"]
        )
        self._emit(
            "plugin.rolled_back",
            summary=f"Plugin '{row['name']}' rolled back to {target['version']}.",
            payload={"plugin": row["id"], "version": target["version"]},
        )
        return self.show(row["id"])

    # -- skills -------------------------------------------------------------
    def skills(self, plugin_id: str) -> dict[str, Any]:
        self._require()
        row = self._row(plugin_id)
        install_dir = Path(row["install_dir"])
        skills = []
        for relative in _decode(row["skills_json"], []):
            path = (install_dir / str(relative)).resolve()
            if install_dir.resolve() not in path.parents and path != install_dir.resolve():
                raise PluginError(
                    f"Skill path '{relative}' escapes the plugin directory and was refused."
                )
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise PluginError(f"Skill '{relative}' could not be read.") from exc
            skills.append(
                {
                    "path": str(relative),
                    "chars": min(len(text), MAX_SKILL_CHARS),
                    "text": text[:MAX_SKILL_CHARS],
                }
            )
        return {"plugin": row["id"], "skills": skills, "total": len(skills)}

    # -- execution ----------------------------------------------------------
    def run_tool(
        self,
        plugin_id: str,
        tool: str,
        *,
        values: dict[str, Any] | None = None,
        confirm: str | None = None,
    ) -> dict[str, Any]:
        self._require()
        row = self._row(plugin_id)
        if row["status"] != "enabled":
            raise PluginError(
                f"Plugin '{row['name']}' is {row['status']}; enable it before running tools."
            )
        tools = {item.get("name"): item for item in _decode(row["tools_json"], [])}
        declared = tools.get(str(tool))
        if declared is None:
            raise PluginError(
                f"Plugin '{row['id']}' does not declare a tool named '{tool}'."
            )
        if str(confirm or "") != row["id"]:
            raise PluginError(
                "Running a plugin tool needs explicit confirmation: pass "
                f'confirm="{row["id"]}" (the plugin id) to accept the risk.'
            )
        install_dir = Path(row["install_dir"])
        entry = install_dir / ENTRY_NAME
        for item in install_dir.rglob("*.py"):
            if item.name == ENTRY_NAME:
                entry = item
                break
        if not entry.is_file():
            raise PluginError(f"Plugin '{row['id']}' entry file is missing from disk.")

        run_dir = self._run_root() / uuid.uuid4().hex
        run_dir.mkdir(parents=True, exist_ok=True)
        capabilities = [str(item) for item in _decode(row["capabilities_json"], [])]
        request = {
            "plugin_id": row["id"],
            "plugin_dir": str(install_dir),
            "entry": str(entry),
            "tool": str(tool),
            "arguments": dict(declared.get("arguments") or {}),
            "values": dict(values or {}),
            "capabilities": capabilities,
            "workspace": str(getattr(self._settings, "workspace_dir", run_dir)),
            "sandbox_dir": str(run_dir),
            "allow_network": False,
            "allow_subprocess": False,
        }
        limits = self.limits()
        try:
            outcome = run_sandboxed(
                [sys.executable, "-I", "-B", str(WORKER)],
                cwd=run_dir,
                payload=json.dumps(request).encode("utf-8"),
                limits=limits,
            )
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)

        status = "ok"
        detail = ""
        result: Any = None
        if outcome.spawn_error:
            status = "error"
            detail = outcome.spawn_error
        elif outcome.timed_out:
            status = "timeout"
            detail = f"Plugin exceeded the {limits.timeout_seconds:g}s sandbox timeout."
        else:
            try:
                parsed = json.loads(outcome.stdout or "{}")
            except ValueError:
                parsed = {}
            if not isinstance(parsed, dict):
                parsed = {}
            if parsed.get("ok") is True:
                result = parsed.get("result")
            else:
                status = "error"
                detail = str(
                    parsed.get("error")
                    or outcome.stderr.strip()[-500:]
                    or f"plugin worker exited with code {outcome.returncode}"
                )
        record = self._store.record_run(
            plugin_id=row["id"],
            tool=str(tool),
            status=status,
            detail=detail,
            duration_ms=outcome.duration_ms,
            output_bytes=len(outcome.stdout.encode("utf-8")),
            sandbox=outcome.mechanism,
        )
        self._emit(
            "plugin.tool_run",
            summary=f"Plugin '{row['name']}' tool '{tool}' {status}.",
            payload={
                "plugin": row["id"],
                "tool": str(tool),
                "status": status,
                "duration_ms": outcome.duration_ms,
            },
        )
        return {
            "plugin": row["id"],
            "tool": str(tool),
            "status": status,
            "detail": detail,
            "result": result,
            "duration_ms": outcome.duration_ms,
            "sandbox": {
                "mechanism": outcome.mechanism,
                "limits": limits.to_dict(),
                "truncated": outcome.truncated,
                "spawn_error": outcome.spawn_error,
                "interpreter": outcome.interpreter,
            },
            "ledger": record,
        }

    def runs(self, *, plugin_id: str | None = None, limit: int = 25) -> dict[str, Any]:
        rows = self._store.runs(plugin_id=plugin_id, limit=int(limit))
        return {"runs": rows, "total": len(rows)}

    # -- events -------------------------------------------------------------
    def _emit(self, event_type: str, *, summary: str, payload: dict[str, Any]) -> None:
        if self._events is None:
            return
        try:
            self._events.record(event_type, source="plugins", summary=summary, payload=payload)
        except PcAgentError:
            _LOG.exception("Failed to record plugin event %s", event_type)
