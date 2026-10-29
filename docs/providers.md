# AI providers - through Phase 27

## Implemented adapters

| Slot | Implementation | Credential |
|---|---|---|
| `mock` | Offline deterministic mock | none |
| `gemini` | Gemini Interactions REST (`store: false`) | `GEMINI_API_KEY` |
| `openrouter` | OpenAI-compatible Chat Completions | `OPENROUTER_API_KEY` |
| `openai` | OpenAI-compatible Chat Completions | `OPENAI_API_KEY` |
| `openai-compatible` | Generic configurable compatible endpoint | optional `OPENAI_COMPATIBLE_API_KEY` |
| `ollama` | Ollama `/api/chat` (local by default) | optional `OLLAMA_API_KEY` |
| `anthropic` | Anthropic Messages API (version `2026-10-21`) | `ANTHROPIC_API_KEY` |

Run `pc-agent providers list` to see the exact route on a machine. It never
prints keys and makes no network call. Phase 16 also shows each key's
non-secret source: `environment`, `windows-credential-manager` or
`missing`. Phase 19 adds the direct Anthropic Messages API adapter. System
text is sent as top-level blocks, `max_tokens` is always bounded, and the
adapter never emits `temperature`, `top_p` or `top_k` because API version
`2026-10-21` removed those fields. It supports the same inline-image path as
the other vision providers and returns Anthropic usage to the Phase 17
budget tracker. See `docs/phase19.md`.

## Credential storage (Phase 16)

Provider keys resolve in this order:

1. environment variable or local `.env`;
2. Windows Credential Manager when no environment value exists;
3. missing.

Use `pc-agent credentials status|list|set|delete|migrate` to manage the
Windows generic credentials. `credentials set` reads the key twice through
a hidden prompt and deliberately has no command-line key argument. Set
`PC_AGENT_CREDENTIAL_STORE=env` to disable OS lookup, `windows` to select
the Windows backend explicitly, or leave the default `auto`. See
`docs/phase16.md` for the migration sequence and the same-Windows-user
security boundary.

## Task budgets (Phase 17)

Provider calls made for AI planning or approved image/PDF-page vision
inside an orchestrated task pass through a budgeted router. One logical routed
completion consumes one provider-call budget unit; retries and fallback
attempts are counted separately in the report. Before sending, the tracker
reserves estimated input plus the request's maximum output tokens. Reported
provider usage is preferred afterward; otherwise text is estimated at about
four characters/token and each inline image receives a 1,024-token reserve.
Defaults are 20,000 tokens and 10 logical calls per task, configurable with
`PC_AGENT_TASK_TOKEN_BUDGET` / `PC_AGENT_TASK_PROVIDER_CALL_BUDGET` or the
matching execution flags (`0` means unlimited). `pc-agent ask` and Dynamic
Tool Builder generation are direct user actions, not orchestrated tasks, so
they do not consume a task budget. See `docs/phase17.md`.

Phase 18 added no provider change. Phase 19 adds only the direct Anthropic
route; production audit and diagnostics continue to use the same
no-network provider-status surface, reporting models and credential-source
labels without key values. Phase 20 makes no provider change, except that
AI planning may now propose read-only `pdf.search_text`; the new PDF
writers remain deterministic/direct. Phase 21 makes no provider, routing
or credential change; signed offline updates are local administration and
never call a provider. Phase 22 reuses the existing vision-capable route
for PDF OCR: selected pages are rendered locally, embedded-text pages are
reused by default, and each remaining page becomes one bounded JPEG vision
request after the default-off gate and exact approval. No new provider or
credential source is added. Phase 23 PDF table extraction is local
embedded-text geometry analysis and makes no provider call. Phase 24 adds
no provider, routing or credential change: its optional HTTPS contact is
only the operator-configured update feed origin, and neither the feed nor
the downloaded bundle/signature is sent to an AI provider. Phase 25 PDF
form inspection/filling is local deterministic AcroForm processing and
makes no provider call; AI planning cannot propose either form tool. Phase
26 PDF password security is direct-only local processing: hidden password
prompts and PDF bytes are never sent to a provider, and no AI planner can
request or receive a PDF password. Phase 27 PDF signature inspection is
also local direct-only CMS/certificate analysis; signed PDF bytes and
certificate material are never sent to a provider. See
`docs/phase18.md`, `docs/phase19.md`, `docs/phase20.md`, `docs/phase21.md`,
`docs/phase22.md`, `docs/phase23.md`, `docs/phase24.md`,
`docs/phase25.md`, `docs/phase26.md` and `docs/phase27.md`.

## Routing and retries

- Route order comes only from `PC_AGENT_PROVIDER_ORDER`.
- A provider without its required key/model is skipped when the route is
  built.
- Transient provider errors are retried only up to `PC_AGENT_MAX_RETRIES`
  (maximum 5) with bounded backoff.
- After a provider fails, the router moves to the next configured provider.
- It never silently sends data to a provider that is absent from your route.
- If no usable provider remains, the offline mock is added so local flows can
  report their state honestly.
- `pc-agent ask "..."` uses this route. Add `--no-fallback` to test only the
  first usable provider.

## When does the agent call a provider?

| Command | External provider call? |
|---|---|
| `providers list`, `credentials status/list`, `doctor`, GUI Dashboard/Providers refresh | No |
| Supported deterministic `run` task (CLI or GUI Tasks tab) | No |
| Direct `office`, `web fetch` or `desktop` command, or the matching GUI tab | No provider call; web fetch contacts only the exact requested site |
| Automatic recovery retry | No; local deterministic rules only |
| `task recover` without `--use-ai` | No |
| `run --use-ai` when deterministic planning recognizes the task | No; deterministic wins |
| `run --use-ai` for an unrecognized task | Yes, planning only |
| `ask "..."` or the GUI Providers tab **Ask Provider** button | Yes, unless only mock is usable |
| `pdf ocr`, GUI **OCR Scanned PDF**, or a deterministic OCR task | Yes for each selected page needing OCR; embedded-text pages are reused locally by default |
| `tool-builder suggest/template/import/test/approve` | No |
| `tool-builder generate` / `repair` | Yes; a configured real provider is required, mock output is rejected |
| `update online-check|online-download|online-stage` or GUI online update buttons | No provider call; HTTPS contacts only the configured update-feed origin after the explicit action |

## AI planning safety

Provider planning returns JSON, but that output is untrusted. Local code
accepts only:

- `pdf.merge_folders`, `pdf.merge`, `pdf.verify`, `pdf.inspect`,
  `pdf.search_text`,
  `workspace.list_files`, `office.inspect`, `office.csv_inspect`,
  `office.csv_to_json`, `office.json_to_csv`, `office.csv_to_xlsx`, and
  `desktop.system_info`;
- known argument names and value types;
- workspace-relative paths with no absolute path or `..`;
- a matching intent/tool pair.

An AI plan cannot add tools, approve external folders, fetch a URL, open a
desktop target, use a shell, send an image/PDF page for OCR, delete or overwrite files, access credentials,
or bypass registry schemas / policy checks at execution time. Unclear plans
become `needs_input`, not guesses.

## AI tool-generation safety

`tool-builder generate` and `repair` ask the configured route for one strict
JSON proposal. The response is untrusted data, not executable authority.
Local code fixes the requested `custom.*` id, rejects permissions/network and
unknown fields, validates schemas themselves, checks Python AST/imports,
stores an immutable proposed version, runs exact tests in a restricted
subprocess, and still requires the user's `approve --yes`. Repairs are capped
at three per chain and cannot replace the parent schemas or expected outputs.

The local sandbox subprocess receives a minimal environment, so provider API
keys are not passed to candidate code. Provider generation itself sends the
user's tool requirement to the selected provider; do not include secrets or
private file contents in that requirement.

## Privacy and HTTP safety

- Keys are read only from environment/local `.env` or the Phase 16
  Windows Credential Manager fallback, and are excluded from summaries and
  normal CLI/GUI output.
- A provider receives the prompt/instruction needed for `ask` or optional AI
  planning. It does not receive file contents or your API keys for other
  providers.
- Base URLs must use HTTPS unless they point to localhost/loopback. Provider
  HTTPS uses the same verified system + Windows-store + Certifi TLS context
  as web fetch; certificate verification is never disabled.
- Provider responses are size-capped, timeouts are bounded, and provider
  errors are redacted before display/storage.
- Gemini requests set `store: false`; this build does not use provider-side
  conversation state.

## Model overrides

Set these in `.env` if your provider account uses different model ids:

```dotenv
PC_AGENT_GEMINI_MODEL=gemini-3.8-flash
PC_AGENT_OPENROUTER_MODEL=openrouter/auto
PC_AGENT_OPENAI_MODEL=gpt-5-mini
PC_AGENT_OPENAI_COMPATIBLE_MODEL=
PC_AGENT_OLLAMA_MODEL=llama3.1
PC_AGENT_ANTHROPIC_MODEL=claude-sonnet-5-5
```

Model availability/pricing belongs to the provider/account, not this local
software. A provider returning “model not found” should be fixed by changing
the model variable, not by editing source code.
