# Nexarl Forge

中文使用说明：[Nexarl 协作工坊中文指南](docs/README.zh-CN.md)

[GitHub](https://github.com/StephenBo-China/nexarl_forge) · [中文白皮书](docs/WHITEPAPER.zh-CN.md) · [English whitepaper](docs/WHITEPAPER.md) · [Contributing](CONTRIBUTING.md)

Nexarl Forge is a local-first workspace for human–AI coding collaboration. It
combines Loop engineering workflows, shared Codex and Claude Code hooks,
approval-gated long- and short-term memory, design governance, and a loopback-only
management console. Moving or deleting the source clone after installation does
not break the installed runtime.

The first public release supports macOS (Apple Silicon and Intel) and requires
Python 3.10+. It has no third-party Python dependencies. See the
[Chinese user guide](docs/MEMORY_REVIEW_USER_GUIDE.zh-CN.md) for the complete
Chinese walkthrough and the [release checklist](docs/RELEASE_CHECKLIST.md)
before publishing a release candidate.

## Install from a clone

Use a normal Terminal. Replace the example repository URL if you are installing
from a fork:

```bash
git clone git@github.com:StephenBo-China/nexarl_forge.git
cd nexarl_forge
./install.sh
```

To enable both Codex and Claude Code hooks during installation:

```bash
./install.sh --with-claude-hooks
```

The installer verifies macOS and Python 3.10+, copies an immutable release to
`~/Library/Application Support/VibeMemory/`, creates
`~/.local/bin/nexarl-forge` (with the historical `vibe-memory` alias retained),
structurally merges manager-owned user hooks, installs the existing
`com.noema.vibe-memory` LaunchAgent, starts the local service, and runs
`nexarl-forge doctor`. Unrelated hooks are preserved and
changed managed files receive timestamped backups.

The storage directory, LaunchAgent label, and internal service identity retain
their historical `VibeMemory`/`vibe-memory` names for upgrade compatibility.
The legacy source directory name `vibe_coding_manage_platform` may still appear
in older local clones; new clones use `nexarl_forge`.

If `nexarl-forge` is not found in zsh, add `~/.local/bin` to `PATH` and reload
the shell configuration:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.zshrc"
source "$HOME/.zshrc"
```

## Complete first run

Open the healthy local service:

```bash
nexarl-forge open
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

If you disable login startup, the LaunchAgent remains stopped after first run.
Start it only for the current login session, without changing that preference:

```bash
nexarl-forge start && nexarl-forge open
```

The manual plist sets both `RunAtLoad=false` and `KeepAlive=false`, so it does
not start at the next login and does not automatically relaunch after exit.
If login startup is enabled, `nexarl-forge start` preserves both lifecycle keys
as true. In either mode it starts the current session without changing the
saved preference.

## Register and initialize a workspace

A workspace may be a code repository or any other directory. Registration and
initialization are deliberately separate:

```bash
nexarl-forge project register "/path/to/workspace"
nexarl-forge project init "/path/to/workspace"
nexarl-forge project list
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
nexarl-forge migrate preview --project-root "/path/to/workspace"
nexarl-forge migrate apply --approved --project-root "/path/to/workspace"
nexarl-forge doctor
```

Migration validates existing memory, projects, design preferences, UI design
approval data, UI Skills, Loop configuration, and policy data. It removes only
recognized legacy project-hook entries, preserves unrelated client config, and
writes timestamped backups plus an audit result. A partial or failed result is
non-zero; inspect the reported root, changed paths, backup paths, and failed
control-plane areas before retrying. Always inspect both the preview JSON and
its exit status: any project `error`, invalid preflight, or non-clean preview
returns non-zero even though the complete diagnostic JSON is still printed.

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

`nexarl-forge open` opens the loopback-only console. Its complete control plane
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
cd "/path/to/local/clone"
git pull --ff-only
nexarl-forge update --source-root "/path/to/local/clone"
nexarl-forge doctor
```

Update installs and validates a new release before switching the `current`
runtime. It preserves data and keeps the previous release. If the updated
runtime fails its smoke checks, inspect `nexarl-forge doctor` and roll back:

```bash
nexarl-forge rollback
nexarl-forge doctor
```

Rollback switches managed program/configuration state; it does not discard
memory created after the update. For drifted runtime assets, the LaunchAgent,
or both hook clients, use:

```bash
nexarl-forge repair
nexarl-forge hooks status
nexarl-forge hooks repair
nexarl-forge doctor
```

`repair` recreates manager-owned runtime assets and restarts the LaunchAgent.
`hooks repair` changes only recognized managed entries, preserves unrelated
hooks, and runs hook smoke tests. Start a fresh Codex or Claude Code session
after either command.

## Uninstall safely

The default uninstall removes the versioned runtime, CLI launcher, LaunchAgent,
and manager-owned hook entries, but retains all user data:

```bash
nexarl-forge uninstall
```

Retained data includes personal/project memory, proposals and review history,
the project registry, design preferences, UI design approval/audit data, UI
Skills and deployments, Loop configuration/worktree state, logs, and backups.
Removing data requires both an explicit deletion approval and every exact
managed target; review the paths before running it:

```bash
nexarl-forge uninstall --remove-data --approved-data-deletion \
  --data-path "$HOME/.codex/memory_review/projects.json"
```

Each `--data-path` must be an allowlisted exact managed regular file; it cannot be a directory
or symlink. All targets are validated before the service is
stopped or any hook/runtime asset is changed. Project data is never inferred
for deletion. Omitting `--remove-data` is the recommended, recoverable uninstall.

## Troubleshooting

Start with machine-readable diagnostics:

```bash
nexarl-forge status
nexarl-forge doctor --json
nexarl-forge hooks status
```

- **Service or LaunchAgent unavailable:** if login startup is disabled, run
  `nexarl-forge start && nexarl-forge open`. Otherwise run `nexarl-forge repair`,
  then `nexarl-forge doctor`. Check
  `~/Library/Logs/VibeMemory/` and the LaunchAgent status if it remains down.
- **Port conflict:** stop the unexpected listener or reinstall/repair with a
  free loopback port. `nexarl-forge open` refuses an unhealthy or wrong service.
- **Hook drift:** run `nexarl-forge hooks repair`, then launch fresh clients.
- **Claude hooks missing:** rerun `./install.sh --with-claude-hooks` from a
  reviewed clone, or enable Claude Code in first run and repair.
- **A registered project is not selected:** use the canonical directory path,
  inspect `nexarl-forge project list`, and register the correct parent.
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
