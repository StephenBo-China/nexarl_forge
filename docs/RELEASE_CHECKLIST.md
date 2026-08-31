# Release Checklist

Use this order for every public release candidate. Record the commit, command,
timestamp, platform, Python version, exit status, pass/fail/skip counts, and
sanitized artifact paths. Never invent evidence or reuse an earlier run.

## User-journey acceptance

- [ ] On clean Apple Silicon and Intel macOS environments with Python 3.10+,
  run `git clone`, `cd vibe_coding_manage_platform`, and `./install.sh`.
- [ ] Repeat the clean path with `./install.sh --with-claude-hooks`; preserve
  unrelated pre-existing client hooks.
- [ ] Run `nexarl-forge open` and complete every first-run choice: clients,
  candidate checks, retention, login startup, port, and optional workspace.
- [ ] In fresh Codex and Claude Code sessions, approve/trust the installed user
  configuration and prove the managed hooks—not source-tree shortcuts—run.
- [ ] Run `nexarl-forge project register "/path/to/workspace"`, then
  `nexarl-forge project init "/path/to/workspace"`; prove init creates no project
  hooks and an unregistered cwd remains personal-only.
- [ ] Run `nexarl-forge migrate preview --project-root "/path/to/workspace"`,
  require clean JSON and zero preview exit status, then run
  `nexarl-forge migrate apply --approved --project-root "/path/to/workspace"` on a
  registered legacy fixture; audit backups and changed paths.
- [ ] Exercise pending/active memory, projects, design preferences, UI design
  approval, UI Skills, Loop, and policy in the installed review console.
- [ ] Verify hooks write event metadata/reminders only, store no prompt text,
  call no model API, and manufacture no candidate. Verify the active client
  model distills personal/project/short candidates and approval gates formal
  memory.
- [ ] Run `nexarl-forge doctor` and require all runtime, hooks, service, data,
  and control-plane areas healthy.
- [ ] From a reviewed clone run
  `nexarl-forge update --source-root "/path/to/local/clone"`, then test
  `nexarl-forge rollback` without losing post-update data.
- [ ] Run `nexarl-forge repair`, `nexarl-forge hooks status`, and
  `nexarl-forge hooks repair`; verify LaunchAgent recovery and fresh-client
  restart behavior.
- [ ] With login startup disabled, run
  `nexarl-forge start && nexarl-forge open`; prove it recreates a manual
  plist with both `RunAtLoad=false` and `KeepAlive=false`, starts a healthy
  current service, does not change the persisted preference, and has no
  simulated next-login auto-start or automatic relaunch semantics.
- [ ] With login startup enabled, run `nexarl-forge start`; prove the persisted
  plist keeps both `RunAtLoad=true` and `KeepAlive=true`, the current service
  becomes healthy, and the saved preference remains enabled.
- [ ] Run `nexarl-forge uninstall`; prove runtime, launcher, LaunchAgent, and
  managed hooks are removed while memories, review/audit state, projects,
  design state, UI Skills, Loop state, logs, and backups remain.
- [ ] Separately verify data deletion is refused unless `--remove-data`,
  `--approved-data-deletion`, and the exact allowlisted regular-file target
  `--data-path "$HOME/.codex/memory_review/projects.json"` are all supplied.
  Reject every directory, symlink, unknown path, or invalid batch before
  bootout or any hook/runtime mutation.

## Automated release evidence

- [ ] Freeze and record a clean candidate commit; run `git diff --check`.
- [ ] Run `python3 -m unittest discover -s tests -v` with zero failures/errors.
- [ ] Run `python3 scripts/verify_release.py --tree .` and require all 13-gate
  checks to be `ok`: manifest, Python, unit tests, install E2E, public tree,
  plist, loopback, permissions, Codex hook, Claude hook, control plane,
  rollback, and uninstall.
- [ ] On macOS, require the real Darwin installed-runtime E2E to execute and
  pass; a skip is not release evidence. It must use a disposable HOME, dynamic
  loopback port, installed launcher, real LaunchAgent/service, first-run API,
  registered project, migration apply, lifecycle commands, and complete cleanup.
- [ ] Run `python3 scripts/public_release_check.py --tree .` against the tested
  implementation tree; require an empty violation list. This does not cover a
  nested untracked report package. Failure output may show only root-relative
  path and pattern, never matched sensitive text.
- [ ] Run a separate Git-history secret scan. Stop publication if it finds a
  material secret; history rewriting is a separately approved destructive task.
- [ ] Run a visual smoke test against the installed review console, not a
  source development server.

## Independent evaluation and reports

- [ ] Start a fresh independent Claude Code evaluation at the tested commit.
  Give it the original acceptance criteria, completion plan, and disposable
  installed-runtime URL. Require code inspection, the release gate, installed
  launcher/first-run/migration/lifecycle tests, and structured P0/P1/P2/P3
  findings.
- [ ] Accept the Claude Code evaluation only with verdict `pass`, zero open
  P0/P1, and no reproducible acceptance failure. Fix failures through fresh
  RED/GREEN evidence and repeat evaluation on the new commit.
- [ ] Store sanitized real evaluator evidence in the designated Loop Markdown
  and JSON reports; never fabricate commands, screenshots, browser results, or
  verdicts.
- [ ] Produce the three release reports: functional acceptance, code changes,
  and independent evaluation. Each report identifies the exact tested commit
  and links only sanitized evidence.
- [ ] Confirm the candidate tree contains no personal filesystem paths, private
  memory, runtime data, credentials, tokens, verification codes, cloud secrets,
  local-only URLs, or generated caches.

The gate is local-only. It requires no production credentials, production
deployment, main/master merge, or public network access. Those actions remain
separate explicit approval boundaries.

## Current candidate evidence (2026-08-14)

Tested implementation commit: `cada208d90bcb00d168dd3af0155eff756139231`.
Final-commit Codex verification ran on macOS 15.7.4 (Build 24G517), Apple
Silicon (`arm64`), Python 3.14.6; per-command timestamps/durations were not
separately captured. Claude postfix evidence was captured at
`2026-08-14T12:13:31Z` in a clean disposable clone.

Overall candidate status: **Task 9 release-evidence gate PASS; PUBLIC RELEASE
READINESS NOT COMPLETE.** Unchecked physical Intel, separate Git-history secret
scan, and installed-console visual-smoke gates continue to block the candidate.

- [x] Full suite: 612 tests, 0 failures/errors/skips, exit `0` in fresh Codex
  and Claude final-commit runs; Claude recorded 69.961 seconds.
- [x] Install module only: 102 tests, 0 skips, 5.874 seconds, exit `0` in the
  Claude postfix run. Do not report this focused count as the full suite.
- [x] Release gate: all 13 keys `ok`, exit `0`.
- [x] Tested implementation-tree scan: `public release tree check: ok`, exit
  `0`; this result does not cover the nested report package.
- [x] Independent final report-package content scan: read-only `rg` checks over
  the checklist and `docs/reports/` returned 0 macOS absolute home-path matches and 0
  credential-like assigned-value/provider-token-shape matches. Both commands
  exited `1` with no output, meaning no matches; this is not a history scan.
- [ ] After the report-only commit, confirm the checklist, three reports, and
  both evidence-bundle files are tracked by Git.
- [x] Standalone Darwin installed-runtime E2E: 3 tests, 0
  failures/errors/skips, exit `0` in fresh Codex and Claude final-commit runs;
  Claude recorded 34.272 seconds and no macOS skip.
- [x] Lifecycle P1 remediation: final review found `LC-01`; old-implementation
  RED produced 6 failures plus 7 errors; TDD commits `2d5b256` and `3be3bc0`
  produced GREEN across 28 focused subcases; final specification and quality
  reviews both returned `APPROVED`; postfix evaluation marks `LC-01` closed.
- [x] Independent Claude Code postfix evaluation: operational model label
  `claude-sonnet-4-6`, clean disposable clone, full 612, release 13/13, Darwin
  E2E 3/3, focused lifecycle 28, verdict `pass`, P0/P1=0/0, P2/P3=1/2, and no
  acceptance failures. Prior `a94f356` CLI plus curl/HTTP runtime evidence is
  inherited only for unchanged surfaces; no new-commit manual URL rerun is
  claimed.
- [ ] Physical Intel macOS evidence: not run in this verification session.
- [ ] Separate Git-history secret scan and installed-console visual smoke:
  retain as explicit evidence requirements; the public-tree scan does not
  substitute for either.

Candidate reports: [functional](reports/universal-memory-manager-functional-report.md),
[code changes](reports/universal-memory-manager-code-change-report.md), and
[evaluation](reports/universal-memory-manager-evaluation-report.md). Tracked,
sanitized Claude evidence: [Markdown](reports/evidence/universal-memory-manager-claude-evidence.md)
and [JSON](reports/evidence/universal-memory-manager-claude-evidence.json).
Ignored raw `loop/reports/` artifacts, including the corrected postfix pair,
remain local traceability sources only.
CLI init metadata reported `claude-sonnet-4-6`; both raw `claude_gate.json` and
`universal_memory_manager_claude_static.json` separately self-label
`claude-sonnet-4-5`. The public bundle omits the raw init metadata records and
cannot independently adjudicate the model identity; this workflow uses the CLI
init metadata value as its operational label.
