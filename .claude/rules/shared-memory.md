# Shared Codex/Claude Memory Rule

Codex and Claude Code should use the same project memory files in this
repository and the same approval-gated personal memory files.

## Read Policy

- Read project and personal long memory before broad implementation,
  deployment, product, architecture, or workflow decisions.
- Read project short memory selectively: recent entries, targeted searches, or
  status summaries.
- If `.loop/config.json` exists, read it before loop planning, staging
  evaluation, Playwright/browser testing, report writing, or pass/block
  decisions.
- When `.loop/config.json` exists, also read:
  - `/Users/stephenbo/.codex/loop_engineering`
  - `/Users/stephenbo/.claude/loop_engineering`

## Write Policy

- Project short memory may be appended by hooks.
- Project long memory receives reviewed durable facts only; write candidates to
  `codex/memory_proposals.md` first.
- Personal long and short memory require explicit approval of exact content.
- Personal candidates may be written only to
  `/Users/stephenbo/.codex/personal_memory/proposals.md`, and only when they
  are distilled cross-project habits, preferences, thinking style, workflow
  preferences, or user-profile facts.

## Memory Review Console

The central memory review console is maintained at:

- `/Users/stephenbo/Noema/Projects/vibe_coding_manage_platform`

For this project, pass:

- `MEMORY_REVIEW_PROJECT_ROOT=/Users/stephenbo/Noema/Projects/vibe_coding_manage_platform`
