# Universal Local Memory Manager for Codex and Claude Code

Date: 2026-07-30

Status: Approved design

Initial platform: macOS

License for the public release: MIT

## 1. Purpose

Turn `vibe_coding_manage_platform` into a public, clone-to-install local manager
for Codex users. A new user should be able to clone the repository, run one
installer, and gain approval-gated personal long- and short-term memory across
all Codex conversations. When Claude Code is installed, it can use the same
memory and review system through its own client adapter.

The manager must also support project memory for code and non-code workspaces.
Project memory is active only when the current working directory belongs to an
explicitly registered project. An unregistered working directory produces only
personal candidates and must not be initialized or mutated as a project.

The public release must preserve all current memory-review-console features:

- pending and active memory management;
- project registration, initialization, switching, and upgrade;
- global and project UI design preferences;
- UI design-package approval and enforcement;
- UI Skill intake, validation, approval, publication, disabling, and rollback;
- Loop Engineering and worktree documentation and management;
- configurable, approval-gated memory policy.

## 2. Product Decisions

### 2.1 Distribution model

The Git clone is an installation source, not the permanent runtime location.
`./install.sh` copies a versioned runtime into the user's Library directory and
installs a stable CLI entry point. Moving or deleting the clone after a
successful installation must not break hooks or the review console.

This is preferred over hooks that reference the clone because clone paths are
not durable. A signed `.app` or `.pkg` can be added later, but it is not part of
the first public release.

### 2.2 Supported clients

Codex is the required client. Claude Code support is installed when the client
is detected and the user enables it. Both clients share storage, governance,
review state, project registration, and control-plane data. Each client has a
thin adapter for its configuration and hook payload.

### 2.3 Supported platform

The first release supports macOS only, including Apple Silicon and Intel Macs.
Core formats and Python modules must remain portable so Linux and Windows can
be added without redesigning the memory model.

### 2.4 Project identity

A project is an explicitly registered filesystem directory; it need not be a
Git repository or contain code. The router resolves symlinks and matches the
current working directory against registered roots. The longest matching
parent root wins. If no root matches, only personal memory is active.

## 3. Architecture

The system has one shared core and two client adapters:

```text
Codex user hooks ───────┐
                       ├─> shared hook router ─> memory and control-plane data
Claude Code user hooks ┘                       └> local review console
```

The installer merges managed entries into:

```text
~/.codex/hooks.json
~/.claude/settings.json
```

Both sets of entries call the same stable CLI with an agent and event:

```text
vibe-memory hook --agent codex --event EVENT_NAME
vibe-memory hook --agent claude-code --event EVENT_NAME
```

The router normalizes client payloads into:

```text
agent, event, cwd, session_id, timestamp, payload_digest
```

It then loads personal memory, resolves the registered project, conditionally
loads project memory, refreshes review state, and returns client-appropriate
additional context. The additional context carries the same memory policy for
both agents while preserving the source client on candidates and audit events.

The official Codex configuration model supports user hooks in
`~/.codex/hooks.json` and project hooks in `<repo>/.codex/hooks.json`.
User-level hooks remain active independently of project trust. The public
manager therefore uses user-level hooks for universal personal memory and the
registry for project routing instead of requiring per-project hook copies.

## 4. Installation Layout

The macOS installation uses:

```text
~/Library/Application Support/VibeMemory/
├── releases/VERSION/
├── current -> releases/VERSION
├── state/
├── backups/
├── logs/
└── config.json

~/.local/bin/vibe-memory
~/Library/LaunchAgents/com.noema.vibe-memory.plist
```

The review console binds only to `127.0.0.1`; port `8897` remains the default
and is configurable when occupied. The LaunchAgent starts the selected runtime
without embedding credentials.

For backward compatibility, the first public release retains the current
canonical data paths rather than renaming them:

```text
~/.codex/personal_memory/
~/.codex/memory_review/projects.json
~/.codex/ui_design/
~/.codex/worktree_manager/
<project>/codex/
<project>/codex/ui_design/
<project>/.loop/config.json
```

A future neutral namespace requires a separate, explicit migration design.

## 5. Installer and First-Run Experience

The supported entry point is:

The user clones the published repository, enters its
`vibe_coding_manage_platform` directory, and runs:

```bash
./install.sh
```

The installer:

1. verifies macOS, Python 3.10 or newer, and Codex;
2. detects Claude Code as an optional integration;
3. previews every file and service it will modify;
4. displays the local-memory and approval privacy contract;
5. backs up existing client configuration;
6. installs a versioned runtime and stable CLI;
7. structurally merges managed hook entries without replacing unrelated hooks;
8. initializes only missing personal data files;
9. installs and starts the LaunchAgent;
10. runs hook smoke tests, API checks, and a service health check;
11. opens the first-run page after success.

The first-run page configures Codex hooks, optional Claude Code hooks, automatic
candidate checks, personal short-memory retention, login startup, and optional
registration of a selected workspace. The clone itself is not automatically
registered.

Formal personal long memory, personal short memory, and project long memory
remain approval-gated and cannot be configured to bypass review.

## 6. Safe Configuration Ownership

The installer parses and merges JSON or TOML rather than overwriting whole
files. A malformed configuration stops the affected modification and leaves
the original bytes unchanged. Every modification creates a timestamped backup.

Managed entries have a stable command signature so installation is idempotent,
repair can find drift, and uninstall can remove only manager-owned entries.
Existing user and third-party hooks remain untouched.

The CLI includes:

```text
vibe-memory status | open | doctor
vibe-memory project register | unregister | list | init
vibe-memory hooks status | repair
vibe-memory migrate preview | apply
vibe-memory update | rollback | uninstall
```

Uninstall removes the selected runtime, LaunchAgent, CLI link, and managed hook
entries. It does not remove memories, project data, review history, UI Skills,
or backups. Data deletion is a separate operation with an explicit target and
second confirmation.

## 7. Existing Installation Migration

Migration preserves current files as the canonical data. Before making
changes, it inventories:

- personal and project long/short memories;
- personal and project proposal and review state;
- registered projects;
- global and project design preferences;
- UI design configuration, packages, approvals, and audit history;
- UI Skill drafts, packages, deployment state, and audit history;
- Loop configurations and worktree manager state;
- existing Codex and Claude Code managed hooks.

The migration installs the new runtime, validates all old data read-only, tests
the new service, installs user-level hooks, and only then switches the active
runtime. Old project-level managed hooks are removed only through a reviewed
migration. Their scripts are retained as timestamped backups, and unmanaged
hooks or rule text are not changed.

During coexistence, the router deduplicates calls with:

```text
agent + session_id + event + canonical cwd + payload_digest
```

The same key within a bounded window executes once. This prevents duplicated
short-memory entries, context packets, queue refreshes, or notifications while
legacy and user-level hooks overlap.

## 8. Data Ownership and Feature Compatibility

Runtime upgrades replace program code only. Global and project data never
follow release replacement.

| Capability | Canonical data | Compatibility rule |
| --- | --- | --- |
| Pending memory | personal/project proposals and review queue | Preserve and rescan |
| Active memory | personal/project long and short Markdown | Preserve without rewrite |
| Project management | project registry | Preserve; migrate schema from backup |
| Global design preferences | global UI design preferences | Preserve |
| Project design preferences | project UI design preferences | Preserve |
| UI approval | project config, approvals, audit, packages | Preserve full lifecycle |
| UI Skills | global registry, package store, deployments, audit | Preserve both targets |
| Loop | bundled docs/templates and project `.loop` config | Replace code; preserve config |
| Memory policy | built-in defaults, user policy, project policy | Version and layer |

The release is accepted only if every current console capability passes its
existing workflow after clean installation and migration.

## 9. Memory Lifecycle

On session and prompt events, the router injects applicable memory plus a
candidate-generation contract into the active model. The active Codex or
Claude model, not the hook or a secondary API, distills candidates from context
it already has. A substantial instruction triggers candidate review without
requiring the user to say "remember this."

The memory types are:

| Type | Lifecycle |
| --- | --- |
| Personal long | automatic candidate; user approval before activation |
| Personal short | automatic candidate; user approval before activation; optional expiry |
| Project long | automatic candidate; user approval before activation |
| Project short | model-distilled working summary; bounded and periodically compacted |
| Event metadata | hook-written metadata without prompt body |

Hooks must stop storing truncated raw prompts in project short memory. Only the
active model may write a distilled working summary. If the model does not emit
a candidate, the hook does not manufacture one from raw conversation text.

Each candidate records source client, scope, target, category, title, summary,
creation time, source event, policy version, and approval status.

Allowed personal categories are development habit, collaboration preference,
work style, thinking style, workflow preference, and stable user profile.
Allowed project categories are project architecture, deployment rule, product
direction, technical constraint, and project workflow.

At most two candidates may be created for one substantial instruction.
One-off tasks, raw prompts, screenshots, URLs, local paths, uncertain guesses,
credentials, tokens, verification codes, passwords, and infrastructure secrets
are excluded.

## 10. Deduplication and Conflicts

Candidate identity includes scope, target, category, normalized title, and
normalized summary. Exact pending duplicates are not recreated. Exact active
duplicates are ignored. A material revision becomes an update candidate.
Contradictions become conflict candidates and never overwrite active memory
automatically. Equivalent Codex and Claude candidates merge their provenance.

All shared JSON and Markdown writes are atomic and protected by bounded locks.
Corrupt input is preserved and reported rather than silently reset.

## 11. Policy Precedence

Memory behavior is layered from lowest to highest priority:

1. versioned built-in default policy;
2. user global policy;
3. registered project policy;
4. explicit instruction in the current conversation.

Higher layers override lower layers. Product safety invariants remain fixed:
no secret retention, no raw prompt promotion, and no formal long-memory
activation without approval.

## 12. Privacy and Security

- The service listens only on loopback.
- The service does not call external model APIs.
- Memory, candidates, registries, and audits are not uploaded.
- Runtime data directories default to mode `0700` and private files to `0600`.
- Logs contain event metadata and errors, not prompt bodies or credentials.
- Sensitive candidate fields are rejected or masked in the UI.
- Export is an explicit user action.
- Backups receive the same restrictive permissions as source data.
- Formal activation and destructive memory actions are audited.

Before a public release, the tracked tree and Git history are scanned for
credentials, personal paths, private memory, and infrastructure secrets. A
history rewrite is a separate destructive release operation requiring explicit
approval if the scan finds material sensitive history.

## 13. Failure Behavior

Memory enrichment fails open for the user's primary task:

- an unavailable review service leaves candidates in a local queue;
- lock contention retries briefly and then defers work;
- a corrupt memory file is preserved and quarantined from writes;
- a router error does not block ordinary Codex or Claude use;
- a broken project does not disable personal or other project memory.

An explicitly enabled UI hard gate fails closed for formal UI writes when its
approval state cannot be verified. Projects without the hard gate are not
blocked. Errors provide repair and explicit gate-disable commands. The gate
reads local approval state and does not depend on the browser page being open.

## 14. Versioning, Upgrade, and Rollback

Each release includes an application version, data schema version, hook
protocol version, minimum Python version, and platform declaration.

The first release does not update automatically. Users run `git pull` followed
by `./install.sh --upgrade`, or use
`vibe-memory update --source /path/to/local/clone`.

Upgrade installs a new release directory, validates schema compatibility,
backs up mutable configuration, prepares migrated copies where needed, runs
the complete smoke suite, atomically switches the `current` link, and restarts
the LaunchAgent. The previous runtime is retained.

`vibe-memory rollback` switches the program and managed configuration back.
Memory created after an upgrade is not silently reverted. An incompatible data
migration must create a complete snapshot and refuse to proceed unless the
previous runtime can safely read the restored snapshot.

## 15. Verification Matrix

### Installation

- clean macOS account;
- Codex-only and Codex-plus-Claude setups;
- pre-existing third-party hooks;
- clone paths containing spaces and non-ASCII characters;
- moved or deleted clone after installation;
- insufficient Python version and occupied default port;
- repeated installation;
- interrupted installation with rollback.

### Memory

- non-code conversations create personal candidates;
- unregistered directories create no project files;
- registered code and non-code folders create project candidates;
- nested directories select the longest registered parent;
- both clients read the same active memory;
- equivalent cross-client candidates merge;
- approval gates apply to formal memory;
- project short memory is model-distilled and bounded;
- sensitive data is rejected or masked;
- concurrent events do not corrupt data.

### Complete control plane

- pending and active memory workflows;
- project registration, initialization, switching, and upgrade;
- global preferences and project overrides;
- UI design-package creation, revision, approval, rejection, and hard gate;
- UI Skill import, validation, approval, two-client publication, disable, and rollback;
- Loop documentation, initialization, upgrade, and worktree workflow;
- policy layering, audits, backup, restore, and uninstall.

### macOS integration

- valid LaunchAgent configuration;
- login startup and crash recovery;
- loopback-only listener;
- correct permissions;
- Apple Silicon and Intel execution;
- fresh Codex and Claude sessions load the installed hooks.

## 16. Implementation Phases

### Phase 0: Public-release hygiene

Separate distributable templates from runtime data, remove personal absolute
paths from active code and docs, harden `.gitignore`, add license/privacy/security
documents, and scan the current tree and history.

### Phase 1: Portable shared core

Extract the router and client payload adapters; implement registry routing,
shared context, atomic storage, locks, deduplication, candidate validation, and
sensitive-data filtering; remove raw prompt capture from short memory.

### Phase 2: macOS product installation

Build the installer, versioned runtime, CLI, LaunchAgent, structural hook merge,
doctor, repair, rollback, and safe uninstall.

### Phase 3: Complete control-plane migration

Move the full current review console into the installed runtime, preserve all
data paths and workflows, and add legacy project-hook preview and migration.

### Phase 4: Release gate

Run clean-install, old-data migration, full regression, architecture-specific,
privacy, and documentation verification before publishing `v1.0.0`.

## 17. Acceptance Criteria

A new macOS user can clone the repository and run `./install.sh` without
manually editing a path. All Codex conversations then receive approval-gated
personal memory support. Claude Code can opt into the same storage and review
system. Registered code or non-code directories receive project memory; an
unregistered directory remains personal-only.

The complete current review console remains available. Existing client config
is merged rather than replaced. The installed system continues working after
the clone moves. It can diagnose, upgrade, roll back, and uninstall without
deleting user data. No formal personal long/short or project long memory takes
effect without explicit approval.
