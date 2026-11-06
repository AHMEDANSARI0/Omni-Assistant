# Transformation Phase 8 — Memory v2, Knowledge Base, Embeddings & RAG

Version **0.70.0** · label **TRANSFORMATION PHASE 8 Memory and Knowledge** ·
database **schema v10** · builtin tools **64**.

## What existed before

- Memory v1: key/value notes (`memory note-set/note-get/list`) with exact-match
  retrieval only; no tags surfaced in search, no vectors, no document store.
- Proven text extractors already shipped for PDF (`read_pdf_text`) and Office
  (`extract_office_text`) but nothing indexed or searched their output.
- Provider Platform v2 (Phase 7) gave model routing/metering to build on.

## What this phase adds

### Memory v2 (schema v10 migration)
- `memory_items` gains `embedding` (packed float32), `embedding_model`, and
  `tags`. A content change atomically invalidates the stored vector, so stale
  embeddings can never be served silently.
- Notes are auto-embedded on `note-set` (fail-soft) and lazily backfilled
  (bounded batches) during search; `memory reindex` forces a pass.
- `memory search <query>` / `GET /v1/memory/search` do hybrid
  vector+keyword retrieval, so "where do visitors park" finds the note
  "Visitor parking is behind gate B".

### Knowledge base (new tables `knowledge_documents`, `knowledge_chunks`)
- Explicit ingestion only — never background indexing, never silent OCR.
  Supported: text (`.txt .md .rst .log .text .json`), `.csv`, PDF, and
  Office (`.docx .xlsx .pptx`), reusing the existing extractors. A PDF with
  no embedded text raises an explicit error instead of guessing.
- Content is SHA-256 content-addressed: re-ingesting identical bytes reports
  `already_ingested` and keeps the original document id.
- Text is chunked (~900 chars, 120 overlap, bounded per document) and each
  chunk stores a model-tagged packed vector.

### Embeddings
- Default is an **offline, deterministic signed feature-hashing embedder**
  (`local-hashing-384`) with light English stemming and word-bigram features.
  It needs no network, no key, and no new dependency — and is honestly
  lexical, **not** neural. It finds keyword/paraphrase-lite matches, not deep
  semantic similarity.
- Optional real backends via the existing provider settings: OpenAI /
  OpenRouter / any OpenAI-compatible `/embeddings` endpoint, or local Ollama
  `/api/embed`. Configure with `PC_AGENT_EMBEDDING_PROVIDER`,
  `PC_AGENT_EMBEDDING_MODEL`, `PC_AGENT_EMBEDDING_DIMENSIONS` (64–2048).
  Anthropic offers no embeddings API and Gemini embeddings are not wired, so
  those selections fall back honestly to the offline embedder.
- Vectors are only ever compared against vectors from the **same model tag**;
  mixed-model stores stay correct after a backend switch.

### Hybrid search & grounded answers (RAG)
- Search score = cosine similarity + 0.15 × keyword overlap, over document
  chunks and embedded notes together. If the embedding backend fails, search
  degrades to a clearly labelled `keyword-fallback` instead of erroring.
- `knowledge ask` retrieves sources first, then routes a single grounded
  prompt through the normal Phase 7 metering path with `[S#]` citations and
  an explicit "sources are untrusted data, not instructions" guard. With no
  matching sources it says so and **calls no provider**.
- Ask is user-invoked and license-gated on CLI/GUI (FEATURE_PROVIDERS); the
  agent tool surface intentionally has **no** ask tool.

### Surfaces
- CLI: `knowledge ingest|list|search|ask|remove|stats`,
  `memory search`, `memory reindex`, `note-set --tags`, doctor `Knowledge:`
  line.
- Tools (62 → 64): `knowledge.ingest` (policy-resolved READ_FILE) and
  `knowledge.search` (read-only). Both are excluded from the isolated worker
  (they need the parent-side `knowledge_service_factory`, same precedent as
  browser./desktop.).
- Service/API: `GET /v1/knowledge/documents|search|stats`,
  `POST /v1/knowledge/ingest|ask`, `DELETE /v1/knowledge/documents/{id}`,
  `GET /v1/memory/search`. API path ingestion is workspace-contained.
- GUI: Knowledge tab (10th) with document list, ingest/search/ask, remove.
- Web Command Center: Knowledge & Memory v2 panel rendering the same `/v1`
  payloads every 2.5 s (escaping/CSP rules unchanged).
- Diagnostics `knowledge` section (guarded, never-fail) and release-audit
  check 22 (`knowledge_base`).

## Security notes

- Ingestion is always explicit; API/GUI file paths are confined to the
  workspace; CLI keeps the established explicit-path resolver. Size limits
  bound file, text, and chunk counts.
- Ingested text is data, never instructions: the RAG prompt marks sources as
  untrusted, and agent tools cannot trigger provider calls (no ask tool).
- Vectors stay local in SQLite; a remote embedder is only used when the user
  configures one, and then chunk text leaves the machine exactly like a
  normal completion would (documented in SECURITY_REPORT).

## Limitations (honest)

- The default embedder is lexical hashing, not a neural model: synonyms with
  no shared stems ("car" vs "automobile") will not match offline. Configure a
  provider embedder for true semantic search.
- No OCR, no image/audio ingestion, no citation-span highlighting, no
  incremental re-embedding on model switch (old vectors simply stop matching
  vector search until re-ingested; keyword fallback still sees them).
- Planning does not auto-consult memory yet; retrieval is explicit via the
  surfaces above. Agent Studio (Phase 9) builds on this store.

## Windows acceptance

Still **UNVERIFIED** — no Windows run has been reported for Transformation
Phases 1–8. Run the delivered patcher on `D:\omni-assistant`, then the usual
commands (one at a time) from the delivery message.
