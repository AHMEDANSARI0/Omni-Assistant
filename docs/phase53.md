# Phase 53 — Live Anthropic Verification

Phase 53 completes the **Live Anthropic Verification** item left open by
Phase 19. The Anthropic Messages API adapter itself already existed; what
was missing was a safe, repeatable way to prove that one real configured
Anthropic request succeeds without confusing it with router retries or a
fallback answer from another provider.

| Property | Value |
|---|---|
| Version / phase label | 0.55.0 / PHASE 53 Live Anthropic Verification |
| Commands | direct `providers verify-anthropic` and `providers verify-anthropic-report` |
| Provider call | exactly one direct Anthropic Messages API request |
| Fallback / router retry | no fallback and no router retry — the router is bypassed |
| Prompt | fixed public connectivity prompt; no user text or files |
| Maximum output | 32 tokens |
| Evidence | secret-safe JSON report with response hash, model and usage metadata |
| Registered tools | 62 built-ins (unchanged) |
| Database schema | v6 (unchanged) |
| New Python dependency | none |

## Run the live check

Configure the Anthropic key through the existing hidden credential prompt
or `ANTHROPIC_API_KEY`, choose the model with
`PC_AGENT_ANTHROPIC_MODEL` if needed, then run:

```text
pc-agent providers verify-anthropic
```

To choose the evidence file explicitly (it must not already exist):

```text
pc-agent providers verify-anthropic --report workspace\anthropic-live-report.json
```

Machine-readable output is available with `--json`. The command returns
exit code 0 only when every check passes. A configuration or provider
failure still writes a failed report and returns 1, unless the requested
report path itself is unsafe or already exists.

Without `--report`, the report is written under
`data/provider-verification/` with a UTC timestamp in its filename.

## Verify a retained report

```text
pc-agent providers verify-anthropic-report workspace\anthropic-live-report.json
```

The offline verifier makes no network call. It requires the current app
version, a passed status, every required check, the fixed response-token
record, bounded model/timing/usage fields, the recorded endpoint origin,
and a matching SHA-256 self-hash.

## Checks performed

The live command records these checks in order:

1. `configuration_present` — model and timeout are present and bounded.
2. `credential_present` — a usable key exists; only its source is recorded.
3. `endpoint_allowed` — HTTPS outside loopback (or HTTP only on loopback),
   with no URL credentials, query or fragment.
4. `single_direct_request` — the Anthropic adapter is called directly once,
   bypassing the model router's fallback and retry behavior.
5. `live_response_received` — the request returns instead of raising.
6. `provider_identity` — the normalized response identifies `anthropic`.
7. `model_reported` — the response reports a bounded model identifier.
8. `expected_response` — the model returns the fixed public token
   `PC_AGENT_ANTHROPIC_OK`; only the response length and SHA-256 are kept.
9. `usage_reported` — bounded input/output token counts are present.
10. `secret_not_recorded` — the serialized report does not contain the key.

## Security and privacy boundaries

- The API key is never accepted as a command-line argument and never
  printed or written to the report.
- Only the fixed connectivity prompt is sent. No workspace file, image,
  task text, database content, tool schema or approval authority is sent.
- This is a text verification only; the report honestly records
  `vision_status: not_tested`. The existing vision gate and approval flow
  are unchanged.
- A wrong-token response, missing usage, malformed model field, provider
  error, unsafe endpoint or missing key cannot produce a passed report.
- The report self-hash is an integrity check against later alteration. It
  is local operator evidence, not a cryptographic attestation issued by
  Anthropic; Anthropic does not sign ordinary Messages API responses.
- Automated tests inject a fake HTTP transport and make no live network
  call. The user's explicit `verify-anthropic` run is the live evidence.
- No background polling, automatic retry loop, silent provider call,
  new provider, registered-tool change, schema migration or dependency is
  introduced.

## Verified states

Automated Phase 53 coverage proves:

- a complete successful direct request and retained-report verification;
- exact Anthropic endpoint, fixed prompt, 32-token ceiling and no router;
- failure before any network call when the key is absent;
- redacted failed reports for provider errors;
- failure for a wrong response or missing token usage;
- refusal to overwrite an existing report before contacting Anthropic;
- tampered-report rejection through the self-hash;
- CLI success/report-verification paths with no secret in stdout or JSON.

See `docs/phase19.md` for the underlying adapter and `docs/providers.md`
for provider configuration and routing behavior.
