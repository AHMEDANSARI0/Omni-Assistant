"""Phase 54: live GNOME Keyring/KWallet Secret Service evidence."""

from __future__ import annotations

import json
import subprocess
import tomllib
from collections.abc import Sequence
from pathlib import Path

import pytest

from pc_agent import __version__
from pc_agent import cli as cli_module
from pc_agent.cli import build_parser, main
from pc_agent.config import Settings
from pc_agent.credentials import LinuxSecretServiceBackend
from pc_agent.errors import PcAgentError
from pc_agent.linux_secret_service_acceptance import (
    SecretServiceProviderIdentity,
    detect_secret_service_provider,
    run_linux_secret_service_acceptance,
    verify_linux_secret_service_acceptance_report,
)
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry

ROOT = Path(__file__).resolve().parents[1]
_REAL_SECRET = "phase54-real-gemini-secret"


class FakeSecretTool:
    def __init__(self) -> None:
        self.values: dict[tuple[str, ...], bytes] = {}
        self.calls: list[tuple[tuple[str, ...], bytes | None, float]] = []
        self.search_returncode = 0
        self.wrong_acceptance_reads = 0
        self.fail_acceptance_store = False
        self.mutate_real_on_acceptance_store = False

    def __call__(
        self,
        argv: Sequence[str],
        input_data: bytes | None,
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes]:
        argv_tuple = tuple(str(part) for part in argv)
        self.calls.append((argv_tuple, input_data, timeout))
        action = argv_tuple[1]
        attrs = argv_tuple[3:] if action == "store" else argv_tuple[2:]
        key = tuple(attrs)
        acceptance = "live-acceptance-probe" in attrs

        if action == "search":
            return self._completed(self.search_returncode)
        if action == "store":
            if acceptance and self.fail_acceptance_store:
                return self._completed(1, stderr=b"synthetic store failure")
            assert input_data is not None
            self.values[key] = input_data
            if acceptance and self.mutate_real_on_acceptance_store:
                for existing_key in list(self.values):
                    if "provider-api-key" in existing_key:
                        self.values[existing_key] = b"phase54-changed-real-secret"
            return self._completed(0)
        if action == "lookup":
            value = self.values.get(key)
            if value is None:
                return self._completed(1)
            if acceptance and self.wrong_acceptance_reads:
                self.wrong_acceptance_reads -= 1
                return self._completed(0, stdout=b"phase54-wrong-probe-value")
            return self._completed(0, stdout=value)
        if action == "clear":
            if self.values.pop(key, None) is None:
                return self._completed(1)
            return self._completed(0)
        raise AssertionError(f"unexpected secret-tool action: {action}")

    @staticmethod
    def _completed(
        returncode: int,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args=(), returncode=returncode, stdout=stdout, stderr=stderr
        )


def _backend(fake: FakeSecretTool) -> LinuxSecretServiceBackend:
    return LinuxSecretServiceBackend(
        command_prefix=("/fake/secret-tool",),
        is_linux=True,
        runner=fake,
    )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        root_dir=tmp_path,
        workspace_dir=tmp_path / "workspace",
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "pc_agent.db",
        credential_store="linux",
    )


def _identity(kind: str = "gnome-keyring") -> SecretServiceProviderIdentity:
    process = "gnome-keyring-daemon" if kind == "gnome-keyring" else "kwalletd6"
    return SecretServiceProviderIdentity(
        kind=kind, process_name=process, detection="gdbus-owner"
    )


def _run(
    tmp_path: Path,
    backend: LinuxSecretServiceBackend,
    *,
    report_name: str = "report.json",
    identity: SecretServiceProviderIdentity | None = None,
    environ: dict[str, str] | None = None,
):
    return run_linux_secret_service_acceptance(
        _settings(tmp_path),
        tmp_path / report_name,
        backend=backend,
        environ=(
            {
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/fake-session-bus",
                "XDG_CURRENT_DESKTOP": "GNOME",
            }
            if environ is None
            else environ
        ),
        provider_detector=lambda: identity or _identity(),
        is_linux=True,
    )


@pytest.mark.parametrize(
    ("kind", "process"),
    [("gnome-keyring", "gnome-keyring-daemon"), ("kwallet", "kwalletd6")],
)
def test_phase54_live_acceptance_round_trip_and_report_verification(
    tmp_path: Path, kind: str, process: str
) -> None:
    fake = FakeSecretTool()
    backend = _backend(fake)
    backend.set("gemini", _REAL_SECRET)
    fake.calls.clear()
    identity = SecretServiceProviderIdentity(
        kind=kind, process_name=process, detection="gdbus-owner"
    )

    report = _run(tmp_path, backend, identity=identity)

    assert report.status == "passed"
    assert all(check.status == "passed" for check in report.checks)
    assert report.provider.kind == kind
    assert report.probe_cleaned_up is True
    assert report.stored_provider_names_before == ("gemini",)
    assert report.stored_provider_names_after == ("gemini",)
    assert backend.get("gemini") == _REAL_SECRET
    assert not any("live-acceptance-probe" in key for key in fake.values)

    acceptance_stores = [
        call
        for call in fake.calls
        if call[0][1] == "store" and "live-acceptance-probe" in call[0]
    ]
    assert len(acceptance_stores) == 1
    argv, input_data, _timeout = acceptance_stores[0]
    assert input_data is not None and len(input_data) == 32
    probe_secret = input_data.decode("utf-8")
    assert probe_secret not in " ".join(argv)
    assert _REAL_SECRET not in " ".join(argv)

    raw = report.report_path.read_text(encoding="utf-8")
    assert probe_secret not in raw
    assert _REAL_SECRET not in raw
    payload = json.loads(raw)
    assert payload["real_provider_keys"]["values_recorded"] is False
    assert payload["probe"]["secret_chars"] == 32

    verification = verify_linux_secret_service_acceptance_report(
        report.report_path
    )
    assert verification.provider_kind == kind
    assert verification.process_name == process
    assert verification.report_sha256 == payload["report_sha256"]

    payload["provider"]["process_name"] = "keepassxc"
    report.report_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PcAgentError, match="hash does not match"):
        verify_linux_secret_service_acceptance_report(report.report_path)


def _dbus_completed(text: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=(), returncode=0, stdout=text, stderr=""
    )


def test_phase54_dbus_owner_detection_for_gdbus_and_busctl(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    gnome_proc = proc_root / "4321"
    gnome_proc.mkdir(parents=True)
    (gnome_proc / "cmdline").write_bytes(
        b"/usr/bin/gnome-keyring-daemon\0--start\0"
    )

    def gdbus_runner(argv: Sequence[str], _timeout: float):
        if "org.freedesktop.DBus.GetNameOwner" in argv:
            return _dbus_completed("(':1.77',)\n")
        return _dbus_completed("(uint32 4321,)\n")

    gnome = detect_secret_service_provider(
        runner=gdbus_runner,
        which=lambda name: "/fake/gdbus" if name == "gdbus" else None,
        proc_root=proc_root,
    )
    assert gnome.kind == "gnome-keyring"
    assert gnome.process_name == "gnome-keyring-daemon"
    assert gnome.detection == "gdbus-owner"

    kwallet_proc = proc_root / "5432"
    kwallet_proc.mkdir()
    (kwallet_proc / "cmdline").write_bytes(b"/usr/bin/ksecretd\0")

    def busctl_runner(argv: Sequence[str], _timeout: float):
        if "GetNameOwner" in argv:
            return _dbus_completed('s ":1.88"\n')
        return _dbus_completed("u 5432\n")

    kwallet = detect_secret_service_provider(
        runner=busctl_runner,
        which=lambda name: "/fake/busctl" if name == "busctl" else None,
        proc_root=proc_root,
    )
    assert kwallet.kind == "kwallet"
    assert kwallet.process_name == "ksecretd"
    assert kwallet.detection == "busctl-owner"


def test_phase54_unavailable_or_unknown_provider_writes_no_probe(
    tmp_path: Path,
) -> None:
    no_bus_fake = FakeSecretTool()
    no_bus = _run(
        tmp_path,
        _backend(no_bus_fake),
        report_name="no-bus.json",
        environ={},
    )
    assert no_bus.status == "failed"
    assert no_bus.check("session_bus_present").status == "failed"
    assert not any(call[0][1] == "store" for call in no_bus_fake.calls)

    unknown_fake = FakeSecretTool()
    unknown_backend = _backend(unknown_fake)
    unknown_backend.set("gemini", _REAL_SECRET)
    unknown_fake.calls.clear()
    unknown = _run(
        tmp_path,
        unknown_backend,
        report_name="unknown-provider.json",
        identity=SecretServiceProviderIdentity(
            kind="unknown", process_name="keepassxc", detection="gdbus-owner"
        ),
    )
    assert unknown.status == "failed"
    assert unknown.check("provider_is_gnome_or_kwallet").status == "failed"
    assert not any("live-acceptance-probe" in call[0] for call in unknown_fake.calls)
    assert unknown_backend.get("gemini") == _REAL_SECRET
    with pytest.raises(PcAgentError, match="does not record a passed Linux run"):
        verify_linux_secret_service_acceptance_report(tmp_path / "unknown-provider.json")

    unavailable_fake = FakeSecretTool()
    unavailable_fake.search_returncode = 1
    unavailable = _run(
        tmp_path,
        _backend(unavailable_fake),
        report_name="unavailable.json",
    )
    assert unavailable.status == "failed"
    assert unavailable.check("secret_service_available").status == "failed"
    assert not any(call[0][1] == "store" for call in unavailable_fake.calls)


def test_phase54_failure_cleanup_and_real_key_change_detection(
    tmp_path: Path,
) -> None:
    wrong_fake = FakeSecretTool()
    wrong_fake.wrong_acceptance_reads = 1
    wrong_backend = _backend(wrong_fake)
    wrong = _run(tmp_path, wrong_backend, report_name="wrong-readback.json")
    assert wrong.status == "failed"
    assert wrong.check("probe_store_and_readback").status == "failed"
    assert wrong.check("probe_delete_and_absent_after").status == "passed"
    assert not any("live-acceptance-probe" in key for key in wrong_fake.values)

    mutation_fake = FakeSecretTool()
    mutation_fake.mutate_real_on_acceptance_store = True
    mutation_backend = _backend(mutation_fake)
    mutation_backend.set("gemini", _REAL_SECRET)
    mutated = _run(
        tmp_path, mutation_backend, report_name="real-key-changed.json"
    )
    assert mutated.status == "failed"
    assert mutated.check("real_provider_keys_untouched").status == "failed"
    raw = (tmp_path / "real-key-changed.json").read_text(encoding="utf-8")
    assert _REAL_SECRET not in raw
    assert "phase54-changed-real-secret" not in raw

    failed_store_fake = FakeSecretTool()
    failed_store_fake.fail_acceptance_store = True
    failed_store = _run(
        tmp_path, _backend(failed_store_fake), report_name="store-failed.json"
    )
    assert failed_store.status == "failed"
    assert failed_store.check("probe_store_and_readback").status == "failed"
    assert not any("live-acceptance-probe" in key for key in failed_store_fake.values)


def test_phase54_existing_report_refused_before_keyring_calls(tmp_path: Path) -> None:
    fake = FakeSecretTool()
    report_path = tmp_path / "already-there.json"
    report_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(PcAgentError, match="already exists"):
        run_linux_secret_service_acceptance(
            _settings(tmp_path),
            report_path,
            backend=_backend(fake),
            environ={"DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/fake"},
            provider_detector=lambda: _identity(),
            is_linux=True,
        )
    assert fake.calls == []


def test_phase54_cli_run_and_offline_report_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert "credentials" in build_parser().format_help()
    fake = FakeSecretTool()
    backend = _backend(fake)
    backend.set("gemini", _REAL_SECRET)

    def fake_run(settings: Settings, report_path: Path | None):
        return run_linux_secret_service_acceptance(
            settings,
            report_path,
            backend=backend,
            environ={
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/fake-session-bus",
                "XDG_CURRENT_DESKTOP": "KDE",
            },
            provider_detector=lambda: _identity("kwallet"),
            is_linux=True,
        )

    monkeypatch.setattr(
        cli_module, "run_linux_secret_service_acceptance", fake_run
    )
    (tmp_path / ".env").write_text(
        "PC_AGENT_CREDENTIAL_STORE=linux\n", encoding="utf-8"
    )
    report_path = tmp_path / "cli-report.json"

    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "credentials",
                "linux-secret-service-acceptance",
                "--report",
                str(report_path),
                "--json",
            ]
        )
        == 0
    )
    run_output = capsys.readouterr().out
    assert json.loads(run_output)["status"] == "passed"
    assert _REAL_SECRET not in run_output

    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "credentials",
                "linux-secret-service-acceptance-verify",
                str(report_path),
                "--json",
            ]
        )
        == 0
    )
    verify_output = capsys.readouterr().out
    assert json.loads(verify_output)["provider_kind"] == "kwallet"
    assert _REAL_SECRET not in verify_output


def test_phase54_scope_inventory_and_documentation() -> None:
    assert __version__ == "0.82.12"
    assert LATEST_SCHEMA_VERSION == 18
    assert len(build_default_registry().list_tools()) == 69

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["version"] == __version__
    assert project["project"]["dependencies"] == [
        "certifi>=2024.7.4",
        "cryptography>=42,<51",
        "pypdf[crypto]>=5.0,<7",
        "pypdfium2>=5,<6",
        "Pillow>=10.4,<14",
        "comtypes>=1.4,<2; sys_platform == 'win32'",
    ]

    credentials = (ROOT / "src/pc_agent/credentials.py").read_text(
        encoding="utf-8"
    )
    module = (
        ROOT / "src/pc_agent/linux_secret_service_acceptance.py"
    ).read_text(encoding="utf-8")
    cli = (ROOT / "src/pc_agent/cli.py").read_text(encoding="utf-8")
    phase = (ROOT / "docs/phase54.md").read_text(encoding="utf-8")
    phase44 = (ROOT / "docs/phase44.md").read_text(encoding="utf-8")
    providers = (ROOT / "docs/providers.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    tools = (ROOT / "docs/tools.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert "get_acceptance_secret" in credentials
    assert "set_acceptance_secret" in credentials
    assert "delete_acceptance_secret" in credentials
    assert "live-acceptance-probe" in credentials
    assert "org.freedesktop.secrets" in module
    assert '"values_recorded": False' in module
    assert "build_model_router" not in module
    assert "linux-secret-service-acceptance" in cli
    assert "linux-secret-service-acceptance-verify" in cli

    release_names = {path.as_posix() for path in release_candidate_files(ROOT)}
    for expected in (
        "src/pc_agent/linux_secret_service_acceptance.py",
        "docs/phase54.md",
        "tests/test_phase54_linux_secret_service_acceptance.py",
    ):
        assert expected in release_names

    for text in (phase, phase44, providers, readme, tools):
        assert "Live GNOME Keyring/KWallet Evidence" in text
    assert "org.freedesktop.secrets" in phase
    assert "values and hashes are not recorded" in phase
    assert "## 0.56.0 - PHASE 54 Live GNOME Keyring/KWallet Evidence" in changelog
