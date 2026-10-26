# AI providers - Phase 1C

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
| `providers list`, `doctor` | No |
| Supported deterministic `run` task | No |
| `run --use-ai` when deterministic planning recognizes the task | No; deterministic wins |
| `run --use-ai` for an unrecognized task | Yes, planning only |
| `ask "..."` | Yes, unless only mock is usable |

## AI planning safety

Provider planning returns JSON, but that output is untrusted. Local code
accepts only:

- `pdf.merge_folders`, `pdf.merge`, `pdf.verify`, `pdf.inspect`, and
  `workspace.list_files`;
- known argument names and value types;
- workspace-relative paths with no absolute path or `..`;
- a matching intent/tool pair.

An AI plan cannot add tools, approve external folders, use a shell, delete or
overwrite files, access credentials, or bypass registry schemas / policy
checks at execution time. Unclear plans become `needs_input`, not guesses.

## Privacy and HTTP safety

- Keys are read only from environment/local `.env` and excluded from
  summaries and normal CLI output.
- A provider receives the prompt/instruction needed for `ask` or optional AI
  planning. It does not receive file contents or your API keys for other
  providers.
- Base URLs must use HTTPS unless they point to localhost/loopback.
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
