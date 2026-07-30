"""Resolve workspace directories to registered memory projects."""

from __future__ import annotations

import hashlib
import json
import pathlib
import time
from typing import Mapping

from ui_design_store import atomic_write_json, exclusive_lock
from vibe_memory_events import NormalizedEvent


PERSONAL_CATEGORIES = (
    "development_habit",
    "collaboration_preference",
    "work_style",
    "thinking_style",
    "user_profile",
    "workflow_preference",
)
PROJECT_CATEGORIES = (
    "project_architecture",
    "deployment_rule",
    "product_direction",
    "technical_constraint",
    "project_workflow",
)


class IdempotencyStore:
    """Claim normalized hook events once within a short retry window."""

    def __init__(self, path: pathlib.Path, ttl_seconds: float = 30) -> None:
        self.path = pathlib.Path(path)
        self.ttl_seconds = ttl_seconds
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def claim(self, event: NormalizedEvent) -> bool:
        key_material = "|".join(
            (
                event.agent,
                event.session_id,
                event.event,
                str(event.cwd),
                event.payload_digest,
            )
        )
        key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
        with exclusive_lock(self.lock_path):
            entries = self._read_entries()
            current = time.time()
            cutoff = current - self.ttl_seconds
            live = {
                entry_key: timestamp
                for entry_key, timestamp in entries.items()
                if isinstance(timestamp, (int, float))
                and not isinstance(timestamp, bool)
                and timestamp > cutoff
            }
            if key in live:
                return False
            live[key] = current
            atomic_write_json(self.path, live)
            return True

    def _read_entries(self) -> dict[str, object]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid idempotency store {self.path}: {error}") from error
        if not isinstance(value, dict):
            raise ValueError(f"invalid idempotency store {self.path}: root must be an object")
        return value


def build_context(
    event: NormalizedEvent,
    project_root: pathlib.Path | None,
    pending: Mapping[str, int],
) -> str:
    """Build one policy packet for registered projects or personal-only events."""
    personal_root = pathlib.Path.home() / ".codex" / "personal_memory"
    required = [
        personal_root / "long.md",
        personal_root / "short.md",
        personal_root / "proposals.md",
    ]
    project_section = ""
    cli_prefix = "python3"
    if project_root is not None:
        root = pathlib.Path(project_root)
        required = [
            root / "README.md",
            root / "codex" / "codex_long_memory.md",
            root / "codex" / "codex_short_memory.md",
            root / "codex" / "memory_proposals.md",
            root / "codex" / "codex_context_packet.md",
            *required,
        ]
        project_section = f"\nRegistered project: `{root}`\n"
        cli_prefix = f"MEMORY_REVIEW_PROJECT_ROOT={root} python3"
    required_lines = "\n".join(f"- `{path}`" for path in required)
    personal_categories = ", ".join(PERSONAL_CATEGORIES)
    project_categories = ", ".join(PROJECT_CATEGORIES)
    return f"""# Shared Memory Context

- source agent: {event.agent}
- event: {event.event}
- pending total: {pending.get("pending", 0)}
- personal candidates: {pending.get("personal_pending", 0)}
- project candidates: {pending.get("project_pending", 0)}
{project_section}
## Required Memory

{required_lines}

## Approval and Candidate Governance

- Official personal long/short memory and project long memory may change only
  after explicit approval of the exact candidate content.
- Write candidates only to the proposals files; never write directly to
  approved long or short memory.
- The active conversation model may create at most two distilled candidates.
- Personal categories: {personal_categories}.
- Project categories: {project_categories}.
- Never capture raw prompts, secrets, filesystem paths, one-off tasks,
  screenshots, URLs, credentials, tokens, or uncertain assumptions.
- Hooks provide metadata and policy context only; they do not summarize prompts
  or call another model.

Candidate CLI:
`{cli_prefix} {pathlib.Path(__file__).resolve().parent / "memory_review.py"} propose --scope personal --target long --category CATEGORY --title TITLE --summary SUMMARY --source-event agent_summary --source-agent {event.agent} --policy-version 1`
"""


def resolve_registered_project(
    cwd: pathlib.Path, projects: list[dict[str, object]]
) -> pathlib.Path | None:
    """Return the deepest registered root containing *cwd*, if any."""
    current = cwd.expanduser().resolve()
    matches: list[pathlib.Path] = []
    for entry in projects:
        raw_root = entry.get("root") if isinstance(entry, dict) else None
        if not isinstance(raw_root, str) or not raw_root:
            continue
        try:
            root = pathlib.Path(raw_root).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if current == root or root in current.parents:
            matches.append(root)
    return max(matches, key=lambda root: len(root.parts)) if matches else None
