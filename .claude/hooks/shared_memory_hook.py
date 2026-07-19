#!/usr/bin/env python3
"""Shared memory hook installed by vibe_coding_manage_platform."""

from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import subprocess
import sys
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[2]
APP_ROOT = pathlib.Path("/Users/stephenbo/Noema/Projects/vibe_coding_manage_platform")
CODEX_DIR = ROOT / "codex"
SHORT_MEMORY = CODEX_DIR / "codex_short_memory.md"
PROJECT_PROPOSALS = CODEX_DIR / "memory_proposals.md"
CONTEXT_PACKET = CODEX_DIR / "codex_context_packet.md"
SHARED_CONTEXT_PACKET = CODEX_DIR / "shared_memory_context_packet.md"
LONG_MEMORY = CODEX_DIR / "codex_long_memory.md"
REVIEW_SCRIPT = APP_ROOT / "scripts" / "memory_review_queue.py"
REVIEW_URL = "http://127.0.0.1:8897"
LOOP_CONFIG = ROOT / ".loop" / "config.json"
SOURCE = "claude_code"


def now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def read_stdin_json() -> Any:
    raw = sys.stdin.read()
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw_stdin": raw[:4000]}


def find_prompt(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, [find_prompt(item) for item in value])).strip()
    if isinstance(value, dict):
        for key in ["prompt", "user_prompt", "userPrompt", "input", "message", "text", "content"]:
            if key in value:
                found = find_prompt(value[key])
                if found:
                    return found
        for item in value.values():
            found = find_prompt(item)
            if found:
                return found
    return ""


def ensure_files() -> None:
    CODEX_DIR.mkdir(parents=True, exist_ok=True)
    if not SHORT_MEMORY.exists():
        SHORT_MEMORY.write_text("# Project Short Memory\n\n## Recent Hook Events\n", encoding="utf-8")
    if not PROJECT_PROPOSALS.exists():
        PROJECT_PROPOSALS.write_text("# Project Memory Proposals\n\n## Pending Project Long-Memory Candidates\n", encoding="utf-8")


def refresh_queue() -> dict[str, int]:
    if not REVIEW_SCRIPT.exists():
        return {"pending": 0, "project_pending": 0, "personal_pending": 0}
    env = os.environ.copy()
    env["MEMORY_REVIEW_PROJECT_ROOT"] = str(ROOT)
    subprocess.run([sys.executable, str(REVIEW_SCRIPT), "refresh"], cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8, check=False)
    try:
        data = json.loads((CODEX_DIR / "memory_review_queue.json").read_text(encoding="utf-8"))
        return data.get("counts", {})
    except Exception:
        return {"pending": 0, "project_pending": 0, "personal_pending": 0}


def context_text(event: str, counts: dict[str, int]) -> str:
    loop = ""
    if LOOP_CONFIG.exists():
        loop = f"""

## Loop Engineering

- project loop config: `{LOOP_CONFIG}` (present)
- Codex loop directory: `/Users/stephenbo/.codex/loop_engineering`
- Claude loop directory: `/Users/stephenbo/.claude/loop_engineering`
- Required behavior: read `.loop/config.json` before loop planning,
  staging work, Claude evaluation, or master/production decisions.
- Worktree behavior: use a dedicated worktree when the user says `开 worktree`;
  loop implementation starts in a dedicated worktree by default.
"""
    return f"""# Shared Memory Context Packet

Generated: {now()}
Trigger: {SOURCE}:{event}
Repository: `{ROOT}`

## Required Memory

- `README.md`
- `codex/codex_long_memory.md`
- `codex/codex_short_memory.md` (read selectively)
- `codex/memory_proposals.md`
- `/Users/stephenbo/.codex/personal_memory/long.md`
- `/Users/stephenbo/.codex/personal_memory/short.md`

## Pending Memory Review

- pending total: {counts.get("pending", 0)}
- project candidates: {counts.get("project_pending", 0)}
- personal candidates: {counts.get("personal_pending", 0)}
- review URL: {REVIEW_URL}
- CLI: `MEMORY_REVIEW_PROJECT_ROOT={ROOT} python3 {APP_ROOT}/scripts/memory_review.py list`

## Candidate Generation Reminder

- The active {SOURCE} conversation model reviews memory candidates before its
  final response and at compaction boundaries.
- Hooks must not copy raw prompts into candidate files or call another model.
- Create at most two distilled candidates through `memory_review.py propose`.
- If creation fails, report the concrete reason in the current conversation.
{loop}
"""


def append_short(event: str, payload: Any) -> None:
    prompt = find_prompt(payload)
    entry = f"\n### {now()} - {SOURCE}:{event}\n\n- cwd: `{os.getcwd()}`\n"
    if prompt:
        compact = " ".join(prompt.split())
        if len(compact) > 280:
            compact = compact[:277].rstrip() + "..."
        entry += "- summary: " + compact + "\n"
    else:
        entry += "- no user prompt payload was available to this hook.\n"
    with SHORT_MEMORY.open("a", encoding="utf-8") as handle:
        handle.write(entry)


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    payload = read_stdin_json()
    ensure_files()
    counts = refresh_queue()
    text = context_text(event, counts).rstrip() + "\n"
    CONTEXT_PACKET.write_text(text, encoding="utf-8")
    SHARED_CONTEXT_PACKET.write_text(text, encoding="utf-8")
    append_short(event, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
