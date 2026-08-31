# Universal Memory Manager Claude Evidence Bundle

This tracked, sanitized bundle is the public evidence summary for the independent Claude evaluation. It is derived from ignored local evaluator artifacts and does not contain credentials, prompt text, or machine-specific disposable paths.

Tested implementation commit: `cada208d90bcb00d168dd3af0155eff756139231`

Prior installed-runtime commit: `a94f356ddeb07cfb2478790f8a9d0bc090469113`

Overall Claude verdict: **pass**

Open findings: P0=0, P1=0, P2=0, P3=2. Acceptance failures: 0.

## Provenance

- Two successful Claude CLI session init metadata records reported `claude-sonnet-4-6`. The public bundle does not include those raw init records and therefore cannot independently adjudicate the model identity. This evaluation workflow uses the CLI init metadata value as its operational model label.
- `loop/reports/claude_gate.json` self-labels `claude-sonnet-4-5`. This is the first recorded provenance mismatch.
- `loop/reports/universal_memory_manager_claude_static.json` self-labels `claude-sonnet-4-5`. This is the second, separate recorded provenance mismatch.
- The raw static JSON lists 11 primitive operations, while its Markdown groups the same inspection work into 8 steps. This bundle fixes the canonical representation at 8 ordered static inspection groups by folding repository/subsystem enumeration and related file reads into their documented groups. The Markdown and JSON bundles use the same IDs, order, commands, exit fields, and outcomes.
- The runtime stage used Claude Code 2.1.85, the installed CLI, and curl/HTTP against a loopback service in an isolated HOME. No Playwright run occurred.
- The postfix stage used operational model label `claude-sonnet-4-6` in a clean disposable clone at the final commit. It freshly ran the full suite, install module, release gate, Darwin E2E, and focused lifecycle delta. It inherits the prior manual CLI/curl runtime evidence only for unchanged surfaces and does not claim that the final commit repeated the manual URL flow.

## Sanitized environment

| Stage | Timestamp | Environment |
|---|---|---|
| gate | `2026-08-14T10:09:50Z` | isolated evaluator tree |
| static | `2026-08-14T10:14:33Z` | `<DISPOSABLE_STATIC_REPO>` |
| runtime | `2026-08-14T18:40:00+08:00` | Claude Code 2.1.85; Python 3.14.6; Darwin 24.6.0 arm64; HOME `<DISPOSABLE_EVAL_HOME>`; URL `http://127.0.0.1:<DISPOSABLE_PORT>` |
| final | `2026-08-14T18:41:00+08:00` | read-only staged synthesis |
| postfix | `2026-08-14T12:13:31Z` | clean disposable clone; Claude Code 2.1.85; model `claude-sonnet-4-6`; Python 3.14.6; Darwin 24.6.0 arm64 |

## Canonical ordered command records

`N/A (read-only inspection)` is used when the evidence records a source-read/grouped inspection rather than a separately captured process exit. Runtime records retain `not separately recorded` because the raw runtime evidence captured observed results but not individual exit codes; no exit value is invented. Records G/S/RT/F describe prior `a94f356` evidence. Records D/P describe final-review discovery, TDD correction, review approval, and fresh postfix verification at `3be3bc0`. The `RT-*` namespace identifies runtime command records; the separate `R-*` namespace below identifies open findings.

| ID | Stage | Command or operation | Exit | Outcome |
|---|---|---|---|---|
| G-01 | gate | `git rev-parse HEAD && git status --short` | `0` | HEAD matched the tested commit; isolated evaluator tree clean. |
| G-02 | gate | `python3 scripts/verify_release.py --tree .` | `0` | All 13 release checks returned `ok`. |
| S-01 | static | Enumerate `<DISPOSABLE_STATIC_REPO>`, `scripts/`, and evaluator-report inventory. | `N/A (read-only inspection)` | Repository and relevant subsystem/evidence files identified. |
| S-02 | static | Inspect line counts for `vibe_memory_install.py`, `vibe_memory_hooks.py`, `vibe_memory_router.py`, `vibe_memory_paths.py`, `vibe_memory_settings.py`, and `vibe_memory_migration.py`. | `N/A (read-only inspection)` | File sizes established for bounded review. |
| S-03 | static | Inspect `git log --oneline -8`. | `N/A (read-only inspection)` | Tested commit and recent lifecycle-hardening history confirmed. |
| S-04 | static | Enumerate `templates/loop/` and `templates/macos/`. | `N/A (read-only inspection)` | Loop and LaunchAgent template assets identified. |
| S-05 | static | Read `scripts/vibe_memory_install.py` in chunks 1-1000, 1001-2000, and 2001-2901. | `N/A (read-only inspection)` | Python discovery, launcher, lifecycle, migration validation, and uninstall controls inspected. |
| S-06 | static | Read `vibe_memory_hooks.py`, `vibe_memory_router.py`, `vibe_memory_paths.py`, targeted `vibe_memory_migration.py`, and `com.noema.vibe-memory.plist`. | `N/A (read-only inspection)` | Shared hooks, routing, canonical paths, 17-area control plane, and LaunchAgent behavior inspected. |
| S-07 | static | Search `scripts/` for `anthropic` and `openai` model-API imports. | `N/A (read-only inspection)` | No model-API import was found in hook/router implementation. |
| S-08 | static | Read targeted `vibe_memory_settings.py` and `vibe_memory_cli.py` ranges. | `N/A (read-only inspection)` | First-run, retention, approval, and installed CLI behavior inspected. |
| RT-01 | runtime | `<DISPOSABLE_EVAL_HOME>/.local/bin/vibe-memory doctor --json` | `not separately recorded` | Runtime, service, hooks, data, and control plane all `ok`. |
| RT-02 | runtime | `curl http://127.0.0.1:<DISPOSABLE_PORT>/health` | `not separately recorded` | Healthy Vibe Memory 1.0.0 response with data schema 1. |
| RT-03 | runtime | `curl http://127.0.0.1:<DISPOSABLE_PORT>/` | `not separately recorded` | Expected first-run wizard HTML returned in isolated HOME. |
| RT-04 | runtime | `vibe-memory project register <DISPOSABLE_PROJECT>` | `not separately recorded` | Project registered with `memory_status: not_initialized`. |
| RT-05 | runtime | `vibe-memory project init <DISPOSABLE_PROJECT>` | `not separately recorded` | Project initialized; 18 managed files created. |
| RT-06 | runtime | `vibe-memory project list` | `not separately recorded` | Initialized project listed. |
| RT-07 | runtime | `vibe-memory hook --agent claude-code --event SessionStart` from `<DISPOSABLE_UNREGISTERED_CWD>` | `not separately recorded` | Personal-only context returned; no project files created. |
| RT-08 | runtime | `vibe-memory hook --agent claude-code --event SessionStart` from `<DISPOSABLE_PROJECT>` | `not separately recorded` | Project and personal context returned; context packet written. |
| RT-09 | runtime | `vibe-memory migrate preview --project-root <DISPOSABLE_PROJECT>` | `not separately recorded` | Clean preview; no legacy targets or errors. |
| RT-10 | runtime | `vibe-memory migrate apply --project-root <DISPOSABLE_PROJECT> --approved` | `not separately recorded` | Applied with unchanged result; audit written. |
| RT-11 | runtime | `vibe-memory hooks status` | `not separately recorded` | Codex and Claude hooks current. |
| RT-12 | runtime | `vibe-memory hooks repair` | `not separately recorded` | No repair needed. |
| RT-13 | runtime | `vibe-memory doctor --json` after flows | `not separately recorded` | All areas remained `ok`. |
| RT-14 | runtime | `curl -X PATCH http://127.0.0.1:<DISPOSABLE_PORT>/api/settings` with `formal_memory_requires_approval=false` | `not separately recorded` | Request rejected; approval must remain true. |
| RT-15 | runtime | `curl -X POST http://127.0.0.1:<DISPOSABLE_PORT>/api/settings/first-run` with `formal_memory_requires_approval=false` | `not separately recorded` | Request rejected; approval must remain true. |
| F-01 | final | Read gate, static, and runtime evidence and synthesize prior adjudication. | `N/A (read-only inspection)` | Prior `a94f356` overall `pass`; P0=0, P1=0, P2=1, P3=2; no acceptance failure. |
| D-01 | remediation | Review lifecycle cleanup across update, rollback, and repair. | `N/A (read-only inspection)` | Blocking P1 `LC-01` identified: dropped BaseException, non-exact hook rollback, concurrent-change overwrite risk, and lost cleanup sub-failures. |
| D-02 | remediation | `python3 -m unittest tests.test_vibe_memory_install.LaunchAgentLifecycleTest.test_lifecycle_hook_rollback_uses_immediate_commit_snapshots -v` against the old implementation. | `nonzero (exact code not separately recorded)` | Genuine RED: 6 failures and 7 errors. |
| D-03 | remediation | Inspect TDD commit `2d5b256` (`fix: preserve concurrent lifecycle hook changes`). | `N/A (read-only inspection)` | Immediate commit snapshots and concurrent hook-change preservation implemented with focused tests. |
| D-04 | remediation | Inspect TDD follow-up commit `3be3bc0` (`fix: preserve lifecycle failures during cleanup`). | `N/A (read-only inspection)` | Original interruption identity and aggregated lifecycle cleanup failures preserved. |
| D-05 | remediation | Final specification review. | `N/A (read-only inspection)` | `APPROVED`. |
| D-06 | remediation | Final code-quality review. | `N/A (read-only inspection)` | `APPROVED`. |
| P-01 | postfix | `git rev-parse HEAD && git status --short` in `<DISPOSABLE_POSTFIX_REPO>`. | `not separately recorded` | HEAD matched the final tested commit; disposable clone clean. |
| P-02 | postfix | `python3 scripts/verify_release.py --tree .` | `0` | All 13 release gates returned `ok`. |
| P-03 | postfix | `python3 -m unittest discover -s tests -v` | `0` | Full suite: 518 tests in 69.961 seconds, 0 failures/errors/skips, `OK`. |
| P-04 | postfix | `python3 -m unittest discover -s tests -p test_vibe_memory_install.py` | `0` | Install module only: 102 tests in 5.874 seconds, 0 skips, `OK`; not the full-suite count. |
| P-05 | postfix | `python3 -m unittest tests.test_macos_install_e2e -v` | `0` | Darwin E2E: 3 tests in 34.272 seconds, 0 failures/skips, `OK`. |
| P-06 | postfix | `python3 -m unittest tests.test_vibe_memory_install.LaunchAgentLifecycleTest.test_lifecycle_hook_rollback_uses_immediate_commit_snapshots -v` | `0` | GREEN: one selector in 1.466 seconds; all 28 lifecycle subcases passed. |
| P-07 | postfix | `git diff a94f356ddeb07cfb2478790f8a9d0bc090469113..3be3bc070666d20325dc94c5521822a638aed30c --stat` | `0` | Delta limited to lifecycle install logic and its tests: 2 files, +481/-96. |
| P-08 | postfix | Read prior runtime evidence, final delta, fresh results, and review decisions; synthesize postfix adjudication. | `N/A (read-only inspection)` | Final verdict `pass`; `LC-01` closed; P0=0, P1=0, P2=1, P3=2; no acceptance failure. |

## Stage results

| Stage | Verdict | P0 | P1 | P2 | P3 | Acceptance failures |
|---|---:|---:|---:|---:|---:|---:|
| gate | pass | 0 | 0 | 0 | 0 | 0 |
| static | pass | 0 | 0 | 0 | 0 | 0 |
| runtime | pass | 0 | 0 | 1 | 2 | 0 |
| final | pass | 0 | 0 | 1 | 2 | 0 |
| remediation | approved | 0 | 0 | 0 | 0 | 0 |
| postfix | pass | 0 | 0 | 1 | 2 | 0 |

## Open findings and residual risks

- Finding P2 R-01: a hook invoked by a test harness from the wrong cwd resolves personal-only context and does not populate the project context packet.
- Finding P3 R-02: a newly initialized project may immediately show cosmetic `managed_hooks_status: upgrade_available`.
- Finding P3 R-03: the isolated evaluation HOME displayed the expected first-run wizard because `first_run_complete=false`.
- Closed P1 LC-01: lifecycle cleanup now preserves original BaseException identity, uses exact expected-current rollback for hook writes, preserves concurrent changes, and aggregates cleanup failures. RED was 6 failures plus 7 errors; GREEN passed 28 subcases; specification and quality reviews were `APPROVED`.
- Physical Intel hardware was not tested. Signed/notarized app or pkg packaging and Linux/Windows support remain deferred.
- `launchctl print` ownership parsing and the subsequent non-CAS bootout remain a lifecycle maintenance risk.
- Hook smoke output can grow temporary-disk usage until process completion or the 10-second timeout.

## Local source artifacts

The ignored raw sources remain available only for this worktree's traceability at:

- `loop/reports/claude_gate.{md,json}`
- `loop/reports/universal_memory_manager_claude_static.{md,json}`
- `loop/reports/universal_memory_manager_claude_runtime.{md,json}`
- `loop/reports/universal_memory_manager_claude_eval.{md,json}`
- `loop/reports/universal_memory_manager_claude_postfix.{md,json}`

These ignored paths are not the public report dependency; this tracked bundle is.

## Independent final report-package scan

The implementation-tree `public_release_check.py --tree .` result does not cover this nested, currently untracked report package. A separate read-only scan was therefore run across `docs/RELEASE_CHECKLIST.md` and `docs/reports/`.

- Absolute user-path scan: `rg` searched for the standard macOS absolute home-path prefix; exit `1` with no output, meaning 0 matches.
- Sensitive-value scan: `rg` searched for credential-label/value assignments and common provider-token shapes; exit `1` with no output, meaning 0 matches.
- These are content scans, not a Git-history scan and not proof that the files are tracked. The report-only commit must be followed by an explicit tracked-file check.
