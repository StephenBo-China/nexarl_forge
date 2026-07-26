# Vibe Coding Manage Platform

`vibe_coding_manage_platform` is a local management platform for Codex and
Claude Code collaboration workflows. Its first built-in tool is the memory
review console: a local-only web UI for reviewing, approving, editing, and
rejecting project and personal memory candidates.

The service is designed to be copied to another computer and used directly by
Codex. It has no third-party Python dependencies; Python 3.10+ is enough.

## 中文使用说明

完整的中文安装、操作、UI 设计审批、UI Skill 管理和故障排查说明，请参阅
[记忆审核台中文版使用说明书](docs/MEMORY_REVIEW_USER_GUIDE.zh-CN.md)。

## What This Project Provides

- A dark local web console at `http://127.0.0.1:8897/`.
- Pending memory candidate review.
- Active memory browsing, editing, and deletion.
- Project registration and project switching.
- New project memory initialization.
- Optional Loop Engineering project initialization.
- Personal long/short memory approval flow.
- Project long memory approval flow.
- Noise personal-memory candidate preview/rejection.
- Built-in Loop Engineering usage documentation.
- Multi-conversation worktree task registry and safe release CLI.
- Repository-level release/staging locks and canonical workspace synchronization.
- Remote-main, canonical-main, feature-ancestry, and deployment commit verification.
- A CLI for terminal-based review and approval.

## Directory Layout

```text
.
├── README.md
├── scripts/
│   ├── memory_review.py
│   ├── memory_project.py
│   ├── memory_review_queue.py
│   └── memory_review_server.py
└── codex/                 # generated at runtime, ignored by git
```

Runtime files are generated under the target project's `codex/` directory:

```text
codex/memory_review_queue.json
codex/memory_review_state.json
codex/memory_review_server.log
```

These files may contain local candidate content or approval state and should not
be committed.

## Memory File Model

The review console code lives in this repository. The project memory files are
read from the target project root.

By default, the target project root is the current working directory. You can
override it with:

```bash
export MEMORY_REVIEW_PROJECT_ROOT=/path/to/project
```

The review console reads these project memory files relative to the target
project root:

```text
codex/codex_long_memory.md
codex/codex_short_memory.md
codex/memory_proposals.md
```

It also reads personal memory files from the current user home directory:

```text
~/.codex/personal_memory/long.md
~/.codex/personal_memory/short.md
~/.codex/personal_memory/proposals.md
```

Approved personal memories are written only after explicit approval. Candidate
personal memories should remain in `~/.codex/personal_memory/proposals.md` until
the user approves them.

## Project Registry

The memory review console is cross-project. It stores the list of known projects
and the current project in:

```text
~/.codex/memory_review/projects.json
```

Each registered project keeps its own project memory files under that project
root. Switching projects in the web UI updates the registry and reloads the
backend project root without restarting the service.

## Initialize A New Project

Register a project:

```bash
python3 scripts/memory_project.py register /path/to/repo
```

Initialize project memory and Codex/Claude hooks:

```bash
python3 scripts/memory_project.py init /path/to/repo
```

This creates missing files only. Existing files are reported as `existing` and
are not overwritten.

Initialize a new Loop Engineering × Superpowers project. This creates schema 3,
the complete method contract, standard artifact directories, and the managed
completion validator:

```bash
python3 scripts/memory_project.py init-loop /path/to/repo --port 8082
```

Preview an existing Loop project upgrade without writing files:

```bash
python3 scripts/memory_project.py preview-loop-upgrade /path/to/repo
```

Explicitly upgrade an existing Loop project after reviewing the preview. The
upgrade preserves project-specific staging, database, OSS, port, remote path,
verification commands, production guardrails, and unknown extension fields:

```bash
python3 scripts/memory_project.py upgrade-loop /path/to/repo
```

Upgrade the central manager's marked memory-rule blocks and the two existing
Codex/Claude memory hooks together. Changed managed files are preserved as
timestamped `.bak.*` audit backups; no separate Superpowers hook is added:

```bash
python3 scripts/memory_project.py upgrade-memory /path/to/repo
```

Initialization and upgrade do not install plugins, call external models, deploy
staging, merge main, or access production. Codex and Claude Code use their own
official Superpowers plugins. Loop remains the lifecycle authority for
worktrees, branches, staging, evaluation, release, main merge, and production.

## Candidate Quality Policy

- The active Codex or Claude Code conversation model distills candidates from
  context it already has; hooks do not call another model API.
- Hooks create local reminders and context packets only. They never copy raw
  prompts into long-memory candidate files.
- Personal candidates must be distilled cross-project habits, preferences,
  thinking styles, collaboration patterns, or user-profile facts.
- Project long-memory candidates are summarized by the active agent as durable
  Markdown facts before review.
- Project short memory stores a bounded prompt summary instead of the complete
  raw prompt.
- Detected personal noise can be quarantined and marked rejected while the
  original proposal file remains untouched:

```bash
python3 scripts/memory_review.py reject-noise-personal --apply
```

If `--port` is omitted, the tool recommends the next available port based on
registered projects. The web UI always asks the user to confirm the port before
writing `.loop/config.json`.

List registered projects:

```bash
python3 scripts/memory_project.py list
```

Switch the current project:

```bash
python3 scripts/memory_project.py use /path/to/repo
```

## Start The Service

For `noema_ai_box`, start the service from this repository and point it at the
target project:

```bash
scripts/start_memory_review.sh /Users/stephenbo/Noema/Projects/noema_ai_box
```

Equivalent explicit command:

```bash
MEMORY_REVIEW_PROJECT_ROOT=/Users/stephenbo/Noema/Projects/noema_ai_box \
  python3 scripts/memory_review_queue.py ensure-server
```

From another project directory, you can also run:

```bash
/Users/stephenbo/Noema/Projects/vibe_coding_manage_platform/scripts/start_memory_review.sh "$(pwd)"
```

Open:

```text
http://127.0.0.1:8897/
```

Health check:

```bash
curl -s http://127.0.0.1:8897/health
```

Foreground mode for debugging a specific project:

```bash
MEMORY_REVIEW_PROJECT_ROOT=/path/to/project python3 scripts/memory_review.py serve
```

or:

```bash
python3 scripts/memory_review_server.py
```

## CLI Usage

List pending candidates for a target project:

```bash
MEMORY_REVIEW_PROJECT_ROOT=/path/to/project python3 scripts/memory_review.py list
```

Show a candidate:

```bash
python3 scripts/memory_review.py show M-YYYYMMDD-HHMMSS
```

Approve a project long-memory candidate:

```bash
python3 scripts/memory_review.py approve P-xxxxxxxxxxxx --target project_long
```

Approve a personal long-memory candidate:

```bash
python3 scripts/memory_review.py approve M-YYYYMMDD-HHMMSS --target personal_long
```

Approve a personal short-memory candidate:

```bash
python3 scripts/memory_review.py approve M-YYYYMMDD-HHMMSS --target personal_short
```

Reject a candidate:

```bash
python3 scripts/memory_review.py reject M-YYYYMMDD-HHMMSS
```

Defer a candidate:

```bash
python3 scripts/memory_review.py defer M-YYYYMMDD-HHMMSS
```

Preview obvious noisy personal candidates:

```bash
python3 scripts/memory_review.py reject-noise-personal
```

Reject obvious noisy personal candidates:

```bash
python3 scripts/memory_review.py reject-noise-personal --apply
```

### Bootstrap Managed UI Design Skills

Create reviewable, validated drafts for the manager workflow and the two
initial UI design skills:

```bash
python3 scripts/memory_review.py ui-skill bootstrap ui-design-workflow \
  --idempotency-key bootstrap-ui-design-workflow-001

python3 scripts/memory_review.py ui-skill bootstrap frontend-design \
  --revision b29e7cf65e5cb78a5ac33d582270551bc74a14eb \
  --idempotency-key bootstrap-frontend-design-001

python3 scripts/memory_review.py ui-skill bootstrap ui-ux-pro-max \
  --release v2.11.0 \
  --revision 6142b073958df645d0fb27e682428e69599386dc \
  --cli-version 2.11.0 \
  --expected-npm-shasum 2ff4d811cf1dded593b9d1f37bad65ffa80cb87c \
  --idempotency-key bootstrap-ui-ux-pro-max-001
```

Bootstrap never approves or publishes a skill. Each draft remains visible in
the review console until the user explicitly approves and publishes it. The UI
UX Pro Max bootstrap needs normal network and sandbox approval to inspect npm
metadata and run its pinned generator. Generation happens in temporary
directories; publication later copies the approved Codex or Claude variant and
does not invoke `npx`.

## UI Design Control Plane

The `UI 设计审批`, `设计偏好`, and `UI Skills` console tabs form one shared
control plane for Codex and Claude Code. For Web, App, and mini-program work,
agents may research, read code, and create design artifacts before approval,
but the managed `PreToolUse` hook blocks writes to formal frontend paths. Pure
backend and non-visual work bypasses this gate.

### Storage, backup, and recovery

Global manager state lives below `UI_DESIGN_HOME` (default
`~/.codex/ui_design`): global preferences, immutable Skill drafts and versions,
deployment reports, discovery state, audit records, and idempotency results.
Each project owns its gate state below `codex/ui_design/`:

```text
codex/ui_design/config.json
codex/ui_design/preferences.json
codex/ui_design/active-skills.json
codex/ui_design/approvals.json
codex/ui_design/audit.jsonl
codex/ui_design/effective-context.json
codex/ui_design/design-packages/<task-id>/
```

Managed installs target `CODEX_UI_SKILLS_DIR` and `CLAUDE_UI_SKILLS_DIR`
(defaults `~/.codex/skills` and `~/.claude/skills`). Tests should always point
all three environment variables to disposable directories. Atomic writes and
two-target publication use staging directories; upgrades preserve changed
managed hooks as timestamped `.bak.*` files. To recover, disable the hard gate,
inspect `audit.jsonl` and deployment reports, restore the relevant backup, then
run the hook smoke test before re-enabling. Never delete an unmanaged Skill as
part of recovery.

### Preferences and gate modes

Global preferences are a complete schema inherited by every project. Project
overrides use `inherit`, `replace`, `append`, or `clear`; the console shows the
effective value and source of every field.

Projects choose one approval mode:

- `design_package` (default): approval is bound to one task, package digest,
  version, and declared file patterns. A changed design document invalidates
  the approval; undeclared frontend paths remain blocked.
- `project_global`: a designated baseline design package unlocks all configured
  formal frontend paths until explicit relock, mode change, or baseline digest
  change. Switching modes always relocks.

Changing path configuration sets `hard_gate_enabled` to false and resets both
hook smoke-test results. Enabling the hard gate requires non-empty formal and
design-artifact paths, explicit confirmation, and passing isolated smoke tests
for both installed hooks. Generated and test-artifact path lists remain
separate from formal frontend paths.

```bash
python3 scripts/memory_review.py ui-design project-config show --project /path/to/repo
python3 scripts/memory_review.py ui-design project-config set-paths \
  --project /path/to/repo --json-file /tmp/ui-paths.json \
  --idempotency-key paths-001
python3 scripts/memory_review.py ui-design project-config set-mode \
  --project /path/to/repo --mode design_package --confirmed \
  --idempotency-key mode-001
python3 scripts/memory_review.py ui-design project-config enable-hard-gate \
  --project /path/to/repo --confirmed --idempotency-key gate-001
python3 scripts/memory_review.py ui-design project-config relock \
  --project /path/to/repo --confirmed --idempotency-key relock-001
```

### Design-package lifecycle

A package manifest declares pages, components, design files, and allowed formal
frontend patterns. `design-brief.md`, `interaction-spec.md`, and
`responsive-spec.md` are required. Approvals bind the normalized manifest and
all declared file bytes to one SHA-256 digest.

```bash
python3 scripts/memory_review.py ui-design package create \
  --project /path/to/repo --manifest /tmp/design-package.json \
  --idempotency-key package-create-001
python3 scripts/memory_review.py ui-design package list --project /path/to/repo
python3 scripts/memory_review.py ui-design package approve \
  --project /path/to/repo --task checkout-redesign --digest <sha256> \
  --confirmed --idempotency-key package-approve-001
python3 scripts/memory_review.py ui-design package request-revision \
  --project /path/to/repo --task checkout-redesign \
  --reason "Add touch error states" \
  --idempotency-key package-request-revision-001
python3 scripts/memory_review.py ui-design package invalidate \
  --project /path/to/repo --task checkout-redesign --reason "Scope changed" \
  --confirmed --idempotency-key package-invalidate-001
```

Use `package revise` with a reviewed replacement manifest. In
`project_global` mode, set `project_global_baseline_task` in the reviewed
project config and use `ui-design baseline approve` with `--confirmed`. Reject,
invalidate, mode-change, and hard-gate actions require explicit confirmation.

### UI Skill intake and publication

The manager accepts editor text, a local directory, a ZIP archive, or a pinned
GitHub repository/path/revision. Imported code is staged and statically
validated; package scripts are reported but never executed. An agent-created UI
Skill therefore appears as a visible draft in the console, where the user can
inspect `SKILL.md`, license, scripts, digest, and diff before approval.

The normal workflow is import → validate → approve → atomic publish to both
agents. Use `request-revision` to return a draft, `rollback` to restore an
approved immutable version on both targets, and `disable` to remove the managed
version from both targets transactionally. Bootstrap creates reviewable drafts
for `ui-design-workflow`, pinned `frontend-design`, and pinned UI UX Pro Max; it
does not approve or publish them. Discovery is read-only: unmanaged, ignored,
conflicting, or drifted Skill directories are reported without modification.

Every mutation requires a unique `--idempotency-key`. Retrying the exact same
operation returns the recorded result; reusing the key with different arguments
returns a conflict. Digest conflicts must be resolved by reviewing the current
content and approving its new digest, never by bypassing the check.

### Hook trust and client boundaries

The project initializer installs dependency-free manager-owned hooks for both
clients and merges their configuration without replacing unrelated hooks.
After hook or Skill changes, start fresh Codex and Claude Code sessions so each
client reloads its configuration. Keep `hard_gate_enabled` false if either
client fails the block contract.

#### Real-client smoke test

A Real-client smoke test is intentionally separate from deterministic tests. It
requires explicit user approval before writing real home Skill directories or
starting real clients. Publish the three approved Skills to both clients, start
fresh sessions, compare visible names and versions, then use a disposable
project to prove that design-artifact writes are allowed and formal frontend
writes are denied before approval. Record the exact hook payload and decision;
do not enable the real hard gate when either client behaves differently.

The control plane never authorizes a main/master merge, production deployment,
or write to real Codex/Claude Skill directories by itself. Those remain separate
user approval boundaries. Prepared forward-test prompts are stored under
`docs/superpowers/forward-tests/` and must not be dispatched to subagents or
real clients without explicit authorization.

## How To Associate Codex With This Project

On another computer, clone this repository and tell Codex:

```text
Use /path/to/vibe_coding_manage_platform as my local memory review and vibe
coding management platform. Start the memory review console with:
python3 scripts/memory_review_queue.py ensure-server
```

Recommended Codex instruction:

```text
When I ask to review memories, open http://127.0.0.1:8897/ from the
vibe_coding_manage_platform repository. Use this platform to review project and
personal memory candidates. Do not write approved personal memory directly;
only approve exact candidates after I confirm them.
```

If a project has Codex hooks, point the hook command to this repository's
scripts or copy these scripts into the project. A typical project hook can run:

```bash
python3 /path/to/vibe_coding_manage_platform/scripts/memory_review_queue.py ensure-server
```

## How To Use With Another Project

There are two common modes.

### Mode 1: Run Review From This Platform Repo

Use this repository as the management console. The console will read:

- project memory files in the target project under `codex/`
- personal memory files under `~/.codex/personal_memory/`

This is the preferred mode. It keeps all memory review console code in this
repository while each project keeps its own project memory data.

### Mode 2: Legacy Per-Project Script Copy

Older projects may still have copies of the three scripts in their own
`scripts/` directory:

```text
scripts/memory_review.py
scripts/memory_review_queue.py
scripts/memory_review_server.py
```

Prefer replacing those hooks with a call to this central repository:

```bash
MEMORY_REVIEW_PROJECT_ROOT=/path/to/project \
  python3 /Users/stephenbo/Noema/Projects/vibe_coding_manage_platform/scripts/memory_review_queue.py ensure-server
```

Each project still owns its own project memory files:

```text
codex/codex_long_memory.md
codex/codex_short_memory.md
codex/memory_proposals.md
```

## Personal Memory Governance

Personal memory is approval-gated.

Automatic personal-memory candidates should only describe the user as a worker
and collaborator:

- development habits
- thinking style
- collaboration preferences
- workflow preferences
- cross-project user profile

Do not create personal-memory candidates from:

- raw conversation transcripts
- PRDs
- screenshots
- hook payloads
- system prompts
- ambient suggestions
- one-off project task content
- secrets, tokens, API keys, verification codes, RDS/OSS credentials

## Loop Engineering Documentation

The web console includes a `Loop 说明` tab. It documents:

- how to start loop development
- worktree-first multi-conversation development
- how Codex and Claude Code divide work
- how to use Playwright-based Claude evaluation
- loop branch naming
- staging deployment rules
- stop conditions
- common commands and examples

## Worktree Development

The original repository is the canonical workspace. Keep it on the configured
main branch as a mirror of remote main; perform feature work in external
worktrees. The required mapping is:

```text
one task or loop feature = one conversation = one worktree = one branch
```

Feature development may run concurrently. Main integration, main push,
canonical synchronization, and shared staging deployment are serialized with
repository locks. Main integration happens in a temporary release worktree
based on the latest remote main branch, not in a dirty canonical workspace.

After an approved merge, canonical synchronization uses `ff-only`. Dirty paths
that overlap incoming paths block synchronization. The workflow never performs
an automatic stash, reset, force push, checkout overwrite, or deletion of user
files. Final completion requires remote main, canonical main, and deployment
commits to match, and the feature commit to be an ancestor of main.

Use the central CLI:

```bash
python3 scripts/worktree_flow.py start /path/to/repo \
  --task feature-name --conversation conversation-id
python3 scripts/worktree_flow.py status /path/to/repo
python3 scripts/worktree_flow.py finish /path/to/repo --task feature-name

# Only after explicit user approval to merge main:
python3 scripts/worktree_flow.py release /path/to/repo \
  --task feature-name --approved \
  --test-command "python3 -m pytest -q tests"

python3 scripts/worktree_flow.py sync-canonical /path/to/repo
python3 scripts/worktree_flow.py deploy-staging /path/to/repo \
  --task feature-name --approved \
  --command "./scripts/deploy_staging.sh" \
  --deployed-commit-command "./scripts/deployed_commit.sh"
python3 scripts/worktree_flow.py verify /path/to/repo --task feature-name
python3 scripts/worktree_flow.py cleanup /path/to/repo \
  --task feature-name --approved
```

The complete lifecycle, safety rules, state model, and completion report are in
[`docs/worktree_loop_workflow.md`](docs/worktree_loop_workflow.md).

## Port And Security

The service binds only to:

```text
127.0.0.1:8897
```

It is intended for local machine use only. Do not expose it directly to the
public internet.

## Troubleshooting

If the service does not start:

```bash
lsof -nP -iTCP:8897 -sTCP:LISTEN
```

If another process is using the port, stop it or change `REVIEW_PORT` in
`scripts/memory_review_queue.py`.

If browser access fails under a sandboxed Codex session, run the service from a
normal terminal:

```bash
cd /path/to/vibe_coding_manage_platform
python3 scripts/memory_review.py serve
```
