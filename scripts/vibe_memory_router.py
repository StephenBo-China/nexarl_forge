"""Resolve workspace directories to registered memory projects."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shlex
import subprocess
import sys
import time
from typing import Any, Mapping

import memory_project
import vibe_memory_paths
from loop_superpowers import atomic_write_text
from ui_design_store import atomic_write_json, exclusive_lock
from vibe_memory_events import NormalizedEvent, normalize_event


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
IDEMPOTENCY_FILENAME = "hook_events.json"


def _markdown_escape(value: object) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("\r", " ")
        .replace("\n", " ")
    )


class IdempotencyStore:
    """Claim normalized hook events once within a short retry window."""

    def __init__(self, path: pathlib.Path, ttl_seconds: float = 30) -> None:
        self.path = pathlib.Path(path)
        self.ttl_seconds = ttl_seconds
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def claim(self, event: NormalizedEvent) -> bool:
        key_material = json.dumps(
            [
                event.agent,
                event.session_id,
                event.event,
                str(event.cwd),
                event.payload_digest,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
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
    project_pending_line = ""
    project_category_line = ""
    approval_scope = "Official personal long/short memory"
    command_parts = ["python3"]
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
        project_section = f"\nRegistered project: `{_markdown_escape(root)}`\n"
        project_pending_line = (
            f'\n- project candidates: {pending.get("project_pending", 0)}'
        )
        project_category_line = f"\n- Project categories: {', '.join(PROJECT_CATEGORIES)}."
        approval_scope += " and project long memory"
        command_parts = [
            "env",
            f"MEMORY_REVIEW_PROJECT_ROOT={root}",
            *command_parts,
        ]
    required_lines = "\n".join(f"- `{_markdown_escape(path)}`" for path in required)
    personal_categories = ", ".join(PERSONAL_CATEGORIES)
    command_parts.extend(
        [
            str(pathlib.Path(__file__).resolve().parent / "memory_review.py"),
            "propose",
            "--scope",
            "personal",
            "--target",
            "long",
            "--category",
            "CATEGORY",
            "--title",
            "TITLE",
            "--summary",
            "SUMMARY",
            "--source-event",
            "agent_summary",
            "--source-agent",
            event.agent,
            "--policy-version",
            "1",
        ]
    )
    command = " ".join(shlex.quote(part) for part in command_parts)
    return f"""# Shared Memory Context

- source agent: {_markdown_escape(event.agent)}
- event: {_markdown_escape(event.event)}
- pending total: {pending.get("pending", 0)}
- personal candidates: {pending.get("personal_pending", 0)}{project_pending_line}
{project_section}
## Required Memory

{required_lines}

## Approval and Candidate Governance

- {approval_scope} may change only
  after explicit approval of the exact candidate content.
- Write candidates only to the proposals files; never write directly to
  approved long or short memory.
- The active conversation model may create at most two distilled candidates.
- Personal categories: {personal_categories}.{project_category_line}
- Never capture raw prompts, secrets, filesystem paths, one-off tasks,
  screenshots, URLs, credentials, tokens, or uncertain assumptions.
- Hooks provide metadata and policy context only; they do not summarize prompts
  or call another model.

Candidate CLI:

    {command}
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


def _idempotency_path() -> pathlib.Path:
    runtime = vibe_memory_paths.for_home()
    return runtime.install_root / "state" / IDEMPOTENCY_FILENAME


def _refresh_review_queue(project_root: pathlib.Path) -> Mapping[str, int]:
    """Refresh and return counts for exactly one registered project."""
    script = pathlib.Path(__file__).resolve().parent / "memory_review_queue.py"
    environment = os.environ.copy()
    environment["MEMORY_REVIEW_PROJECT_ROOT"] = str(project_root)
    subprocess.run(
        [sys.executable, str(script), "refresh"],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=8,
        check=True,
    )
    queue_path = project_root / "codex" / "memory_review_queue.json"
    try:
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid memory review queue {queue_path}: {error}") from error
    if not isinstance(queue, dict) or not isinstance(queue.get("counts"), dict):
        raise ValueError(f"invalid memory review queue {queue_path}: missing counts")
    return queue["counts"]


def _registry_projects() -> list[dict[str, object]]:
    value = memory_project.registry().get("projects", [])
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def handle_event(
    agent: str,
    event: str,
    payload: object,
    cwd: pathlib.Path,
) -> dict[str, Any]:
    """Route one shared hook event without retaining its raw payload."""
    normalized = normalize_event(agent, event, payload, cwd)
    if not IdempotencyStore(_idempotency_path()).claim(normalized):
        return {"status": "duplicate"}

    project_root = resolve_registered_project(normalized.cwd, _registry_projects())
    counts: Mapping[str, int]
    if project_root is None:
        counts = {"pending": 0, "personal_pending": 0, "project_pending": 0}
    else:
        counts = _refresh_review_queue(project_root)

    context = build_context(normalized, project_root, counts)
    if project_root is not None:
        atomic_write_text(project_root / "codex" / "codex_context_packet.md", context)
        atomic_write_text(
            project_root / "codex" / "shared_memory_context_packet.md", context
        )

    return {
        "status": "ok",
        "hookSpecificOutput": {"additionalContext": context},
    }
