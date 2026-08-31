# Nexarl 协作工坊 — 代码改动报告

## Scope

- Tested commit: `cada208d90bcb00d168dd3af0155eff756139231`
- Comparison base: `d9835e4b70b1a9a09c29972096f2c98c9051292c`
- The Loop branch contains lifecycle hardening, approval-scope enforcement, project-boundary correction, release-boundary alignment, and UI publication compensation commits.

## Main code areas changed

- `scripts/vibe_memory_install.py`, `scripts/vibe_memory_cli.py`: persisted interpreter, stable launcher, LaunchAgent lifecycle, exact CAS, quarantine, rollback, uninstall, and interrupt-safe recovery.
- `scripts/vibe_memory_router.py`, `scripts/memory_project.py`: shared candidate command routing and registered/unregistered project boundary.
- `scripts/memory_review_queue.py`: approval target validation by candidate scope.
- `scripts/memory_review_server.py`: registered-project enforcement for project switching/init payloads; no implicit registry mutation.
- `scripts/ui_skill_publisher.py`, `scripts/ui_skill_registry.py`, `scripts/ui_design_cli.py`, `scripts/memory_review_server.py`: publication scope, global target binding, lock-time revalidation, and audit-failure compensation.
- `scripts/public_release_check.py`, `scripts/verify_release.py`: installer-aligned public asset scanning and 13-check release gate.
- Tests cover race windows, failure rollback, scope boundaries, migration fixtures, and real macOS installed-runtime behavior.

## Fresh evidence

- `python3 -m unittest discover -s tests -v`: 612 passed.
- `python3 scripts/verify_release.py --tree .`: all 13 checks `ok`.
- `python3 -m unittest tests.test_macos_install_e2e -v`: 3 passed on Darwin with loopback available.
- `python3 scripts/public_release_check.py --tree .`: 0 violations.
- `git diff --check`: clean.

## Safety boundaries

The branch does not merge main, deploy production, rewrite history, or delete retained memory data. Uninstall defaults to retaining personal/project memory, approvals, registry, Skills, Loop data, and worktrees unless explicit approved data-deletion paths are supplied.
