"""Phase 26: direct-only PDF password protection and unprotection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    ByteStringObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    RectangleObject,
    TextStringObject,
)

import pc_agent.cli as cli_module
from pc_agent import __version__
from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.documents.pdf import write_blank_pdf
from pc_agent.documents.pdf_security import (
    inspect_pdf_security,
    protect_pdf,
    unprotect_pdf,
)
from pc_agent.errors import PcAgentError, ToolError
from pc_agent.gui.controller import GuiController
from pc_agent.release import release_candidate_files
from pc_agent.storage.db import LATEST_SCHEMA_VERSION
from pc_agent.tools import build_default_registry

USER_PASSWORD = "Correct-Horse-42!"
OWNER_PASSWORD = "Different-Owner-97!"
NEW_USER_PASSWORD = "Replacement-User-12!"
NEW_OWNER_PASSWORD = "Replacement-Owner-34!"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_signature_field_pdf(path: Path, *, signed: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    try:
        page = writer.add_blank_page(width=300, height=200)
        field = DictionaryObject()
        field.update(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Widget"),
                NameObject("/Rect"): RectangleObject([20, 20, 220, 60]),
                NameObject("/F"): NumberObject(4),
                NameObject("/FT"): NameObject("/Sig"),
                NameObject("/T"): TextStringObject("Signature"),
            }
        )
        if signed:
            signature = DictionaryObject()
            signature.update(
                {
                    NameObject("/Filter"): NameObject("/Adobe.PPKLite"),
                    NameObject("/ByteRange"): ArrayObject(
                        [
                            NumberObject(0),
                            NumberObject(1),
                            NumberObject(2),
                            NumberObject(3),
                        ]
                    ),
                    NameObject("/Contents"): ByteStringObject(b"signature"),
                }
            )
            field[NameObject("/V")] = writer._add_object(signature)
        reference = writer._add_object(field)
        page[NameObject("/Annots")] = ArrayObject([reference])
        acro_form = DictionaryObject()
        acro_form[NameObject("/Fields")] = ArrayObject([reference])
        writer._root_object.update(
            {NameObject("/AcroForm"): writer._add_object(acro_form)}
        )
        with path.open("wb") as handle:
            writer.write(handle)
    finally:
        writer.close()


def _fake_getpass(monkeypatch: pytest.MonkeyPatch, values: list[str]) -> list[str]:
    prompts: list[str] = []
    remaining = list(values)

    def fake(prompt: str = "Password: ", stream: object = None) -> str:
        del stream
        prompts.append(prompt)
        if not remaining:
            raise AssertionError("Unexpected extra PDF password prompt")
        return remaining.pop(0)

    monkeypatch.setattr(cli_module.getpass, "getpass", fake)
    return prompts


def test_pdf_password_core_round_trip_and_rejections(tmp_path: Path) -> None:
    source = tmp_path / "plain.pdf"
    write_blank_pdf(source, pages=2)
    source_hash = _sha256(source)
    plain = inspect_pdf_security(source)
    assert plain.encrypted is False and plain.pages == 2 and plain.unlocked

    locked_path = tmp_path / "locked.pdf"
    protected = protect_pdf(
        source,
        locked_path,
        user_password=USER_PASSWORD,
        owner_password=OWNER_PASSWORD,
        permissions=("print",),
    )
    assert _sha256(source) == source_hash
    assert protected.algorithm == "AES-256"
    assert protected.permissions == ("print",)
    assert USER_PASSWORD not in repr(protected)
    assert OWNER_PASSWORD not in repr(protected)

    locked = inspect_pdf_security(locked_path)
    assert locked.encrypted and locked.pages is None and not locked.unlocked
    assert locked.algorithm == "AES-256" and locked.key_bits == 256
    assert locked.handler_version == 5 and locked.handler_revision == 6
    assert locked.permissions == ("print",)

    as_user = inspect_pdf_security(locked_path, password=USER_PASSWORD)
    assert as_user.password_accepted and as_user.password_type == "user"
    assert as_user.pages == 2 and as_user.permissions_valid is True
    as_owner = inspect_pdf_security(locked_path, password=OWNER_PASSWORD)
    assert as_owner.password_accepted and as_owner.password_type == "owner"
    wrong = inspect_pdf_security(locked_path, password="not-the-password")
    assert wrong.password_accepted is False and wrong.pages is None

    unlocked_path = tmp_path / "unlocked.pdf"
    assert not unprotect_pdf(locked_path, unlocked_path, password=OWNER_PASSWORD).encrypted
    assert inspect_pdf_security(unlocked_path).pages == 2

    relocked_path = tmp_path / "relocked.pdf"
    protect_pdf(
        locked_path,
        relocked_path,
        current_password=USER_PASSWORD,
        user_password=NEW_USER_PASSWORD,
        owner_password=NEW_OWNER_PASSWORD,
    )
    assert inspect_pdf_security(relocked_path, password=USER_PASSWORD).password_accepted is False
    assert inspect_pdf_security(relocked_path, password=NEW_USER_PASSWORD).password_accepted

    rejected = tmp_path / "rejected.pdf"
    with pytest.raises(ToolError, match="never overwrite"):
        protect_pdf(source, locked_path, user_password=USER_PASSWORD, owner_password=OWNER_PASSWORD)
    with pytest.raises(ToolError, match="at least 8"):
        protect_pdf(source, rejected, user_password="short", owner_password=OWNER_PASSWORD)
    with pytest.raises(ToolError, match="must be different"):
        protect_pdf(source, rejected, user_password=USER_PASSWORD, owner_password=USER_PASSWORD)
    with pytest.raises(ToolError, match="Unknown PDF permission"):
        protect_pdf(
            source,
            rejected,
            user_password=USER_PASSWORD,
            owner_password=OWNER_PASSWORD,
            permissions=("everything",),
        )
    with pytest.raises(ToolError, match="current password is required"):
        protect_pdf(
            locked_path,
            rejected,
            user_password=NEW_USER_PASSWORD,
            owner_password=NEW_OWNER_PASSWORD,
        )
    with pytest.raises(ToolError, match="Incorrect PDF password"):
        unprotect_pdf(locked_path, rejected, password="wrong-password")
    with pytest.raises(ToolError, match="not encrypted"):
        unprotect_pdf(source, rejected, password=USER_PASSWORD)
    assert not rejected.exists()

    blank_signature_path = tmp_path / "blank-signature-field.pdf"
    _write_signature_field_pdf(blank_signature_path)
    blank_signature_output = tmp_path / "blank-signature-locked.pdf"
    protect_pdf(
        blank_signature_path,
        blank_signature_output,
        user_password=USER_PASSWORD,
        owner_password=OWNER_PASSWORD,
    )
    assert inspect_pdf_security(blank_signature_output).encrypted

    signed_path = tmp_path / "signed.pdf"
    _write_signature_field_pdf(signed_path, signed=True)
    with pytest.raises(ToolError, match="Digitally signed PDFs are refused"):
        protect_pdf(
            signed_path,
            rejected,
            user_password=USER_PASSWORD,
            owner_password=OWNER_PASSWORD,
        )



def test_pdf_password_empty_user_password(tmp_path: Path) -> None:
    source = tmp_path / "plain.pdf"
    write_blank_pdf(source, pages=1)
    restricted = tmp_path / "restricted.pdf"
    writer = PdfWriter(clone_from=str(source))
    try:
        writer.encrypt("", OWNER_PASSWORD, algorithm="AES-256")
        with restricted.open("wb") as handle:
            writer.write(handle)
    finally:
        writer.close()

    info = inspect_pdf_security(restricted, password="")
    assert info.encrypted and info.password_accepted
    assert info.password_type == "user" and info.pages == 1
    output = tmp_path / "opened.pdf"
    unprotect_pdf(restricted, output, password="")
    assert inspect_pdf_security(output).encrypted is False


def test_pdf_passwords_have_no_registered_tool_surface() -> None:
    tools = build_default_registry().list_tools()
    ids = {tool.spec.id for tool in tools}
    assert len(ids) == 62
    assert not {
        "direct.pdf_security",
        "direct.pdf_protect",
        "direct.pdf_unprotect",
    } & ids
    assert not any("password" in tool_id for tool_id in ids)
    for tool in tools:
        assert '"password"' not in json.dumps(dict(tool.spec.input_schema)).lower()

    with pytest.raises(SystemExit) as raised:
        main(
            [
                "pdf",
                "protect",
                "plain.pdf",
                "--output",
                "locked.pdf",
                "--password",
                USER_PASSWORD,
            ]
        )
    assert raised.value.code == 2


def test_pdf_password_direct_cli_hidden_prompts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_blank_pdf(tmp_path / "workspace" / "plain.pdf", pages=1)
    root = ["--root", str(tmp_path)]
    prompts = _fake_getpass(
        monkeypatch,
        [USER_PASSWORD, USER_PASSWORD, OWNER_PASSWORD, OWNER_PASSWORD],
    )
    assert main(
        [
            *root,
            "pdf",
            "protect",
            "plain.pdf",
            "--output",
            "locked.pdf",
            "--permission",
            "print",
        ]
    ) == 0
    captured = capsys.readouterr()
    assert "Protected PDF: locked.pdf" in captured.out
    assert USER_PASSWORD not in captured.out + captured.err
    assert OWNER_PASSWORD not in captured.out + captured.err
    assert len(prompts) == 4

    _fake_getpass(monkeypatch, [])
    assert main([*root, "pdf", "security", "locked.pdf"]) == 0
    text = capsys.readouterr().out
    assert "Encrypted: True" in text and "Algorithm: AES-256" in text

    _fake_getpass(monkeypatch, [USER_PASSWORD])
    assert main([*root, "pdf", "security", "locked.pdf", "--unlock", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] and payload["output"]["password_type"] == "user"
    assert USER_PASSWORD not in json.dumps(payload)

    _fake_getpass(monkeypatch, ["wrong-password"])
    assert main([*root, "pdf", "security", "locked.pdf", "--unlock", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert not payload["success"]
    assert payload["output"]["password_accepted"] is False

    prompts = _fake_getpass(
        monkeypatch,
        [
            OWNER_PASSWORD,
            NEW_USER_PASSWORD,
            NEW_USER_PASSWORD,
            NEW_OWNER_PASSWORD,
            NEW_OWNER_PASSWORD,
        ],
    )
    assert main(
        [*root, "pdf", "protect", "locked.pdf", "--output", "relocked.pdf", "--json"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] and payload["output"]["algorithm"] == "AES-256"
    assert len(prompts) == 5
    for password in (
        USER_PASSWORD,
        OWNER_PASSWORD,
        NEW_USER_PASSWORD,
        NEW_OWNER_PASSWORD,
    ):
        assert password not in json.dumps(payload)

    _fake_getpass(monkeypatch, [NEW_OWNER_PASSWORD])
    assert main(
        [*root, "pdf", "unprotect", "relocked.pdf", "--output", "opened.pdf"]
    ) == 0
    captured = capsys.readouterr()
    assert "Unprotected PDF: opened.pdf" in captured.out
    assert NEW_OWNER_PASSWORD not in captured.out + captured.err

    with pytest.raises(PcAgentError, match="must stay inside the workspace"):
        cli_module._workspace_pdf_path(
            Settings.from_env(tmp_path, environ={}),
            "../outside.pdf",
            must_exist=False,
        )



def test_pdf_password_gui_controller_direct_methods(tmp_path: Path) -> None:
    settings = Settings.from_env(tmp_path, environ={})
    settings.ensure_directories()
    write_blank_pdf(settings.workspace_dir / "plain.pdf", pages=1)
    controller = GuiController(settings)

    protected = controller.pdf_protect(
        "plain.pdf",
        "locked.pdf",
        user_password=USER_PASSWORD,
        owner_password=OWNER_PASSWORD,
    )
    assert protected.success, protected.error
    assert protected.tool_id == "direct.pdf_protect"
    assert protected.output is not None
    assert protected.output["algorithm"] == "AES-256"
    assert USER_PASSWORD not in repr(protected)
    assert OWNER_PASSWORD not in repr(protected)

    wrong = controller.pdf_security("locked.pdf", password="wrong-password")
    assert not wrong.success and wrong.error == "Incorrect PDF password."
    assert wrong.output is not None
    assert wrong.output["password_accepted"] is False

    unprotected = controller.pdf_unprotect(
        "locked.pdf", "opened.pdf", password=OWNER_PASSWORD
    )
    assert unprotected.success, unprotected.error

    with pytest.raises(PcAgentError, match="must stay inside the workspace"):
        controller.pdf_security("../outside.pdf")

    summary = controller.doctor_summary()
    assert summary.version == __version__ == "0.29.0"
    assert summary.builtin_tools == 62
    assert "PHASE 27 PDF Signature Inspection" in summary.to_text()


def test_phase26_scope_docs_schema_and_release_candidates() -> None:
    project_root = Path(__file__).resolve().parents[1]
    candidates = {path.as_posix() for path in release_candidate_files(project_root)}
    ids = {item.spec.id for item in build_default_registry().list_tools()}
    pyproject = (project_root / "pyproject.toml").read_text(encoding="utf-8")

    assert __version__ == "0.29.0"
    assert LATEST_SCHEMA_VERSION == 6
    assert len(ids) == 62
    assert "pypdf[crypto]" in pyproject
    assert "src/pc_agent/documents/pdf_security.py" in candidates
    assert "docs/phase26.md" in candidates
    assert "tests/test_phase26_pdf_passwords.py" in candidates

    phase_doc = (project_root / "docs" / "phase26.md").read_text(encoding="utf-8")
    lowered = phase_doc.lower()
    assert "no registered tool" in lowered
    assert "hidden prompt" in lowered
    assert "aes-256" in lowered
    assert "never overwrites" in lowered


