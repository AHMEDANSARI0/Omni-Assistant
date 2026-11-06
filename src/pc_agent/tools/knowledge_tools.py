"""Knowledge base tools (Transformation Phase 8).

``knowledge.ingest`` reads one policy-approved workspace file into the
local knowledge base; ``knowledge.search`` runs local semantic/hybrid
search over ingested chunks and saved memory notes. Neither tool can
send anything anywhere: ingestion is read-then-local-write, search is
read-only, and grounded answer generation stays an explicit
CLI/API/UI action so document text is never shipped to a provider by
a background plan step.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import KnowledgeError, ToolError
from ..policies.policy import Action, ActionRequest
from .registry import ToolContext, ToolHandler, ToolRegistry, ToolSpec
from .workspace_tools import relative_posix


def _service(context: ToolContext) -> Any:
    if context.knowledge_service_factory is None:
        raise ToolError(
            "Knowledge tools are not available in this runtime context."
        )
    return context.knowledge_service_factory()


def _resolve_readable(context: ToolContext, raw_path: str) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = context.workspace_dir / candidate
    target = candidate.resolve()
    decision = context.policy_engine.evaluate(
        ActionRequest(Action.READ_FILE, target)
    )
    if not decision.allowed:
        raise ToolError(f"Policy {decision.outcome.value}: {decision.reason}")
    return target


def _ingest(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    raw_path = str(arguments["path"])
    target = _resolve_readable(context, raw_path)
    service = _service(context)
    try:
        document = service.ingest_file(target)
    except KnowledgeError as exc:
        raise ToolError(str(exc)) from exc
    return {
        "path": relative_posix(context, target),
        "document_id": document["id"],
        "title": document["title"],
        "kind": document["kind"],
        "chunk_count": document["chunk_count"],
        "already_ingested": bool(document.get("already_ingested")),
        "embedding_model": document["embedding_model"],
    }


def _search(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    query = str(arguments["query"]).strip()
    if not query:
        raise ToolError("query must not be empty.")
    limit = int(arguments.get("limit", 5))
    if limit < 1 or limit > 25:
        raise ToolError("limit must be between 1 and 25.")
    service = _service(context)
    try:
        payload = service.search(query, limit=limit)
    except KnowledgeError as exc:
        raise ToolError(str(exc)) from exc
    return {
        "query": payload["query"],
        "mode": payload["mode"],
        "embedding_model": payload["embedding_model"],
        "results": [
            {
                "source": item["source"],
                "title": item["title"],
                "score": item["score"],
                "snippet": item["snippet"],
                "document_id": int(item.get("document_id") or 0),
            }
            for item in payload["results"]
        ],
        "count": len(payload["results"]),
    }


def _specs() -> list[tuple[ToolSpec, ToolHandler]]:
    return [
        (
            ToolSpec(
                id="knowledge.ingest",
                name="Ingest file into knowledge base",
                version="1.0.0",
                description=(
                    "Ingest one workspace text/PDF/Office file into the local "
                    "knowledge base for later semantic search. Local only."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "minLength": 1},
                    },
                    "required": ["path"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "document_id": {"type": "integer"},
                        "title": {"type": "string"},
                        "kind": {"type": "string"},
                        "chunk_count": {"type": "integer"},
                        "already_ingested": {"type": "boolean"},
                        "embedding_model": {"type": "string"},
                    },
                    "required": [
                        "path",
                        "document_id",
                        "title",
                        "kind",
                        "chunk_count",
                        "already_ingested",
                        "embedding_model",
                    ],
                },
                permissions=(Action.READ_FILE,),
                timeout_seconds=120.0,
            ),
            _ingest,
        ),
        (
            ToolSpec(
                id="knowledge.search",
                name="Search knowledge base and memory",
                version="1.0.0",
                description=(
                    "Semantic/hybrid search over ingested document chunks and "
                    "saved memory notes. Read-only and fully local."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1},
                        "limit": {"type": "integer"},
                    },
                    "required": ["query"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "mode": {"type": "string"},
                        "embedding_model": {"type": "string"},
                        "results": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "source": {"type": "string"},
                                    "title": {"type": "string"},
                                    "score": {"type": "number"},
                                    "snippet": {"type": "string"},
                                    "document_id": {"type": "integer"},
                                },
                                "required": [
                                    "source",
                                    "title",
                                    "score",
                                    "snippet",
                                ],
                            },
                        },
                        "count": {"type": "integer"},
                    },
                    "required": ["query", "mode", "results", "count"],
                },
                permissions=(),
                timeout_seconds=60.0,
            ),
            _search,
        ),
    ]


def register_knowledge_tools(registry: ToolRegistry) -> None:
    for spec, handler in _specs():
        registry.register(spec, handler)
