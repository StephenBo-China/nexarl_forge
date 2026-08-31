# AI Coding 管理后台 — 功能报告

## Tested implementation

- Commit: `cada208d90bcb00d168dd3af0155eff756139231`
- Branch: `loop/vibe-coding-manage-platform-20260730-universal-local-memory-manager`
- Platform: macOS 15.7.4 / Darwin 24.6.0 / arm64

## Delivered capabilities

- Clone-to-local macOS installer with persisted Python 3.10+ and stable `vibe-memory` launcher.
- Shared Codex and Claude Code user hooks with fail-open degraded responses, idempotency, safe queueing, and personal/project context routing.
- Registered-project boundary: registered cwd writes project memory; unregistered cwd produces personal candidates only. Project initialization and registration are separate, while already-registered init remains compatible.
- Approval-gated personal long/short memory and project long memory, retention, candidate deduplication, provenance, and scope-safe approvals.
- Migration and control-plane inventory covering review queue, policy, design preferences/packages/approvals/audit, UI Skills, Loop state, worktrees, and legacy hooks.
- Transactional install/update/start/repair/rollback/uninstall with LaunchAgent identity checks, exact CAS, quarantine, rollback, interrupt preservation, and data-retention safeguards.
- UI Skill publication scope enforcement and audit-failure compensation; release scanner and installer asset boundaries are aligned.
- Project API and review-queue boundaries reject unregistered roots; `/projects/init` no longer implicitly registers, and `/projects/use` requires an existing registry entry.

## Verification summary

- Codex full suite: 612/612 passed, 0 failures, 0 errors, 0 skips.
- Release gate: 13/13 checks `ok`.
- macOS install E2E: 3/3 passed with real loopback/installed-runtime execution.
- Public release tree: `public release tree check: ok`.

## Known release gates

Physical Intel hardware verification, separate Git-history secret scan, installed-console visual smoke, and final tracked-report confirmation remain procedural gates. No merge to main or production deployment is included.
