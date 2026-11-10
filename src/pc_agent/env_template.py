"""The `.env.example` template, kept in code so an install can always restore it.

`.env.example` is not documentation - it is a **required payload file**: the
Windows installer, the POSIX package and the Debian/RPM builders all refuse to
build without it, because the installer must ship a template the user can copy to
`.env` and fill in. That made it a single point of failure with a terrible failure
mode: deleting one template file (a cleanup step, a sync tool, a stray `mv`)
turned into nineteen failing tests and four builders that could not run, with an
error that named the file but not the fix.

So the template lives here too. When it is missing, the builders restore it before
complaining about anything else, `pc-agent release env-template --write` does the
same thing on demand, and `scripts/verify_windows.py` reports it as its own gate
so it is one visible line instead of nineteen puzzling ones.

The content contains **no secrets by construction** - it is the sample file that
ships in the repository, and the same text is what `.env` is copied from.
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "ENV_TEMPLATE",
    "ENV_TEMPLATE_NAME",
    "env_template_path",
    "restore_env_template_if_missing",
    "write_env_template",
]

ENV_TEMPLATE_NAME = ".env.example"

#: The canonical template. Keep this byte-identical to the shipped file; the test
#: suite asserts that, so the two can never drift.
ENV_TEMPLATE = """# Copy this file to ".env" on your own machine and fill in values there.
# NEVER commit a real .env file and never share API keys in chat or source code.

# Local app configuration
PC_AGENT_ENVIRONMENT=development
PC_AGENT_WORKSPACE_DIR=workspace
PC_AGENT_DATA_DIR=data
# PC_AGENT_DATABASE_PATH=data/pc_agent.db
PC_AGENT_LOG_LEVEL=INFO
PC_AGENT_MAX_RETRIES=2
# Bounded Phase 3 self-healing: at most 3 recovery retries per task execution.
PC_AGENT_MAX_RECOVERY_ATTEMPTS=2
PC_AGENT_RECOVERY_BACKOFF_SECONDS=0.25
PC_AGENT_RECOVERY_STALE_AFTER_MINUTES=10
# Phase 4 user-requested web fetch limits. Private/local network targets stay
# blocked unless one direct command explicitly passes --allow-private.
PC_AGENT_WEB_TIMEOUT_SECONDS=15
PC_AGENT_WEB_MAX_BYTES=2000000

# Phase 6 offline licensing (optional). Enforcement defaults to off so source
# and development installs keep working without a license. Commercial builds
# set the vendor PUBLIC key (never a private key) and choose warn or enforce.
# PC_AGENT_LICENSE_ENFORCEMENT=off
# PC_AGENT_LICENSE_PUBLIC_KEY=
# PC_AGENT_LICENSE_KEY_ID=default
# PC_AGENT_LICENSE_FILE=data/license.key
# PC_AGENT_TRIAL_DAYS=14

# AI provider route. Fallback happens ONLY between providers listed here that
# are usable. Deterministic tasks (including supported PDF runs) do not call AI.
# Add "ollama" for a local model, "openai" for OpenAI, "anthropic" for the
# direct Claude Messages API, or "openai-compatible" for another endpoint.
PC_AGENT_PROVIDER_ORDER=gemini,openrouter
PC_AGENT_PROVIDER_TIMEOUT_SECONDS=30

# Provider keys: keep these only in your local .env.
GEMINI_API_KEY=
OPENROUTER_API_KEY=
OPENAI_API_KEY=
OPENAI_COMPATIBLE_API_KEY=
OLLAMA_API_KEY=
ANTHROPIC_API_KEY=

# Optional model overrides
PC_AGENT_GEMINI_MODEL=gemini-3.8-flash
PC_AGENT_OPENROUTER_MODEL=openrouter/auto
PC_AGENT_OPENAI_MODEL=gpt-5-mini
PC_AGENT_OPENAI_COMPATIBLE_MODEL=
PC_AGENT_OLLAMA_MODEL=llama3.1
PC_AGENT_ANTHROPIC_MODEL=claude-sonnet-5-5

# Optional base URL overrides (HTTPS required except loopback)
# PC_AGENT_GEMINI_BASE_URL=https://generativelanguage.googleapis.com/v1beta
# PC_AGENT_OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
# PC_AGENT_OPENAI_BASE_URL=https://api.openai.com/v1
# PC_AGENT_OPENAI_COMPATIBLE_BASE_URL=https://example.invalid/v1
# PC_AGENT_OLLAMA_BASE_URL=http://localhost:11434
# PC_AGENT_ANTHROPIC_BASE_URL=https://api.anthropic.com

# Phase 12: approved subprocess + local Git (off by default; commands are
# allow-listed, workspace-confined, time/output bounded, and write
# operations need explicit approval)
# PC_AGENT_ALLOW_SUBPROCESS=1
# PC_AGENT_COMMAND_ALLOWLIST=git,python,python3,py,node,npm
# PC_AGENT_COMMAND_TIMEOUT_SECONDS=60

# Phase 13: browser automation against a debug browser YOU start with a
# throwaway profile (off by default; see docs/phase13.md for the exact
# Edge/Chrome start commands).
# PC_AGENT_ALLOW_BROWSER=1
# PC_AGENT_BROWSER_PORT=9222
# PC_AGENT_BROWSER_TIMEOUT_SECONDS=20

# Phase 14: Windows UI Automation of already-open application windows
# (off by default; focus/click/type/keys still require per-action approval;
# see docs/phase14.md). Windows installs also get comtypes automatically.
# PC_AGENT_ALLOW_DESKTOP_AUTOMATION=1
# PC_AGENT_DESKTOP_AUTOMATION_TIMEOUT_SECONDS=30

# Phase 15/22: provider vision for one exact image/question and approved
# scanned-PDF OCR pages (off by default; local image tools and embedded
# PDF text work without this setting).
# PC_AGENT_ALLOW_VISION=1
# PC_AGENT_VISION_MAX_IMAGE_BYTES=10000000

# Phase 16/44/45: provider API-key lookup. auto (default) uses
# environment/.env first, then Windows Credential Manager on Windows,
# Linux Secret Service (secret-tool/libsecret) on Linux, or macOS Keychain
# (/usr/bin/security) on macOS. Use env to disable OS credential lookup, or
# windows/linux/macos to select a backend explicitly.
# PC_AGENT_CREDENTIAL_STORE=auto

# Phase 17: deterministic core self-repair (off by default). Diagnosis is
# read-only; baseline/stage/apply/rollback remain explicit CLI operations.
# PC_AGENT_ALLOW_SELF_REPAIR=1
# Per-task budgets (0=unlimited except steps, which are 1-20).
# PC_AGENT_TASK_TOKEN_BUDGET=20000
# PC_AGENT_TASK_PROVIDER_CALL_BUDGET=10
# PC_AGENT_TASK_TOOL_BUDGET=25
# PC_AGENT_TASK_STEP_BUDGET=5
# PC_AGENT_TASK_TIME_BUDGET_SECONDS=600

# Transformation Phase 2 Safety Kernel. Hard tool timeouts and the isolated
# worker are ON by default; only disable them deliberately. The emergency
# stop is normally managed with `pc-agent safety stop|resume`.
# PC_AGENT_SAFETY_HARD_TIMEOUTS=1
# PC_AGENT_SAFETY_WORKER_ISOLATION=1
# PC_AGENT_SAFETY_MAX_TOOL_SECONDS=600
# PC_AGENT_SAFETY_APPROVAL_EXPIRY_MINUTES=1440
# PC_AGENT_EMERGENCY_STOP=1
# Automation only: passphrase source for maintenance backup --encrypt /
# encrypted verify|restore. Interactive use should prefer the hidden prompt.
# PC_AGENT_BACKUP_PASSPHRASE=

# Transformation Phase 3: Local Service API, structured logging, profiles.
# The API is loopback-only; without a token here one is generated into
# data/api-token. Profiles hold non-secret settings only; explicit
# environment values always override a profile (pc-agent profiles ...).
# PC_AGENT_API_ENABLED=true
# PC_AGENT_API_HOST=127.0.0.1
# PC_AGENT_API_PORT=8765
# PC_AGENT_API_TOKEN=
# PC_AGENT_LOG_FORMAT=text
# PC_AGENT_PROFILE=
# Transformation Phase 4: Mission Center parallelism and the API
# scheduler tick (0 disables the loop; schedules still fire on
# pc-agent schedules tick).
# PC_AGENT_MISSION_MAX_PARALLEL=3
# PC_AGENT_SCHEDULER_INTERVAL_SECONDS=30

# Phase 21: signed offline updates. Put only the vendor PUBLIC key here;
# never copy a release-signing private key to a customer PC. Set required=1
# to reject unsigned or unverifiable portable bundles during staging.
# PC_AGENT_RELEASE_PUBLIC_KEY=
# PC_AGENT_RELEASE_KEY_ID=release-1
# PC_AGENT_REQUIRE_SIGNED_UPDATES=0

# Phase 24: explicit online signed update downloads. Disabled by default.
# When enabled, online-check fetches only this HTTPS feed; online-download
# and online-stage additionally verify the bundle SHA-256/signature.
# online-stage itself never auto-applies.
# PC_AGENT_ONLINE_UPDATES=0
# PC_AGENT_UPDATE_FEED_URL=https://updates.example.com/self-healing-pc-agent/stable.json
# PC_AGENT_UPDATE_CHANNEL=stable
# PC_AGENT_ONLINE_UPDATE_TIMEOUT_SECONDS=30
# PC_AGENT_ONLINE_UPDATE_MAX_BYTES=25000000

# Phase 56: Windows background auto-upgrade. This is a separate opt-in and
# requires the Phase 24 online feed above plus a release public key/key ID
# and PC_AGENT_REQUIRE_SIGNED_UPDATES=1. stage-only downloads and stages a
# verified signed update; apply-when-idle may also apply it when the local
# maintenance window, idle, AC-power and active-task gates all pass.
# PC_AGENT_AUTO_UPGRADE=0
# PC_AGENT_AUTO_UPGRADE_MODE=stage-only
# PC_AGENT_AUTO_UPGRADE_INTERVAL_HOURS=24
# PC_AGENT_AUTO_UPGRADE_IDLE_MINUTES=15
# PC_AGENT_AUTO_UPGRADE_MAINTENANCE_WINDOW=02:00-05:00
# PC_AGENT_AUTO_UPGRADE_REQUIRE_AC_POWER=1
# PC_AGENT_AUTO_UPGRADE_REINSTALL=0

# Transformation Phase 7: Provider Platform v2 (optional). Strategy is a
# routing preference only. Analytics are local (data/provider-usage.jsonl;
# no prompt text is stored). Prices and context sizes are never guessed:
# cost reporting exists only for entries you configure here.
# PC_AGENT_PROVIDER_STRATEGY=default
# PC_AGENT_PROVIDER_ANALYTICS=1
# PC_AGENT_PROVIDER_PRICES={"anthropic": {"input_per_1k": 0.003, "output_per_1k": 0.015}}
# PC_AGENT_MODEL_CONTEXT_TOKENS={"ollama": 8192}

# Transformation Phase 8: Memory v2 / knowledge embeddings (optional).
# Default is the offline deterministic hashing embedder (no network,
# no key): provider=auto/local/mock all stay local unless a concrete
# embedding backend below is configured. Remote backends send chunk
# text to that provider exactly like a normal completion would.
# PC_AGENT_EMBEDDING_PROVIDER=auto
# PC_AGENT_EMBEDDING_MODEL=
# PC_AGENT_EMBEDDING_DIMENSIONS=384
"""


def env_template_path(root_dir: Path | str) -> Path:
    return Path(root_dir).expanduser() / ENV_TEMPLATE_NAME


def write_env_template(root_dir: Path | str, *, force: bool = False) -> tuple[Path, bool]:
    """Write the template if it is missing. Returns (path, wrote_anything).

    ``force=True`` overwrites an existing file, which is only ever right when the
    user asks for it explicitly - a real `.env` is never touched, and the template
    is only rewritten on request.
    """

    target = env_template_path(root_dir)
    if target.exists() and not force:
        return target, False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(ENV_TEMPLATE, encoding="utf-8", newline="\n")
    return target, True


def restore_env_template_if_missing(root_dir: Path | str) -> Path | None:
    """Restore the template when it is absent; return the path if it was written."""

    target = env_template_path(root_dir)
    if target.is_file() and not target.is_symlink():
        return None
    path, wrote = write_env_template(root_dir)
    return path if wrote else None
