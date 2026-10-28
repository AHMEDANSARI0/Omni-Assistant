"""Phase 12.1: confirmation prompts cannot be answered by pasted text."""

from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pc_agent import cli
from pc_agent.cli import _approval_callback, _confirm_direct_write, main
from pc_agent.config import Settings


def _no_flush(monkeypatch: pytest.MonkeyPatch, *, found: bool = False) -> None:
    monkeypatch.setattr(cli, "_flush_pending_console_input", lambda: found)
    # Typed-word path: deterministic regardless of the runner's terminal.
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: False)


def test_confirm_accepts_case_insensitive_approve(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_flush(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _prompt: "  approve  ")

    assert _confirm_direct_write("Do the thing", preapproved=False) is True
    assert "Decision: APPROVED" in capsys.readouterr().out


def test_confirm_denies_command_like_text_and_prints_retry_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_flush(monkeypatch)
    # Exactly what happened on the user's machine: the next pasted command
    # landed in the prompt instead of an approval.
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: 'pc-agent run "Run the test suite" --yes',
    )

    assert (
        _confirm_direct_write(
            "Initialize a Git repository in my-project",
            preapproved=False,
            retry_hint="pc-agent git init my-project --yes",
        )
        is False
    )
    out = capsys.readouterr().out
    assert "Decision: DENIED" in out
    assert "pc-agent git init my-project --yes" in out


def test_confirm_reports_ignored_pasted_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_flush(monkeypatch, found=True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "APPROVE")

    assert _confirm_direct_write("Do the thing", preapproved=False) is True
    out = capsys.readouterr().out
    assert "already typed or pasted was ignored" in out


def test_confirm_eof_denies_with_guidance(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_flush(monkeypatch)

    def _eof(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)

    assert (
        _confirm_direct_write(
            "Do the thing",
            preapproved=False,
            retry_hint="pc-agent git init repo --yes",
        )
        is False
    )
    out = capsys.readouterr().out
    assert "No interactive input available" in out
    assert "pc-agent git init repo --yes" in out


def test_flush_leaves_non_tty_stdin_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_stdin = io.StringIO("APPROVE\n")  # StringIO.isatty() is False
    monkeypatch.setattr(sys, "stdin", fake_stdin)

    assert cli._flush_pending_console_input() is False
    assert fake_stdin.getvalue() == "APPROVE\n"  # piped input preserved


def test_task_approval_callback_denies_and_explains(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_flush(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes please")
    decide = _approval_callback(preapproved=False)
    request = SimpleNamespace(
        action="run_command",
        task_id="task-1",
        plan=SimpleNamespace(
            summary="Run tests",
            tool_id="commands.run",
            arguments={"executable": "python"},
        ),
        read_roots=(),
    )

    assert decide(request) is False
    out = capsys.readouterr().out
    assert "Decision: DENIED" in out
    assert "--yes" in out  # tells the user how to approve non-interactively


def test_task_approval_callback_accepts_typed_approve(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_flush(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _prompt: "approve")
    decide = _approval_callback(preapproved=False)
    request = SimpleNamespace(
        action="read_external_roots",
        task_id="task-2",
        plan=SimpleNamespace(
            summary="Merge PDFs",
            tool_id="pdf.merge_folders",
            arguments={"output_path": "merged.pdf"},
        ),
        read_roots=(Path("D:/scans"),),
    )

    assert decide(request) is True
    assert "Decision: APPROVED" in capsys.readouterr().out


def test_cli_git_init_denied_prints_exact_yes_command(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PC_AGENT_ALLOW_SUBPROCESS", "1")
    Settings.from_env(tmp_path, environ={}).ensure_directories()
    _no_flush(monkeypatch)
    monkeypatch.setattr(
        "builtins.input", lambda _prompt: "pc-agent run something else"
    )

    assert (
        main(["--root", str(tmp_path), "git", "init", "my-project"]) == 4
    )
    out = capsys.readouterr().out
    assert "Decision: DENIED" in out
    assert "pc-agent git init my-project --yes" in out
    assert "Nothing was executed." in out
    assert not (tmp_path / "workspace" / "my-project").exists()


def _answers(monkeypatch: pytest.MonkeyPatch, *values: str) -> None:
    iterator = iter(values)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(iterator))


def test_empty_answers_reask_then_approve(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_flush(monkeypatch)
    # The leftover newline of a pasted command must not deny instantly.
    _answers(monkeypatch, "", "", "APPROVE")

    assert _confirm_direct_write("Do the thing", preapproved=False) is True
    out = capsys.readouterr().out
    assert "Empty answer - nothing confirmed yet" in out
    assert "Decision: APPROVED" in out


def test_wrong_answer_is_shown_then_approve(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_flush(monkeypatch)
    _answers(monkeypatch, "pc-agent git init my-project --yes", "approve")

    assert _confirm_direct_write("Do the thing", preapproved=False) is True
    out = capsys.readouterr().out
    assert "that is not APPROVE" in out
    assert "Decision: APPROVED" in out


def test_three_wrong_answers_deny(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_flush(monkeypatch)
    _answers(monkeypatch, "yes", "ok", "sure")

    assert (
        _confirm_direct_write(
            "Do the thing",
            preapproved=False,
            retry_hint="pc-agent git init repo --yes",
        )
        is False
    )
    out = capsys.readouterr().out
    assert "Decision: DENIED" in out
    assert "pc-agent git init repo --yes" in out


def test_ctrl_c_cancels_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_flush(monkeypatch)

    def _interrupt(_prompt: str) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", _interrupt)

    assert _confirm_direct_write("Do the thing", preapproved=False) is False
    out = capsys.readouterr().out
    assert "Cancelled with Ctrl+C." in out
    assert "Decision: DENIED" in out


def test_callback_recovers_from_pasted_command_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_flush(monkeypatch)
    _answers(monkeypatch, "pc-agent run something --yes", "APPROVE")
    decide = _approval_callback(preapproved=False)
    request = SimpleNamespace(
        action="run_command",
        task_id="task-3",
        plan=SimpleNamespace(
            summary="Run tests",
            tool_id="commands.run",
            arguments={"executable": "python"},
        ),
        read_roots=(),
    )

    assert decide(request) is True
    assert "Decision: APPROVED" in capsys.readouterr().out


# ----------------------------------------------------------------- key mode


def _key_mode(
    monkeypatch: pytest.MonkeyPatch,
    keys: list[str],
    trailings: tuple[str, ...] = (),
) -> None:
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "_flush_pending_console_input", lambda: False)
    key_iter = iter(keys)
    monkeypatch.setattr(cli, "_read_single_key", lambda: next(key_iter))
    trailing_iter = iter(trailings)
    monkeypatch.setattr(
        cli,
        "_drain_available_keys",
        lambda limit=256: next(trailing_iter, ""),
    )


def test_key_mode_lone_y_approves(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _key_mode(monkeypatch, ["y"])

    assert _confirm_direct_write("Do the thing", preapproved=False) is True
    out = capsys.readouterr().out
    assert "Press Y to approve" in out
    assert "Decision: APPROVED" in out


def test_key_mode_y_with_trailing_enter_approves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Typing "y" then Enter out of habit is still a deliberate approval.
    _key_mode(monkeypatch, ["y"], trailings=("\r\n",))

    assert _confirm_direct_write("Do the thing", preapproved=False) is True


def test_key_mode_pasted_yes_burst_is_ignored(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A pasted "--yes" reaches the reader as 'y' followed by "es...".
    _key_mode(monkeypatch, ["y", "n"], trailings=("es\r\n", ""))

    assert _confirm_direct_write("Do the thing", preapproved=False) is False
    out = capsys.readouterr().out
    assert "looked like pasted text" in out
    assert "Decision: DENIED" in out


def test_key_mode_enter_keys_teach_then_y_approves(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _key_mode(monkeypatch, ["\r", "\n", "Y"])

    assert _confirm_direct_write("Do the thing", preapproved=False) is True
    out = capsys.readouterr().out
    assert "no need to press Enter" in out
    assert "Decision: APPROVED" in out


def test_key_mode_ctrl_c_character_cancels(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _key_mode(monkeypatch, [chr(3)])

    assert _confirm_direct_write("Do the thing", preapproved=False) is False
    out = capsys.readouterr().out
    assert "Cancelled with Ctrl+C." in out
    assert "Decision: DENIED" in out


def test_key_mode_callback_approves_with_y(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _key_mode(monkeypatch, ["y"])
    decide = _approval_callback(preapproved=False)
    request = SimpleNamespace(
        action="run_command",
        task_id="task-9",
        plan=SimpleNamespace(
            summary="Run tests",
            tool_id="commands.run",
            arguments={"executable": "python"},
        ),
        read_roots=(),
    )

    assert decide(request) is True
    assert "Decision: APPROVED" in capsys.readouterr().out
