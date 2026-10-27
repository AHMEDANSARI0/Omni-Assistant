from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pc_agent.cli import main
from pc_agent.errors import StorageError, ToolBuilderError, ToolError
from pc_agent.providers.base import CompletionRequest, CompletionResponse
from pc_agent.providers.router import ModelRouter
from pc_agent.storage.db import Database
from pc_agent.tooling.models import ToolProposal
from pc_agent.tooling.sandbox import invoke_candidate
from pc_agent.tooling.service import ToolBuilderService, build_runtime_registry
from pc_agent.tooling.templates import TEMPLATES
from pc_agent.tooling.validation import prepare_proposal, validate_source
from pc_agent.tools.registry import ToolContext


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "PC_AGENT_HOME",
        "PC_AGENT_ENVIRONMENT",
        "PC_AGENT_WORKSPACE_DIR",
        "PC_AGENT_DATA_DIR",
        "PC_AGENT_DATABASE_PATH",
        "PC_AGENT_PROVIDER_ORDER",
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_COMPATIBLE_API_KEY",
        "OLLAMA_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


class FakeProvider:
    implemented = True

    def __init__(self, contents: list[str]) -> None:
        self.name = "fake"
        self.model = "fake-model"
        self.contents = list(contents)
        self.requests: list[CompletionRequest] = []

    @property
    def configured(self) -> bool:
        return True

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        return CompletionResponse(
            content=self.contents.pop(0), provider=self.name, model=self.model
        )


def make_service(
    tmp_path: Path, *, router: ModelRouter | None = None
) -> tuple[ToolBuilderService, Database]:
    database = Database(tmp_path / "data" / "test.db")
    database.initialize()
    return (
        ToolBuilderService(database, data_dir=tmp_path / "data", model_router=router),
        database,
    )


def proposal_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tool_id": "custom.double_number",
        "name": "Double number",
        "version": "1.0.0",
        "description": "Double one number.",
        "input_schema": {
            "type": "object",
            "properties": {"value": {"type": "number"}},
            "required": ["value"],
        },
        "output_schema": {
            "type": "object",
            "properties": {"value": {"type": "number"}},
            "required": ["value"],
        },
        "source_code": "def run(arguments):\n    return {'value': arguments['value'] * 2}\n",
        "test_cases": [
            {
                "name": "double two",
                "arguments": {"value": 2},
                "expected_output": {"value": 4},
            }
        ],
        "dependencies": [],
        "timeout_seconds": 5,
        "permissions": [],
        "network_required": False,
    }
    data.update(overrides)
    return data


def create_tested(service: ToolBuilderService, data: dict[str, Any]):
    record = service.create_candidate(ToolProposal.from_dict(data), provenance="test")
    return service.test_candidate(record.candidate_key)


def create_approved(service: ToolBuilderService, data: dict[str, Any]):
    tested = create_tested(service, data)
    return service.approve_candidate(tested.candidate_key)


def invoke_runtime(database: Database, tmp_path: Path, tool_id: str, arguments: dict[str, Any]):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    registry = build_runtime_registry(database, data_dir=tmp_path / "data")
    return registry.invoke(tool_id, arguments, ToolContext.for_workspace(workspace))


def test_proposal_cannot_request_permissions_or_network() -> None:
    with pytest.raises(ToolBuilderError, match="pure-compute"):
        ToolProposal.from_dict(proposal_data(permissions=["filesystem.read"]))
    with pytest.raises(ToolBuilderError, match="network"):
        ToolProposal.from_dict(proposal_data(network_required=True))


def test_prepare_rejects_non_custom_builtin_collision_and_bad_dependencies() -> None:
    builtin_ids = {"pdf.merge"}
    with pytest.raises(ToolBuilderError, match="custom"):
        prepare_proposal(
            ToolProposal.from_dict(proposal_data(tool_id="pdf.merge")),
            builtin_tool_ids=builtin_ids,
        )
    with pytest.raises(ToolBuilderError, match="exactly match"):
        prepare_proposal(
            ToolProposal.from_dict(proposal_data(dependencies=["re"])),
            builtin_tool_ids=builtin_ids,
        )
    with pytest.raises(ToolBuilderError, match="pip dependencies"):
        prepare_proposal(
            ToolProposal.from_dict(proposal_data(dependencies=["requests"])),
            builtin_tool_ids=builtin_ids,
        )


def test_schema_definitions_are_strict() -> None:
    missing_items = proposal_data(
        input_schema={
            "type": "object",
            "properties": {"values": {"type": "array"}},
            "required": ["values"],
        }
    )
    with pytest.raises(ToolBuilderError, match="items is required"):
        prepare_proposal(ToolProposal.from_dict(missing_items), builtin_tool_ids=set())

    nullable_object = proposal_data(
        input_schema={
            "type": "object",
            "properties": {
                "nested": {
                    "type": ["object", "null"],
                    "properties": {"value": {"type": "number"}},
                }
            },
            "required": ["nested"],
        }
    )
    with pytest.raises(ToolBuilderError, match="single value 'object'"):
        prepare_proposal(ToolProposal.from_dict(nullable_object), builtin_tool_ids=set())


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ("import os\n\ndef run(arguments):\n    return {}\n", "import_not_allowed"),
        ("def run(arguments):\n    return open('x')\n", "banned_name"),
        ("class Box:\n    pass\n\ndef run(arguments):\n    return {}\n", "banned_construct"),
        ("VALUE = len('x')\n\ndef run(arguments):\n    return {}\n", "top_level_non_constant"),
        (
            "def run(arguments):\n    return {'x': arguments.__class__}\n",
            "private_attribute",
        ),
        (
            "def run(arguments):\n    return {'x': '{}'.format(arguments)}\n",
            "dynamic_format",
        ),
    ],
)
def test_static_validation_blocks_unsafe_constructs(source: str, code: str) -> None:
    report = validate_source(source)
    assert report.passed is False
    assert code in {issue.code for issue in report.issues}


def test_all_offline_templates_pass_sandbox_tests(tmp_path: Path) -> None:
    service, _database = make_service(tmp_path)
    for template_id in TEMPLATES:
        record = service.create_from_template(template_id)
        tested = service.test_candidate(record.candidate_key)
        assert tested.status == "tested", template_id


def test_approval_is_blocked_until_tests_pass(tmp_path: Path) -> None:
    service, _database = make_service(tmp_path)
    record = service.create_candidate(
        ToolProposal.from_dict(proposal_data()), provenance="test"
    )
    with pytest.raises(ToolBuilderError, match="passing tests"):
        service.approve_candidate(record.candidate_key)

    tested = service.test_candidate(record.candidate_key)
    approved = service.approve_candidate(tested.candidate_key)
    assert approved.status == "approved"
    assert approved.enabled is True


def test_failed_expected_output_blocks_approval(tmp_path: Path) -> None:
    service, _database = make_service(tmp_path)
    data = proposal_data(
        test_cases=[
            {
                "name": "wrong expectation",
                "arguments": {"value": 2},
                "expected_output": {"value": 99},
            }
        ]
    )
    tested = create_tested(service, data)
    assert tested.status == "test_failed"
    with pytest.raises(ToolBuilderError, match="passing tests"):
        service.approve_candidate(tested.candidate_key)


def test_static_failure_is_recorded_without_running_sandbox(tmp_path: Path) -> None:
    service, _database = make_service(tmp_path)
    data = proposal_data(
        source_code="def run(arguments):\n    return {'value': open('secret')}\n"
    )
    tested = create_tested(service, data)
    assert tested.status == "test_failed"
    assert tested.test_report is not None
    assert "Static validation failed" in tested.test_report["cases"][0]["error"]


def test_approved_tool_can_be_invoked_disabled_and_reenabled(tmp_path: Path) -> None:
    service, database = make_service(tmp_path)
    create_approved(service, proposal_data())

    result = invoke_runtime(database, tmp_path, "custom.double_number", {"value": 3})
    assert result.success is True
    assert result.output == {"value": 6}

    service.set_enabled("custom.double_number", enabled=False)
    disabled = invoke_runtime(database, tmp_path, "custom.double_number", {"value": 3})
    assert disabled.success is False
    assert "no enabled, approved version" in str(disabled.error)

    service.set_enabled("custom.double_number", enabled=True)
    enabled = invoke_runtime(database, tmp_path, "custom.double_number", {"value": 4})
    assert enabled.success is True
    assert enabled.output == {"value": 8}


def test_new_approved_version_supersedes_old_and_rollback_restores_it(tmp_path: Path) -> None:
    service, database = make_service(tmp_path)
    first = create_approved(service, proposal_data())
    second_data = proposal_data(
        version="1.0.1",
        source_code=(
            "def run(arguments):\n"
            "    return {'value': arguments['value'] + 10}\n"
        ),
        test_cases=[
            {
                "name": "plus ten",
                "arguments": {"value": 2},
                "expected_output": {"value": 12},
            }
        ],
    )
    second = create_approved(service, second_data)

    assert service.get_candidate(first.candidate_key).status == "superseded"
    assert service.get_candidate(second.candidate_key).status == "approved"
    current = invoke_runtime(database, tmp_path, "custom.double_number", {"value": 2})
    assert current.output == {"value": 12}

    rolled_back = service.rollback("custom.double_number")
    assert rolled_back.version == "1.0.0"
    assert service.get_candidate(second.candidate_key).status == "superseded"
    restored = invoke_runtime(database, tmp_path, "custom.double_number", {"value": 2})
    assert restored.output == {"value": 4}
    event_types = {event["event_type"] for event in service.events(first.candidate_key)}
    assert {"approved", "superseded", "rollback_approved"}.issubset(event_types)


def test_provider_generation_uses_strict_json_and_still_requires_tests(tmp_path: Path) -> None:
    provider = FakeProvider([json.dumps(proposal_data())])
    service, _database = make_service(
        tmp_path, router=ModelRouter([provider], backoff_seconds=0)
    )
    record = service.generate_candidate(
        "double a number", tool_id="custom.double_number"
    )
    assert record.provenance == "provider-generated"
    assert record.generator_model == "fake:fake-model"
    assert record.status == "proposed"
    assert service.test_candidate(record.candidate_key).status == "tested"


def test_repair_preserves_contract_and_expected_outputs(tmp_path: Path) -> None:
    broken = proposal_data(
        source_code=(
            "def run(arguments):\n"
            "    return {'value': arguments['value'] + 1}\n"
        )
    )
    repaired = proposal_data(
        version="9.9.9",
        test_cases=[
            {
                "name": "provider tries to weaken the test",
                "arguments": {"value": 2},
                "expected_output": {"value": 999},
            }
        ],
    )
    provider = FakeProvider([json.dumps(repaired)])
    service, _database = make_service(
        tmp_path, router=ModelRouter([provider], backoff_seconds=0)
    )
    failed = create_tested(service, broken)
    assert failed.status == "test_failed"

    repair = service.repair_candidate(
        failed.candidate_key, requirement="double a number"
    )
    assert repair.version == "1.0.1"
    assert repair.repair_attempt == 1
    assert repair.parent_candidate_id == failed.database_id
    assert repair.proposal.test_cases == failed.proposal.test_cases
    assert repair.proposal.output_schema == failed.proposal.output_schema
    assert service.test_candidate(repair.candidate_key).status == "tested"


def test_import_proposal_file(tmp_path: Path) -> None:
    service, _database = make_service(tmp_path)
    path = tmp_path / "proposal.json"
    path.write_text(json.dumps(proposal_data()), encoding="utf-8")
    record = service.import_proposal_file(path)
    assert record.provenance == "import:proposal.json"
    assert record.status == "proposed"


def test_sandbox_timeout_and_second_layer_import_guard(tmp_path: Path) -> None:
    service, _database = make_service(tmp_path)
    infinite = proposal_data(
        tool_id="custom.infinite_loop",
        source_code="def run(arguments):\n    while True:\n        pass\n",
        timeout_seconds=1,
    )
    record = service.create_candidate(
        ToolProposal.from_dict(infinite), provenance="test"
    )
    tested = service.test_candidate(record.candidate_key)
    assert tested.status == "test_failed"
    assert tested.test_report is not None
    assert "Timed out" in tested.test_report["cases"][0]["error"]

    with pytest.raises(ToolError, match="Import 'os' is not allowed"):
        invoke_candidate(
            tool_id="custom.unsafe",
            source="import os\n\ndef run(arguments):\n    return {}\n",
            arguments={},
            timeout_seconds=2,
            data_dir=tmp_path / "data",
        )


def test_stored_source_tampering_is_detected(tmp_path: Path) -> None:
    service, database = make_service(tmp_path)
    record = service.create_from_template("percentage")
    with database.connect() as connection:
        connection.execute(
            "UPDATE dynamic_tool_candidates SET source_code = ? WHERE id = ?",
            (
                "def run(arguments):\n    return {'percentage': 0}\n",
                record.database_id,
            ),
        )
    with pytest.raises(StorageError, match="Integrity check failed"):
        service.get_candidate(record.candidate_key)


def test_search_and_suggest_find_existing_candidates(tmp_path: Path) -> None:
    service, _database = make_service(tmp_path)
    record = service.create_from_template("word_count")
    assert service.search_candidates("count words")[0].candidate_key == record.candidate_key
    suggestion = service.suggest("I need a word count tool")
    assert suggestion["suggested_template"] == "word_count"
    assert suggestion["builtin_matches"] == []
    assert suggestion["dynamic_matches"]


def test_cli_dynamic_tool_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = ["--root", str(tmp_path)]

    assert main([*root, "tool-builder", "suggest", "word", "count", "tool"]) == 0
    assert "Suggested template: word_count" in capsys.readouterr().out

    assert (
        main([*root, "tool-builder", "template", "word_count", "--run-tests"]) == 0
    )
    created = capsys.readouterr().out
    assert "Status: tested" in created

    assert (
        main(
            [
                *root,
                "tool-builder",
                "approve",
                "custom.text_word_count@1.0.0",
            ]
        )
        == 2
    )
    unapproved = capsys.readouterr()
    assert "Approval was NOT recorded" in unapproved.err

    assert (
        main(
            [
                *root,
                "tool-builder",
                "approve",
                "custom.text_word_count@1.0.0",
                "--yes",
            ]
        )
        == 0
    )
    assert "Approved for runtime use" in capsys.readouterr().out

    assert main([*root, "tools", "list"]) == 0
    assert "custom.text_word_count v1.0.0" in capsys.readouterr().out

    assert (
        main(
            [
                *root,
                "tools",
                "invoke",
                "custom.text_word_count",
                "--arguments",
                '{"text": "one two three"}',
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["words"] == 3

    assert main([*root, "tools", "disable", "custom.text_word_count"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                *root,
                "tools",
                "invoke",
                "custom.text_word_count",
                "--arguments",
                '{"text": "one"}',
            ]
        )
        == 1
    )
    assert "no enabled, approved version" in capsys.readouterr().err

    assert main([*root, "tools", "enable", "custom.text_word_count"]) == 0
    assert "Dynamic tool enabled" in capsys.readouterr().out


def test_cli_generate_without_real_provider_fails_honestly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "tool-builder",
                "generate",
                "convert",
                "roman",
                "numerals",
            ]
        )
        == 1
    )
    assert "configured real provider" in capsys.readouterr().err
