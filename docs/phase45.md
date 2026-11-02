# Phase 45 — macOS Keychain Credentials (0.47.0)

Phase 45 completes the three desktop provider-key stores started by Phase 16
(Windows Credential Manager) and Phase 44 (Linux Secret Service). On macOS,
provider API keys can now live in the current user's Keychain instead of the
process environment or a `.env` file.

| Item | Phase 45 behavior |
| --- | --- |
| backend | `macos-keychain` via Apple's system `/usr/bin/security` executable |
| modes | `PC_AGENT_CREDENTIAL_STORE=auto|windows|linux|macos|env` |
| automatic selection | `auto` selects macOS Keychain when `sys.platform` is `darwin` |
| service | `self-healing-pc-agent-provider-api-key` |
| account | the canonical provider name (`gemini`, `openrouter`, `openai`, `openai-compatible`, `ollama`, or `anthropic`) |
| source label | `macos-keychain` |
| timeout | 10 seconds per `security` invocation |

## Secret transport

Passing `security add-generic-password -w <secret>` would put the API key in
the child process argument list, and a trailing prompt-only `-w` reads from
the controlling terminal rather than a pipe. Phase 45 therefore never puts a
secret in argv. A store operation invokes only:

```text
/usr/bin/security -q -i
```

It then sends one bounded `add-generic-password -U ... -X <hex>` command on
stdin. The hex alphabet needs no shell or interactive-tokenizer quoting. The
encoded bytes carry a versioned `pc-agent-v1:` prefix. That prefix makes the
read path unambiguous when different macOS releases print a non-ASCII stored
value as raw text, bare hex, or `0x` hex, and it prevents an API key that
happens to look like hex from being decoded by mistake.

The interactive command is rejected before execution if its complete line
would reach 4,000 bytes, safely below `security -i`'s line-buffer boundary.
This supports roughly 1,900 bytes of UTF-8 API-key data, far above normal
provider keys; an unusual longer Unicode value fails closed rather than
falling back to an argv-visible command. Interactive mode can exit zero even
when its inner command failed, so any stderr is treated as failure and every
write is read back and compared before success is reported. stderr is never
copied into an exception, status, log, or Doctor output.

Lookups use `find-generic-password -w` with both the fixed service and the
provider account. Apple's documented "specified item could not be found"
diagnostic (or `errSecItemNotFound` / `-25300`) maps only to "not stored";
locked-keychain, authorization, timeout, malformed-output, and other service
errors fail closed with generic messages. Delete first proves an item exists,
runs `delete-generic-password` for the same fixed service/account pair, and
then verifies that a fresh lookup reports it absent.

## Access model and precedence

`/usr/bin/security` creates the item, so the same Apple-signed executable
remains the trusted reader used by this backend. The app never specifies
`-A` (allow any application) and never broadens the item ACL. macOS may still
show its normal Keychain authorization prompt for a locked keychain or an
ACL decision; cancelling or denying it is a failed operation, not a fallback
to writing the key somewhere else.

Environment and `.env` values still take precedence, exactly as they do for
the Windows and Linux stores. Once a key has been copied into macOS Keychain
with `credentials set`, removing the old `.env` line makes the effective
source `macos-keychain`; no command prints its value. The existing CLI/GUI
credential status, set, delete, and migration-cleanup flows are reused.

## Verification boundary

The implementation is covered by an in-memory fake `security` runner and by a
fake executable driven through Python's real subprocess runner. Tests prove
round-trip set/get/delete, plaintext absence from argv, raw/hex/`0x` read
decoding, all-hex and Unicode secrets, missing-versus-denied lookups,
interactive-command failure handling, the transport-size guard, timeout
handling, explicit/auto mode selection, and Settings source labels.

This Linux development host does not provide a live macOS Keychain transcript;
that evidence requires a logged-in Mac user session. Phase 45 also does not
implement a macOS installer/PKG/DMG, Apple code signing or notarization, live
Gatekeeper verification, or iCloud Keychain synchronization. macOS desktop
automation parity remains future work, as do native DEB/RPM packages and the
other platform release evidence items.

No registered tool (still 62), database schema change (still v6), Python
dependency, provider wire format, approval rule, or Windows/Linux credential
behavior is introduced by Phase 45.

## Phase 49 follow-up — **Windows UTF-8 Test Hotfix**

The Phase 45 scope/documentation test originally read this and other
UTF-8 documents using Python's locale default encoding. On Windows that
could be cp1252 and fail before the Keychain assertions ran. The
**Windows UTF-8 Test Hotfix** makes those test reads explicitly UTF-8 and
adds a repository-wide regression guard. Phase 45 runtime credential
behavior is unchanged. See `docs/phase49.md`.
