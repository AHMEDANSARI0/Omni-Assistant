# Feature Matrix

Status values are from the Phase 0 audit of GitHub commit
`89610f3d18f9348b6fe8ead679bab81ee34155df`, reconciled against the delivered
Phase 60 tree. **COMPLETE** means complete only within the bounded scope in
the notes.

| Capability | Status | Current boundary / next phase |
|---|---|---|
| Local CLI/GUI/SQLite/tool core | PARTIAL | Strong reusable foundation; service and safety boundaries evolve in Phases 2–3 |
| JARVIS command center | PARTIAL | Functional Tkinter GUI; modern shell in Phase 5 |
| Accessibility/localization/RTL | MISSING | Phase 17, with requirements carried into Phase 5 |
| Voice/wake word | MISSING | Phase 10 |
| Orchestration/planning | PARTIAL | Sequential five-step maximum; graph in Phase 4 |
| Parallel/multi-agent delegation | MISSING | Phase 9 after Phases 2 and 4 |
| Task/Mission Center | PARTIAL | Lifecycle exists; metadata/scheduler in Phase 4 |
| Structured memory/workflows | PARTIAL | Notes/preferences/exact workflows; Memory v2 in Phase 8 |
| Knowledge/RAG/vector search | MISSING | Phase 8 |
| Provider router | PARTIAL | Six external slots plus mock; Provider v2 in Phase 7 |
| Live provider compatibility | UNVERIFIED | Secret-safe live matrix still required |
| Built-in tools | PARTIAL | 62 bounded tools; worker isolation in Phase 2 |
| Office/PDF/image tools | PARTIAL | Strong bounded engines; local OCR/knowledge extraction later |
| PDF trust/PAdES external acceptance | UNVERIFIED | External validator/reader evidence required |
| Dynamic tools/plugins | PARTIAL | Pure-compute restricted subprocess; plugin v2 in Phase 15 |
| Browser automation | PARTIAL | Local CDP only; native matrix and expansion in Phase 11 |
| Desktop automation/CV | PARTIAL | Exact Windows UIA subset; Phase 11 |
| Native Windows acceptance | UNVERIFIED | User Windows evidence required for exact builds |
| Visual workflow builder | MISSING | Phase 12 |
| Calendar/email/chat/cloud connectors | MISSING | Phase 13 |
| Proactive intelligence/notifications | MISSING | Phase 14 |
| Live system monitoring | PARTIAL | Static snapshot only; Phase 6 |
| Diagnostics/self-healing | PARTIAL | Task/app recovery exists; broader diagnostics in Phase 6 |
| Policy/approvals/emergency stop | PARTIAL | Exact approvals exist; unified center/worker stop in Phase 2 |
| Hard timeout/isolation | BROKEN | Built-in registry timeout is post-hoc; repair in Phase 2 |
| OS credential storage | PARTIAL | Win/Linux/macOS backends; native proof varies by platform |
| Encryption/privacy governance | PARTIAL | SQLite/backups plaintext; Phase 2 design/controls |
| Database/backups/migrations | PARTIAL | Schema v6 and verified backups; concurrency/upgrade hardening later |
| General application API | MISSING | Phase 3 |
| Cloud/device sync | MISSING | Phase 16 |
| SaaS accounts/tenancy/billing | MISSING | Deferred beyond personal local transformation |
| Offline licensing | COMPLETE | Bounded signed-token licensing; not accounts or payments |
| Logging/metrics/tracing | PARTIAL | SQLite records strong; structured events in Phase 3 |
| Analytics/evaluation/cost | MISSING | Phase 7 |
| Settings/profiles/resource limits | PARTIAL | Environment settings and task budgets; Phase 3 |
| Performance/resource governance | PARTIAL | Linux smoke data only; Windows benchmarks in Phases 1/17 |
| Test/quality system | PARTIAL | Large deterministic suite; CI/native/E2E layers in Phase 18 |
| Packaging/update/rollback | PARTIAL | Signed bundles/feeds and package builders; signing/native proof later |
| Supply-chain integrity | PARTIAL | Release signing exists; lock/SBOM/CI/Authenticode later |
| Required documentation | PARTIAL | Phase 1 root set started; complete set in Phase 18 |
| Cross-platform parity | PARTIAL | Windows-first; Linux partial; macOS packaging absent |

The detailed evidence, source modules and test references are in the Phase 0
`AUDIT_REPORT.md` retained with the project audit records.
