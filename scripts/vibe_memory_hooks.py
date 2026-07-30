"""Safely merge Vibe Memory's managed Codex and Claude Code hook entries."""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import pathlib
import shlex
import shutil
import stat
import tempfile
from typing import Any


MANAGED_SIGNATURE = "vibe-memory hook --agent"
EVENTS = ("SessionStart", "UserPromptSubmit", "PreCompact", "PostCompact", "Stop")
AGENTS = ("codex", "claude-code")


def _validate_agent_event(agent: str, event: str) -> None:
    if agent not in AGENTS:
        raise ValueError(f"unsupported agent: {agent!r}")
    if event not in EVENTS:
        raise ValueError(f"unsupported hook event: {event!r}")


def command(runtime: str | pathlib.Path, agent: str, event: str) -> str:
    """Return a shell-safe managed hook command for one supported event."""
    _validate_agent_event(agent, event)
    runtime_path = pathlib.PurePath(runtime) / "scripts" / "vibe_memory_cli.py"
    executable = shlex.quote(str(runtime_path))
    return (
        f"/usr/bin/python3 {executable} hook --agent {agent} --event {event} "
        f"# {MANAGED_SIGNATURE}"
    )


def _require_document(value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(value, dict):
        raise ValueError("hook configuration root must be an object")
    hooks = value.get("hooks")
    if not isinstance(hooks, dict):
        raise ValueError("hook configuration hooks must be an object")
    return value, hooks


def _is_managed_handler(handler: Any) -> bool:
    return (
        isinstance(handler, dict)
        and isinstance(handler.get("command"), str)
        and MANAGED_SIGNATURE in handler["command"]
    )


def remove_managed_entries(value: Any) -> dict[str, Any]:
    """Return a copy without managed handler commands in managed hook events."""
    copied = copy.deepcopy(value)
    _, hook_events = _require_document(copied)
    for event in EVENTS:
        groups = hook_events.get(event)
        if groups is None:
            continue
        if not isinstance(groups, list):
            raise ValueError(f"hook configuration hooks.{event} must be an array")
        retained_groups: list[Any] = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                retained_groups.append(group)
                continue
            retained_handlers = [
                handler for handler in group["hooks"] if not _is_managed_handler(handler)
            ]
            if not retained_handlers:
                continue
            updated_group = copy.deepcopy(group)
            updated_group["hooks"] = retained_handlers
            retained_groups.append(updated_group)
        if retained_groups:
            hook_events[event] = retained_groups
        else:
            del hook_events[event]
    return copied


def _managed_group(runtime: str | pathlib.Path, agent: str, event: str) -> dict[str, Any]:
    return {
        "hooks": [{
            "type": "command",
            "command": command(runtime, agent, event),
        }]
    }


def merge_document(value: Any, agent: str, runtime: str | pathlib.Path) -> dict[str, Any]:
    """Safely merge exactly one managed handler group into every supported event."""
    _validate_agent_event(agent, EVENTS[0])
    copied = remove_managed_entries(value)
    _, hook_events = _require_document(copied)
    for event in EVENTS:
        groups = hook_events.get(event)
        if groups is None:
            groups = []
            hook_events[event] = groups
        if not isinstance(groups, list):
            raise ValueError(f"hook configuration hooks.{event} must be an array")
        groups.append(_managed_group(runtime, agent, event))
    return copied


def load_document(path: str | pathlib.Path) -> dict[str, Any]:
    """Load a hook JSON document without changing the source file."""
    target = pathlib.Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in hook configuration {target}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("hook configuration root must be an object")
    return value


def _serialized_document(value: Any) -> bytes:
    if not isinstance(value, dict):
        raise ValueError("hook configuration root must be an object")
    try:
        return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode(
            "utf-8"
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("hook configuration must be a JSON-serializable object") from exc


def _backup_path(path: pathlib.Path) -> pathlib.Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = path.with_name(f"{path.name}.bak.{stamp}")
    suffix = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak.{stamp}-{suffix}")
        suffix += 1
    return candidate


def write_with_backup(path: str | pathlib.Path, value: Any) -> dict[str, Any]:
    """Atomically write an object, retaining a permission-preserving backup on change."""
    target = pathlib.Path(path)
    content = _serialized_document(value)
    parent_created = not target.parent.exists()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if parent_created:
        os.chmod(target.parent, 0o700)
    try:
        original = target.read_bytes()
        existing_mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        original = None
        existing_mode = 0o600
    if original == content:
        return {"changed": False, "path": str(target), "backup": None}

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = pathlib.Path(temporary_name)
    backup: pathlib.Path | None = None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, existing_mode)
        if original is not None:
            backup = _backup_path(target)
            shutil.copy2(target, backup)
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        if backup is not None:
            backup.unlink(missing_ok=True)
        raise
    return {
        "changed": True,
        "path": str(target),
        "backup": str(backup) if backup is not None else None,
    }


def status(path: str | pathlib.Path, agent: str, runtime: str | pathlib.Path) -> dict[str, Any]:
    """Report whether a hook document is missing, current, drifted, or malformed."""
    target = pathlib.Path(path)
    if not target.exists():
        return {"status": "missing", "path": str(target)}
    try:
        current = load_document(target)
        expected = merge_document(current, agent, runtime)
    except (OSError, ValueError) as exc:
        return {"status": "malformed", "path": str(target), "error": str(exc)}
    state = "current" if current == expected else "drifted"
    return {"status": state, "path": str(target)}


def repair(path: str | pathlib.Path, agent: str, runtime: str | pathlib.Path) -> dict[str, Any]:
    """Create or repair one document and describe its resulting backup, if any."""
    target = pathlib.Path(path)
    existed = target.exists()
    current = load_document(target) if existed else {"hooks": {}}
    expected = merge_document(current, agent, runtime)
    result = write_with_backup(target, expected)
    result["status"] = "created" if result["changed"] and not existed else (
        "updated" if result["changed"] else "current"
    )
    return result
