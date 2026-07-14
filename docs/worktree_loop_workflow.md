# Multi-Conversation Worktree And Loop Workflow

This document defines the safe cross-project workflow used by Codex and Claude
Code when several conversations develop against the same repository.

## Invariants

- The original repository is the canonical workspace and normally stays on the
  main branch as a mirror of `origin/<main>`.
- One task equals one conversation, one external worktree, and one feature
  branch. Two conversations must never write the same worktree or branch.
- Feature development may run concurrently. Main-branch integration, main push,
  canonical synchronization, and shared staging deployment are serialized with
  repository locks.
- Feature worktrees live outside the canonical repository so recursive search,
  packaging, file watching, and `git status` do not discover sibling worktrees.
- Main integration happens in a temporary release worktree based on the latest
  remote main branch. The original repository is not used to resolve conflicts.
- Force push, automatic stash, reset, checkout-overwrite, and deletion of user
  files are forbidden.
- A final delivery is complete only when remote main, canonical main, and the
  deployed commit match, and every released feature commit is an ancestor of
  remote main.

## Lifecycle

1. `start`: fetch remote main, create a unique feature branch and external
   worktree, and register task ownership.
2. `developing`: implement only in the task worktree. Commit and push only the
   task branch.
3. `finish`: require a clean worktree, record the feature commit, and mark the
   task ready for user acceptance. This does not merge main.
4. `release`: after explicit user approval, acquire the repository release
   lock, integrate the feature into the latest remote main in a temporary
   release worktree, run verification, and push with a normal fast-forward
   update. A concurrent remote update causes a safe failure and retry.
5. `sync-canonical`: fetch remote main and update the original repository with
   `--ff-only`. Dirty paths that overlap incoming paths block synchronization;
   user changes are never stashed or overwritten.
6. `deploy`: shared staging requires its own lock and deploys the exact remote
   main commit after merge. Loop-branch staging is either serialized or uses
   explicitly isolated remote paths, ports, data prefixes, and OSS prefixes.
7. `verify`: report feature ancestry and the commits for remote main, canonical
   main, and deployment. Mismatch means the task is not complete.
8. `cleanup`: remove a clean feature worktree only after its commit is an
   ancestor of remote main. Remote branch deletion always needs separate user
   approval.

## CLI

```bash
python3 scripts/worktree_flow.py start /path/to/repo \
  --task preview-user-chat --conversation <conversation-id>

python3 scripts/worktree_flow.py status /path/to/repo
python3 scripts/worktree_flow.py finish /path/to/repo --task preview-user-chat

# Only after the user explicitly approves merging main:
python3 scripts/worktree_flow.py release /path/to/repo \
  --task preview-user-chat --approved \
  --test-command "python3 -m pytest -q tests"

python3 scripts/worktree_flow.py sync-canonical /path/to/repo
python3 scripts/worktree_flow.py deploy-staging /path/to/repo \
  --task preview-user-chat --approved \
  --command "./scripts/deploy_staging.sh" \
  --deployed-commit-command "./scripts/deployed_commit.sh"
python3 scripts/worktree_flow.py verify /path/to/repo --task preview-user-chat
python3 scripts/worktree_flow.py cleanup /path/to/repo \
  --task preview-user-chat --approved
```

The runtime registry and locks are local machine state and are not committed:

```text
~/.codex/worktree_manager/tasks.json
~/.codex/worktree_manager/locks/<repository-id>.release.lock/
~/.codex/worktree_manager/locks/<repository-id>.staging.lock/
```

## Canonical Synchronization

The original repository should stay clean and on the configured main branch.
When it is dirty, synchronization compares dirty paths with paths changed by
`HEAD..origin/<main>`:

- no overlap: an `ff-only` update is allowed and dirty content must remain;
- overlap: stop with `blocked_by_dirty_overlap`;
- wrong current branch: stop with `blocked_by_current_branch`;
- divergent history: stop with `blocked_by_non_fast_forward`.

The workflow must report the blocker instead of mutating user work.

## Completion Report

Every final report includes:

| Location | Required evidence |
| --- | --- |
| Feature worktree | branch, clean state, feature commit |
| Remote main | main commit and feature ancestry |
| Canonical repository | current branch, commit, dirty-state preservation |
| Staging | deployed commit, health, environment and resource boundary |
