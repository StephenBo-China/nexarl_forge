"""Safely merge Vibe Memory's managed Codex and Claude Code hook entries.

On Darwin, repair uses ``renamex_np`` with ``RENAME_SWAP`` for existing files
and ``RENAME_EXCL`` for new files. The exchanged-out file is verified before
the change is accepted, closing the final pathname race against atomic saves.
Unknown displaced versions are moved to exclusive, fsynced recovery artifacts;
only an inode whose identity and bytes match the manager attempt is disposable.
Recovery swaps converge by inode-and-bytes observation order and are bounded;
an infinite noncooperating writer can make "newest active" impossible, so the
manager stops with every observed version retained and an explicit conflict.
Other POSIX systems retain the weaker userspace inode/content CAS fallback:
it detects and recovers many noncooperating writes, but cannot eliminate every
race without an equivalent kernel exchange primitive.
"""

from __future__ import annotations

import copy
import ctypes
import datetime as dt
import json
import os
import pathlib
import shlex
import stat
import sys
import tempfile
from typing import Any, NoReturn

from ui_design_store import exclusive_lock
from vibe_memory_events import EVENTS


MANAGED_SIGNATURE = "vibe-memory hook --agent"
AGENTS = ("codex", "claude-code")
_NO_SOURCE_CHECK = object()
RENAME_SWAP = 0x00000002
RENAME_EXCL = 0x00000004
DARWIN_RECOVERY_SWAP_LIMIT = 8


class ConcurrentConfigChange(RuntimeError):
    """Raised when a config changes after repair loaded it."""

    def __init__(
        self,
        message: str,
        *,
        backup: pathlib.Path | None = None,
        attempt: pathlib.Path | None = None,
        recovery_paths: list[pathlib.Path] | None = None,
    ) -> None:
        super().__init__(message)
        self.backup = str(backup) if backup is not None else None
        self.attempt = str(attempt) if attempt is not None else None
        self.recovery_paths = [str(path) for path in recovery_paths or []]


class ContinuousConfigChange(ConcurrentConfigChange):
    """Raised when noncooperating writers prevent bounded swap convergence."""


class ConfigWriteError(OSError):
    """Raised after replacement when durability confirmation fails."""

    def __init__(
        self,
        message: str,
        backup: pathlib.Path | None,
        *,
        commit_snapshot: tuple[tuple[int, int], bytes, int] | None = None,
    ) -> None:
        super().__init__(message)
        self.backup = str(backup) if backup is not None else None
        self._commit_snapshot = commit_snapshot


class RecoveryArtifactError(OSError):
    """Raised when a recovery path exists but its durability sync failed."""

    def __init__(self, path: pathlib.Path, cause: BaseException) -> None:
        super().__init__(f"recovery artifact sync failed: {path}: {cause}")
        self.path = path


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
    launcher = pathlib.Path(os.path.abspath(os.path.expanduser(os.fspath(runtime))))
    escaped = (
        str(launcher)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )
    rendered = f'"{escaped}" hook --agent {agent} --event {event}'
    # New manager call sites pass the exact per-user stable launcher. Keep an
    # ownership marker for older direct callers that still supply an arbitrary
    # executable-like path, so cleanup never guesses at a custom command.
    if launcher.parts[-3:] != (".local", "bin", "vibe-memory"):
        return f"{rendered} # {MANAGED_SIGNATURE}"
    return rendered


def _require_document(value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(value, dict):
        raise ValueError("hook configuration root must be an object")
    hooks = value.get("hooks")
    if not isinstance(hooks, dict):
        raise ValueError("hook configuration hooks must be an object")
    return value, hooks


def _launcher_path(runtime: str | pathlib.Path | None = None) -> pathlib.Path:
    value = (
        pathlib.Path.home() / ".local" / "bin" / "vibe-memory"
        if runtime is None
        else pathlib.Path(os.path.abspath(os.path.expanduser(os.fspath(runtime))))
    )
    return pathlib.Path(os.path.abspath(os.fspath(value)))


def _is_managed_handler(
    handler: Any,
    runtime: str | pathlib.Path | None = None,
) -> bool:
    if (
        not isinstance(handler, dict)
        or handler.get("type") != "command"
        or not isinstance(handler.get("command"), str)
    ):
        return False
    try:
        tokens = shlex.split(handler["command"], comments=False, posix=True)
    except ValueError:
        return False
    if len(tokens) == 6:
        launcher, action, agent_flag, agent, event_flag, event = tokens
        launcher_path = pathlib.PurePath(launcher)
        return (
            launcher_path.is_absolute()
            and launcher_path == pathlib.PurePath(_launcher_path(runtime))
            and action == "hook"
            and agent_flag == "--agent"
            and agent in AGENTS
            and event_flag == "--event"
            and event in EVENTS
        )
    marker = f" # {MANAGED_SIGNATURE}"
    if not handler["command"].endswith(marker):
        return False
    command_text = handler["command"][:-len(marker)]
    try:
        tokens = shlex.split(command_text, comments=False, posix=True)
    except ValueError:
        return False
    if len(tokens) == 6:
        executable, action, agent_flag, agent, event_flag, event = tokens
        return (
            pathlib.PurePath(executable).is_absolute()
            and action == "hook"
            and agent_flag == "--agent"
            and agent in AGENTS
            and event_flag == "--event"
            and event in EVENTS
        )
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


def remove_managed_entries(
    value: Any,
    runtime: str | pathlib.Path | None = None,
) -> dict[str, Any]:
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
                handler for handler in group["hooks"] if _is_managed_handler(handler, runtime)
            ]
            if not managed_handlers:
                retained_groups.append(group)
                continue
            retained_handlers = [
                handler for handler in group["hooks"] if not _is_managed_handler(handler, runtime)
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
    copied = remove_managed_entries(copied, runtime)
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
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError(
            f"Invalid UTF-8 hook configuration {target}: byte-order marks are not allowed"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Invalid UTF-8 hook configuration {target}: {exc}") from exc
    try:
        value = json.loads(text, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError) as exc:
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
    return _artifact_path(path, "bak", suffix)


def _artifact_path(path: pathlib.Path, label: str, suffix: int = 0) -> pathlib.Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    extra = f"-{suffix}" if suffix else ""
    return path.with_name(f"{path.name}.{label}.{stamp}{extra}")


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


def _darwin_rename(
    source: str | pathlib.Path,
    destination: str | pathlib.Path,
    flags: int,
) -> None:
    """Invoke Darwin's flagged atomic rename and raise the corresponding errno."""
    if sys.platform != "darwin":
        raise NotImplementedError("flagged atomic rename is only supported on Darwin")
    libc = ctypes.CDLL(None, use_errno=True)
    renamex_np = libc.renamex_np
    renamex_np.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    renamex_np.restype = ctypes.c_int
    result = renamex_np(os.fsencode(source), os.fsencode(destination), flags)
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            str(source),
            str(destination),
        )


def _promote_recovery_path(
    target: pathlib.Path,
    source: pathlib.Path,
    label: str = "conflict",
) -> pathlib.Path:
    """Atomically give a held version an exclusive durable artifact name."""
    suffix = 0
    while True:
        artifact = _artifact_path(target, label, suffix)
        try:
            _darwin_rename(source, artifact, RENAME_EXCL)
        except FileExistsError:
            suffix += 1
            continue
        break
    try:
        descriptor = os.open(artifact, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(target.parent)
    except Exception as exc:
        raise RecoveryArtifactError(artifact, exc) from exc
    return artifact


def _create_artifact_exclusive(
    target: pathlib.Path, content: bytes, mode: int, label: str
) -> pathlib.Path:
    suffix = 0
    while True:
        artifact = _artifact_path(target, label, suffix)
        try:
            descriptor = os.open(
                artifact, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode
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
            artifact.unlink(missing_ok=True)
            raise
        else:
            os.close(descriptor)
        try:
            _fsync_directory(target.parent)
        except Exception:
            artifact.unlink(missing_ok=True)
            raise
        return artifact


def _create_backup_exclusive(
    target: pathlib.Path, content: bytes, mode: int
) -> pathlib.Path:
    return _create_artifact_exclusive(target, content, mode, "bak")


def _create_attempt_exclusive(
    target: pathlib.Path, content: bytes, mode: int
) -> pathlib.Path:
    return _create_artifact_exclusive(target, content, mode, "attempt")


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


def _open_source_descriptor(target: pathlib.Path) -> int | None:
    _reject_symlink(target)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(target, flags)
    except FileNotFoundError:
        return None


def _descriptor_bytes(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 64 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _path_matches(
    target: pathlib.Path, identity: tuple[int, int], content: bytes
) -> bool:
    _reject_symlink(target)
    try:
        before = target.stat()
        current = target.read_bytes()
        after = target.stat()
    except FileNotFoundError:
        return False
    return _identity(before) == identity == _identity(after) and current == content


def _snapshot_path(
    target: pathlib.Path,
) -> tuple[tuple[int, int], bytes, int] | None:
    descriptor = _open_source_descriptor(target)
    if descriptor is None:
        return None
    try:
        metadata = os.fstat(descriptor)
        identity = _identity(metadata)
        content = _descriptor_bytes(descriptor)
        mode = stat.S_IMODE(metadata.st_mode)
        if not _path_matches(target, identity, content):
            return None
        return identity, content, mode
    finally:
        os.close(descriptor)


def _preserve_manager_attempt(
    target: pathlib.Path,
    temporary: pathlib.Path,
    manager_identity: tuple[int, int] | None,
    content: bytes,
    mode: int,
) -> pathlib.Path | None:
    if (
        manager_identity is not None
        and temporary.exists()
        and _path_matches(temporary, manager_identity, content)
    ):
        try:
            return _promote_recovery_path(target, temporary, "attempt")
        except RecoveryArtifactError as exc:
            return exc.path
        except Exception:
            return temporary
    try:
        return _create_attempt_exclusive(target, content, mode)
    except Exception:
        return None


def _promote_unknown_or_report(
    target: pathlib.Path,
    temporary: pathlib.Path,
) -> pathlib.Path:
    try:
        return _promote_recovery_path(target, temporary, "conflict")
    except RecoveryArtifactError as exc:
        return exc.path
    except Exception:
        return temporary


def _recover_darwin_source_mismatch(
    target: pathlib.Path,
    temporary: pathlib.Path,
    manager_identity: tuple[int, int] | None,
    content: bytes,
    manager_mode: int,
) -> NoReturn:
    """Converge observed external versions without discarding any held version."""
    seen: set[tuple[tuple[int, int], bytes]] = set()
    recovery_paths: list[pathlib.Path] = []
    first_artifact: pathlib.Path | None = None

    for _ in range(DARWIN_RECOVERY_SWAP_LIMIT):
        observed = _snapshot_path(temporary)
        if observed is None:
            backup = _promote_unknown_or_report(target, temporary)
            recovery_paths.append(backup)
            attempt = _preserve_manager_attempt(
                target, temporary, manager_identity, content, manager_mode
            )
            raise ConcurrentConfigChange(
                f"hook configuration changed concurrently; recovery versions retained: "
                f"{target}",
                backup=first_artifact or backup,
                attempt=attempt,
                recovery_paths=recovery_paths,
            )

        identity, observed_content, observed_mode = observed
        key = identity, observed_content
        if key in seen:
            displaced = _promote_unknown_or_report(target, temporary)
            recovery_paths.append(displaced)
            attempt = _preserve_manager_attempt(
                target, temporary, manager_identity, content, manager_mode
            )
            if attempt is not None:
                recovery_paths.append(attempt)
            raise ConcurrentConfigChange(
                f"hook configuration changed concurrently; recovery swaps converged: "
                f"{target}",
                backup=first_artifact or displaced,
                attempt=attempt,
                recovery_paths=recovery_paths,
            )

        seen.add(key)
        try:
            artifact = _create_artifact_exclusive(
                target, observed_content, observed_mode, "conflict"
            )
        except Exception:
            attempt = _preserve_manager_attempt(
                target, temporary, manager_identity, content, manager_mode
            )
            raise ConcurrentConfigChange(
                f"hook configuration changed concurrently; recovery version retained: "
                f"{target}",
                backup=temporary,
                attempt=attempt,
                recovery_paths=[*recovery_paths, temporary],
            )
        recovery_paths.append(artifact)
        if first_artifact is None:
            first_artifact = artifact

        _darwin_rename(temporary, target, RENAME_SWAP)
        if manager_identity is not None and _path_matches(
            temporary, manager_identity, content
        ):
            attempt = _preserve_manager_attempt(
                target, temporary, manager_identity, content, manager_mode
            )
            if attempt is not None:
                recovery_paths.append(attempt)
            raise ConcurrentConfigChange(
                f"hook configuration changed concurrently; recovery swaps converged: "
                f"{target}",
                backup=first_artifact,
                attempt=attempt,
                recovery_paths=recovery_paths,
            )

    latest = _promote_unknown_or_report(target, temporary)
    recovery_paths.append(latest)
    attempt = _preserve_manager_attempt(
        target, temporary, manager_identity, content, manager_mode
    )
    if attempt is not None:
        recovery_paths.append(attempt)
    raise ContinuousConfigChange(
        f"hook configuration changed concurrently; continuous concurrent writes prevented "
        f"recovery convergence after {DARWIN_RECOVERY_SWAP_LIMIT} swaps: {target}",
        backup=first_artifact or latest,
        attempt=attempt,
        recovery_paths=recovery_paths,
    )


def _restore_external_bytes(
    target: pathlib.Path,
    external_content: bytes,
    mode: int,
    manager_identity: tuple[int, int],
    manager_content: bytes,
) -> bool:
    if not _path_matches(target, manager_identity, manager_content):
        return False
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.restore.", dir=target.parent
    )
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(external_content)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        if not _path_matches(target, manager_identity, manager_content):
            return False
        os.replace(temporary, target)
        _fsync_directory(target.parent)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _raise_preserved_conflict(
    target: pathlib.Path,
    temporary: pathlib.Path,
    content: bytes,
    mode: int,
    backup: pathlib.Path | None,
    outcome: str,
    *,
    temporary_is_attempt: bool = True,
) -> NoReturn:
    """Persist the manager attempt only after the external active path is safe."""
    try:
        attempt = _create_attempt_exclusive(target, content, mode)
    except Exception as exc:
        raise ConcurrentConfigChange(
            f"hook configuration changed concurrently; {outcome}: {target}",
            backup=backup,
            attempt=temporary if temporary_is_attempt else None,
        ) from exc
    raise ConcurrentConfigChange(
        f"hook configuration changed concurrently; {outcome}: {target}",
        backup=backup,
        attempt=attempt,
    )


def write_with_backup(
    path: str | pathlib.Path,
    value: Any,
    *,
    expected_source: bytes | None | object = _NO_SOURCE_CHECK,
    _source_descriptor: int | None = None,
    _include_commit_snapshot: bool = False,
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

    held_descriptor = _source_descriptor
    owns_descriptor = False
    if (
        held_descriptor is None
        and expected_source is not _NO_SOURCE_CHECK
        and expected_source is not None
    ):
        held_descriptor = _open_source_descriptor(target)
        owns_descriptor = True
    if held_descriptor is not None and expected_source is not _NO_SOURCE_CHECK:
        if _descriptor_bytes(held_descriptor) != expected_source:
            if owns_descriptor:
                os.close(held_descriptor)
            raise ConcurrentConfigChange(
                f"hook configuration changed concurrently: {target}"
            )
        held_metadata = os.fstat(held_descriptor)
        held_identity = _identity(held_metadata)
        held_mode = stat.S_IMODE(held_metadata.st_mode)
    else:
        held_identity = None
        held_mode = existing_mode

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = pathlib.Path(temporary_name)
    backup: pathlib.Path | None = None
    attempt: pathlib.Path | None = None
    replaced = False
    manager_identity: tuple[int, int] | None = None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fchmod(handle.fileno(), existing_mode)
            os.fsync(handle.fileno())
            manager_identity = _identity(os.fstat(handle.fileno()))
        current_source = _source_bytes(target)
        if expected_source is not _NO_SOURCE_CHECK:
            source_matches = current_source == expected_source
            if source_matches and held_identity is not None:
                source_matches = _path_matches(target, held_identity, expected_source)
            if not source_matches:
                raise ConcurrentConfigChange(
                    f"hook configuration changed concurrently: {target}"
                )
        if current_source == content:
            temporary.unlink(missing_ok=True)
            return {"changed": False, "path": str(target), "backup": None}
        if sys.platform == "darwin" and expected_source is not _NO_SOURCE_CHECK:
            if expected_source is None:
                try:
                    _darwin_rename(temporary, target, RENAME_EXCL)
                except FileExistsError:
                    _raise_preserved_conflict(
                        target,
                        temporary,
                        content,
                        existing_mode,
                        None,
                        "newer active path preserved",
                    )
                replaced = True
                _fsync_directory(target.parent)
                result = {
                    "changed": True,
                    "path": str(target),
                    "backup": None,
                }
                if _include_commit_snapshot:
                    result["_commit_snapshot"] = (manager_identity, content, existing_mode)
                return result

            _darwin_rename(temporary, target, RENAME_SWAP)
            replaced = True

            def exchanged_source_matches() -> bool:
                try:
                    if _source_bytes(temporary) != expected_source:
                        return False
                    if held_identity is None:
                        return True
                    return _path_matches(temporary, held_identity, expected_source)
                except (OSError, ConfigSymlinkError):
                    return False

            if not exchanged_source_matches():
                _recover_darwin_source_mismatch(
                    target,
                    temporary,
                    manager_identity,
                    content,
                    existing_mode,
                )
            if manager_identity is None or not _path_matches(
                target, manager_identity, content
            ):
                replaced = False
                backup = _promote_unknown_or_report(target, temporary)
                attempt = _preserve_manager_attempt(
                    target, temporary, manager_identity, content, existing_mode
                )
                raise ConcurrentConfigChange(
                    f"hook configuration changed concurrently; newer active path preserved: "
                    f"{target}",
                    backup=backup,
                    attempt=attempt,
                    recovery_paths=[backup],
                )

            try:
                backup = _promote_recovery_path(target, temporary, "bak")
            except Exception:
                if temporary.exists() and manager_identity is not None and _path_matches(
                    target, manager_identity, content
                ):
                    _darwin_rename(temporary, target, RENAME_SWAP)
                replaced = False
                raise
            if held_descriptor is not None and (
                _descriptor_bytes(held_descriptor) != expected_source
                or held_identity is None
                or not _path_matches(backup, held_identity, expected_source)
            ):
                _recover_darwin_source_mismatch(
                    target,
                    backup,
                    manager_identity,
                    content,
                    existing_mode,
                )
            if manager_identity is None or not _path_matches(
                target, manager_identity, content
            ):
                replaced = False
                attempt = _preserve_manager_attempt(
                    target, temporary, manager_identity, content, existing_mode
                )
                raise ConcurrentConfigChange(
                    f"hook configuration changed concurrently; newer active path preserved: "
                    f"{target}",
                    backup=backup,
                    attempt=attempt,
                    recovery_paths=[backup],
                )
            result = {
                "changed": True,
                "path": str(target),
                "backup": str(backup),
            }
            if _include_commit_snapshot:
                result["_commit_snapshot"] = (manager_identity, content, existing_mode)
            return result
        if current_source is not None:
            current_mode = stat.S_IMODE(target.stat().st_mode)
            backup = _create_backup_exclusive(target, current_source, current_mode)
        if expected_source is not _NO_SOURCE_CHECK:
            source_matches = _source_bytes(target) == expected_source
            if source_matches and held_identity is not None:
                source_matches = _path_matches(target, held_identity, expected_source)
            if not source_matches:
                raise ConcurrentConfigChange(
                    f"hook configuration changed concurrently: {target}"
                )
        os.replace(temporary, target)
        replaced = True
        if held_descriptor is not None and expected_source is not _NO_SOURCE_CHECK:
            external_content = _descriptor_bytes(held_descriptor)
            if external_content != expected_source:
                attempt = _create_attempt_exclusive(target, content, existing_mode)
                restored = (
                    manager_identity is not None
                    and _restore_external_bytes(
                        target,
                        external_content,
                        held_mode,
                        manager_identity,
                        content,
                    )
                )
                outcome = "external bytes restored" if restored else "newer active path preserved"
                raise ConcurrentConfigChange(
                    f"hook configuration changed concurrently; {outcome}: {target}",
                    backup=backup,
                    attempt=attempt,
                )
        _fsync_directory(target.parent)
    except ConcurrentConfigChange as conflict:
        if sys.platform == "darwin" and expected_source is not _NO_SOURCE_CHECK:
            if temporary.exists():
                if manager_identity is not None and _path_matches(
                    temporary, manager_identity, content
                ):
                    preserved = _preserve_manager_attempt(
                        target,
                        temporary,
                        manager_identity,
                        content,
                        existing_mode,
                    )
                    if conflict.attempt is None and preserved is not None:
                        conflict.attempt = str(preserved)
                else:
                    preserved = _promote_unknown_or_report(target, temporary)
                    if conflict.backup in (None, str(temporary)):
                        conflict.backup = str(preserved)
                    conflict.recovery_paths = [
                        str(preserved) if path == str(temporary) else path
                        for path in conflict.recovery_paths
                    ]
                    if str(preserved) not in conflict.recovery_paths:
                        conflict.recovery_paths.append(str(preserved))
            raise
        if conflict.attempt != str(temporary):
            temporary.unlink(missing_ok=True)
        if not replaced and backup is not None and backup.exists():
            backup.unlink(missing_ok=True)
            try:
                _fsync_directory(target.parent)
            except OSError:
                pass
        raise
    except Exception as exc:
        if sys.platform == "darwin" and expected_source is not _NO_SOURCE_CHECK:
            if temporary.exists():
                if manager_identity is not None and _path_matches(
                    temporary, manager_identity, content
                ):
                    attempt = _preserve_manager_attempt(
                        target,
                        temporary,
                        manager_identity,
                        content,
                        existing_mode,
                    )
                else:
                    backup = _promote_unknown_or_report(target, temporary)
            if replaced:
                backup_description = str(backup) if backup is not None else "unavailable"
                raise ConfigWriteError(
                    f"hook configuration was replaced but durability sync failed; backup: "
                    f"{backup_description}",
                    backup,
                    commit_snapshot=(manager_identity, content, existing_mode)
                    if manager_identity is not None
                    and _path_matches(target, manager_identity, content)
                    else None,
                ) from exc
            raise
        temporary.unlink(missing_ok=True)
        if replaced:
            backup_description = str(backup) if backup is not None else "unavailable"
            raise ConfigWriteError(
                f"hook configuration was replaced but durability sync failed; backup: "
                f"{backup_description}",
                backup,
                commit_snapshot=(manager_identity, content, existing_mode)
                if manager_identity is not None
                and _path_matches(target, manager_identity, content)
                else None,
            ) from exc
        if backup is not None and backup.exists():
            backup.unlink(missing_ok=True)
            try:
                _fsync_directory(target.parent)
            except OSError:
                pass
        raise
    except BaseException as interruption:
        cleanup_errors: list[str] = []
        try:
            if sys.platform == "darwin" and expected_source is not _NO_SOURCE_CHECK:
                if temporary.exists():
                    if manager_identity is not None and _path_matches(
                        temporary, manager_identity, content
                    ):
                        if replaced:
                            _preserve_manager_attempt(
                                target,
                                temporary,
                                manager_identity,
                                content,
                                existing_mode,
                            )
                        else:
                            temporary.unlink(missing_ok=True)
                    else:
                        promoted = _promote_unknown_or_report(target, temporary)
                        if backup is None:
                            backup = promoted
            else:
                temporary.unlink(missing_ok=True)
                if not replaced and backup is not None and backup.exists():
                    backup.unlink(missing_ok=True)
                    _fsync_directory(target.parent)
        except BaseException as cleanup_error:
            cleanup_errors.append(type(cleanup_error).__name__)
        if (
            _include_commit_snapshot
            and manager_identity is not None
            and _path_matches(target, manager_identity, content)
        ):
            interruption._commit_snapshot = (
                manager_identity,
                content,
                existing_mode,
            )
        if cleanup_errors:
            interruption._cleanup_errors = cleanup_errors
        raise
    finally:
        if owns_descriptor and held_descriptor is not None:
            os.close(held_descriptor)
    result = {
        "changed": True,
        "path": str(target),
        "backup": str(backup) if backup is not None else None,
    }
    if _include_commit_snapshot:
        result["_commit_snapshot"] = (manager_identity, content, existing_mode)
    return result


def status(path: str | pathlib.Path, agent: str, runtime: str | pathlib.Path) -> dict[str, Any]:
    """Report whether a hook document is missing, current, drifted, or malformed."""
    _validate_agent_event(agent, EVENTS[0])
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


def preview(path: str | pathlib.Path, agent: str, runtime: str | pathlib.Path) -> dict[str, Any]:
    """Validate and render a hook repair without writing any filesystem state."""
    _validate_agent_event(agent, EVENTS[0])
    target = pathlib.Path(path)
    _reject_symlink(target)
    existed = target.exists()
    current = load_document(target) if existed else {"hooks": {}}
    expected = merge_document(current, agent, runtime)
    return {
        "status": "current" if current == expected else ("drifted" if existed else "missing"),
        "path": str(target),
        "document": expected,
    }


def repair(
    path: str | pathlib.Path,
    agent: str,
    runtime: str | pathlib.Path,
    *,
    _include_commit_snapshot: bool = False,
) -> dict[str, Any]:
    """Create or repair one document and describe its resulting backup, if any."""
    target = pathlib.Path(path)
    _reject_symlink(target)
    _ensure_parent(target)
    lock_path = target.with_name(f".{target.name}.vibe-memory.lock")
    with exclusive_lock(lock_path):
        _reject_symlink(target)
        source_descriptor = _open_source_descriptor(target)
        try:
            source = (
                _descriptor_bytes(source_descriptor)
                if source_descriptor is not None
                else None
            )
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
            result = write_with_backup(
                target,
                expected,
                expected_source=source,
                _source_descriptor=source_descriptor,
                _include_commit_snapshot=_include_commit_snapshot,
            )
            result["status"] = "created" if result["changed"] and not existed else (
                "updated" if result["changed"] else "current"
            )
            return result
        finally:
            if source_descriptor is not None:
                os.close(source_descriptor)


def uninstall(
    path: str | pathlib.Path,
    runtime: str | pathlib.Path | None = None,
    *,
    _include_commit_snapshot: bool = False,
) -> dict[str, Any]:
    """Remove Vibe Memory managed hook entries while preserving custom handlers."""
    target = pathlib.Path(path)
    _reject_symlink(target)
    if not target.exists():
        return {
            "changed": False,
            "path": str(target),
            "backup": None,
            "status": "missing",
        }
    lock_path = target.with_name(f".{target.name}.vibe-memory.lock")
    with exclusive_lock(lock_path):
        _reject_symlink(target)
        source_descriptor = _open_source_descriptor(target)
        if source_descriptor is None:
            return {
                "changed": False,
                "path": str(target),
                "backup": None,
                "status": "missing",
            }
        try:
            source = _descriptor_bytes(source_descriptor)
            current = _parse_document_bytes(target, source)
            updated = remove_managed_entries(current, runtime)
            if current == updated:
                return {
                    "changed": False,
                    "path": str(target),
                    "backup": None,
                    "status": "current",
                }
            result = write_with_backup(
                target,
                updated,
                expected_source=source,
                _source_descriptor=source_descriptor,
                _include_commit_snapshot=_include_commit_snapshot,
            )
            result["status"] = "updated" if result["changed"] else "current"
            return result
        finally:
            os.close(source_descriptor)
