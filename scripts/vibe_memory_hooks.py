"""Safely merge Vibe Memory's managed Codex and Claude Code hook entries."""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import pathlib
import shlex
import stat
import tempfile
from typing import Any

from ui_design_store import exclusive_lock


MANAGED_SIGNATURE = "vibe-memory hook --agent"
EVENTS = ("SessionStart", "UserPromptSubmit", "PreCompact", "PostCompact", "Stop")
AGENTS = ("codex", "claude-code")
_NO_SOURCE_CHECK = object()


class ConcurrentConfigChange(RuntimeError):
    """Raised when a config changes after repair loaded it."""


class ConfigWriteError(OSError):
    """Raised after replacement when durability confirmation fails."""

    def __init__(self, message: str, backup: pathlib.Path | None) -> None:
        super().__init__(message)
        self.backup = str(backup) if backup is not None else None


class ConfigSymlinkError(ValueError):
    """Raised when a config path is a symlink instead of a regular path."""


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
    if not isinstance(handler, dict) or not isinstance(handler.get("command"), str):
        return False
    try:
        tokens = shlex.split(handler["command"], comments=True, posix=True)
    except ValueError:
        return False
    if len(tokens) != 7:
        return False
    executable, script, action, agent_flag, agent, event_flag, event = tokens
    return (
        executable == "/usr/bin/python3"
        and script.endswith("/scripts/vibe_memory_cli.py")
        and action == "hook"
        and agent_flag == "--agent"
        and agent in AGENTS
        and event_flag == "--event"
        and event in EVENTS
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
            managed_handlers = [
                handler for handler in group["hooks"] if _is_managed_handler(handler)
            ]
            if not managed_handlers:
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
    if not isinstance(value, dict):
        raise ValueError("hook configuration root must be an object")
    copied = copy.deepcopy(value)
    if "hooks" not in copied:
        copied["hooks"] = {}
    elif not isinstance(copied["hooks"], dict):
        raise ValueError("hook configuration hooks must be an object")
    copied = remove_managed_entries(copied)
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
    _reject_symlink(target)
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        return {}
    return _parse_document_bytes(target, raw)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


def _parse_document_bytes(target: pathlib.Path, raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        detail = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
        raise ValueError(f"Invalid JSON in hook configuration {target}: {detail}") from exc
    if not isinstance(value, dict):
        raise ValueError("hook configuration root must be an object")
    return value


def _reject_symlink(target: pathlib.Path) -> None:
    if target.is_symlink():
        raise ConfigSymlinkError(f"hook configuration path must not be a symlink: {target}")


def _serialized_document(value: Any) -> bytes:
    if not isinstance(value, dict):
        raise ValueError("hook configuration root must be an object")
    try:
        return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode(
            "utf-8"
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("hook configuration must be a JSON-serializable object") from exc


def _backup_path(path: pathlib.Path, suffix: int = 0) -> pathlib.Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    extra = f"-{suffix}" if suffix else ""
    return path.with_name(f"{path.name}.bak.{stamp}{extra}")


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError(f"short write after {offset} of {len(content)} bytes")
        offset += written


def _fsync_directory(path: pathlib.Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _create_backup_exclusive(
    target: pathlib.Path, content: bytes, mode: int
) -> pathlib.Path:
    suffix = 0
    while True:
        backup = _backup_path(target, suffix)
        try:
            descriptor = os.open(
                backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode
            )
        except FileExistsError:
            suffix += 1
            continue
        try:
            os.fchmod(descriptor, mode)
            _write_all(descriptor, content)
            os.fsync(descriptor)
        except Exception:
            os.close(descriptor)
            backup.unlink(missing_ok=True)
            raise
        else:
            os.close(descriptor)
        try:
            _fsync_directory(target.parent)
        except Exception:
            backup.unlink(missing_ok=True)
            raise
        return backup


def _ensure_parent(target: pathlib.Path) -> None:
    parent_created = not target.parent.exists()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if parent_created:
        os.chmod(target.parent, 0o700)


def _source_bytes(target: pathlib.Path) -> bytes | None:
    _reject_symlink(target)
    try:
        return target.read_bytes()
    except FileNotFoundError:
        return None


def write_with_backup(
    path: str | pathlib.Path,
    value: Any,
    *,
    expected_source: bytes | None | object = _NO_SOURCE_CHECK,
) -> dict[str, Any]:
    """Atomically write an object, retaining a permission-preserving backup on change."""
    target = pathlib.Path(path)
    _reject_symlink(target)
    content = _serialized_document(value)
    _ensure_parent(target)
    original = _source_bytes(target)
    if expected_source is _NO_SOURCE_CHECK and original == content:
        return {"changed": False, "path": str(target), "backup": None}

    if original is None:
        existing_mode = 0o600
    else:
        existing_mode = stat.S_IMODE(target.stat().st_mode)

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = pathlib.Path(temporary_name)
    backup: pathlib.Path | None = None
    replaced = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fchmod(handle.fileno(), existing_mode)
            os.fsync(handle.fileno())
        current_source = _source_bytes(target)
        if expected_source is not _NO_SOURCE_CHECK and current_source != expected_source:
            raise ConcurrentConfigChange(
                f"hook configuration changed concurrently: {target}"
            )
        if current_source == content:
            temporary.unlink(missing_ok=True)
            return {"changed": False, "path": str(target), "backup": None}
        if current_source is not None:
            current_mode = stat.S_IMODE(target.stat().st_mode)
            backup = _create_backup_exclusive(target, current_source, current_mode)
        if expected_source is not _NO_SOURCE_CHECK and _source_bytes(target) != expected_source:
            raise ConcurrentConfigChange(
                f"hook configuration changed concurrently: {target}"
            )
        os.replace(temporary, target)
        replaced = True
        _fsync_directory(target.parent)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        if replaced:
            backup_description = str(backup) if backup is not None else "unavailable"
            raise ConfigWriteError(
                f"hook configuration was replaced but durability sync failed; backup: "
                f"{backup_description}",
                backup,
            ) from exc
        if backup is not None and backup.exists():
            backup.unlink(missing_ok=True)
            try:
                _fsync_directory(target.parent)
            except OSError:
                pass
        raise
    return {
        "changed": True,
        "path": str(target),
        "backup": str(backup) if backup is not None else None,
    }


def status(path: str | pathlib.Path, agent: str, runtime: str | pathlib.Path) -> dict[str, Any]:
    """Report whether a hook document is missing, current, drifted, or malformed."""
    target = pathlib.Path(path)
    if target.is_symlink():
        error = ConfigSymlinkError(f"hook configuration path must not be a symlink: {target}")
        return {"status": "malformed", "path": str(target), "error": str(error)}
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
    _reject_symlink(target)
    _ensure_parent(target)
    lock_path = target.with_name(f".{target.name}.vibe-memory.lock")
    with exclusive_lock(lock_path):
        _reject_symlink(target)
        source = _source_bytes(target)
        existed = source is not None
        current = _parse_document_bytes(target, source) if existed else {"hooks": {}}
        expected = merge_document(current, agent, runtime)
        if current == expected:
            return {
                "changed": False,
                "path": str(target),
                "backup": None,
                "status": "current",
            }
        result = write_with_backup(target, expected, expected_source=source)
        result["status"] = "created" if result["changed"] and not existed else (
            "updated" if result["changed"] else "current"
        )
        return result
