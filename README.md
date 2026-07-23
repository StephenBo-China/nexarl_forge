# Vibe Coding Manage Platform

`vibe_coding_manage_platform` is a local management platform for Codex and
Claude Code collaboration workflows. Its first built-in tool is the memory
review console: a local-only web UI for reviewing, approving, editing, and
rejecting project and personal memory candidates.

The service is designed to be copied to another computer and used directly by
Codex. It has no third-party Python dependencies; Python 3.10+ is enough.

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
