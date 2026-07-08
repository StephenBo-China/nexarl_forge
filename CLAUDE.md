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
- `/Users/stephenbo/.codex/personal_memory/long.md`
- `/Users/stephenbo/.codex/personal_memory/short.md`

Read project short memory selectively from `codex/codex_short_memory.md`; do
not load the entire file by default.
