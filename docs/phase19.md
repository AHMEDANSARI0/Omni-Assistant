# Phase 19: Direct Anthropic Provider

Phase 19 replaces the previously reserved `anthropic` provider slot with a
real Anthropic Messages API adapter. It adds no registered tool, database
migration or runtime dependency. The database schema remains **v6** and the
built-in tool count remains **54**.

## What is included

- `pc_agent.providers.AnthropicProvider`, using the same
  `CompletionRequest` / `CompletionResponse` contract as Gemini,
  OpenRouter, OpenAI, generic OpenAI-compatible endpoints and Ollama.
- `POST /v1/messages` with `Authorization: Bearer ...` and
  `anthropic-version: 2026-10-21`.
- Top-level system text blocks, user/assistant messages, required
  `max_tokens`, non-streaming responses and text-block extraction.
- Inline base64 PNG, JPEG, WebP and GIF image blocks for the existing
  approved `image.analyze` path.
- Anthropic `usage.input_tokens` / `usage.output_tokens` passed unchanged
  to the Phase 17 task-budget accounting.
- Explicit provider failure for Anthropic error payloads, refusals, empty
  responses and malformed/non-text responses.
- Default model `claude-sonnet-5-5`, overridable by
  `PC_AGENT_ANTHROPIC_MODEL`. `PC_AGENT_ANTHROPIC_BASE_URL` may point at
  the service root, `/v1` or the full `/v1/messages` endpoint; non-loopback
  bases must use HTTPS.

Anthropic API version `2026-10-21` removed `temperature`, `top_p` and
`top_k`. The adapter therefore never emits those fields, even when a
provider-independent request carries a temperature value. JSON-shaped
answers continue to be requested in the prompt; this adapter does not use
provider-hosted tools, streaming, prompt caching or server-side conversation
state.

## Configure Anthropic

Store the key through the existing hidden Windows Credential Manager prompt:

```powershell
pc-agent credentials set anthropic
```

Then include `anthropic` where you want it in `PC_AGENT_PROVIDER_ORDER` in
your own `.env`, for example:

```dotenv
PC_AGENT_PROVIDER_ORDER=anthropic,gemini,openrouter,mock
```

An environment key is also supported through `ANTHROPIC_API_KEY` and takes
precedence over Credential Manager, matching every other provider. To use a
different Anthropic model, add:

```dotenv
PC_AGENT_ANTHROPIC_MODEL=claude-opus-5-5
```

`pc-agent providers list`, `pc-agent doctor`, the GUI and diagnostics report
model/credential-source status without printing a key. An `anthropic` route
entry with no configured key is skipped; the application never invents a key
or silently calls Anthropic.

## Vision and task behavior

Anthropic is marked vision-capable, so the existing router can select it for
an approved inline-image request when it is present in the configured route.
The adapter sends labels for multiple images, image blocks first and the
question as the final text block. It receives no tool schemas, filesystem
paths or approval authority: `image.analyze` still validates and reads the
local file, `PC_AGENT_ALLOW_VISION=1` still gates sending it, and the exact
send still requires approval.

Anthropic text use is subject to the same deterministic-first routing,
planner allow-list, budgets, tool schemas, policy checks and approval rules
as the other providers. Provider text remains untrusted data; it cannot add
registered tools or approve an action.

## Security and privacy behavior

- The API key appears only in the HTTPS authorization header, never in the
  request URL or in public CLI/GUI/provider-status output.
- Requests and responses use the shared bounded standard-library HTTP
  transport, verified TLS and provider timeout/retry policy.
- Prompt or image bytes are sent only when the user invokes the relevant
  provider path and `anthropic` is in the configured usable route.
- Anthropic refusals are recorded as non-retryable provider errors; the
  adapter does not return a fake successful answer.

## Remaining work after Phase 19

- A signed Windows installer, publisher code signing and auto-update are not
  implemented; the verified distribution is still the editable source tree
  plus the deterministic portable ZIP.
- macOS/Linux packaging, Keychain/Secret Service credentials and desktop
  automation parity are not implemented.
- The new Anthropic transport is covered by deterministic mocked-transport
  tests. A live Anthropic call can only be verified after the user adds an
  Anthropic key; key/account/model availability remains external.
- `install_windows.ps1`, `uninstall_windows.ps1` and the `.cmd` launchers
  have static coverage and conservative paths, but still warrant one live
  Windows execution. `build_release.ps1` was live-run successfully under
  Phase 18.
- Commercial SaaS, subscriptions, online billing and multi-tenancy remain
  outside the personal-use scope unless explicitly requested later.
