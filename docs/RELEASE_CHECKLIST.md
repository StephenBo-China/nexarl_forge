# Release Checklist

Use this order for every public release candidate. Record the commit, command,
timestamp, platform, Python version, exit status, pass/fail/skip counts, and
sanitized artifact paths. Never invent evidence or reuse an earlier run.

## User-journey acceptance

- [ ] On clean Apple Silicon and Intel macOS environments with Python 3.10+,
  run `git clone`, `cd vibe_coding_manage_platform`, and `./install.sh`.
- [ ] Repeat the clean path with `./install.sh --with-claude-hooks`; preserve
  unrelated pre-existing client hooks.
- [ ] Run `vibe-memory open` and complete every first-run choice: clients,
  candidate checks, retention, login startup, port, and optional workspace.
- [ ] In fresh Codex and Claude Code sessions, approve/trust the installed user
  configuration and prove the managed hooks—not source-tree shortcuts—run.
- [ ] Run `vibe-memory project register "/path/to/workspace"`, then
  `vibe-memory project init "/path/to/workspace"`; prove init creates no project
  hooks and an unregistered cwd remains personal-only.
- [ ] Run `vibe-memory migrate preview --project-root "/path/to/workspace"`,
  require clean JSON and zero preview exit status, then run
  `vibe-memory migrate apply --approved --project-root "/path/to/workspace"` on a
  registered legacy fixture; audit backups and changed paths.
- [ ] Exercise pending/active memory, projects, design preferences, UI design
  approval, UI Skills, Loop, and policy in the installed review console.
- [ ] Verify hooks write event metadata/reminders only, store no prompt text,
  call no model API, and manufacture no candidate. Verify the active client
  model distills personal/project/short candidates and approval gates formal
  memory.
- [ ] Run `vibe-memory doctor` and require all runtime, hooks, service, data,
  and control-plane areas healthy.
- [ ] From a reviewed clone run
  `vibe-memory update --source-root "/path/to/local/clone"`, then test
  `vibe-memory rollback` without losing post-update data.
- [ ] Run `vibe-memory repair`, `vibe-memory hooks status`, and
  `vibe-memory hooks repair`; verify LaunchAgent recovery and fresh-client
  restart behavior.
- [ ] With login startup disabled, run
  `vibe-memory start && vibe-memory open`; prove it recreates a manual
  `RunAtLoad=false` plist, starts a healthy current service, and does not change
  the persisted preference.
- [ ] Run `vibe-memory uninstall`; prove runtime, launcher, LaunchAgent, and
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
- [ ] Run `python3 scripts/public_release_check.py --tree .`; require an empty
  violation list. Failure output may show only root-relative path and pattern,
  never matched secret text.
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
