# Architecture

This document freezes the architecture boundary for Transformation Phase 1.
Detailed historical contracts remain in `docs/architecture.md` and the phase
documents. This root document records what is canonical now and which
boundaries later transformation phases must preserve.

## Canonical baseline

- Audited GitHub comparison point: `main` at
  `89610f3d18f9348b6fe8ead679bab81ee34155df` (Phase 58, 0.60.0).
- Canonical implementation source after reconciliation: the delivered
  Phase 60 tree (0.62.0), including Phase 59 and Phase 60.
- Transformation Phase 1 application version: 0.63.0.
- Windows acceptance for this exact build remains pending real Windows
  evidence. Linux test success is not a Windows platform claim.

## Current logical layers

1. **Presentation**: `pc_agent.cli`, `pc_agent.gui.app`.
2. **Application facades**: `pc_agent.gui.controller`, CLI command handlers,
   diagnostics and release operations.
3. **Orchestration**: deterministic/AI planning, orchestration, verification,
   budgets and recovery.
4. **Tools**: schema-validated built-in registry, workspace policy and the
   restricted dynamic-tool subsystem.
5. **Providers**: synchronous provider contracts, adapters and fallback router.
   Providers propose text/JSON only; they never receive tool handles or
   approval authority.
6. **Storage**: SQLite schema v6 repositories for tasks, plans, approvals,
   invocations, checkpoints, recovery, licenses, memory and dynamic tools.
7. **Platform adapters**: browser CDP, Windows UI Automation, OS credential
   stores, signed updates, packaging and operating-system schedulers.

## Trust boundaries

- Model output, web pages, browser content, imported workflows and dynamic
  tool source are untrusted until locally validated.
- The local same-user operating-system account is currently the principal
  trust boundary. Local SQLite data and backups are not encrypted at rest.
- High-impact gates default off. Designated browser, desktop, command,
  Git-write and vision actions require exact approval in the orchestrated
  path.
- Signed update artifacts are verified locally; TLS and release-key custody
  remain operator responsibilities.
- The vendor update-feed host is not the future application API.

## Frozen decisions for later phases

- Preserve deterministic local execution whenever a safe deterministic plan
  exists.
- Do not claim hard tool timeout, OS sandboxing, Windows acceptance or
  provider capability until implemented and tested at that boundary.
- Introduce the isolated worker and hard cancellation before parallel or
  multi-agent side effects.
- Introduce a versioned local service/API before a web or multi-process UI.
- Add semantic memory only with provenance, user review and deletion controls.
- Keep cloud/device features optional and local-first.
- Do not build SaaS tenancy or billing into the personal local core during
  the 18-phase transformation; preserve replaceable interfaces only.
