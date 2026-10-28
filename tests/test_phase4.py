"""Phase 4 Office, web, desktop and task-reference tests."""

from __future__ import annotations

import io
import json
import logging
import socket
import zipfile
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self

import pytest

from pc_agent import __version__
from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.documents.office import (
    build_docx,
    build_xlsx,
    create_demo_office_files,
    extract_office_text,
    inspect_csv,
    inspect_office,
)
from pc_agent.documents.pdf import create_demo_pdfs
from pc_agent.errors import PlanningError, ToolError
from pc_agent.planning import (
    DeterministicPlanner,
    PlannerOptions,
    PlanningOutcome,
    _validate_ai_plan,
)
from pc_agent.tools import ToolContext, build_default_registry
from pc_agent.web import WebFetchResult, WebLink, fetch_url, validate_url


def _tool_fixture(
    tmp_path: Path, **context_kwargs: Any
) -> tuple[Any, ToolContext, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return (
        build_default_registry(),
        ToolContext.for_workspace(workspace, **context_kwargs),
        workspace,
    )


def _invoke_ok(registry: Any, context: ToolContext, tool_id: str, args: dict[str, Any]) -> dict[str, Any]:
    result = registry.invoke(tool_id, args, context)
    assert result.success, result.error
    assert result.output is not None
    return dict(result.output)


def _write_shared_strings_xlsx(path: Path) -> None:
    workbook = b"""<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
    workbook_rels = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""
    shared = b"""<?xml version="1.0" encoding="UTF-8"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="3" uniqueCount="3">
 <si><t>Name</t></si><si><t>Total</t></si><si><t>Alpha</t></si>
</sst>"""
    sheet = b"""<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
 <sheetData>
  <row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>
  <row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><f>1+2</f><v>3</v></c><c r="C2" t="b"><v>1</v></c></row>
 </sheetData>
</worksheet>"""
    content_types = b"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="xml" ContentType="application/xml"/>
 <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
</Types>"""
    root_rels = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("_rels/.rels", root_rels)
        package.writestr("xl/workbook.xml", workbook)
        package.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        package.writestr("xl/sharedStrings.xml", shared)
        package.writestr("xl/worksheets/sheet1.xml", sheet)


def _write_pptx_fixture(path: Path) -> None:
    # Presentation order intentionally places slide2.xml before slide1.xml.
    presentation = b"""<?xml version="1.0" encoding="UTF-8"?>
<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <p:sldIdLst><p:sldId id="257" r:id="rId2"/><p:sldId id="256" r:id="rId1"/></p:sldIdLst>
</p:presentation>"""
    presentation_rels = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/>
 <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide2.xml"/>
</Relationships>"""

    def slide(title: str, body: str) -> bytes:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
 <p:cSld><p:spTree><p:sp><p:txBody>
  <a:p><a:r><a:t>{title}</a:t></a:r></a:p>
  <a:p><a:r><a:t>{body}</a:t></a:r></a:p>
 </p:txBody></p:sp></p:spTree></p:cSld>
</p:sld>""".encode()

    content_types = b"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="xml" ContentType="application/xml"/>
 <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
</Types>"""
    root_rels = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
</Relationships>"""
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("_rels/.rels", root_rels)
        package.writestr("ppt/presentation.xml", presentation)
        package.writestr("ppt/_rels/presentation.xml.rels", presentation_rels)
        package.writestr("ppt/slides/slide1.xml", slide("Second shown", "File one"))
        package.writestr("ppt/slides/slide2.xml", slide("First shown", "File two"))


def test_office_demo_docx_xlsx_csv_round_trip(tmp_path: Path) -> None:
    create_demo_office_files(tmp_path)
    demo = tmp_path / "Office Demo"

    document = inspect_office(demo / "summary.docx")
    assert document.format == "docx"
    assert document.title == "Sales Summary"
    assert document.unit_name == "paragraphs"
    assert "Gamma leads" in extract_office_text(demo / "summary.docx").text

    workbook = inspect_office(demo / "sales.xlsx")
    assert workbook.format == "xlsx"
    assert workbook.unit_count == 1
    assert "Gamma\t20\t3100" in extract_office_text(demo / "sales.xlsx").text

    csv_info = inspect_office(demo / "sales.csv")
    assert csv_info.format == "csv"
    assert csv_info.unit_name == "rows"
    assert csv_info.unit_count == 3


def test_office_builders_create_parseable_packages(tmp_path: Path) -> None:
    document = tmp_path / "report.docx"
    document.write_bytes(
        build_docx(
            title="Quarterly <Report>",
            paragraphs=["Revenue rose.", "Costs fell."],
        )
    )
    info = inspect_office(document)
    assert info.title == "Quarterly <Report>"
    assert "Revenue rose." in extract_office_text(document).text

    workbook = tmp_path / "book.xlsx"
    workbook.write_bytes(
        build_xlsx([["name", "score"], ["Ayesha", "9"], ["Bilal", "7"]], sheet_name="Scores")
    )
    workbook_info = inspect_office(workbook)
    assert workbook_info.format == "xlsx"
    assert workbook_info.details["sheets"][0]["name"] == "Scores"
    assert "Ayesha\t9" in extract_office_text(workbook).text


def test_xlsx_shared_strings_formula_and_boolean_cells(tmp_path: Path) -> None:
    workbook = tmp_path / "shared.xlsx"
    _write_shared_strings_xlsx(workbook)

    info = inspect_office(workbook)
    text = extract_office_text(workbook).text

    assert info.format == "xlsx"
    assert info.details["sheets"][0]["formulas"] == 1
    assert "Name\tTotal" in text
    assert "Alpha\t3\tTRUE" in text


def test_pptx_uses_presentation_slide_order(tmp_path: Path) -> None:
    deck = tmp_path / "deck.pptx"
    _write_pptx_fixture(deck)

    info = inspect_office(deck)
    text = extract_office_text(deck).text

    assert info.format == "pptx"
    assert info.unit_count == 2
    assert text.index("First shown") < text.index("Second shown")


def test_csv_inspection_statistics_and_malformed_rows(tmp_path: Path) -> None:
    csv_path = tmp_path / "data.csv"
    csv_path.write_text(
        "name;score\nAyesha;10\nBilal;20\nBroken\n", encoding="utf-8-sig"
    )

    result = inspect_csv(csv_path, sample_rows=2)

    assert result.data.columns == ("name", "score")
    assert result.data.delimiter == ";"
    assert result.data.malformed_rows == 1
    assert result.sample_rows[0]["name"] == "Ayesha"
    score = next(item for item in result.column_stats if item.name == "score")
    assert score.minimum == 10
    assert score.maximum == 20
    assert score.mean == 15


def test_office_rejects_macro_and_doctype_packages(tmp_path: Path) -> None:
    macro = tmp_path / "macro.docm"
    macro.write_bytes(b"not allowed")
    with pytest.raises(ToolError, match="Macro-enabled"):
        inspect_office(macro)

    bad_docx = tmp_path / "bad.docx"
    with zipfile.ZipFile(bad_docx, "w") as package:
        package.writestr("[Content_Types].xml", b"<Types/>")
        package.writestr("_rels/.rels", b"<Relationships/>")
        package.writestr(
            "word/document.xml",
            b"<?xml version='1.0'?><!DOCTYPE x [<!ENTITY x 'boom'>]><document/>",
        )
    with pytest.raises(ToolError, match="DOCTYPE"):
        inspect_office(bad_docx)


def test_office_tools_convert_and_never_overwrite(tmp_path: Path) -> None:
    registry, context, workspace = _tool_fixture(tmp_path)
    (workspace / "sales.csv").write_text(
        "product,units\nAlpha,2\nBeta,3\n", encoding="utf-8"
    )
    (workspace / "records.json").write_text(
        json.dumps([{"name": "Ayesha", "active": True, "score": 9}]),
        encoding="utf-8",
    )

    csv_json = _invoke_ok(
        registry,
        context,
        "office.csv_to_json",
        {"input_path": "sales.csv", "output_path": "sales.json"},
    )
    assert csv_json["verified"] is True
    assert json.loads((workspace / "sales.json").read_text(encoding="utf-8"))[0] == {
        "product": "Alpha",
        "units": "2",
    }

    json_csv = _invoke_ok(
        registry,
        context,
        "office.json_to_csv",
        {"input_path": "records.json", "output_path": "records.csv"},
    )
    assert json_csv["columns"] == ["name", "active", "score"]
    assert "Ayesha,true,9" in (workspace / "records.csv").read_text(encoding="utf-8")

    xlsx = _invoke_ok(
        registry,
        context,
        "office.csv_to_xlsx",
        {
            "input_path": "sales.csv",
            "output_path": "sales.xlsx",
            "sheet_name": "Sales",
        },
    )
    assert xlsx["verified"] is True
    assert inspect_office(workspace / "sales.xlsx").format == "xlsx"

    created = _invoke_ok(
        registry,
        context,
        "office.create_docx",
        {
            "output_path": "note.docx",
            "title": "Release note",
            "paragraphs": ["Phase 4 is testable."],
        },
    )
    assert created["verified"] is True
    assert "Phase 4" in extract_office_text(workspace / "note.docx").text

    collision = registry.invoke(
        "office.csv_to_json",
        {"input_path": "sales.csv", "output_path": "sales.json"},
        context,
    )
    assert not collision.success
    assert "never overwrite" in str(collision.error)


def test_office_tool_blocks_workspace_escape(tmp_path: Path) -> None:
    registry, context, workspace = _tool_fixture(tmp_path)
    outside = tmp_path / "outside.csv"
    outside.write_text("a\n1\n", encoding="utf-8")
    (workspace / "inside.csv").write_text("a\n1\n", encoding="utf-8")

    result = registry.invoke(
        "office.inspect", {"path": str(outside)}, context
    )

    assert not result.success
    assert "Policy" in str(result.error)


class _FakeResponse:
    status = 200

    def __init__(self, body: bytes, *, content_type: str, url: str) -> None:
        self._body = io.BytesIO(body)
        self._url = url
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def geturl(self) -> str:
        return self._url

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)


def _public_resolver(host: str, port: int, **_kwargs: Any) -> list[Any]:
    assert host
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def test_web_url_validation_blocks_unsafe_targets() -> None:
    assert validate_url(
        "https://Example.COM/a?x=1#ignored", resolver=_public_resolver
    ).url == "https://example.com/a?x=1"

    unsafe = [
        "file:///etc/passwd",
        "https://user:secret@example.com/",
        "http://127.0.0.1/",
        "http://10.0.0.5/",
        "https://example.com:8443/",
    ]
    for url in unsafe:
        with pytest.raises(ToolError):
            validate_url(url, resolver=_public_resolver)

    private = validate_url(
        "http://127.0.0.1:8765/test",
        allow_private_network=True,
        resolver=_public_resolver,
    )
    assert private.port == 8765


def test_web_fetch_extracts_html_without_scripts() -> None:
    body = b"""<html><head><title>Example Page</title><style>.x{}</style></head>
<body><script>alert(1)</script><h1>Hello</h1><p>Safe text.</p>
<a href=\"/next\">Next page</a></body></html>"""

    result = fetch_url(
        "https://example.com/start",
        resolver=_public_resolver,
        opener=lambda _request, timeout: _FakeResponse(
            body, content_type="text/html; charset=utf-8", url="https://example.com/start"
        ),
    )

    assert result.title == "Example Page"
    assert "Hello" in result.text
    assert "alert" not in result.text
    assert result.links[0].url == "https://example.com/next"
    assert not result.truncated


def test_web_fetch_caps_body_and_rejects_binary_content() -> None:
    large = fetch_url(
        "https://example.com/large",
        max_bytes=1024,
        resolver=_public_resolver,
        opener=lambda _request, timeout: _FakeResponse(
            b"x" * 4096,
            content_type="text/plain; charset=utf-8",
            url="https://example.com/large",
        ),
    )
    assert large.size_bytes == 1024
    assert large.truncated

    with pytest.raises(ToolError, match="only accepts"):
        fetch_url(
            "https://example.com/file",
            resolver=_public_resolver,
            opener=lambda _request, timeout: _FakeResponse(
                b"binary",
                content_type="application/octet-stream",
                url="https://example.com/file",
            ),
        )


def test_web_tool_is_denied_without_direct_command_context(tmp_path: Path) -> None:
    registry, context, _workspace = _tool_fixture(tmp_path)

    result = registry.invoke(
        "web.fetch", {"url": "https://example.com/"}, context
    )

    assert not result.success
    assert "Policy approval_required" in str(result.error)
    assert "pc-agent web fetch" in str(result.error)


def test_desktop_tools_require_direct_context_and_block_executables(
    tmp_path: Path,
) -> None:
    opened: list[str] = []
    registry, default_context, workspace = _tool_fixture(
        tmp_path, desktop_opener=opened.append
    )
    (workspace / "note.txt").write_text("hello", encoding="utf-8")
    (workspace / "run.exe").write_bytes(b"not run")

    denied = registry.invoke(
        "desktop.open_path", {"path": "note.txt"}, default_context
    )
    assert not denied.success
    assert not opened

    direct_context = ToolContext.for_workspace(
        workspace, desktop_open_allowed=True, desktop_opener=opened.append
    )
    opened_result = _invoke_ok(
        registry, direct_context, "desktop.open_path", {"path": "note.txt"}
    )
    assert opened_result == {"target": "note.txt", "kind": "file", "opened": True}
    assert opened == [str(workspace / "note.txt")]

    executable = registry.invoke(
        "desktop.open_path", {"path": "run.exe"}, direct_context
    )
    assert not executable.success
    assert "cannot be opened" in str(executable.error)

    info = _invoke_ok(registry, direct_context, "desktop.system_info", {})
    assert info["agent_version"] == __version__
    assert info["workspace"] == str(workspace)
    assert int(info["cpu_count"]) >= 1


def test_deterministic_planner_supports_office_and_system_info(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sales.csv").write_text("a\n1\n", encoding="utf-8")
    planner = DeterministicPlanner(workspace)

    conversion = planner.plan(
        "Convert CSV to JSON",
        PlannerOptions(target_path="sales.csv", output_path="sales.json"),
    )
    assert conversion is not None and conversion.plan is not None
    assert conversion.plan.tool_id == "office.csv_to_json"

    inspection = planner.plan(
        "Analyze this CSV", PlannerOptions(target_path="sales.csv")
    )
    assert inspection is not None and inspection.plan is not None
    assert inspection.plan.tool_id == "office.csv_inspect"

    system = planner.plan("Show system information")
    assert system is not None and system.plan is not None
    assert system.plan.tool_id == "desktop.system_info"


def test_ai_plan_allow_list_accepts_office_but_rejects_web_desktop_open() -> None:
    response = SimpleNamespace(provider="fake", model="planner")
    office_plan = _validate_ai_plan(
        json.dumps(
            {
                "intent": "office.csv_to_json",
                "tool_id": "office.csv_to_json",
                "arguments": {
                    "input_path": "data.csv",
                    "output_path": "data.json",
                },
                "expected_pages": None,
                "summary": "Convert a CSV.",
                "missing_information": [],
            }
        ),
        response,
    )
    assert not isinstance(office_plan, PlanningOutcome)
    assert office_plan.tool_id == "office.csv_to_json"

    with pytest.raises(PlanningError, match="unsupported intent"):
        _validate_ai_plan(
            json.dumps(
                {
                    "intent": "web.fetch",
                    "tool_id": "web.fetch",
                    "arguments": {"url": "https://example.com/"},
                    "expected_pages": None,
                    "summary": "Fetch a page.",
                    "missing_information": [],
                }
            ),
            response,
        )


def test_cli_office_demo_and_direct_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = ["--root", str(tmp_path)]
    assert main([*root, "demo", "office"]) == 0
    assert "Demo Office data" in capsys.readouterr().out

    assert main([*root, "office", "csv-inspect", "Office Demo/sales.csv"]) == 0
    inspected = capsys.readouterr().out
    assert "Rows: 3" in inspected
    assert "Column statistics:" in inspected

    assert (
        main(
            [
                *root,
                "office",
                "csv-to-json",
                "Office Demo/sales.csv",
                "--output",
                "Office Demo/sales-converted.json",
            ]
        )
        == 0
    )
    assert "Verified: True" in capsys.readouterr().out
    assert (tmp_path / "workspace/Office Demo/sales-converted.json").is_file()

    assert main([*root, "desktop", "info"]) == 0
    assert "Workspace disk:" in capsys.readouterr().out


def test_cli_orchestrated_office_conversion_and_collision_recovery(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = ["--root", str(tmp_path)]
    assert main([*root, "demo", "office"]) == 0
    capsys.readouterr()
    output = tmp_path / "workspace" / "Office Demo" / "task-sales.json"
    output.write_text("preserve me", encoding="utf-8")

    assert (
        main(
            [
                *root,
                "run",
                "Convert CSV to JSON",
                "--target",
                "Office Demo/sales.csv",
                "--output",
                "Office Demo/task-sales.json",
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "succeeded"
    assert report["recovered"] is True
    assert report["plan"]["tool_id"] == "office.csv_to_json"
    assert report["tool_result"]["output"]["output_path"].endswith(
        "task-sales-recovered.json"
    )
    assert output.read_text(encoding="utf-8") == "preserve me"


def test_cli_web_fetch_uses_direct_context_and_saves_new_text(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pc_agent.tools import web_tools

    def fake_fetch(url: str, **_kwargs: Any) -> WebFetchResult:
        return WebFetchResult(
            requested_url=url,
            final_url=url,
            status=200,
            content_type="text/html",
            title="Example",
            text="Fetched safely.",
            links=(WebLink(url="https://example.com/next", text="Next"),),
            size_bytes=42,
            body_sha256="a" * 64,
            truncated=False,
        )

    monkeypatch.setattr(web_tools, "fetch_url", fake_fetch)
    root = ["--root", str(tmp_path)]
    assert (
        main(
            [
                *root,
                "web",
                "fetch",
                "https://example.com/",
                "--output",
                "example.txt",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "Web text saved: example.txt" in out
    assert (tmp_path / "workspace/example.txt").read_text(
        encoding="utf-8"
    ).startswith("Fetched safely.")


def test_failed_task_alias_exact_commands_and_status_filter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = ["--root", str(tmp_path)]
    assert main([*root, "demo", "pdf"]) == 0
    capsys.readouterr()
    (tmp_path / "workspace" / "merged.pdf").write_bytes(b"existing")

    assert (
        main(
            [
                *root,
                "run",
                "Merge PDFs from Folder A and Folder B",
                "--output",
                "merged.pdf",
                "--no-recovery",
            ]
        )
        == 1
    )
    failed_capture = capsys.readouterr()
    task_line = next(
        line for line in failed_capture.out.splitlines() if line.startswith("Task: ")
    )
    task_id = task_line.removeprefix("Task: ").strip()
    assert f"pc-agent task diagnose {task_id}" in failed_capture.out
    assert f"pc-agent task recover {task_id}" in failed_capture.out

    assert main([*root, "task", "list", "--status", "failed"]) == 0
    assert task_id in capsys.readouterr().out

    assert main([*root, "task", "diagnose", "latest-failed", "--json"]) == 0
    diagnosed = capsys.readouterr()
    assert json.loads(diagnosed.out)["task_id"] == task_id
    assert f"Resolved latest-failed -> {task_id}" in diagnosed.err

    assert main([*root, "task", "recover", "latest-failed"]) == 0
    recovered = capsys.readouterr()
    assert "Recovered: True" in recovered.out


def test_corrupt_pdf_skip_reason_without_pypdf_warning_noise(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    create_demo_pdfs(workspace)
    registry = build_default_registry()
    context = ToolContext.for_workspace(workspace)
    logging.getLogger("pypdf").setLevel(logging.NOTSET)
    caplog.clear()

    result = registry.invoke(
        "pdf.merge_folders",
        {
            "input_folders": ["Folder A", "Folder B"],
            "output_path": "combined.pdf",
            "recursive": False,
        },
        context,
    )

    assert result.success, result.error
    assert result.output is not None
    assert result.output["skipped_files"]
    assert "Could not read PDF" in result.output["skipped_files"][0]["reason"]
    assert "invalid pdf header" not in caplog.text.lower()
    assert "invalid pdf header" not in capsys.readouterr().err.lower()


def test_phase4_version_and_web_settings_validation(tmp_path: Path) -> None:
    assert __version__ == "0.18.1"
    settings = Settings.from_env(
        tmp_path,
        environ={
            "PC_AGENT_WEB_TIMEOUT_SECONDS": "20",
            "PC_AGENT_WEB_MAX_BYTES": "123456",
        },
    )
    assert settings.web_timeout_seconds == 20
    assert settings.web_max_bytes == 123456

    with pytest.raises(Exception, match="PC_AGENT_WEB_TIMEOUT_SECONDS"):
        Settings.from_env(
            tmp_path, environ={"PC_AGENT_WEB_TIMEOUT_SECONDS": "999"}
        )
    with pytest.raises(Exception, match="PC_AGENT_WEB_MAX_BYTES"):
        Settings.from_env(
            tmp_path, environ={"PC_AGENT_WEB_MAX_BYTES": "10"}
        )
