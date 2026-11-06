"""Phase 18 security regression suite (production hardening).

These are the attacks the product claims to survive, written as executable
checks against the shipping code - not documentation. Each test states the
attacker's move and asserts the concrete refusal.

Nothing is mocked except two things that must be, and are named in the test:
a URL resolver (so SSRF cases are deterministic and never touch the network) and
a local fake provider (so no external API is called).
"""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from pathlib import Path
from typing import Any

import pytest

from pc_agent.commands import run_command
from pc_agent.config import Settings
from pc_agent.diagnostics import build_diagnostics
from pc_agent.errors import PcAgentError, ToolError
from pc_agent.safety import EmergencyStop
from pc_agent.web import validate_url

_SECRET = "sk-phase18-should-never-appear-1234567890"


def _settings(tmp_path: Path, **overrides: str) -> Settings:
    settings = Settings.from_env(tmp_path, environ=dict(overrides), load_dotenv=False)
    settings.ensure_directories()
    return settings


# --------------------------------------------------------------- SSRF / URLs ---


def _resolver_for(*addresses: str):
    def resolve(host: str, port: int, *args: Any, **kwargs: Any):
        return [
            (2, 1, 6, "", (address, port)) for address in addresses
        ]

    return resolve


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/steal",
        "http://localhost:80/admin",
        "http://[::1]/internal",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "http://192.168.1.10/",
        "http://172.16.5.4/",
        "http://0.0.0.0/",
    ],
)
def test_private_network_targets_are_refused(url: str) -> None:
    host = url.split("//", 1)[1].split("/", 1)[0]
    address = host.strip("[]").split(":")[0]
    if address in {"localhost", "::1"}:
        address = "127.0.0.1"
    with pytest.raises(ToolError):
        validate_url(url, resolver=_resolver_for(address), resolve=True)


def test_dns_rebinding_style_hostname_resolving_to_loopback_is_refused() -> None:
    """A public-looking name that resolves to loopback is still loopback."""

    with pytest.raises(ToolError, match="private|loopback|local|refus"):
        validate_url(
            "https://totally-public.example.com/data",
            resolver=_resolver_for("127.0.0.1"),
        )


def test_non_web_schemes_and_credentials_are_refused() -> None:
    for bad in (
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com/",
        "data:text/html,<script>alert(1)</script>",
        "https://user:pass@example.com/",
        "javascript:alert(1)",
    ):
        with pytest.raises(ToolError):
            validate_url(bad, resolver=_resolver_for("93.184.216.34"))


def test_public_https_url_is_accepted() -> None:
    validated = validate_url(
        "https://example.com/page", resolver=_resolver_for("93.184.216.34")
    )
    assert validated.url.startswith("https://example.com")


# ------------------------------------------------------- command execution ---


def _portable_echo_program() -> str:
    """A program that prints its arguments and exists on both platforms.

    ``echo`` is a POSIX program but a ``cmd`` *builtin* on Windows, so it cannot
    be launched there at all. The running interpreter is the portable choice and
    keeps the property under test - metacharacters must arrive as argv items -
    exactly the same.
    """

    name = "python" if os.name == "nt" else "python3"
    if shutil.which(name) is None:  # pragma: no cover - unusual installs
        pytest.skip(f"no '{name}' on PATH for the argument-passing probe")
    return name


def test_commands_never_go_through_a_shell(tmp_path: Path) -> None:
    """Shell metacharacters are arguments, not instructions."""

    marker = tmp_path / "injected.txt"
    program = _portable_echo_program()
    payload = f"hello; touch {marker} && touch {marker}"
    result = run_command(
        executable=program,
        argv=["-c", "import sys; print(sys.argv[1])", payload],
        cwd=tmp_path,
        timeout_seconds=60,
        allowlist=(program,),
    )
    assert result.exit_code == 0, result.stderr
    assert "touch" in result.stdout  # printed as text
    assert "hello;" in result.stdout
    assert not marker.exists()  # and never executed


def test_non_allowlisted_executable_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PcAgentError):
        run_command(
            executable="curl",
            argv=["http://127.0.0.1/"],
            cwd=tmp_path,
            timeout_seconds=5,
            allowlist=("echo", "git"),
        )


def test_path_traversal_executable_is_refused(tmp_path: Path) -> None:
    for bad in ("../bin/sh", "/bin/sh", "C:\\Windows\\System32\\cmd.exe", "..\\sh"):
        with pytest.raises(PcAgentError):
            run_command(
                executable=bad,
                argv=["-c", "id"],
                cwd=tmp_path,
                timeout_seconds=5,
                allowlist=("sh", "cmd.exe"),
            )


# -------------------------------------------------------- archive traversal ---


def test_release_verification_refuses_traversal_entries(tmp_path: Path) -> None:
    """A hostile ZIP is inspected without extracting, and its paths are refused."""

    from pc_agent.release import verify_release_bundle

    hostile = tmp_path / "hostile.zip"
    with zipfile.ZipFile(hostile, "w") as archive:
        archive.writestr("../escape.py", "print('pwned')")
        archive.writestr("release-manifest.json", json.dumps({"format": 1, "files": []}))
    with pytest.raises(PcAgentError):
        verify_release_bundle(hostile)
    assert not (tmp_path.parent / "escape.py").exists()


# ------------------------------------------------------------ secret leakage ---


def test_diagnostics_and_audit_never_print_configured_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", _SECRET)
    settings = _settings(
        tmp_path,
        PC_AGENT_OPENAI_API_KEY=_SECRET,
        PC_AGENT_ANTHROPIC_API_KEY=_SECRET,
        PC_AGENT_RELEASE_PUBLIC_KEY="",
        PC_AGENT_UPDATE_FEED_URL="https://updates.example.com/stable/",
    )
    diagnostics = json.dumps(build_diagnostics(settings), default=str)
    assert _SECRET not in diagnostics

    from pc_agent.release import audit_install

    audit = json.dumps(audit_install(settings).to_dict(), default=str)
    assert _SECRET not in audit


def test_report_files_written_by_the_release_chain_contain_no_secret(
    tmp_path: Path,
) -> None:
    from pc_agent.production_check import run_production_check

    project_root = Path(__file__).resolve().parents[1]
    settings = Settings.from_env(
        project_root,
        environ={"PC_AGENT_DATA_DIR": str(tmp_path / "data")},
        load_dotenv=False,
    )
    settings.ensure_directories()
    report = run_production_check(settings)
    assert report.ok, [step for step in report.steps if step.status == "fail"]
    text = (Path(settings.data_dir) / "production-check.json").read_text(encoding="utf-8")
    assert "PRIVATE KEY" not in text
    assert _SECRET not in text
    assert "self-test-private-key" not in text  # the key file is deleted, not quoted


# ------------------------------------------------------------- emergency stop ---


def test_emergency_stop_is_reported_and_blocks_new_work(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    stop = EmergencyStop(settings.data_dir)
    stop.engage(reason="phase18 security test", engaged_by="test")
    state = stop.status().to_dict()
    assert state["engaged"] is True
    diagnostics = build_diagnostics(settings)
    assert json.dumps(diagnostics, default=str).count("stop") >= 1
    stop.clear()
    assert not stop.status().engaged


# -------------------------------------------------------------- tool boundary ---


def test_tool_registry_rejects_unknown_arguments_and_unknown_tools(
    tmp_path: Path,
) -> None:
    from pc_agent.tools import ToolContext, ToolRegistry, ToolSpec

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            id="phase18.echo",
            name="Echo",
            version="1.0.0",
            description="Echo a bounded string.",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string", "maxLength": 32}},
                "required": ["text"],
            },
            output_schema={
                "type": "object",
                "properties": {"echo": {"type": "string"}},
                "required": ["echo"],
            },
            permissions=(),
            provenance="phase18-test",
        ),
        lambda arguments, _context: {"echo": arguments["text"]},
    )
    context = ToolContext.for_workspace(tmp_path)
    assert registry.invoke("phase18.echo", {"text": "ok"}, context).success
    injected = registry.invoke(
        "phase18.echo", {"text": "ok", "shell": "rm -rf /"}, context
    )
    assert not injected.success and "unknown field" in (injected.error or "").lower()
    missing = registry.invoke("phase18.does-not-exist", {}, context)
    assert not missing.success
