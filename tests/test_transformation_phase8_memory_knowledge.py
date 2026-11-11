"""Transformation Phase 8: Memory v2, knowledge base, embeddings, RAG."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from pc_agent import __version__
from pc_agent.api_server import LocalApiServer
from pc_agent.baseline import TRANSFORMATION_LABEL, TRANSFORMATION_PHASE
from pc_agent.cli import main
from pc_agent.config import Settings
from pc_agent.embeddings import (
    HashingEmbedder,
    build_embedder,
    cosine_similarity,
    keyword_overlap,
    pack_vector,
    tokenize,
    unpack_vector,
)
from pc_agent.errors import ConfigError, KnowledgeError, ProviderError
from pc_agent.gui.controller import GuiController
from pc_agent.knowledge import KnowledgeService, chunk_text, supported_suffixes
from pc_agent.providers.mock import MockProvider
from pc_agent.providers.router import ModelRouter
from pc_agent.safety import worker_eligible
from pc_agent.storage.db import LATEST_SCHEMA_VERSION, Database
from pc_agent.storage.repositories import MemoryRepository
from pc_agent.tools import build_default_registry


def _service(tmp_path: Path, **kwargs) -> tuple[Settings, Database, KnowledgeService]:
    settings = Settings.from_env(tmp_path)
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    return settings, database, KnowledgeService(settings, database, **kwargs)


def _write_text_pdf(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    try:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject()
        font.update(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        fonts = DictionaryObject()
        fonts[NameObject("/F1")] = writer._add_object(font)
        resources = DictionaryObject()
        resources[NameObject("/Font")] = fonts
        page[NameObject("/Resources")] = resources
        operations = []
        y = 720
        for line in lines:
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            operations.append(f"BT /F1 14 Tf 72 {y} Td ({escaped}) Tj ET")
            y -= 24
        stream = DecodedStreamObject()
        stream.set_data("\n".join(operations).encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
        with path.open("wb") as handle:
            writer.write(handle)
    finally:
        writer.close()


def test_phase8_identity():
    assert TRANSFORMATION_PHASE == 18
    assert TRANSFORMATION_LABEL == "TRANSFORMATION PHASE 18 Production Hardening"
    assert __version__ == "0.82.17"
    assert LATEST_SCHEMA_VERSION == 18


def test_hashing_embedder_deterministic_stemming_and_packing():
    embedder = HashingEmbedder()
    assert embedder.model == "local-hashing-384"
    assert embedder.local is True
    first = embedder.embed(["Visitors parking near the gates"])[0]
    again = embedder.embed(["Visitors parking near the gates"])[0]
    assert first == again  # deterministic
    values = unpack_vector(first)
    assert len(values) == 384
    assert pack_vector(values) == first
    assert cosine_similarity(values, values) == pytest.approx(1.0)
    # Light stemming: inflectional variants share tokens.
    assert set(tokenize("Visitors were parking")) == {"visitor", "were", "park"}
    paraphrase = embedder.embed(["where can a visitor park"])[0]
    assert cosine_similarity(values, unpack_vector(paraphrase)) > 0
    assert keyword_overlap("visitor park", "Visitors parking") > 0


def test_build_embedder_defaults_to_offline_local(tmp_path):
    settings = Settings.from_env(tmp_path)
    embedder = build_embedder(settings)
    assert isinstance(embedder, HashingEmbedder)
    mocked = build_embedder(replace(settings, embedding_provider="mock"))
    assert isinstance(mocked, HashingEmbedder)


def test_embedding_config_validation(tmp_path):
    with pytest.raises(ConfigError):
        Settings.from_env(tmp_path, environ={"PC_AGENT_EMBEDDING_PROVIDER": "magic"})
    with pytest.raises(ConfigError):
        Settings.from_env(tmp_path, environ={"PC_AGENT_EMBEDDING_DIMENSIONS": "32"})
    with pytest.raises(ConfigError):
        Settings.from_env(tmp_path, environ={"PC_AGENT_EMBEDDING_DIMENSIONS": "9999"})
    settings = Settings.from_env(
        tmp_path,
        environ={
            "PC_AGENT_EMBEDDING_PROVIDER": "local",
            "PC_AGENT_EMBEDDING_DIMENSIONS": "256",
        },
    )
    assert settings.embedding_provider == "local"
    assert settings.embedding_dimensions == 256


def test_chunk_text_bounds():
    text = "\n\n".join(f"Paragraph {index} " + "word " * 60 for index in range(8))
    chunks = chunk_text(text, max_chars=300, overlap=40)
    assert len(chunks) > 3
    assert all(len(chunk) <= 300 for chunk in chunks)
    assert chunk_text("  short text  ") == ["short text"]
    assert chunk_text("   ") == []


def test_ingest_markdown_dedupe_search_and_remove(tmp_path):
    _, _, service = _service(tmp_path)
    guide = tmp_path / "guide.md"
    guide.write_text(
        "# Release Guide\n\nThe October release ships after the full suite "
        "passes and the rollback bundle is verified.\n",
        encoding="utf-8",
    )
    other = tmp_path / "notes.txt"
    other.write_text("Buy milk and call the plumber about the kitchen sink.\n", encoding="utf-8")

    first = service.ingest_file(guide)
    assert first["already_ingested"] is False
    assert first["chunk_count"] >= 1
    assert first["embedding_model"] == "local-hashing-384"
    again = service.ingest_file(guide)
    assert again["already_ingested"] is True
    assert again["id"] == first["id"]
    service.ingest_file(other)

    found = service.search("when does the october release ship")
    assert found["mode"] == "vector+keyword"
    assert found["results"][0]["title"] == "Release Guide"

    documents = service.list_documents()
    assert documents["total"] == 2
    removed = service.remove_document(first["id"])
    assert removed["removed"] == first["id"]
    assert service.list_documents()["total"] == 1
    with pytest.raises(KnowledgeError):
        service.remove_document(first["id"])


def test_ingest_pdf_requires_embedded_text(tmp_path):
    _, _, service = _service(tmp_path)
    pdf = tmp_path / "manual.pdf"
    _write_text_pdf(pdf, ["Battery Manual", "Charge the battery overnight before first use."])
    result = service.ingest_file(pdf)
    assert result["kind"] == "pdf"
    found = service.search("how long should the battery charge")
    assert found["results"] and found["results"][0]["document_id"] == result["id"]

    blank = tmp_path / "blank.pdf"
    writer = PdfWriter()
    try:
        writer.add_blank_page(width=612, height=792)
        with blank.open("wb") as handle:
            writer.write(handle)
    finally:
        writer.close()
    with pytest.raises(KnowledgeError, match="never.*OCR|no embedded text"):
        service.ingest_file(blank)

    mystery = tmp_path / "mystery.xyz"
    mystery.write_text("some bytes", encoding="utf-8")
    with pytest.raises(KnowledgeError, match="Unsupported file type"):
        service.ingest_file(mystery)


def test_memory_v2_embedding_search_invalidation_and_backfill(tmp_path):
    _, database, service = _service(tmp_path)
    memory = MemoryRepository(database)
    memory.upsert(
        kind="note",
        key="wifi",
        title="Office wifi",
        content={"text": "The office wifi password hint is on the whiteboard."},
        tags="office network",
    )
    memory.upsert(
        kind="note",
        key="lunch",
        title="Lunch",
        content={"text": "Team lunch is at the bakery on Fridays."},
    )
    assert service.backfill_memory_embeddings() == 2
    assert service.backfill_memory_embeddings() == 0

    found = service.search_memories("wifi whiteboard")
    assert found["results"][0]["key"] == "wifi"
    assert found["results"][0]["tags"] == "office network"

    # A content change invalidates the stored vector until re-embedded.
    memory.upsert(
        kind="note",
        key="wifi",
        title="Office wifi",
        content={"text": "Wifi moved to the new router in the server room."},
    )
    record = memory.get("note", "wifi")
    assert record is not None and record.embedding_model is None
    assert service.embed_memory("note", "wifi") is True
    record = memory.get("note", "wifi")
    assert record is not None and record.embedding_model == "local-hashing-384"

    stats = service.stats()
    assert stats["memories"] == {"note": 2, "workflow": 0}
    assert stats["embedded_notes"] == 2
    assert stats["embedding_local"] is True
    assert set(supported_suffixes()) >= {".md", ".pdf", ".docx"}


class _CountingRouter:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request):  # pragma: no cover - must never be called here
        self.calls += 1
        raise AssertionError("provider must not be called without sources")


def test_ask_grounded_with_sources_and_no_source_skips_provider(tmp_path):
    router = ModelRouter([MockProvider()], max_retries=0)
    _, _, service = _service(tmp_path, router=router)
    service.ingest_text(
        title="Deploy Notes",
        text="Deploys happen on Monday at 9am after the full test suite passes.",
    )
    answered = service.ask("when do deploys happen")
    assert answered["answer"]
    assert answered["provider"] == "mock"
    assert answered["sources"] and answered["sources"][0]["title"] == "Deploy Notes"

    counting = _CountingRouter()
    _, _, quiet = _service(tmp_path / "empty", router=counting)
    result = quiet.ask("zzzzqqqq unmatched nonsense")
    assert result["answer"] is None
    assert result["sources"] == []
    assert "nothing was sent to a provider" in result["note"]
    assert counting.calls == 0


class _FailingEmbedder:
    model = "local-hashing-384"
    local = False

    def embed(self, texts):
        raise ProviderError("embedding backend is down")


def test_search_falls_back_to_keyword_when_embeddings_fail(tmp_path):
    settings, database, service = _service(tmp_path)
    service.ingest_text(
        title="Rollback Plan",
        text="If the deploy fails, restore the previous bundle and database backup.",
    )
    degraded = KnowledgeService(settings, database, embedder=_FailingEmbedder())
    found = degraded.search("restore the database backup")
    assert found["mode"] == "keyword-fallback"
    assert found["results"] and found["results"][0]["title"] == "Rollback Plan"


def test_knowledge_tools_registered_and_worker_excluded():
    tools = build_default_registry().list_tools()
    ids = {tool.spec.id for tool in tools}
    assert len(tools) == 69
    assert {"knowledge.ingest", "knowledge.search"} <= ids
    # Knowledge tools need the parent-side service factory: never in the worker.
    assert worker_eligible("knowledge.ingest") is False
    assert worker_eligible("knowledge.search") is False
    assert worker_eligible("files.list") is True


def test_cli_knowledge_and_memory_search(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "deploy.md").write_text(
        "# Deploy Notes\n\nDeploy on Monday at 9am after the full suite passes.\n",
        encoding="utf-8",
    )
    assert main(["--root", str(tmp_path), "knowledge", "ingest", "workspace/deploy.md"]) == 0
    assert "Document ingested" in capsys.readouterr().out

    assert main(["--root", str(tmp_path), "knowledge", "search", "when do we deploy"]) == 0
    out = capsys.readouterr().out
    assert "[S1]" in out and "Deploy Notes" in out

    assert main(
        [
            "--root", str(tmp_path), "memory", "note-set", "parking",
            "Visitor parking is behind gate B", "--tags", "office",
        ]
    ) == 0
    assert main(["--root", str(tmp_path), "memory", "search", "where do visitors park"]) == 0
    assert "parking" in capsys.readouterr().out

    assert main(["--root", str(tmp_path), "knowledge", "stats"]) == 0
    assert "local-hashing-384" in capsys.readouterr().out

    assert main(["--root", str(tmp_path), "doctor"]) == 0
    assert "Knowledge: documents=1" in capsys.readouterr().out


def test_service_and_api_knowledge_flow(tmp_path):
    settings = replace(
        Settings.from_env(tmp_path), api_port=0, scheduler_interval_seconds=0
    )
    server = LocalApiServer(settings)
    server.start()
    try:
        host, port = server.address
        token = server.token

        def call(method, path, body=None, auth=True):
            data = json.dumps(body).encode() if body is not None else None
            request = urllib.request.Request(
                f"http://{host}:{port}{path}", data=data, method=method
            )
            if auth:
                request.add_header("Authorization", "Bearer " + token)
            if data is not None:
                request.add_header("Content-Type", "application/json")
            return urllib.request.urlopen(request, timeout=30)

        with call(
            "POST",
            "/v1/knowledge/ingest",
            {"title": "VPN Guide", "text": "Connect to the VPN before opening internal dashboards."},
        ) as response:
            assert response.status == 201
            document = json.loads(response.read())["data"]
        assert document["title"] == "VPN Guide"

        # API path ingestion is workspace-contained.
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            call("POST", "/v1/knowledge/ingest", {"path": "../outside.txt"})
        assert excinfo.value.code in (400, 422)

        with call("GET", "/v1/knowledge/search?q=internal%20dashboards") as response:
            found = json.loads(response.read())["data"]
        assert found["results"] and found["results"][0]["title"] == "VPN Guide"

        with call("POST", "/v1/knowledge/ask", {"question": "how do I open internal dashboards"}) as response:
            answered = json.loads(response.read())["data"]
        assert answered["answer"] and answered["sources"]

        with call("GET", "/v1/knowledge/stats") as response:
            stats = json.loads(response.read())["data"]["knowledge"]
        assert stats["documents"] == 1
        assert stats["active_embedding_model"] == "local-hashing-384"

        with call("GET", "/v1/memory/search?q=vpn") as response:
            memories = json.loads(response.read())["data"]
        assert memories["results"] == []

        with call("DELETE", f"/v1/knowledge/documents/{document['id']}") as response:
            removed = json.loads(response.read())["data"]
        assert removed["removed"] == document["id"]

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            call("GET", "/v1/knowledge/documents", auth=False)
        assert excinfo.value.code == 401
    finally:
        server.stop()


def test_diagnostics_release_and_gui_knowledge(tmp_path):
    settings, database, service = _service(tmp_path)
    service.ingest_text(title="Runbook", text="Restart the worker if jobs stall.")

    from pc_agent.diagnostics import build_diagnostics
    from pc_agent.release import audit_install

    report = build_diagnostics(settings, database)
    knowledge = report["knowledge"]
    assert knowledge["stats"]["documents"] == 1
    assert knowledge["stats"]["active_embedding_model"] == "local-hashing-384"
    assert knowledge["recent_documents"][0]["title"] == "Runbook"

    audit = audit_install(settings)
    checks = {check.name: check for check in audit.checks}
    assert len(audit.checks) == 34
    assert checks["knowledge_base"].status == "pass"

    controller = GuiController(settings, database=database)
    try:
        documents = controller.knowledge_documents()
        assert len(documents) == 1
        assert "Runbook" in controller.knowledge_search_text("worker jobs stall")
        assert "documents=1" in controller.knowledge_stats_text()
        answered = controller.knowledge_ask_text("what if worker jobs stall")
        assert "[S1]" in answered or "Runbook" in answered
    finally:
        orchestrator = getattr(controller, "_orchestrator", None)
        if orchestrator is not None:
            orchestrator._safety.close()


def test_web_ui_knowledge_panel():
    from pc_agent.web_ui import INDEX_HTML

    assert "/v1/knowledge/ingest" in INDEX_HTML
    assert "/v1/knowledge/search" in INDEX_HTML
    assert "/v1/knowledge/ask" in INDEX_HTML
    assert "/v1/knowledge/stats" in INDEX_HTML
    assert "kb-ingest" in INDEX_HTML
