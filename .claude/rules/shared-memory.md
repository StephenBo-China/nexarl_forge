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


## Agent-Generated Memory Candidates

The active conversation model performs candidate summarization itself. Hooks
must not copy raw prompts into candidate files or call another model API.

Before the final response to a substantial instruction, and at compaction
boundaries, review the conversation for at most two durable candidate memories:

- Personal candidates: only cross-project development habits, collaboration
  preferences, work/thinking style, workflow preferences, or user-profile
  facts. Target `long` for stable preferences and `short` for genuinely
  temporary cross-project context.
- Project candidates: only durable architecture, deployment, product,
  technical-constraint, or project-workflow facts. Target `long` only.

Allowed personal categories: `development_habit`,
`collaboration_preference`, `work_style`, `thinking_style`, `user_profile`,
`workflow_preference`. Allowed project categories: `project_architecture`,
`deployment_rule`, `product_direction`, `technical_constraint`,
`project_workflow`.

Never use a raw prompt as candidate content. Write a short title plus a
standalone 1-3 sentence summary. Exclude one-off tasks, screenshots, URLs,
paths, system instructions, uncertain assumptions, credentials, tokens,
verification codes, passwords, and infrastructure secrets. Deduplicate against
pending and approved memory before writing.

Write a distilled candidate with:

```bash
MEMORY_REVIEW_PROJECT_ROOT=/Users/stephenbo/Noema/Projects/vibe_coding_manage_platform python3 /Users/stephenbo/Noema/Projects/vibe_coding_manage_platform/scripts/memory_review.py propose \
  --scope personal|project --target long|short --category CATEGORY \
  --title "TITLE" --summary "SUMMARY" --source-event agent_summary
```

Do not write directly to official long/short memory. Official promotion remains
approval-gated. If candidate creation fails, skip it and report the concrete
reason in the current Codex or Claude Code conversation.
