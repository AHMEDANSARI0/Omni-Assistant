# Phase 39 — Secure `.env` Provider-Key Cleanup

Phase 39 completes the smallest remaining Phase 38 follow-up: the optional
manual step after Phase 16 credential migration. It lets a direct user
remove duplicated provider API-key lines from the project-root `.env`, but
only after the application proves that the **identical effective value** is
already available from the configured OS credential store.

| Item | Value |
|---|---|
| version | 0.41.0 |
| command | `pc-agent credentials remove-env-keys` |
| registered tools | 62 built-ins; no new tool |
| database schema | v6; no migration |
| new dependency | none |
| plaintext backup | never created |

## Direct CLI

```text
pc-agent credentials remove-env-keys [provider] [--dry-run] [--yes] [--json]
```

With no provider, all six supported provider-key slots are reviewed:
Gemini, OpenRouter, OpenAI, OpenAI-compatible, Ollama, and Anthropic. With
one provider, only that provider's variable is considered.

- `--dry-run` prints the exact provider/status/line plan and changes
  nothing.
- Without `--yes`, the command prints the plan and asks for the normal
  direct-write approval.
- `--json` returns the same secret-free plan or applied result as JSON.
- A missing `.env` is a no-op. A symlinked, malformed, oversized, or
  concurrently changed `.env` fails closed.

## Safety rule

For every selected provider, the planner reports exactly one status:

| Status | Meaning | Removed? |
|---|---|---:|
| `verified_match` | Effective `.env` value exactly matches secure OS storage | yes |
| `not_in_dotenv` | No assignment for that provider variable | no |
| `empty_in_dotenv` | Last assignment is empty | no |
| `not_stored` | No corresponding OS credential exists | no |
| `value_mismatch` | `.env` and OS storage contain different keys | no |

Duplicate variable lines follow the existing `.env` loader's last-value
rule. If the effective last value is verified, every assignment line for
that one variable is removed, including stale duplicates. Comments,
non-provider settings, ordering, blank lines, line endings, and a UTF-8 BOM
are otherwise preserved.

The credential store must be available. With
`PC_AGENT_CREDENTIAL_STORE=env`, or on a host without the selected secure
backend, cleanup refuses rather than leaving the application without the
only copy of a key.

## Apply and receipt

Approval is bound to the exact plan hashes. Before replacing anything, the
apply step re-plans and refuses if the `.env` bytes, removable providers,
statuses, backend, or proposed result changed. The replacement is prepared
in the same directory, verified, and atomically moved over `.env`; the final
file is re-read and checked again.

No plaintext backup is created, because retaining one would defeat the
cleanup. A secret-free JSON receipt is written under
`data/credential-cleanups/` with provider names, statuses, removed line
numbers, file hashes/sizes, backend, and verification status. It contains
no API-key values or key fingerprints.

If an API-key variable is also set in the current process environment, the
command says so. Removing the `.env` line does not unset a Windows user or
process environment variable, and that external value can still take
precedence. Start a new CLI/GUI process after cleanup so settings reload.

## Boundaries

- This is a direct user command only. No planner, orchestrated task,
  dynamic tool, GUI action, or model can invoke it.
- It does not prove that an API key is valid with its provider; it proves
  only that the same value exists in both local locations before the `.env`
  copy is removed.
- It does not delete or rotate the Credential Manager copy. Use the existing
  `credentials set` or `credentials delete` commands for those separate
  operations.
- It cannot recover a key after both copies are deleted. The verified
  Credential Manager copy is the intended remaining source.
- It only edits the exact project-root `.env`; it never edits `.env.example`
  or a workspace file.
