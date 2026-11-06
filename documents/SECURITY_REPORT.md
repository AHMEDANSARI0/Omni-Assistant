# Security Report

## Scope

This report summarizes the Phase 0 audit at GitHub commit
`89610f3d18f9348b6fe8ead679bab81ee34155df`, reconciled with the delivered
Phase 60 implementation baseline. It is not a penetration test and does not
claim absolute security.

## Controls already present

- Deterministic planning before AI planning; local validation of every
  model-proposed plan.
- Workspace path policy and traversal/symlink defenses.
- Default-off subprocess, browser, desktop-automation, vision, self-repair
  and online-update gates.
- Exact approvals for designated external, command, Git-write, browser,
  desktop and vision actions.
- OS credential stores on Windows, Linux and macOS; credential values are
  not printed by status commands.
- Bounded web, provider, document, PDF, image and dynamic-tool inputs.
- Signed release/update verification, downgrade refusal and rollback paths.
- Dynamic pure-compute tools require static validation, restricted
  subprocess tests, approval, versioning and rollback.

## Scanner and history evidence

- Bandit on the Phase 58 audit tree: 95 findings (77 Low, 16 Medium, 2
  High), manually triaged. The High SHA-1 findings are standards-required
  WebSocket and PDF LTV identifiers. The dynamic-tool `exec` boundary,
  XML parsing and update-host network exposure remain review priorities.
- `pip-audit --local` found no known vulnerabilities in auditable installed
  packages at audit time; the local project itself was skipped because it
  is not on PyPI.
- Transformation Phase 1 performed a non-shallow GitHub history scan: 9
  commits, 533 reachable blobs and 11,420,515 blob bytes. High-confidence
  AWS, GitHub, OpenAI-style, Google and Slack token patterns had zero hits.
  The only private-key-header hit is source code that rejects private keys
  from an update-hosting package. This pattern scan is not an entropy scan
  and cannot prove that no low-pattern secret was ever present.

## Residual risks

1. Built-in tool timeouts are post-hoc; a hung synchronous handler cannot be
   interrupted by the registry. Repair is Transformation Phase 2.
2. SQLite data, memory, task history and database backups are plaintext to
   the local OS account.
3. Dynamic tools use restricted subprocess isolation, not a complete OS
   sandbox or CPU/RAM quota system.
4. Approved commands and Git operations run with user privileges.
5. A user-launched browser DevTools port is a powerful local control
   surface while open.
6. Desktop screenshots and provider vision/OCR can expose sensitive content;
   approvals and data minimization must be preserved.
7. Web fetch is SSRF-reduced, not a pinned-IP client; DNS rebinding remains
   a test priority.
8. The vendor update-feed serving command can bind beyond loopback and must
   be deployed only with deliberate TLS/firewall decisions.
9. Vendor license/release admin entry points ship in normal packaging even
   though private keys must remain off customer machines.
10. Native Windows security behavior requires exact-build Windows evidence.

## Security rule for later phases

No later phase may silently weaken approvals, path policy, credential
storage, signed updates, provider/data boundaries or audit records. New
network listeners, connectors, plugins, voice capture and cloud features
require explicit threat models and tests before enablement.
