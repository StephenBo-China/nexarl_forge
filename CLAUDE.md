# vibe_coding_manage_platform Shared Memory Instructions

This repository shares memory between Codex and Claude Code through project
memory files under `codex/` and personal memory under `~/.codex/`.

@.claude/rules/shared-memory.md
@codex/codex_context_packet.md
@codex/shared_memory_context_packet.md
@codex/codex_long_memory.md

If `.loop/config.json` exists, also load:

@.loop/config.json

## Required Startup Context

- `README.md`
- `codex/codex_long_memory.md`
- `codex/codex_context_packet.md`
- `codex/shared_memory_context_packet.md`
- `~/.codex/personal_memory/long.md`
- `~/.codex/personal_memory/short.md`

Read project short memory selectively from `codex/codex_short_memory.md`; do
not load the entire file by default.


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
MEMORY_REVIEW_PROJECT_ROOT=$(pwd) python3 scripts/memory_review.py propose \
  --scope personal|project --target long|short --category CATEGORY \
  --title "TITLE" --summary "SUMMARY" --source-event agent_summary
```

Do not write directly to official long/short memory. Official promotion remains
approval-gated. If candidate creation fails, skip it and report the concrete
reason in the current Codex or Claude Code conversation.
