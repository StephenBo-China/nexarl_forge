# Vibe Memory

Vibe Memory is a local, approval-gated memory manager shared by Codex and
Claude Code. It installs a versioned runtime, a stable `vibe-memory` command,
universal user hooks, and a loopback-only review console. Moving or deleting
the source clone after installation does not break the installed runtime.

The first public release supports macOS (Apple Silicon and Intel) and requires
Python 3.10+. It has no third-party Python dependencies. See the
[Chinese user guide](docs/MEMORY_REVIEW_USER_GUIDE.zh-CN.md) for the complete
Chinese walkthrough and the [release checklist](docs/RELEASE_CHECKLIST.md)
before publishing a release candidate.

## Install from a clone

Use a normal Terminal. Replace the example repository URL if you are installing
from a fork:

```bash
git clone https://github.com/noema-ai/vibe_coding_manage_platform.git
cd vibe_coding_manage_platform
./install.sh
```

To enable both Codex and Claude Code hooks during installation:

```bash
./install.sh --with-claude-hooks
```

The installer verifies macOS and Python 3.10+, copies an immutable release to
`~/Library/Application Support/VibeMemory/`, creates
`~/.local/bin/vibe-memory`, structurally merges manager-owned user hooks,
installs `~/Library/LaunchAgents/com.noema.vibe-memory.plist`, starts the local
service, and runs `vibe-memory doctor`. Unrelated hooks are preserved and
changed managed files receive timestamped backups.

If `vibe-memory` is not found in a new shell, add `~/.local/bin` to `PATH`.

## Complete first run

Open the healthy local service:

```bash
vibe-memory open
```

On first run, choose:

- Codex hooks and, optionally, Claude Code hooks;
- automatic candidate checks;
- personal short-memory retention days;
- whether the LaunchAgent starts at login;
- the loopback service port (default `8897`);
- an optional workspace to register.

Formal personal long memory, personal short memory, and project long memory
always require explicit approval; first-run settings cannot disable that rule.
After installing or repairing hooks, close existing clients and start a fresh
Codex or Claude Code session so the client reloads and trusts the new user-hook
configuration.

## Register and initialize a workspace

A workspace may be a code repository or any other directory. Registration and
initialization are deliberately separate:

```bash
vibe-memory project register /path/to/workspace
vibe-memory project init /path/to/workspace
vibe-memory project list
```

`project register` adds the canonical path to the global registry. `project
init` creates missing project memory and managed instruction files only; it
does not install project-local hooks. Universal Codex and Claude Code user
hooks remain the single event entry point.

For a registered cwd, the deepest registered parent is selected and both
project and personal memory policy are supplied. An unregistered cwd is
personal-only: it can produce a personal candidate, but no project candidate or
project files are created.

## Preview and apply legacy migration

Register every target first. Preview is read-only; apply requires the explicit
approval flag and an explicit registered root:

```bash
vibe-memory migrate preview --project-root /path/to/workspace
vibe-memory migrate apply --approved --project-root /path/to/workspace
vibe-memory doctor
```

Migration validates existing memory, projects, design preferences, UI design
approval data, UI Skills, Loop configuration, and policy data. It removes only
recognized legacy project-hook entries, preserves unrelated client config, and
writes timestamped backups plus an audit result. A partial or failed result is
non-zero; inspect the reported root, changed paths, backup paths, and failed
control-plane areas before retrying.

## Memory behavior and governance

The shared hooks write event metadata and a policy reminder only. They do not
copy raw prompt text, summarize a prompt, call a model API, or manufacture a
candidate. The active Codex or Claude Code model uses conversation context to
distill personal long, personal short, project long, or project short
candidates/working summaries, then submits only the distilled content.

- Personal candidates contain reusable development habits, collaboration or
  workflow preferences, thinking style, or stable user-profile facts.
- Project candidates contain durable architecture, deployment, product,
  technical-constraint, or project-workflow facts.
- Project short memory is a model-distilled working summary, not captured
  prompt text.
- Raw conversations, one-off tasks, screenshots, URLs, local paths, uncertain
  guesses, credentials, tokens, passwords, verification codes, and cloud
  secrets are excluded.
- Equivalent candidates are deduplicated; conflicts never overwrite active
  memory automatically.
- Personal long/short and project long candidates remain pending until the user
  reviews the exact content and approves it. Rejection, editing, and deletion
  remain explicit audited actions.

The same storage, policy, approval state, and project registry are shared by
Codex and Claude Code. Candidate provenance records which active model proposed
the distilled content.

## Review console

`vibe-memory open` opens the loopback-only console. Its complete control plane
includes:

- **pending**: inspect, edit, approve, reject, defer, reset, and quarantine
  memory candidates;
- **active**: browse, search, edit, and delete active project/personal memory;
- **projects**: register, switch, initialize, inspect, and upgrade workspaces;
- **design preferences**: edit global defaults and project overrides and view
  the effective merged value and provenance;
- **UI design approval**: configure paths and `design_package` or
  `project_global` hard-gate mode, review packages, approve a package/baseline,
  request revision, reject, invalidate, and relock;
- **UI Skills**: import or bootstrap, inspect source/license/scripts/diff,
  validate, request revision, approve, publish to Codex and Claude Code,
  disable, scan, and rollback immutable versions;
- **Loop**: initialize or upgrade Loop × Superpowers and read worktree,
  staging, evaluation, release, and production-approval guidance;
- **policy**: inspect approval rules, scope routing, candidate categories,
  precedence, privacy exclusions, audit, backup, and recovery behavior.

The service binds only to `127.0.0.1`; do not expose it to the public internet.

### UI Design Control Plane operator notes

Managed publication defaults to `CODEX_UI_SKILLS_DIR=~/.codex/skills` and
`CLAUDE_UI_SKILLS_DIR=~/.claude/skills`; tests must redirect both to disposable
directories. The default `design_package` mode and optional `project_global`
mode both require reviewed digests. Use `request-revision` instead of editing
an approved immutable package, and treat every mutation as an idempotency-keyed
operation. Changing managed paths resets `hard_gate_enabled`; re-enable it only
after isolated hook smoke tests pass. A Real-client smoke test is a separate,
explicitly approved check using fresh clients and a disposable project—it is
never substituted for deterministic tests.

## Update, rollback, and repair

Updates come from an already reviewed local clone:

```bash
cd /path/to/local/clone
git pull --ff-only
vibe-memory update --source-root /path/to/local/clone
vibe-memory doctor
```

Update installs and validates a new release before switching the `current`
runtime. It preserves data and keeps the previous release. If the updated
runtime fails its smoke checks, inspect `vibe-memory doctor` and roll back:

```bash
vibe-memory rollback
vibe-memory doctor
```

Rollback switches managed program/configuration state; it does not discard
memory created after the update. For drifted runtime assets, the LaunchAgent,
or both hook clients, use:

```bash
vibe-memory repair
vibe-memory hooks status
vibe-memory hooks repair
vibe-memory doctor
```

`repair` recreates manager-owned runtime assets and restarts the LaunchAgent.
`hooks repair` changes only recognized managed entries, preserves unrelated
hooks, and runs hook smoke tests. Start a fresh Codex or Claude Code session
after either command.

## Uninstall safely

The default uninstall removes the versioned runtime, CLI launcher, LaunchAgent,
and manager-owned hook entries, but retains all user data:

```bash
vibe-memory uninstall
```

Retained data includes personal/project memory, proposals and review history,
the project registry, design preferences, UI design approval/audit data, UI
Skills and deployments, Loop configuration/worktree state, logs, and backups.
Removing data requires both an explicit deletion approval and every exact
managed target; review the paths before running it:

```bash
vibe-memory uninstall --remove-data --approved-data-deletion \
  --data-path "$HOME/.codex/personal_memory" \
  --data-path "$HOME/.codex/memory_review"
```

Project data is never inferred for deletion. Omitting `--remove-data` is the
recommended, recoverable uninstall.

## Troubleshooting

Start with machine-readable diagnostics:

```bash
vibe-memory status
vibe-memory doctor --json
vibe-memory hooks status
```

- **Service or LaunchAgent unavailable:** run `vibe-memory repair`, then
  `vibe-memory doctor`. Check
  `~/Library/Logs/VibeMemory/` and the LaunchAgent status if it remains down.
- **Port conflict:** stop the unexpected listener or reinstall/repair with a
  free loopback port. `vibe-memory open` refuses an unhealthy or wrong service.
- **Hook drift:** run `vibe-memory hooks repair`, then launch fresh clients.
- **Claude hooks missing:** rerun `./install.sh --with-claude-hooks` from a
  reviewed clone, or enable Claude Code in first run and repair.
- **A registered project is not selected:** use the canonical directory path,
  inspect `vibe-memory project list`, and register the correct parent.
- **Migration is partial:** restore only from the reported timestamped backup,
  correct the named control-plane area, preview again, then reapply with
  `--approved`.
- **Fresh client does not run hooks:** fully quit and restart that client;
  approve/trust its user configuration when prompted. An already-running
  session does not prove the installed hook was loaded.

## Developer-only source workflow

The installed commands above are the supported end-user flow. Maintainers may
run source-tree Python modules and test servers while developing this
repository, but those direct commands are not the installed flow and must not
be copied into user hooks. Run the release gate before publishing:

Legacy control-plane maintenance still exposes the source-only operations
`init-loop`, `preview-loop-upgrade`, `upgrade-loop`, and `upgrade-memory` in
`scripts/memory_project.py`. They are retained for the installed console's Loop
and project-upgrade backend; they are not a replacement for clone/install or
the universal user hooks.

```bash
python3 -m unittest discover -s tests -v
python3 scripts/verify_release.py --tree .
python3 scripts/public_release_check.py --tree .
```

The gate has 13 checks: manifest, Python syntax, full unit tests, real Darwin
installed-runtime E2E, public tree, LaunchAgent plist, loopback binding,
permissions, Codex hook, Claude hook, complete control plane, rollback, and
uninstall. Release evidence and independent Claude Code evaluation are covered
by [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md).

## Security and license

Vibe Memory is local-only, calls no external model API, and stores no
credentials in its runtime configuration. Before publication, scan both the
candidate tree and Git history. See [SECURITY.md](SECURITY.md) and
[LICENSE](LICENSE).
