# Phase 54 — Live GNOME Keyring/KWallet Evidence

Phase 54 completes the live-evidence follow-up left by Phase 44. The
Linux Secret Service backend already existed; this phase adds a guarded,
direct acceptance run that proves it against the Secret Service provider
in a real signed-in Linux desktop session and retains a report that can be
checked later without touching the keyring again.

| Property | Value |
|---|---|
| Version / phase label | 0.56.0 / PHASE 54 Live GNOME Keyring/KWallet Evidence |
| Commands | direct `credentials linux-secret-service-acceptance` and `credentials linux-secret-service-acceptance-verify` |
| Accepted providers | GNOME Keyring, KWallet/KSecretService |
| Provider identity | session-bus owner of `org.freedesktop.secrets`, resolved through `gdbus` or `busctl` |
| Probe | fresh random value under a unique acceptance-only attribute set |
| Real provider keys | compared in memory before/after; values and hashes are not recorded |
| Evidence | secret-safe self-hashed JSON report |
| Registered tools | 62 built-ins (unchanged) |
| Database schema | v6 (unchanged) |
| New Python dependency | none |

## Run the live acceptance

Sign in to the Linux desktop whose Secret Service should be proven, make
sure the distro package providing `secret-tool` is installed and the
collection can be unlocked by the desktop, then run:

```text
pc-agent credentials linux-secret-service-acceptance
```

To choose the evidence file explicitly (it must not already exist):

```text
pc-agent credentials linux-secret-service-acceptance --report workspace/linux-secret-service-report.json
```

Without `--report`, the report is written under
`data/credential-acceptance/` with a UTC timestamp in its filename. JSON
output is available with `--json`. Exit code 0 means every check passed;
failed platform/provider/round-trip states still retain a failed report
and return 1, unless the requested report path itself already exists or
is not a `.json` path.

Verify a retained report later, with no keyring or D-Bus access:

```text
pc-agent credentials linux-secret-service-acceptance-verify workspace/linux-secret-service-report.json
```

## What is proven

The acceptance records these checks in order:

1. `linux_platform` — the host really is Linux.
2. `linux_backend_selected` — settings select the
   `linux-secret-service` backend (normally through `auto` on Linux).
3. `session_bus_present` — a desktop session-bus address exists. Only the
   boolean is recorded, never the address.
4. `secret_service_available` — the existing bounded `secret-tool`
   reachability probe succeeds.
5. `provider_is_gnome_or_kwallet` — the D-Bus owner process for
   `org.freedesktop.secrets` is GNOME Keyring or KWallet/KSecretService.
   Another Secret Service implementation (for example KeePassXC) is not
   misreported as either one, and no probe is written to it by this
   acceptance.
6. `probe_absent_before` — the fresh unique probe item does not already
   exist.
7. `real_provider_keys_snapshot` — current real provider-key values are
   read into memory only for before/after comparison.
8. `probe_store_and_readback` — a fresh random probe is stored by stdin
   and read back exactly.
9. `probe_delete_and_absent_after` — the probe is deleted and a follow-up
   lookup proves it absent.
10. `real_provider_keys_untouched` — every real provider-key value still
    compares unchanged in memory.
11. `secret_not_recorded` — the serialized report contains neither the
    probe secret nor any real key material.

The probe uses the separate attributes
`application=self-healing-pc-agent`,
`provider=__pc_agent_acceptance__`, `purpose=live-acceptance-probe` and a
unique `probe_id`. It cannot overwrite a `purpose=provider-api-key` item.
The report contains the probe ID (useful for diagnosing a cleanup
failure) plus the probe value's length and SHA-256, but not the value.

## Security and honesty boundaries

- The probe secret reaches `secret-tool` only on stdin, never in argv.
- Real provider-key values are compared only in process memory. The
  report records only provider names before/after; no values or hashes of
  real keys are retained.
- The D-Bus helper records only the owner process basename. It does not
  retain the session-bus address, process arguments or helper stderr.
- A locked collection, missing session bus, unavailable `secret-tool`,
  unproven provider process, probe collision, read-back mismatch,
  cleanup failure or unexpected real-key change cannot produce a passed
  report.
- If cleanup itself fails, the report is failed and retains the probe ID,
  so the operator can locate the isolated item. The acceptance never
  silently deletes a pre-existing colliding item.
- The report self-hash is an alteration check for local operator
  evidence, not a cryptographic attestation issued by GNOME or KDE.
- Automated tests use an injected fake `secret-tool` process and fake
  D-Bus owner results; they do not touch a developer's keyring. A passed
  report from the explicit command in a real GNOME or KDE session is the
  live evidence.
- This phase adds no background keyring polling, no key export, no new
  credential backend, no registered tool, no schema migration and no
  dependency.

## Automated coverage

Phase 54 tests prove:

- the complete GNOME-labelled and KWallet-labelled report paths;
- D-Bus owner parsing for both `gdbus` and `busctl`, including the
  truncated GNOME process-name form;
- absence → store → exact read-back → delete → absence;
- real provider keys remain byte-for-byte unchanged;
- missing session bus, unavailable service and unknown-provider failures
  write no probe;
- probe cleanup is attempted after a store/read failure;
- report tampering is rejected by the self-hash;
- CLI run/report-verification paths leak neither the probe nor real keys;
- existing reports are refused before any keyring write.

See `docs/phase44.md` for the underlying Secret Service mapping and
`docs/phase16.md` for credential resolution order.
