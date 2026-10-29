from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pc_agent.errors import ToolError
from pc_agent.tools import ToolContext, ToolRegistry, ToolSpec, ToolStatus, build_default_registry


def _echo_spec(version: str = "1.0.0", status: ToolStatus = ToolStatus.APPROVED) -> ToolSpec:
    return ToolSpec(
        id="test.echo",
        name="Echo",
        version=version,
        description="Echo a message for registry tests.",
        input_schema={
            "type": "object",
            "properties": {"message": {"type": "string", "minLength": 1}},
            "required": ["message"],
        },
        output_schema={
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
        permissions=(),
        status=status,
        provenance="test",
    )


@pytest.fixture()
def context(tmp_path: Path) -> ToolContext:
    return ToolContext.for_workspace(tmp_path)


def test_default_registry_contains_expected_tools() -> None:
    ids = {tool.spec.id for tool in build_default_registry().list_tools()}

    assert ids == {
        "workspace.list_files",
        "workspace.read_text",
        "workspace.write_text",
        "pdf.inspect",
        "pdf.verify",
        "pdf.merge",
        "pdf.merge_folders",
        "office.inspect",
        "office.extract_text",
        "office.csv_inspect",
        "office.csv_to_json",
        "office.json_to_csv",
        "office.create_docx",
        "office.csv_to_xlsx",
        "web.fetch",
        "desktop.system_info",
        "desktop.open_path",
        "desktop.open_url",
        "files.copy",
        "files.move",
        "files.rename",
        "files.organize",
        "files.search",
        "pdf.extract_pages",
        "pdf.split",
        "pdf.extract_text",
        "pdf.extract_tables",
        "pdf.inspect_form",
        "pdf.fill_form",
        "pdf.ocr",
        "pdf.search_text",
        "pdf.reorder_pages",
        "pdf.rotate_pages",
        "office.docx_replace_text",
        "office.docx_append_paragraphs",
        "office.xlsx_set_cells",
        "office.xlsx_verify_formulas",
        "git.status",
        "git.log",
        "git.diff_summary",
        "git.init",
        "git.add",
        "git.commit",
        "commands.run",
        "browser.status",
        "browser.open",
        "browser.snapshot",
        "browser.click",
        "browser.type",
        "browser.screenshot",
        "desktop.windows",
        "desktop.window_inspect",
        "desktop.window_focus",
        "desktop.window_click",
        "desktop.window_type",
        "desktop.send_keys",
        "desktop.screenshot",
        "image.inspect",
        "image.convert",
        "image.transform",
        "image.compare",
        "image.analyze",
    }


def test_invoke_validates_and_returns_output(context: ToolContext) -> None:
    registry = ToolRegistry()
    registry.register(
        _echo_spec(), lambda arguments, _context: {"message": arguments["message"].upper()}
    )

    result = registry.invoke("test.echo", {"message": "hello"}, context)

    assert result.success
    assert result.output == {"message": "HELLO"}


def test_unknown_and_missing_arguments_are_rejected(context: ToolContext) -> None:
    registry = ToolRegistry()
    registry.register(_echo_spec(), lambda arguments, _context: {"message": "x"})

    missing = registry.invoke("test.echo", {}, context)
    unknown = registry.invoke("test.echo", {"message": "x", "surprise": True}, context)

    assert not missing.success and "missing required" in (missing.error or "")
    assert not unknown.success and "unknown field" in (unknown.error or "")


def test_latest_approved_version_is_selected(context: ToolContext) -> None:
    registry = ToolRegistry()
    registry.register(_echo_spec("1.0.0"), lambda arguments, _context: {"message": "v1"})
    registry.register(_echo_spec("2.0.0"), lambda arguments, _context: {"message": "v2"})

    result = registry.invoke("test.echo", {"message": "x"}, context)

    assert result.version == "2.0.0"
    assert result.output == {"message": "v2"}


def test_duplicate_registration_is_rejected() -> None:
    registry = ToolRegistry()
    handler: Any = lambda arguments, _context: {"message": "x"}
    registry.register(_echo_spec(), handler)

    with pytest.raises(ToolError):
        registry.register(_echo_spec(), handler)


def test_disabled_or_experimental_tools_cannot_run(context: ToolContext) -> None:
    handler: Any = lambda arguments, _context: {"message": "x"}
    disabled_registry = ToolRegistry()
    disabled_registry.register(_echo_spec(), handler)
    disabled_registry.set_enabled("test.echo", False)

    experimental_registry = ToolRegistry()
    experimental_registry.register(_echo_spec(status=ToolStatus.EXPERIMENTAL), handler)

    assert not disabled_registry.invoke("test.echo", {"message": "x"}, context).success
    assert not experimental_registry.invoke("test.echo", {"message": "x"}, context).success


def test_unexpected_handler_error_becomes_structured_failure(context: ToolContext) -> None:
    registry = ToolRegistry()

    def explode(_arguments: dict[str, Any], _context: ToolContext) -> dict[str, Any]:
        raise RuntimeError("internal detail")

    registry.register(_echo_spec(), explode)

    result = registry.invoke("test.echo", {"message": "x"}, context)

    assert not result.success
    assert result.error is not None and "Unexpected tool error" in result.error
