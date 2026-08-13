"""Versioned local settings and personal short-memory retention."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import stat
import tempfile
import uuid
from contextlib import contextmanager
from collections.abc import Callable
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
_SECTION = re.compile(r"(?ms)^#{2,3}[ \t]+([^\n]+)\n.*?(?=^#{2,3}[ \t]+|\Z)")
_EXPIRY = re.compile(r"(?m)^expires_on:[ \t]*(\d{4}-\d{2}-\d{2})[ \t]*$")
_ANY_EXPIRY = re.compile(r"(?m)^expires_on:[^\n]*(?:\n|\Z)")
_MANAGED_SHORT = re.compile(
    r"(?m)^(?:<!--[ \t]*vibe-memory:managed-short[ \t]*-->|"
    r"managed_by:[ \t]*vibe-memory|vibe_memory_managed:[ \t]*true)[ \t]*$"
)
_ENVELOPE_BEGIN = re.compile(
    rb"(?m)^(#{2,3})[ \t]+([^\n]+)\n"
    rb"<!--[ \t]*vibe-memory:short:begin length=(\d+) sha256=([0-9a-f]{64})"
    rb"(?: expires_on=(\d{4}-\d{2}-\d{2}))?[ \t]*-->\n"
)
_ENVELOPE_END = b"\n<!-- vibe-memory:short:end -->\n"
_FIRST_RUN_KEYS = {
    "codex_hooks",
    "claude_hooks",
    "automatic_candidate_checks",
    "personal_short_retention_days",
    "start_at_login",
    "service_port",
    "workspace",
    "formal_memory_requires_approval",
    "service_host",
}


class FirstRunTransactionError(RuntimeError):
    """A first-run failure whose rollback also had reportable problems."""


@contextmanager
def lifecycle_lock(paths: RuntimePaths):
    lock_path = pathlib.Path(paths.install_root) / ".lifecycle.vibe-memory.lock"
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with exclusive_lock(lock_path):
        yield


def write_service_action(
    paths: RuntimePaths, *, desired_start_at_login: bool, status: str
) -> dict[str, object]:
    value = {
        "generation": uuid.uuid4().hex,
        "desired_start_at_login": desired_start_at_login,
        "status": status,
    }
    action_path = pathlib.Path(paths.install_root) / "state" / "service-action.json"
    vibe_memory_install._atomic_write_private_json(action_path, value)
    return value


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
    if isinstance(retention, bool) or retention not in {0, 14, 30}:
        raise ValueError("personal_short_retention_days must be 0, 14, or 30")
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


def _is_within(path: pathlib.Path, parent: pathlib.Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def normalize_first_run_request(
    paths: RuntimePaths,
    request: Mapping[str, object],
    *,
    manager_source_root: pathlib.Path | str,
) -> dict[str, object]:
    if not isinstance(request, Mapping):
        raise ValueError("first-run request must contain an object")
    unknown = set(request).difference(_FIRST_RUN_KEYS)
    if unknown:
        raise ValueError(f"unknown first-run settings: {', '.join(sorted(unknown))}")
    if request.get("formal_memory_requires_approval", True) is not True:
        raise ValueError("formal_memory_requires_approval must remain true")
    if request.get("service_host", "127.0.0.1") != "127.0.0.1":
        raise ValueError("service_host must remain 127.0.0.1")
    current = load_settings(paths)
    aliases = {
        "codex_hooks": "codex_hooks_enabled",
        "claude_hooks": "claude_hooks_enabled",
    }
    for request_key, settings_key in aliases.items():
        if request_key in request:
            value = request[request_key]
            if not isinstance(value, bool):
                raise ValueError(f"{request_key} must be a boolean")
            current[settings_key] = value
    for key in ("automatic_candidate_checks", "start_at_login"):
        if key in request:
            if not isinstance(request[key], bool):
                raise ValueError(f"{key} must be a boolean")
            current[key] = request[key]
    for key in ("personal_short_retention_days", "service_port"):
        if key in request:
            current[key] = request[key]
    current["first_run_complete"] = True
    normalized_settings = validate_settings(current)

    workspace_value = request.get("workspace", "")
    if not isinstance(workspace_value, str):
        raise ValueError("workspace must be a path string")
    workspace: str | None = None
    if workspace_value.strip():
        selected = pathlib.Path(workspace_value).expanduser()
        if selected.is_symlink() or not selected.exists() or not selected.is_dir():
            raise ValueError("workspace must be an existing directory")
        selected = selected.resolve(strict=True)
        forbidden = (
            pathlib.Path(paths.install_root).resolve(),
            pathlib.Path(manager_source_root).resolve(),
        )
        if any(_is_within(selected, root) or _is_within(root, selected) for root in forbidden):
            raise ValueError("workspace must not be the installed runtime or manager source root")
        workspace = str(selected)
    return {"settings": normalized_settings, "workspace": workspace}


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError("short memory write")
        offset += written


def render_managed_short_envelope(
    title: str, content: str, *, expires_on: dt.date | None = None
) -> str:
    safe_title = str(title).replace("\n", " ").strip() or "Approved short memory"
    body = content.strip().encode("utf-8")
    expiry = f" expires_on={expires_on.isoformat()}" if expires_on else ""
    return (
        f"### {safe_title}\n"
        f"<!-- vibe-memory:short:begin length={len(body)} sha256={hashlib.sha256(body).hexdigest()}{expiry} -->\n"
        + body.decode("utf-8")
        + _ENVELOPE_END.decode("ascii")
    )


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
        or retention_days not in {0, 14, 30}
    ):
        raise ValueError("retention_days must be 0, 14, or 30")
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

    # New approvals use a byte-length/digest envelope. Parse these before the
    # legacy format so Markdown headings or marker-looking body text are inert.
    envelope_changed = False
    chunks: list[bytes] = []
    cursor = 0
    search = 0
    marker_tokens = (b"vibe-memory:short:begin", b"vibe-memory:short:end")
    while True:
        match = _ENVELOPE_BEGIN.search(source, search)
        if match is None:
            break
        length = int(match.group(3))
        body_start = match.end()
        body_end = body_start + length
        end_end = body_end + len(_ENVELOPE_END)
        if body_end > len(source) or source[body_end:end_end] != _ENVELOPE_END:
            raise ValueError("malformed managed short envelope")
        body = source[body_start:body_end]
        if hashlib.sha256(body).hexdigest().encode() != match.group(4):
            raise ValueError("malformed managed short envelope digest")
        expiry_raw = match.group(5)
        if expiry_raw is not None:
            try:
                expiry = dt.date.fromisoformat(expiry_raw.decode("ascii"))
            except ValueError as error:
                raise ValueError("malformed managed short expiry") from error
        else:
            expiry = None
        title = match.group(2).decode("utf-8").strip()
        remove = retention_days == 0 or (expiry is not None and expiry < cutoff)
        gap = source[cursor:match.start()]
        if any(token in gap for token in marker_tokens):
            raise ValueError("malformed managed short envelope")
        chunks.append(gap)
        if remove:
            removed.append(title)
            envelope_changed = True
        else:
            record = source[match.start():end_end]
            if expiry is None:
                expected = cutoff + dt.timedelta(days=retention_days)
                header_end = match.end() - 5
                record = source[match.start():header_end] + f" expires_on={expected.isoformat()}".encode() + source[header_end:end_end]
                envelope_changed = True
            chunks.append(record)
        cursor = end_end
        search = end_end
    if cursor:
        if any(token in source[cursor:] for token in marker_tokens):
            raise ValueError("malformed managed short envelope")
        chunks.append(source[cursor:])
        source_after_envelopes = b"".join(chunks)
        if envelope_changed:
            mode = 0o600
            _backup_source(target, source, mode)
            _atomic_write_text(target, source_after_envelopes.decode("utf-8"), mode)
        return removed
    if any(token in source for token in marker_tokens):
        raise ValueError("malformed managed short envelope")

    changed = False
    def retain_or_remove(match: re.Match[str]) -> str:
        nonlocal changed
        section = match.group(0)
        header_end = section.find("\n") + 1
        metadata_prefix = section[header_end:]
        marker = _MANAGED_SHORT.match(metadata_prefix.lstrip("\n"))
        if marker is None:
            return section
        if retention_days == 0:
            changed = True
            removed.append(match.group(1).strip())
            return ""
        expiry_match = _EXPIRY.search(section)
        if expiry_match is None and _ANY_EXPIRY.search(section):
            raise ValueError("malformed managed short expiry")
        expected_expiry = cutoff + dt.timedelta(days=retention_days)
        try:
            expiry = dt.date.fromisoformat(expiry_match.group(1)) if expiry_match else None
        except ValueError:  # Defensive: the strict regex currently prevents this.
            expiry = None
        if expiry is not None and expiry < cutoff:
            changed = True
            removed.append(match.group(1).strip())
            return ""
        if expiry is None:
            replacement = f"expires_on: {expected_expiry.isoformat()}\n"
            absolute_marker_end = header_end + len(metadata_prefix) - len(metadata_prefix.lstrip("\n")) + marker.end()
            if _ANY_EXPIRY.search(section):
                section = _ANY_EXPIRY.sub(replacement, section, count=1)
            else:
                section = section[:absolute_marker_end] + "\n" + replacement + section[absolute_marker_end:].lstrip("\n")
            changed = True
            return section
        return section

    updated = _SECTION.sub(retain_or_remove, text)
    if not changed:
        return []
    mode = 0o600
    _backup_source(target, source, mode)
    _atomic_write_text(target, updated, mode)
    return removed


def _transaction_paths(paths: RuntimePaths) -> list[pathlib.Path]:
    home = _home(paths)
    return [
        _config_path(paths),
        home / ".codex" / "hooks.json",
        home / ".claude" / "settings.json",
        pathlib.Path(paths.project_registry),
        pathlib.Path(paths.launch_agent),
        vibe_memory_install.install_state_path(paths),
        pathlib.Path(paths.install_root) / "state" / "service-action.json",
        pathlib.Path(paths.personal_memory) / "short.md",
    ]


def apply_first_run(
    paths: RuntimePaths,
    request: Mapping[str, object],
    *,
    manager_source_root: pathlib.Path | str,
    register_workspace: Callable[[str], dict[str, object]],
) -> dict[str, object]:
    """Apply first-run choices as one rollback-capable lifecycle transaction."""
    normalized = normalize_first_run_request(
        paths, request, manager_source_root=manager_source_root
    )
    selected = normalized["settings"]
    assert isinstance(selected, dict)
    with lifecycle_lock(paths):
        old_settings = load_settings(paths)
        snapshots = {
            path: vibe_memory_install._snapshot_regular_file(path)
            for path in _transaction_paths(paths)
        }
        written: dict[pathlib.Path, object] = {}
        uncertain_paths: set[pathlib.Path] = set()
        def run_write(operation: Callable[[], object], affected: list[pathlib.Path]) -> object:
            try:
                result = operation()
            except Exception:
                # A callee may atomically write and then raise. We cannot prove
                # ownership of the current bytes, so preserve them on rollback.
                uncertain_paths.update(affected)
                raise
            for affected_path in affected:
                written[affected_path] = vibe_memory_install._snapshot_regular_file(affected_path)
            return result
        try:
            config_path = _config_path(paths)
            saved = run_write(lambda: save_settings(paths, selected), [config_path])
            assert isinstance(saved, dict)
            home = _home(paths)
            run_write(lambda: reconcile_hooks(paths, saved), [home / ".codex/hooks.json", home / ".claude/settings.json"])
            state = vibe_memory_install.read_install_state(paths)
            if state:
                state["port"] = saved["service_port"]
                state["installed_clients"] = [
                    client
                    for client, enabled in (
                        ("codex", saved["codex_hooks_enabled"]),
                        ("claude-code", saved["claude_hooks_enabled"]),
                    )
                    if enabled
                ]
                state_path = vibe_memory_install.install_state_path(paths)
                run_write(lambda: vibe_memory_install.write_install_state(paths, state), [state_path])
            short_path = pathlib.Path(paths.personal_memory) / "short.md"
            run_write(lambda: prune_personal_short(short_path, retention_days=int(saved["personal_short_retention_days"])), [short_path])
            registered = None
            workspace = normalized["workspace"]
            if isinstance(workspace, str):
                registry_path = pathlib.Path(paths.project_registry)
                registered = run_write(lambda: register_workspace(workspace), [registry_path])
            plist_path = pathlib.Path(paths.launch_agent)
            launch = run_write(lambda: reconcile_launch_agent(paths, saved), [plist_path])
            action_path = pathlib.Path(paths.install_root) / "state" / "service-action.json"
            desired_start_at_login = bool(saved["start_at_login"])
            service_action = run_write(
                lambda: write_service_action(
                    paths,
                    desired_start_at_login=desired_start_at_login,
                    status="active" if desired_start_at_login else "bootout_pending",
                ),
                [action_path],
            )
            assert isinstance(service_action, dict)
            if saved["start_at_login"] and launch.get("changed", True) is not False:
                runtime = vibe_memory_install.read_runtime_config(paths)
                version = runtime.get("app_version")
                if not isinstance(version, str):
                    raise ValueError("runtime configuration has no app_version")
                vibe_memory_install.activate_launch_agent(
                    paths, expected_version=version
                )
            return {
                "settings": saved,
                "registered_project": registered,
                "launch_agent": launch,
                "bootout_after_response": not bool(saved["start_at_login"]),
                "service_action_generation": service_action["generation"],
            }
        except Exception as original_error:
            try:
                vibe_memory_install.bootout_launch_agent(paths)
            except vibe_memory_install.InstallError:
                pass
            failed_paths: list[str] = []
            for path, snapshot in snapshots.items():
                if path in uncertain_paths:
                    failed_paths.append(str(path))
                    continue
                if path not in written:
                    # This transaction never wrote the path. Preserve any
                    # concurrent change instead of restoring the initial bytes.
                    continue
                try:
                    vibe_memory_install._restore_regular_file(
                        path, snapshot, expected_current=written.get(path, vibe_memory_install._NO_SNAPSHOT_CHECK)
                    )
                except Exception:
                    failed_paths.append(str(path))
            restart_error: Exception | None = None
            if old_settings["start_at_login"] and snapshots[pathlib.Path(paths.launch_agent)] is not None:
                try:
                    version = vibe_memory_install.read_runtime_config(paths)["app_version"]
                    vibe_memory_install.activate_launch_agent(paths, expected_version=str(version))
                except Exception as error:
                    restart_error = error
            if failed_paths or restart_error is not None:
                details = [f"original={original_error}"]
                if failed_paths:
                    details.append("failed_paths=" + ",".join(failed_paths))
                if restart_error is not None:
                    details.append(f"restart={restart_error}")
                raise FirstRunTransactionError(
                    "first-run rollback incomplete: " + "; ".join(details)
                ) from original_error
            raise


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
