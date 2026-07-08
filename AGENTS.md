# vibe_coding_manage_platform Codex Instructions

## Required Memory Context

For every new Codex thread, substantial user instruction, and compaction
boundary in this repository, load relevant memory from:

- `README.md`
- `codex/codex_long_memory.md`
- `codex/codex_context_packet.md`
- `codex/shared_memory_context_packet.md`
- `~/.codex/personal_memory/long.md`
- `~/.codex/personal_memory/short.md`

Project short memory is large by design. Read recent or relevant sections only:

- `codex/codex_short_memory.md`

If `.loop/config.json` exists, read it before PRD planning, loop branch work,
staging deployment, Claude evaluation, or master/production decisions.

## Memory Governance

Project short memory may be appended by hooks. Project long memory should
receive only reviewed durable project facts. Personal long and short memory
require explicit approval of exact content; write personal candidates only to
`~/.codex/personal_memory/proposals.md`.
