# Transformation Phase 7 — Provider Platform v2

## 1. Existing findings

- Six adapters (mock, Gemini Interactions, OpenRouter/OpenAI/generic
  OpenAI-compatible Chat Completions, Ollama, Anthropic Messages) sat
  behind one `ModelRouter` with fixed configured fallback order,
  bounded retries, and per-attempt timing — but no streaming, no
  capability metadata beyond `supports_vision`, no usage metering, no
  cost model, and no way to evaluate a route's behaviour.
- Provider-reported `usage` was passed through raw and discarded by
  callers; budgets used characters/4 estimates per task only.

## 2. What changed

### Streaming
- Adapters opt in with `supports_streaming` + `stream_complete()`:
  mock (chunked), OpenAI-compatible (Chat Completions SSE), Ollama
  (NDJSON), Anthropic (Messages SSE). Gemini's Interactions streaming
  contract is deliberately **not** implemented yet, so its capability
  reports `streaming: false` instead of guessing a wire format.
- `UrllibStreamTransport` shares the JSON transport's URL/TLS
  validation and total-size cap; pure line parsers
  (`parse_openai_sse_lines`, `parse_ollama_stream_lines`,
  `parse_anthropic_sse_lines`) are unit-tested with injected lines.
- `ModelRouter.stream_complete()` falls back only before the first
  delta; a mid-stream failure cannot silently restart on another
  provider. Surfaces: `pc-agent ask --stream`,
  `POST /v1/providers/stream` (NDJSON `start/delta/done/error`),
  web Command Center **Stream Ask**. Streaming calls are plain model
  calls — no tools, files, or approvals are reachable through them.

### Capabilities
- `capability_catalog()` reports per provider: model, configured,
  in-route, local (mock/Ollama/loopback base URL), streaming, vision,
  context tokens, and price. Context sizes and prices exist **only**
  when configured (`PC_AGENT_MODEL_CONTEXT_TOKENS`,
  `PC_AGENT_PROVIDER_PRICES` as JSON maps keyed by `provider` or
  `provider/model`) — nothing is inferred from model names.

### Strategy routing
- `PC_AGENT_PROVIDER_STRATEGY` ∈ `default` (unchanged behaviour),
  `local-first`, `fastest` (measured median latency from local usage
  analytics; unmeasured providers keep configured relative order),
  `cheapest` (ascending user-configured per-1k prices; unpriced last
  in configured order). Ordering is stable and explainable via
  `pc-agent providers route` / `GET /v1/providers/capabilities`.

### Analytics
- Every routed attempt (success, failure, streamed, partial) is
  metered through a router observer into an append-only local
  JSONL file (`<data-dir>/provider-usage.jsonl`, rotated at 4 MB).
  Stored: timestamp, provider, model, success, latency, token counts
  (provider-reported when present, else a clearly labelled estimate),
  estimated cost when priced, json-mode/image flags, redacted error.
  **No prompt or response text is stored.** Metering can never break
  a completion and can be disabled (`PC_AGENT_PROVIDER_ANALYTICS=0`).
- Aggregates (calls, success rate, median/p95 latency, token sums,
  priced cost sums) surface in `providers usage`,
  `GET /v1/providers/usage`, diagnostics, doctor, the web console,
  and the Tkinter Providers tab.

### Evaluations
- Four built-in smoke cases (keyword echo, non-empty reply, JSON
  object, planner-shaped JSON with an `intent` key) run against the
  configured route via `providers evaluate`,
  `POST /v1/providers/evaluations/run`, or the web/GUI buttons.
  Reports record per-case pass/fail, latency, provider/model, and
  are saved to `<data-dir>/provider-eval-latest.json`. With only the
  offline mock they verify wiring, not model quality — the report
  says so.

### Other surfaces
- Release audit gains `provider_platform` (21 checks); doctor prints
  a `Provider platform:` line; diagnostics gain a
  `provider_platform` section; `/v1/providers` is enriched
  backward-compatibly (strategy, effective route, capability flags).
- No schema change (still v9); no new tool authority; task planning
  still uses non-streaming completions through the same router, so
  approvals, budgets, and verification are untouched.

## 3. What was intentionally reused

- The existing adapters, router retry/fallback, budget token
  estimators (same characters/4 convention), Phase 3 service/API,
  and Phase 5 UI shells.

## 4. Deferred / honest limits

- Gemini streaming (wire contract unverified), provider-side token
  counts for streams (streams use labelled estimates), streaming
  inside task planning (planner needs complete JSON to validate —
  streaming to the user is a chat-surface feature only).
- Live-provider streaming/latency/cost behaviour is **UNVERIFIED**
  until a real key exercises it; parsers are fixture-tested offline.
- Quality-scored evals (judge models, golden datasets) belong with
  Memory/RAG evaluation in Phase 8+.
