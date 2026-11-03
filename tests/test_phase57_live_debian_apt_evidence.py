"""Phase 57: live Debian/Ubuntu APT install acceptance evidence."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pc_agent import __version__
from pc_agent import debian_apt_acceptance as apt_acceptance
from pc_agent.cli import _run_release_command, build_parser
from pc_agent.config import Settings
from pc_agent.debian_apt_acceptance import (
    parse_os_release,
    run_debian_apt_acceptance,
    verify_debian_apt_acceptance_report,
)
from pc_agent.debian_package import (
    _gunzip_bounded,
    _parse_ar,
    _parse_tar,
    build_debian_package,
    verify_debian_package,
)
from pc_agent.debian_repository import (
    build_debian_repository,
    verify_debian_repository,
)
from pc_agent.errors import PcAgentError
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "self-healing-pc-agent"


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(
        root_dir=ROOT,
        workspace_dir=tmp_path / "workspace",
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "agent.db",
    )
    settings.ensure_directories()
    return settings


def _os_release(tmp_path: Path, *, os_id: str = "debian") -> Path:
    path = tmp_path / "os-release"
    path.write_text(
        "\n".join(
            [
                'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"',
                'NAME="Debian GNU/Linux"',
                'VERSION_ID="13"',
                "VERSION_CODENAME=trixie",
                f"ID={os_id}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _data_files(package: Path) -> dict[str, tuple[bytes, int]]:
    members = _parse_ar(package.read_bytes())
    return _parse_tar(
        _gunzip_bounded(
            members["data.tar.gz"],
            limit=250_000_000,
            label="data archive",
        ),
        label="data archive",
    )


class _FakeAptRunner:
    def __init__(
        self,
        *,
        repository: Path,
        package: Path,
        system_root: Path,
        installed: bool = False,
        candidate_sha256: str | None = None,
    ) -> None:
        self.repository = repository
        self.package = package
        self.system_root = system_root
        self.installed = installed
        self.candidate_sha256 = candidate_sha256
        self.calls: list[list[str]] = []
        self.repository_info = verify_debian_repository(repository)
        self.package_info = verify_debian_package(package)
        self.data_files = _data_files(package)

    def __call__(
        self,
        argv: Sequence[str],
        timeout_seconds: float,
        cwd: Path | None,
        env: Mapping[str, str] | None,
    ) -> subprocess.CompletedProcess[str]:
        del timeout_seconds, cwd, env
        args = [str(part) for part in argv]
        self.calls.append(args)
        executable = Path(args[0]).name
        if args[-1] == "--version" and executable == "apt-get":
            return self._completed(args, 0, "apt 3.0.3 (amd64)\n")
        if args[-1] == "--version" and executable == "dpkg":
            return self._completed(
                args,
                0,
                "Debian 'dpkg' package management program version 1.22.22 (amd64).\n",
            )
        if args[-1] == "--print-architecture":
            return self._completed(args, 0, "amd64\n")
        if executable == "dpkg-query":
            if self.installed:
                return self._completed(
                    args,
                    0,
                    "install ok installed\n"
                    f"{self.package_info.package_version}\nall\n",
                )
            return self._completed(
                args,
                1,
                "",
                f"dpkg-query: no packages found matching {PACKAGE}\n",
            )
        if "update" in args:
            return self._completed(args, 0, "Reading package lists...\n")
        if "policy" in args:
            installed = (
                self.package_info.package_version if self.installed else "(none)"
            )
            return self._completed(
                args,
                0,
                "\n".join(
                    [
                        f"{PACKAGE}:",
                        f"  Installed: {installed}",
                        f"  Candidate: {self.package_info.package_version}",
                        "  Version table:",
                        f"     {self.package_info.package_version} 500",
                        "        500 file:/acceptance stable/main amd64 Packages",
                        "",
                    ]
                ),
            )
        if "show" in args:
            filename = self.package.relative_to(self.repository).as_posix()
            # The repository package path is in pool/; the input package for
            # this helper is that exact pool file.
            return self._completed(
                args,
                0,
                "\n".join(
                    [
                        f"Package: {PACKAGE}",
                        f"Version: {self.package_info.package_version}",
                        "Architecture: all",
                        f"Filename: {filename}",
                        f"Size: {self.package.stat().st_size}",
                        "SHA256: "
                        + (
                            self.candidate_sha256
                            or self.package_info.package_sha256
                        ),
                        "",
                    ]
                ),
            )
        if "install" in args:
            for relative, (data, mode) in self.data_files.items():
                destination = self.system_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
                if os.name == "posix":
                    destination.chmod(mode & 0o777)
            self.installed = True
            return self._completed(args, 0, "Setting up package ...\n")
        if "purge" in args:
            installed_root = self.system_root / "usr"
            if installed_root.exists():
                shutil.rmtree(installed_root)
            self.installed = False
            return self._completed(args, 0, "Purging package ...\n")
        raise AssertionError(f"unexpected fake APT argv: {args!r}")

    @staticmethod
    def _completed(
        argv: Sequence[str],
        returncode: int,
        stdout: str,
        stderr: str = "",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            [str(part) for part in argv], returncode, stdout, stderr
        )


def _patch_fake_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        apt_acceptance,
        "_find_tools",
        lambda: {
            "apt_cache": "apt-cache",
            "apt_get": "apt-get",
            "dpkg": "dpkg",
            "dpkg_query": "dpkg-query",
            "sudo": "sudo",
        },
    )
    monkeypatch.setattr(
        apt_acceptance,
        "_administrator_prefix",
        lambda report, tools, runner: (),
    )


def _build_repository(tmp_path: Path) -> tuple[Path, Path]:
    package = tmp_path / f"self-healing-pc-agent_{__version__}-1_all.deb"
    build_debian_package(ROOT, package)
    repository = tmp_path / "repository"
    build_debian_repository(ROOT, package, repository)
    info = verify_debian_repository(repository)
    return repository, info.package_path


def test_phase57_os_release_and_source_line_contract(tmp_path: Path) -> None:
    parsed = parse_os_release(
        'NAME="Debian GNU/Linux"\nID=ubuntu\nVERSION_ID="24.04"\n'
    )
    assert parsed["ID"] == "ubuntu"
    assert parsed["NAME"] == "Debian GNU/Linux"

    unsigned = SimpleNamespace(signed=False, suite="stable", component="main")
    line = apt_acceptance._apt_source_line(tmp_path / "repo", unsigned, keyring=None)
    assert "[trusted=yes]" in line
    assert line.endswith(" stable main\n")

    keyring = tmp_path / "archive-keyring.gpg"
    keyring.write_bytes(b"public")
    signed = SimpleNamespace(signed=True, suite="bookworm", component="main")
    signed_line = apt_acceptance._apt_source_line(
        tmp_path / "repo", signed, keyring=keyring
    )
    assert f"signed-by={keyring.resolve()}" in signed_line
    assert signed_line.endswith(" bookworm main\n")


def test_phase57_fake_apt_full_install_verify_purge_and_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, pool_package = _build_repository(tmp_path)
    settings = _settings(tmp_path)
    os_release = _os_release(tmp_path)
    _patch_fake_host(monkeypatch)

    system_root = tmp_path / "fake-system"
    runner = _FakeAptRunner(
        repository=repository,
        package=pool_package,
        system_root=system_root,
    )
    report = run_debian_apt_acceptance(
        settings,
        repository,
        tmp_path / "passed.json",
        runner=runner,
        platform_name="linux",
        os_release_path=os_release,
        system_root=system_root,
    )
    assert report.status == "passed"
    assert all(check.status == "passed" for check in report.checks)
    assert report.host["os_id"] == "debian"
    assert report.repository["trust_mode"] == "trusted-yes-local-acceptance-only"
    assert report.apt_candidate["version"] == f"{__version__}-1"
    assert report.installed["verified_file_count"] == report.installed["file_count"]
    assert report.cleanup == {
        "package_absent_after": True,
        "purge_attempted": True,
    }
    assert any("install" in call for call in runner.calls)
    assert any("purge" in call for call in runner.calls)
    verification = verify_debian_apt_acceptance_report(report.report_path)
    assert verification.status == "passed"
    assert verification.package_sha256 == report.repository["package_sha256"]

    tampered = tmp_path / "tampered.json"
    payload = json.loads(report.report_path.read_text(encoding="utf-8"))
    payload["host"]["os_id"] = "ubuntu"
    tampered.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PcAgentError, match="hash"):
        verify_debian_apt_acceptance_report(tampered)

    wrong_candidate_root = tmp_path / "wrong-candidate-system"
    wrong_runner = _FakeAptRunner(
        repository=repository,
        package=pool_package,
        system_root=wrong_candidate_root,
        candidate_sha256="0" * 64,
    )
    wrong = run_debian_apt_acceptance(
        settings,
        repository,
        tmp_path / "wrong-candidate.json",
        runner=wrong_runner,
        platform_name="linux",
        os_release_path=os_release,
        system_root=wrong_candidate_root,
    )
    assert wrong.status == "failed"
    assert wrong.check("apt_candidate").status == "failed"
    assert not any("install" in call for call in wrong_runner.calls)

    preinstalled_root = tmp_path / "preinstalled-system"
    preinstalled_runner = _FakeAptRunner(
        repository=repository,
        package=pool_package,
        system_root=preinstalled_root,
        installed=True,
    )
    preinstalled = run_debian_apt_acceptance(
        settings,
        repository,
        tmp_path / "preinstalled.json",
        runner=preinstalled_runner,
        platform_name="linux",
        os_release_path=os_release,
        system_root=preinstalled_root,
    )
    assert preinstalled.status == "failed"
    assert preinstalled.check("package_absent_before").status == "failed"
    assert not any("install" in call for call in preinstalled_runner.calls)
    assert not any("purge" in call for call in preinstalled_runner.calls)


def test_phase57_privileged_temporary_state_cleanup_regression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = tmp_path / "root-owned-apt-state"
    (work / "cache" / "archives" / "partial").mkdir(parents=True)
    original_rmtree = apt_acceptance.shutil.rmtree
    attempts = 0

    def locked_rmtree(path: Path, *args: Any, **kwargs: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("simulated root-owned partial directory")
        original_rmtree(path, *args, **kwargs)

    calls: list[list[str]] = []

    def runner(
        argv: Sequence[str],
        timeout_seconds: float,
        cwd: Path | None,
        env: Mapping[str, str] | None,
    ) -> subprocess.CompletedProcess[str]:
        del timeout_seconds, cwd, env
        calls.append([str(part) for part in argv])
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    monkeypatch.setattr(apt_acceptance.shutil, "rmtree", locked_rmtree)
    monkeypatch.setattr(apt_acceptance.shutil, "which", lambda name: "/bin/rm")
    apt_acceptance._remove_temporary_state(
        work,
        prefix=("sudo", "-n"),
        runner=runner,
        env={},
    )
    assert calls == [["sudo", "-n", "/bin/rm", "-rf", "--", str(work)]]
    assert not work.exists()


def test_phase57_non_debian_host_fails_before_apt(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    os_release = _os_release(tmp_path, os_id="fedora")

    def forbidden_runner(*args: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"runner must not be called: {args!r}")

    report = run_debian_apt_acceptance(
        settings,
        tmp_path / "repository",
        tmp_path / "failed-host.json",
        runner=forbidden_runner,
        platform_name="linux",
        os_release_path=os_release,
    )
    assert report.status == "failed"
    assert report.check("debian_or_ubuntu_host").status == "failed"
    assert report.commands == []


def test_phase57_cli_requires_confirmation_and_verifies_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository, pool_package = _build_repository(tmp_path)
    settings = _settings(tmp_path)
    os_release = _os_release(tmp_path)
    _patch_fake_host(monkeypatch)
    system_root = tmp_path / "cli-fake-system"
    runner = _FakeAptRunner(
        repository=repository,
        package=pool_package,
        system_root=system_root,
    )
    report = run_debian_apt_acceptance(
        settings,
        repository,
        tmp_path / "cli-report.json",
        runner=runner,
        platform_name="linux",
        os_release_path=os_release,
        system_root=system_root,
    )
    parser = build_parser()

    cancelled_args = parser.parse_args(
        [
            "release",
            "debian-repository-acceptance",
            str(repository),
            "--json",
        ]
    )
    assert _run_release_command(settings, cancelled_args) == 3
    assert "cancelled" in capsys.readouterr().out

    verify_args = parser.parse_args(
        [
            "release",
            "debian-repository-acceptance-verify",
            str(report.report_path),
            "--json",
        ]
    )
    assert _run_release_command(settings, verify_args) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["status"] == "passed"
    assert verified["package_version"] == f"{__version__}-1"


def test_phase57_scope_inventory_and_documentation() -> None:
    assert __version__ == "0.60.0"
    assert LATEST_SCHEMA_VERSION == 6
    assert len(build_default_registry().list_tools()) == 62

    release_names = {path.as_posix() for path in release_candidate_files(ROOT)}
    for expected in (
        "src/pc_agent/debian_apt_acceptance.py",
        "docs/phase57.md",
        "tests/test_phase57_live_debian_apt_evidence.py",
    ):
        assert expected in release_names

    module = (ROOT / "src/pc_agent/debian_apt_acceptance.py").read_text(
        encoding="utf-8"
    )
    phase = (ROOT / "docs/phase57.md").read_text(encoding="utf-8")
    phase50 = (ROOT / "docs/phase50.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    tools = (ROOT / "docs/tools.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert "run_debian_apt_acceptance" in module
    assert "apt-install-verify-purge" in module
    for text in (phase, phase50, readme, tools):
        assert "Live Debian/Ubuntu APT" in text
    assert "trusted=yes" in phase
    assert "signed-by" in phase
    assert "dependencies" in phase
    assert "## 0.59.0 - PHASE 57 Live Debian/Ubuntu APT Evidence" in (
        changelog.splitlines()
    )
