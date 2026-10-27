# Dynamic Tool Builder - introduced in Phase 2

Phase 2 lets the local agent create a missing **pure-compute** capability as an
untrusted candidate, prove it with deterministic checks, and register it only
after explicit user approval. Phase 3 preserves that lifecycle and hardens
the CLI examples below.

This is deliberately narrower than arbitrary program generation. A dynamic
tool receives one JSON object, returns one JSON object, and has no filesystem,
network, shell, desktop, credential or delete permission.

## Lifecycle

```text
proposed
  -> tested ------> approved -> enabled/disabled
  -> test_failed -> bounded repair creates a new version
  -> rejected

approved older version -> superseded -> may be restored by rollback
```

Candidate source, schemas and tests are immutable for a `tool_id@version`.
Changing behavior requires a new version. Approving a newer tested version
supersedes the previously approved version. Rollback reactivates the newest
superseded version with a stored passing test report.

SQLite schema v3 introduced these dynamic-tool tables (preserved by the
Phase 3 schema-v4 recovery migration):

- `dynamic_tool_candidates` - immutable proposal, source hash, provenance,
  dependencies, status, enabled flag and current reports;
- `dynamic_tool_test_runs` - every sandbox test run;
- `dynamic_tool_events` - created/tested/approved/rejected/disabled/rollback
  lifecycle history.

The stored `source_sha256` is checked whenever a candidate is loaded.

## Offline demo (no API key needed)

Reviewed templates provide a safe way to exercise the complete lifecycle:

```powershell
pc-agent tool-builder templates
pc-agent tool-builder template word_count --run-tests
pc-agent tool-builder show custom.text_word_count@1.0.0 --show-source
pc-agent tool-builder approve custom.text_word_count@1.0.0 --yes
pc-agent tools list
pc-agent tools invoke custom.text_word_count --arguments-file examples\dynamic-tools\word_count_arguments.json
```

Expected output contains `"words": 3`. `approve --yes` prints the final
post-approval snapshot (`Status: approved`, `Enabled: True`). Relative
`--arguments-file` paths are searched in the current folder, app root and
workspace, in that order. If this one bundled word-count example file is
missing, Phase 3 uses an identical embedded copy with a warning; `doctor`
reports the missing project file. For any other missing arguments file, the
error lists all searched paths.

Disable and restore it without deleting its history:

```powershell
pc-agent tools disable custom.text_word_count
pc-agent tools enable custom.text_word_count
```

Available templates are `word_count`, `slugify`, `percentage`, and
`temperature`.

## Find an existing tool before creating one

```powershell
pc-agent tool-builder suggest "I need to count words"
pc-agent tool-builder search "percentage"
pc-agent tools show custom.math_percentage
```

`suggest` searches built-ins and stored candidates, proposes a template when
one matches, and creates nothing.

## Import a proposal

An imported proposal is untrusted input. It becomes a candidate only after
strict metadata/schema validation and static analysis:

```powershell
pc-agent tool-builder import proposal.json --run-tests
pc-agent tool-builder show custom.example@1.0.0 --show-source
pc-agent tool-builder approve custom.example@1.0.0 --yes
```

A ready-to-import example is included at
`examples/dynamic-tools/text_reverse.json`.

A proposal contains exactly these top-level fields:

```json
{
  "tool_id": "custom.double_number",
  "name": "Double number",
  "version": "1.0.0",
  "description": "Double one number.",
  "input_schema": {
    "type": "object",
    "properties": {"value": {"type": "number"}},
    "required": ["value"]
  },
  "output_schema": {
    "type": "object",
    "properties": {"value": {"type": "number"}},
    "required": ["value"]
  },
  "source_code": "def run(arguments):\n    return {'value': arguments['value'] * 2}\n",
  "test_cases": [
    {
      "name": "double two",
      "arguments": {"value": 2},
      "expected_output": {"value": 4}
    }
  ],
  "dependencies": [],
  "timeout_seconds": 5,
  "permissions": [],
  "network_required": false
}
```

Rules enforced before a candidate can be approved:

- `tool_id` must start with `custom.` and cannot shadow a built-in.
- `permissions` must be empty and `network_required` must be `false`.
- Versions use `MAJOR.MINOR.PATCH`; timeout is 1-30 seconds.
- Input/output schemas themselves are validated. Object fields require
  declared properties; arrays require an item schema; unknown schema keys and
  unknown runtime fields are rejected.
- Declared dependencies must exactly match actual imports. Only approved
  standard-library modules are allowed; pip packages cannot be requested or
  installed by a candidate.
- Every test's arguments and expected output must satisfy the declared
  schemas before sandbox execution.

## Static source policy

The trusted validator parses Python AST before any candidate code runs. It
requires one top-level `run(arguments)` function and rejects, among other
things:

- non-allow-listed or nested imports;
- classes, decorators, async code, generators, context managers and top-level
  side effects;
- `open`, `eval`, `exec`, `compile`, dynamic import, `getattr`/`setattr`,
  `globals`/`locals`, printing/input and other reflection escapes;
- private/dunder names or attributes and dynamic `.format(...)` access;
- non-constant function defaults and oversized source/test payloads.

Allowed imports are limited to selected computational standard-library modules
such as `math`, `re`, `json`, `hashlib`, `datetime`, `decimal`, `statistics`,
`textwrap` and `unicodedata`.

Static analysis is a necessary layer, not a proof that code is semantically
correct. The user should inspect the source, schemas and test expectations
with `tool-builder show ... --show-source` before approval.

## Restricted subprocess tests and invocation

Passing static validation only earns a candidate isolated test execution.
Each test case and each approved invocation runs in a fresh Python subprocess
with:

- a trusted standard-library harness copied into a temporary sandbox folder;
- `sys.executable -I -B -s` and no shell;
- JSON over stdin/stdout, capped at 1 MB in each direction;
- restricted builtins and a second allow-list check for imports;
- a minimal environment with provider API keys removed;
- the candidate's hard timeout;
- exact expected-output comparison for tests;
- registry input/output schema validation around runtime invocation.

This is **restricted subprocess isolation, not a complete operating-system or
VM sandbox**. In particular, this build does not claim a hard memory cap or
kernel-level confinement. Do not approve code you have not reviewed merely
because its sample tests pass.

## Provider generation and bounded repair

With a real provider configured in `.env`, a provider may propose a new
candidate:

```powershell
pc-agent tool-builder generate "convert a decimal number to Roman numerals" --tool-id custom.roman_numeral --run-tests
```

The offline mock cannot generate tools. Provider output is accepted only as
one strict JSON proposal with the requested tool id; it then passes through
exactly the same validator, sandbox tests and approval gate as an import.

If static validation or tests fail, one repair request can create a new
version:

```powershell
pc-agent tool-builder repair custom.roman_numeral@1.0.0 --requirement "convert a decimal number to Roman numerals" --run-tests
```

Repair is bounded to three attempts in a candidate chain. A repair may change
implementation details, but the local service preserves the parent input/
output schemas and expected test outputs so the provider cannot repair a
failure merely by weakening the contract. A repaired candidate is never
approved automatically.

No real-provider generation/repair call is part of the offline automated test
suite; tests use a fake provider returning strict JSON. A live API smoke test
still depends on the user's provider account and model availability.

## Runtime commands

```powershell
pc-agent tools list
pc-agent tools show custom.text_word_count
pc-agent tools invoke custom.text_word_count --arguments-file arguments.json --json
pc-agent tools rollback custom.text_word_count
```

Only approved dynamic versions reach the runtime registry. Disabled approved
versions remain visible in `tools list` but cannot execute. Dynamic tools are
invoked explicitly through `tools invoke`; the Phase 1/3 natural-language
orchestrator remains limited to its reviewed built-in allow-list, so Phase 3
automatic task recovery does not retry direct dynamic-tool invocations.

## Explicit limitations

- Python, standard-library-only, pure-compute tools only.
- No file/network/shell/browser/desktop dynamic tools and no pip installs.
- No GUI, licensing, cloud service or autonomous repair of core/dynamic-tool
  source code yet.
- Sample tests are evidence, not mathematical proof for every input.
- A malicious or badly written approved tool can still consume CPU until its
  timeout; review remains part of the trust decision.
