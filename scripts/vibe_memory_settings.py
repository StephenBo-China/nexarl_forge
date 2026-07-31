"""Versioned local settings and personal short-memory retention."""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import stat
import tempfile
import uuid
from typing import Mapping

from ui_design_store import atomic_write_json, exclusive_lock
import vibe_memory_hooks
import vibe_memory_install
from vibe_memory_paths import RuntimePaths


_SETTING_KEYS = (
    "schema_version",
    "first_run_complete",
    "codex_hooks_enabled",
    "claude_hooks_enabled",
    "automatic_candidate_checks",
    "personal_short_retention_days",
    "start_at_login",
    "formal_memory_requires_approval",
    "service_host",
    "service_port",
)
_RUNTIME_KEYS = {
    "app_version",
    "port",
    "python_executable",
    "python_version",
    "service",
}
_SECTION = re.compile(r"(?ms)^##[ \t]+([^\n]+)\n.*?(?=^##[ \t]+|\Z)")
_EXPIRY = re.compile(r"(?m)^expires_on:[ \t]*(\d{4}-\d{2}-\d{2})[ \t]*$")


def default_settings() -> dict[str, object]:
    return {
        "schema_version": 1,
        "first_run_complete": False,
        "codex_hooks_enabled": True,
        "claude_hooks_enabled": False,
        "automatic_candidate_checks": True,
        "personal_short_retention_days": 30,
        "start_at_login": True,
        "formal_memory_requires_approval": True,
        "service_host": "127.0.0.1",
        "service_port": 8897,
    }


def validate_settings(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("settings must contain an object")
    unknown = set(value).difference(_SETTING_KEYS)
    missing = set(_SETTING_KEYS).difference(value)
    if unknown:
        raise ValueError(f"unknown settings: {', '.join(sorted(unknown))}")
    if missing:
        raise ValueError(f"missing settings: {', '.join(sorted(missing))}")
    normalized = dict(value)
    if normalized["schema_version"] != 1:
        raise ValueError("schema_version must be 1")
    for key in (
        "first_run_complete",
        "codex_hooks_enabled",
        "claude_hooks_enabled",
        "automatic_candidate_checks",
        "start_at_login",
    ):
        if not isinstance(normalized[key], bool):
            raise ValueError(f"{key} must be a boolean")
    if normalized["formal_memory_requires_approval"] is not True:
        raise ValueError("formal_memory_requires_approval must remain true")
    if normalized["service_host"] != "127.0.0.1":
        raise ValueError("service_host must remain 127.0.0.1")
    retention = normalized["personal_short_retention_days"]
    if isinstance(retention, bool) or not isinstance(retention, int) or retention < 0:
        raise ValueError("personal_short_retention_days must be a non-negative integer")
    port = normalized["service_port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("service_port must be an integer from 1 through 65535")
    return normalized


def _config_path(paths: RuntimePaths) -> pathlib.Path:
    return pathlib.Path(paths.install_root) / "config.json"


def _read_config(path: pathlib.Path) -> dict[str, object]:
    if path.is_symlink():
        raise ValueError("settings path must not be a symlink")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("settings file is malformed") from error
    if not isinstance(value, dict):
        raise ValueError("settings file must contain an object")
    return value


def load_settings(paths: RuntimePaths) -> dict[str, object]:
    persisted = _read_config(_config_path(paths))
    if not persisted:
        return default_settings()
    unknown = set(persisted).difference(_SETTING_KEYS).difference(_RUNTIME_KEYS)
    if unknown:
        raise ValueError(f"unknown config fields: {', '.join(sorted(unknown))}")
    result = default_settings()
    for key in _SETTING_KEYS:
        if key in persisted:
            result[key] = persisted[key]
    if "service_port" not in persisted and "port" in persisted:
        result["service_port"] = persisted["port"]
    if "service_port" in persisted and "port" in persisted:
        if persisted["service_port"] != persisted["port"]:
            raise ValueError("runtime config ports do not match")
    return validate_settings(result)


def save_settings(paths: RuntimePaths, value: Mapping[str, object]) -> dict[str, object]:
    normalized = validate_settings(value)
    path = _config_path(paths)
    if path.is_symlink():
        raise ValueError("settings path must not be a symlink")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with exclusive_lock(path.with_name(".config.json.vibe-memory.lock")):
        existing = _read_config(path)
        runtime = {key: existing[key] for key in _RUNTIME_KEYS if key in existing}
        if "port" in runtime:
            runtime["port"] = normalized["service_port"]
        persisted = {**runtime, **normalized}
        atomic_write_json(path, persisted, backup=path.exists())
        path.chmod(0o600)
    return dict(normalized)


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError("short memory write")
        offset += written


def _atomic_write_text(path: pathlib.Path, content: str, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _backup_source(path: pathlib.Path, content: bytes, mode: int) -> pathlib.Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    while True:
        backup = path.with_name(f"{path.name}.bak.{stamp}-{uuid.uuid4().hex[:8]}")
        try:
            descriptor = os.open(
                backup,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                mode,
            )
        except FileExistsError:
            continue
        try:
            _write_all(descriptor, content)
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory_fd = os.open(
            path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return backup


def prune_personal_short(
    path: pathlib.Path,
    *,
    today: dt.date | None = None,
    retention_days: int = 30,
) -> list[str]:
    if (
        isinstance(retention_days, bool)
        or not isinstance(retention_days, int)
        or retention_days < 0
    ):
        raise ValueError("retention_days must be a non-negative integer")
    target = pathlib.Path(path)
    if target.is_symlink():
        raise ValueError("personal short memory must not be a symlink")
    try:
        metadata = target.stat()
        source = target.read_bytes()
    except FileNotFoundError:
        return []
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("personal short memory must be a regular file")
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("personal short memory must be UTF-8") from error
    cutoff = today or dt.date.today()
    removed: list[str] = []

    def retain_or_remove(match: re.Match[str]) -> str:
        expiry_match = _EXPIRY.search(match.group(0))
        if expiry_match is None:
            return match.group(0)
        try:
            expiry = dt.date.fromisoformat(expiry_match.group(1))
        except ValueError:
            return match.group(0)
        if expiry >= cutoff:
            return match.group(0)
        removed.append(match.group(1).strip())
        return ""

    updated = _SECTION.sub(retain_or_remove, text)
    if not removed:
        return []
    mode = stat.S_IMODE(metadata.st_mode)
    _backup_source(target, source, mode)
    _atomic_write_text(target, updated, mode)
    return removed


def _home(paths: RuntimePaths) -> pathlib.Path:
    return pathlib.Path(paths.personal_memory).parents[1]


def _disable_hook(path: pathlib.Path) -> dict[str, object]:
    if path.is_symlink():
        raise ValueError("hook configuration path must not be a symlink")
    if not path.exists():
        return {"changed": False, "path": str(path), "status": "missing"}
    current = vibe_memory_hooks.load_document(path)
    if "hooks" not in current:
        return {"changed": False, "path": str(path), "status": "current"}
    updated = vibe_memory_hooks.remove_managed_entries(current)
    if updated == current:
        return {"changed": False, "path": str(path), "status": "current"}
    result = vibe_memory_hooks.write_with_backup(path, updated)
    result["status"] = "updated"
    return result


def reconcile_hooks(
    paths: RuntimePaths, value: Mapping[str, object]
) -> dict[str, dict[str, object]]:
    settings = validate_settings(value)
    home = _home(paths)
    targets = (
        ("codex", home / ".codex" / "hooks.json", "codex", "codex_hooks_enabled"),
        (
            "claude",
            home / ".claude" / "settings.json",
            "claude-code",
            "claude_hooks_enabled",
        ),
    )
    result: dict[str, dict[str, object]] = {}
    for name, target, agent, key in targets:
        result[name] = (
            vibe_memory_hooks.repair(target, agent, paths.launcher)
            if settings[key]
            else _disable_hook(target)
        )
    return result


def reconcile_launch_agent(
    paths: RuntimePaths, value: Mapping[str, object]
) -> dict[str, object]:
    settings = validate_settings(value)
    target = pathlib.Path(paths.launch_agent)
    if settings["start_at_login"]:
        content = vibe_memory_install.render_launch_agent(
            paths, port=int(settings["service_port"])
        )
        return vibe_memory_install.install_launch_agent(paths, content)
    if target.is_symlink():
        raise ValueError("LaunchAgent target must not be a symlink")
    try:
        metadata = target.stat()
    except FileNotFoundError:
        return {"changed": False, "path": str(target)}
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("LaunchAgent target must be a regular file")
    target.unlink()
    return {"changed": True, "path": str(target)}
