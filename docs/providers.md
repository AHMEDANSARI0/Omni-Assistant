# AI providers - through Phase 5

## Implemented adapters

| Slot | Implementation | Credential |
|---|---|---|
| `mock` | Offline deterministic mock | none |
| `gemini` | Gemini Interactions REST (`store: false`) | `GEMINI_API_KEY` |
| `openrouter` | OpenAI-compatible Chat Completions | `OPENROUTER_API_KEY` |
| `openai` | OpenAI-compatible Chat Completions | `OPENAI_API_KEY` |
| `openai-compatible` | Generic configurable compatible endpoint | optional `OPENAI_COMPATIBLE_API_KEY` |
| `ollama` | Ollama `/api/chat` (local by default) | optional `OLLAMA_API_KEY` |
| `anthropic` | **Reserved direct slot, not implemented yet** | not used directly |

Run `pc-agent providers list` to see the exact route on a machine. It never
prints keys and makes no network call. Anthropic models can be reached
through an OpenRouter model choice; a direct Anthropic adapter is a future
addition behind the same provider interface.

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
| `providers list`, `doctor`, GUI Dashboard/Providers refresh | No |
| Supported deterministic `run` task (CLI or GUI Tasks tab) | No |
| Direct `office`, `web fetch` or `desktop` command, or the matching GUI tab | No provider call; web fetch contacts only the exact requested site |
| Automatic recovery retry | No; local deterministic rules only |
| `task recover` without `--use-ai` | No |
| `run --use-ai` when deterministic planning recognizes the task | No; deterministic wins |
| `run --use-ai` for an unrecognized task | Yes, planning only |
| `ask "..."` or the GUI Providers tab **Ask Provider** button | Yes, unless only mock is usable |
| `tool-builder suggest/template/import/test/approve` | No |
| `tool-builder generate` / `repair` | Yes; a configured real provider is required, mock output is rejected |

## AI planning safety

Provider planning returns JSON, but that output is untrusted. Local code
accepts only:

- `pdf.merge_folders`, `pdf.merge`, `pdf.verify`, `pdf.inspect`,
  `workspace.list_files`, `office.inspect`, `office.csv_inspect`,
  `office.csv_to_json`, `office.json_to_csv`, `office.csv_to_xlsx`, and
  `desktop.system_info`;
- known argument names and value types;
- workspace-relative paths with no absolute path or `..`;
- a matching intent/tool pair.

An AI plan cannot add tools, approve external folders, fetch a URL, open a
desktop target, use a shell, delete or overwrite files, access credentials,
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

- Keys are read only from environment/local `.env` and excluded from
  summaries and normal CLI output.
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
```

Model availability/pricing belongs to the provider/account, not this local
software. A provider returning “model not found” should be fixed by changing
the model variable, not by editing source code.
