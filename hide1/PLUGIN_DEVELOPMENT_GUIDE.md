# Plugin development guide

Plugins are third-party Python that the product runs in a sandbox after an
explicit, per-call approval. This guide describes the bundle format, the
capability model, what the sandbox really enforces, and — importantly — what it
does **not**.

The master gate `PC_AGENT_ALLOW_PLUGINS` is **off** by default.

---

## 1. Bundle format

A plugin is a ZIP containing exactly:

```
manifest.json      # signed description of the plugin
plugin.py          # entry module (any *.py at the root may be the entry)
```

Rules enforced at install time (`plugins/manifest.py`):

| Rule | Limit |
| --- | --- |
| Manifest fields | `id`, `name`, `version`, `publisher_key_id`, `tools`, `capabilities`, `description` |
| Bundle size | ≤ 2 MB, ≤ 64 entries |
| Entry paths | relative, no `..`, no absolute paths, no symlinks, no links |
| Tools | each declares `name`, `description` and an argument schema |
| Capabilities | closed list: `read_workspace`, `write_workspace`, `read_plugin_files`, `network`, `subprocess` |
| Signature | Ed25519 over `manifest_bytes()`, verified against `PC_AGENT_PLUGIN_TRUSTED_KEYS` |

`network` and `subprocess` are **never granted**, even if a manifest asks for
them: requesting one is recorded as a refused install, not a silent downgrade.

## 2. A minimal plugin

`plugin.py`:

```python
def count_words(text):
    return {"words": len(text.split()), "chars": len(text)}
```

`manifest.json` (before signing):

```json
{
  "id": "com.example.wordcount",
  "name": "Word count",
  "version": "1.0.0",
  "description": "Counts words in a string.",
  "publisher_key_id": "my-key",
  "capabilities": ["read_plugin_files"],
  "tools": [
    {
      "name": "count_words",
      "description": "Count words and characters.",
      "arguments": {"text": "string"}
    }
  ]
}
```

The tool function receives the declared arguments as keyword arguments and must
return a JSON-serialisable dict. A string result, a non-dict, or a value that is
not JSON-serialisable is reported as a plugin error, never coerced.

## 3. Signing and installing

```
pc-agent plugins keygen --out keys            # once, offline
pc-agent plugins pack dist/my-plugin.zip --dir my-plugin --key keys/plugin-private-key.txt
pc-agent plugins verify dist/my-plugin.zip
```

Install (the gate must be on, and the publisher key must be trusted):

```
setx PC_AGENT_ALLOW_PLUGINS 1
setx PC_AGENT_PLUGIN_TRUSTED_KEYS my-key=<base64url-public-key>
pc-agent plugins install dist/my-plugin.zip
pc-agent plugins enable com.example.wordcount
pc-agent plugins run com.example.wordcount count_words --values "{\"text\":\"a b c\"}" --confirm com.example.wordcount
```

`--confirm` takes the **plugin id**, not a boolean: running third-party code is a
per-call decision and is recorded in `plugin_runs`.

## 4. What the sandbox actually enforces

Two independent layers, applied to every call:

**Layer 1 — the OS.** POSIX: `setrlimit` for address space, CPU seconds, file
size, open files and child processes. Windows: a Job Object with process memory
and active-process limits and kill-on-close. A hard timeout (default 30 s) kills
the whole process group / job.

**Layer 2 — CPython.** The worker runs as a separate process in isolated mode
(`-I -B`) with a scrubbed environment (no provider keys, no proxies, only a
non-secret allow-list of platform variables) and installs
`sys.addaudithook` that refuses:

* `socket.*` (network), unless `network` was granted — it never is;
* `subprocess.*`, `os.system`, `os.exec*`, `os.spawn*`, `pty.spawn`;
* file opens outside the declared capability roots.

The interpreter that actually runs is reported as `sandbox.interpreter`, and a
host that cannot start a worker produces a structured `spawn_error` instead of
blaming your plugin.

## 5. What the sandbox does not do

Read this before shipping anything:

1. **It is not a VM.** Hand-written native code, `ctypes`, or a memory bug can
   escape a Python audit hook. Plugins are for cooperative third-party code.
2. **It is not a network jail.** The refusal is at the CPython API boundary; the
   OS-level layer limits resources, not packets. `network` therefore stays
   ungrantable rather than "grantable but risky".
3. **Capability roots are path-based.** A granted root is a directory; anything
   outside it is refused, including symlinked parents.
4. **No persistent state.** Each call gets a fresh run directory that is deleted
   afterwards; a plugin cannot accumulate files between calls.
5. **Outputs are bounded** (200 KB per stream). Truncation is reported, not
   silently dropped.

## 6. Testing your plugin

```
pc-agent plugins install dist/my-plugin.zip
pc-agent plugins enable com.example.wordcount
pc-agent plugins run com.example.wordcount count_words --values "{\"text\":\"x y\"}" --confirm com.example.wordcount
pc-agent plugins runs                      # the real ledger, including failures
```

Check the refusals too — they are part of the contract:

* a plugin that opens a socket must fail with a network refusal;
* a plugin that writes outside its roots must fail with "outside the declared
  capability roots";
* a plugin that sleeps past the timeout must be reported as `timeout` and killed.

## 7. Versioning and uninstalling

* `version` is MAJOR.MINOR.PATCH; installing a new version keeps the previous one
  in `plugin_versions` so a rollback is possible.
* `pc-agent plugins uninstall <id>` removes the files and the trust record; runs
  already in the ledger stay for audit.
* Re-signing after an edit is mandatory — a bundle whose manifest does not match
  its signature is refused before extraction.

## 8. Checklist before you publish

- [ ] manifest lists only capabilities the code truly needs
- [ ] every tool validates its own arguments again (the schema is a filter, not a
      guarantee)
- [ ] no secrets are read from the environment — there are none to read
- [ ] the tool returns JSON-serialisable data and stays well under 200 KB
- [ ] a timeout path is tested
- [ ] the bundle is signed and the public key is shared out of band

## Interpreter note (Windows, 0.80.1)

The worker is started in isolated mode (`-I -B`), so **your plugin may use the
standard library only** - it never had access to the environment's
site-packages. The sandbox chooses the interpreter that can actually start under
its own limits: normally the running one, but inside a virtualenv-style
environment (whose `python.exe` is a launcher shim) it uses the environment's
base interpreter and reports
`... +launcher-slot(active-process-limit=2)` in the run's `mechanism`. If your
tool behaves differently between those two, the cause is a missing third-party
import - bundle the logic instead of importing it.

