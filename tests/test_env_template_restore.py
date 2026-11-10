"""0.82.9: one missing template file must not break four installers.

`.env.example` is a **required installer payload file**: the Windows installer,
the POSIX package and (through them) the Debian and RPM builders all refuse to
build without it, because a package has to ship the template users copy to `.env`.

That made a single 8 KB sample file a single point of failure with the worst
possible failure mode, measured on a real Windows install:

* 19 tests failed across phases 24, 38, 41, 46, 47, 48, 50, 51, 52 and 57;
* every one of them said "missing required files: .env.example" and none of them
  said what to do about it;
* `pc-agent release audit` reported **READY** while the tree could not build an
  installer at all.

The fix is in three parts, and each part has a test here:

1. the template lives in code (`env_template.ENV_TEMPLATE`) and is restored
   automatically by both payload resolvers when the file is absent - it is the
   sample file, so restoring it can never leak a secret;
2. `pc-agent release env-template --check/--write` does the same thing on demand;
3. `release audit` and `scripts/verify_windows.py` now *check* for it, so the next
   occurrence is one visible line instead of nineteen puzzling ones.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pc_agent import cli
from pc_agent.env_template import (
    ENV_TEMPLATE,
    ENV_TEMPLATE_NAME,
    env_template_path,
    restore_env_template_if_missing,
    write_env_template,
)
from pc_agent.posix_package import posix_package_payload_files
from pc_agent.windows_installer import windows_installer_payload_files

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ template

def test_the_embedded_template_is_the_shipped_file() -> None:
    """If these drift, a restore would silently change the shipped sample."""

    on_disk = (ROOT / ENV_TEMPLATE_NAME).read_text(encoding="utf-8")
    assert ENV_TEMPLATE == on_disk
    assert ENV_TEMPLATE_NAME == ".env.example"


def test_the_template_contains_no_secret_values() -> None:
    """Every `*_KEY=` / `*_TOKEN=` line must be empty - it is a sample file."""

    filled: list[str] = []
    for line in ENV_TEMPLATE.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        lowered = name.lower()
        secret_like = any(word in lowered for word in ("key", "token", "secret", "password"))
        if secret_like and value.strip():
            filled.append(stripped)
    assert filled == [], f"the sample must not ship values: {filled}"
    assert "NEVER commit a real .env file" in ENV_TEMPLATE


def test_write_is_idempotent_and_never_overwrites_by_default(tmp_path: Path) -> None:
    _, wrote = write_env_template(tmp_path)
    assert wrote is True
    target = env_template_path(tmp_path)
    assert target.read_text(encoding="utf-8") == ENV_TEMPLATE
    target.write_text("# edited by the user\n", encoding="utf-8")
    _, wrote_again = write_env_template(tmp_path)
    assert wrote_again is False, "an existing file must be left alone"
    assert target.read_text(encoding="utf-8") == "# edited by the user\n"


def test_force_rewrites_it(tmp_path: Path) -> None:
    target = env_template_path(tmp_path)
    target.write_text("# stale\n", encoding="utf-8")
    _, wrote = write_env_template(tmp_path, force=True)
    assert wrote is True
    assert target.read_text(encoding="utf-8") == ENV_TEMPLATE


def test_restore_reports_only_when_it_wrote(tmp_path: Path) -> None:
    assert restore_env_template_if_missing(tmp_path) is not None
    assert restore_env_template_if_missing(tmp_path) is None


# ------------------------------------------------------- builders self-heal

def _minimal_tree(tmp_path: Path) -> Path:
    """A copy of the project without `.env.example` - the exact broken state."""

    import shutil

    root = tmp_path / "tree"
    root.mkdir()
    for name in ("src", "scripts", "docs", "tests"):
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(source, root / name)
    for name in ("pyproject.toml", "README.md", "CHANGELOG.md"):
        shutil.copy2(ROOT / name, root / name)
    # The POSIX payload requires the empty workspace placeholder as well, so a
    # fixture that omits it would fail for a reason unrelated to this test.
    (root / "workspace").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "workspace" / ".gitkeep", root / "workspace" / ".gitkeep")
    return root


def test_posix_payload_restores_a_missing_template(tmp_path: Path) -> None:
    root = _minimal_tree(tmp_path)
    assert not (root / ENV_TEMPLATE_NAME).exists()
    files = posix_package_payload_files(root)
    assert ENV_TEMPLATE_NAME in {path.as_posix() for path in files}
    assert (root / ENV_TEMPLATE_NAME).is_file(), "the builder must restore it, not just report"


def test_windows_payload_restores_a_missing_template(tmp_path: Path) -> None:
    root = _minimal_tree(tmp_path)
    assert not (root / ENV_TEMPLATE_NAME).exists()
    files = windows_installer_payload_files(root)
    assert ENV_TEMPLATE_NAME in {path.as_posix() for path in files}
    assert (root / ENV_TEMPLATE_NAME).is_file()


def test_a_different_missing_payload_file_still_fails_with_the_fix(tmp_path: Path) -> None:
    """Self-healing is limited to the template; real breakage must stay loud."""

    from pc_agent.errors import PcAgentError

    root = _minimal_tree(tmp_path)
    (root / "scripts" / "install_posix.sh").unlink()
    with pytest.raises(PcAgentError) as caught:
        posix_package_payload_files(root)
    message = str(caught.value)
    assert "scripts/install_posix.sh" in message
    assert "re-running the installer patcher" in message
    assert "env-template --write" in message, "the message must say how to recover"


# -------------------------------------------------------------------- audit

def test_release_audit_reports_the_payload(tmp_path: Path) -> None:
    from pc_agent.config import Settings
    from pc_agent.release import audit_install

    settings = Settings.from_env(ROOT)
    audit = audit_install(settings)
    check = next(item for item in audit.checks if item.name == "installer_payload")
    assert check.status == "pass", check.detail
    assert "required installer payload files are present" in check.detail


def test_release_audit_fails_when_a_full_install_lost_the_template(tmp_path: Path) -> None:
    """The exact reported fault: a complete tree, one payload file missing."""

    import shutil

    from pc_agent.config import Settings
    from pc_agent.release import audit_install

    root = _minimal_tree(tmp_path)
    shutil.copy2(ROOT / ".gitignore", root / ".gitignore")
    settings = Settings.from_env(root)
    check = next(
        item for item in audit_install(settings).checks if item.name == "installer_payload"
    )
    assert check.status == "fail", check.detail
    assert ".env.example" in check.detail
    assert "env-template --write" in check.detail


def test_a_minimal_tree_is_information_not_a_failure(tmp_path: Path) -> None:
    """Tests build 10-file fake roots; failing the audit there would be noise."""

    from pc_agent.config import Settings
    from pc_agent.release import audit_install

    root = tmp_path / "tiny"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    settings = Settings.from_env(root)
    check = next(
        item for item in audit_install(settings).checks if item.name == "installer_payload"
    )
    assert check.status == "info"
    assert "not a full install" in check.detail


# ---------------------------------------------------------------------- CLI

def test_cli_check_reports_present(capsys: pytest.CaptureFixture[str]) -> None:
    from pc_agent.config import Settings

    settings = Settings.from_env(ROOT)
    args = cli.build_parser().parse_args(["release", "env-template", "--check"])
    assert cli._run_release_env_template(settings, args) == 0
    out = capsys.readouterr().out
    assert ".env.example: present" in out


def test_cli_check_reports_missing_and_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from pc_agent.config import Settings

    settings = Settings.from_env(tmp_path)
    settings.ensure_directories()
    args = cli.build_parser().parse_args(["release", "env-template", "--check"])
    assert cli._run_release_env_template(settings, args) == 1
    err = capsys.readouterr().err
    assert "MISSING" in err
    assert "pc-agent release env-template --write" in err


def test_cli_write_restores_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from pc_agent.config import Settings

    settings = Settings.from_env(tmp_path)
    settings.ensure_directories()
    args = cli.build_parser().parse_args(["release", "env-template", "--write"])
    assert cli._run_release_env_template(settings, args) == 0
    assert (tmp_path / ENV_TEMPLATE_NAME).read_text(encoding="utf-8") == ENV_TEMPLATE
    assert "restored" in capsys.readouterr().out


def test_cli_json_is_machine_readable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from pc_agent.config import Settings

    settings = Settings.from_env(tmp_path)
    settings.ensure_directories()
    args = cli.build_parser().parse_args(["release", "env-template", "--write", "--json"])
    assert cli._run_release_env_template(settings, args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["exists"] is True
    assert payload["restored"] is True
    assert payload["contains_secrets"] is False
    assert "release debian-package" in payload["required_by"]


# --------------------------------------------------------------- harness gate

def test_verify_windows_has_a_payload_gate() -> None:
    script = (ROOT / "scripts" / "verify_windows.py").read_text(encoding="utf-8")
    assert '"payload"' in script
    assert "def _payload_gate" in script
    assert "restored" in script
    # The gate must run before the four-minute suite, not after it.
    assert script.index('"payload"') < script.index('"suite"')
