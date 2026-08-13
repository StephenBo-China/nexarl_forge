"""Read-only inventory for the current control-plane migration surface."""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import stat
import tempfile
import datetime as _dt
import contextlib
import fcntl
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import memory_project
from memory_review_queue import count_items
import ui_design_gate
import ui_design_store
import ui_design_preferences
import vibe_memory_settings
from vibe_memory_paths import RuntimePaths


_MARKDOWN_SECTION_RE = re.compile(r"(?m)^#{2,6}\s+")
_LEGACY_MEMORY_HOOK_RE = re.compile(
    r"(^|[/\\\"'\s])\.(?:codex|claude)[/\\]hooks[/\\]"
    r"shared_memory_hook\.py($|[\"'\s])"
)
_LEGACY_HOOK_DOCUMENTS = (
    (
        pathlib.Path(".codex/hooks.json"),
        pathlib.Path(".codex/hooks/shared_memory_hook.py"),
    ),
    (
        pathlib.Path(".claude/settings.json"),
        pathlib.Path(".claude/hooks/shared_memory_hook.py"),
    ),
)


@dataclass(frozen=True)
class ProjectHandle:
    root_path: pathlib.Path
    root_fd: int
    identity: tuple[int, int]
    lock_fd: int


def _assert_project_handle(handle: ProjectHandle) -> None:
    current = handle.root_path.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != handle.identity
    ):
        raise ValueError(f"project root binding changed: {handle.root_path}")


def _issue(path: pathlib.Path, error: Exception) -> dict[str, str]:
    return {"path": str(path), "error": str(error)}


def _read_text(path: pathlib.Path) -> tuple[str | None, dict[str, str] | None]:
    try:
        return path.read_text(encoding="utf-8"), None
    except FileNotFoundError:
        return None, None
    except (OSError, UnicodeDecodeError) as error:
        return None, _issue(path, error)


def _read_json(path: pathlib.Path) -> tuple[Any | None, dict[str, str] | None]:
    text, text_issue = _read_text(path)
    if text_issue is not None or text is None:
        return None, text_issue
    try:
        return json.loads(text), None
    except json.JSONDecodeError as error:
        return None, _issue(path, error)


def _markdown_summary(path: pathlib.Path) -> dict[str, Any]:
    text, issue = _read_text(path)
    if issue is not None:
        return {"path": str(path), "status": "error", "sections": 0, "error": issue["error"]}
    if text is None:
        return {"path": str(path), "status": "missing", "sections": 0}
    return {
        "path": str(path),
        "status": "ok",
        "sections": len(_MARKDOWN_SECTION_RE.findall(text)),
    }


def _json_summary(path: pathlib.Path) -> dict[str, Any]:
    value, issue = _read_json(path)
    if issue is not None:
        return {"path": str(path), "status": "error", "error": issue["error"]}
    if value is None:
        return {"path": str(path), "status": "missing"}
    if isinstance(value, dict):
        return {"path": str(path), "status": "ok", "schema_version": value.get("schema_version")}
    return {"path": str(path), "status": "invalid", "error": "JSON root must be an object"}


def _is_legacy_memory_command(command: object) -> bool:
    return isinstance(command, str) and _LEGACY_MEMORY_HOOK_RE.search(command) is not None


def _canonical_hook_command(command: str) -> str:
    value = command.replace("\\", "/")
    for agent_directory in (".codex/hooks/", ".claude/hooks/"):
        value = value.replace(agent_directory, ".client/hooks/")
    return value


def _hook_entry_counts(
    document: Mapping[str, object], *, owned_script: bool = True
) -> tuple[set[str], set[str]]:
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        raise ValueError("hook configuration hooks must be an object")
    managed: set[str] = set()
    custom: set[str] = set()
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise ValueError(f"hook configuration hooks.{event} must be an array")
        for group in groups:
            if not isinstance(group, dict):
                continue
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                continue
            for handler in handlers:
                if not isinstance(handler, dict):
                    continue
                if handler.get("type") != "command":
                    continue
                command = handler.get("command")
                if not isinstance(command, str):
                    continue
                signature = f"{event}:{_canonical_hook_command(command)}"
                if _is_legacy_memory_command(command) and owned_script:
                    managed.add(signature)
                else:
                    custom.add(signature)
    return managed, custom


def _remove_legacy_memory_entries(
    document: Mapping[str, object], *, owned_script: bool = True
) -> tuple[dict[str, Any], int]:
    copied = json.loads(json.dumps(document, ensure_ascii=False))
    hooks = copied.get("hooks")
    if not isinstance(hooks, dict):
        raise ValueError("hook configuration hooks must be an object")
    removed = 0
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            raise ValueError(f"hook configuration hooks.{event} must be an array")
        retained_groups: list[Any] = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                retained_groups.append(group)
                continue
            retained_handlers = []
            for handler in group["hooks"]:
                if (
                    isinstance(handler, dict)
                    and handler.get("type") == "command"
                    and owned_script
                    and _is_legacy_memory_command(handler.get("command"))
                ):
                    removed += 1
                    continue
                retained_handlers.append(handler)
            if retained_handlers:
                updated_group = dict(group)
                updated_group["hooks"] = retained_handlers
                retained_groups.append(updated_group)
        if retained_groups:
            hooks[event] = retained_groups
        else:
            del hooks[event]
    return copied, removed


def _backup_path(path: pathlib.Path, stamp: str) -> pathlib.Path:
    candidate = path.with_name(f"{path.name}.bak.{stamp}")
    if not candidate.exists():
        return candidate
    index = 1
    while True:
        alternate = path.with_name(f"{path.name}.bak.{stamp}.{index}")
        if not alternate.exists():
            return alternate
        index += 1


def _backup_file(path: pathlib.Path, stamp: str) -> pathlib.Path:
    if path.is_symlink():
        raise ValueError(f"legacy migration refuses to backup symlink: {path}")
    backup = _backup_path(path, stamp)
    shutil.copy2(path, backup)
    return backup


def _atomic_write_json(path: pathlib.Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    mode = 0o600
    if path.exists() and not path.is_symlink():
        mode = path.stat().st_mode & 0o777
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _read_regular_at(directory_fd: int, name: str) -> tuple[bytes, int]:
    descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"migration target is not a regular file: {name}")
        chunks = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), info.st_mode & 0o777
    finally:
        os.close(descriptor)


def _write_bytes_at(directory_fd: int, name: str, content: bytes, mode: int) -> None:
    temporary = f".{name}.tmp-{os.getpid()}-{_dt.datetime.now().timestamp():.6f}"
    descriptor = os.open(
        temporary,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
        mode,
        dir_fd=directory_fd,
    )
    try:
        offset = 0
        while offset < len(content):
            offset += os.write(descriptor, content[offset:])
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _write_json_at(directory_fd: int, name: str, value: Mapping[str, object], mode: int) -> None:
    content = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _write_bytes_at(directory_fd, name, content, mode)


def _read_registry(paths: RuntimePaths) -> Mapping[str, object]:
    value, issue = _read_json(paths.project_registry)
    if issue is not None:
        raise ValueError(
            f"project registry is invalid: {issue['path']}: {issue['error']}"
        )
    if not isinstance(value, dict):
        raise ValueError("project registry is missing or invalid")
    return value


def _selected_registered_roots(
    project_roots: list[pathlib.Path], paths: RuntimePaths
) -> list[pathlib.Path]:
    if not project_roots:
        raise ValueError("at least one explicit project root is required")
    registered = {str(root) for root in valid_project_roots(_read_registry(paths))}
    selected: list[pathlib.Path] = []
    seen: set[str] = set()
    for raw in project_roots:
        requested = pathlib.Path(raw).expanduser()
        if requested.is_symlink():
            raise ValueError(f"legacy migration refuses symlink root: {requested}")
        resolved = requested.resolve()
        key = str(resolved)
        if key not in registered:
            raise ValueError(f"legacy migration requires a registered project root: {resolved}")
        if not resolved.is_dir():
            raise ValueError(f"registered project root is not a directory: {resolved}")
        if key not in seen:
            selected.append(resolved)
            seen.add(key)
    return selected


@contextlib.contextmanager
def _project_lock(root: pathlib.Path):
    descriptor = os.open(
        root,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        info = os.fstat(descriptor)
        handle = ProjectHandle(
            root_path=root,
            root_fd=descriptor,
            identity=(info.st_dev, info.st_ino),
            lock_fd=descriptor,
        )
        _assert_project_handle(handle)
        yield handle
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextlib.contextmanager
def _open_project_dirs(
    root: pathlib.Path,
    layout: Mapping[pathlib.Path, tuple[int, int]] | None = None,
    handle: ProjectHandle | None = None,
):
    """Pin the project and every known migration directory without symlinks."""
    if handle is not None:
        _assert_project_handle(handle)
        root_fd = os.dup(handle.root_fd)
    else:
        root_fd = os.open(
            root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    opened = [root_fd]
    directories: dict[str, int] = {"": root_fd}
    try:
        if layout is not None and (root / "") in layout:
            root_info = os.fstat(root_fd)
            if (root_info.st_dev, root_info.st_ino) != layout[root / ""]:
                raise ValueError(f"project directory binding changed: {root}")
        for relative in (
            ".codex", ".codex/hooks", ".claude", ".claude/hooks",
            "codex", "codex/migration_audits",
        ):
            parent_name, _, name = relative.rpartition("/")
            parent_fd = directories[parent_name]
            if handle is not None:
                _assert_project_handle(handle)
            if parent_fd < 0:
                directories[relative] = -1
                continue
            try:
                descriptor = os.open(
                    name,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                descriptor = -1
            directories[relative] = descriptor
            if descriptor >= 0:
                path = root / relative
                if layout is not None and path in layout:
                    info = os.fstat(descriptor)
                    if (info.st_dev, info.st_ino) != layout[path]:
                        raise ValueError(f"project directory binding changed: {path}")
                opened.append(descriptor)
        yield directories
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


def _validate_bound_directories(root: pathlib.Path) -> None:
    with _open_project_dirs(root):
        pass


def _layout_identities(
    root: pathlib.Path, handle: ProjectHandle | None = None
) -> dict[pathlib.Path, tuple[int, int]]:
    identities: dict[pathlib.Path, tuple[int, int]] = {}
    with _open_project_dirs(root, handle=handle) as directories:
        for relative, descriptor in directories.items():
            if descriptor < 0:
                continue
            info = os.fstat(descriptor)
            identities[root / relative] = (info.st_dev, info.st_ino)
    return identities


def _assert_layout_identities(identities: Mapping[pathlib.Path, tuple[int, int]]) -> None:
    for path, expected in identities.items():
        current = path.stat(follow_symlinks=False)
        if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != expected:
            raise ValueError(f"project directory binding changed: {path}")


def _file_fingerprint(path: pathlib.Path) -> tuple[int, int, int, str] | None:
    try:
        value = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(value.st_mode) or path.is_symlink():
        return None
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode & 0o777,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _file_fingerprint_at(
    directory_fd: int, name: str
) -> tuple[int, int, int, str] | None:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return None
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return (info.st_dev, info.st_ino, info.st_mode & 0o777, digest.hexdigest())
    finally:
        os.close(descriptor)


def _transaction_backups(
    root: pathlib.Path,
    stamp: str,
    *,
    layout: Mapping[pathlib.Path, tuple[int, int]] | None = None,
    handle: ProjectHandle | None = None,
) -> list[str]:
    backups: list[str] = []
    with _open_project_dirs(root, layout=layout, handle=handle) as directories:
        for document_relative, script_relative in _LEGACY_HOOK_DOCUMENTS:
            for relative in (document_relative, script_relative):
                if handle is not None:
                    _assert_project_handle(handle)
                directory_fd = directories[str(relative.parent)]
                if directory_fd < 0:
                    continue
                prefix = f"{relative.name}.bak.{stamp}"
                for backup_name in os.listdir(directory_fd):
                    if not backup_name.startswith(prefix):
                        continue
                    if _file_fingerprint_at(directory_fd, backup_name) is not None:
                        backups.append(
                            str((root / relative).with_name(backup_name))
                        )
    return backups


def _legacy_state_digest(
    root: pathlib.Path, handle: ProjectHandle | None = None
) -> str:
    digest = hashlib.sha256()
    with _open_project_dirs(root, handle=handle) as directories:
        for document_relative, script_relative in _LEGACY_HOOK_DOCUMENTS:
            for relative in (document_relative, script_relative):
                if handle is not None:
                    _assert_project_handle(handle)
                digest.update(str(relative).encode("utf-8"))
                directory_fd = directories[str(relative.parent)]
                if directory_fd < 0:
                    digest.update(b"missing\0")
                    continue
                try:
                    content, _mode = _read_regular_at(directory_fd, relative.name)
                except FileNotFoundError:
                    digest.update(b"missing\0")
                except OSError:
                    digest.update(b"unsafe\0")
                else:
                    digest.update(b"file\0" + content)
    return digest.hexdigest()


def _snapshot_legacy_files(
    root: pathlib.Path, handle: ProjectHandle | None = None
) -> dict[pathlib.Path, tuple[bytes, int] | None]:
    snapshots: dict[pathlib.Path, tuple[bytes, int] | None] = {}
    with _open_project_dirs(root, handle=handle) as directories:
        for document_relative, script_relative in _LEGACY_HOOK_DOCUMENTS:
            for relative in (document_relative, script_relative):
                if handle is not None:
                    _assert_project_handle(handle)
                path = root / relative
                directory_fd = directories[str(relative.parent)]
                if directory_fd < 0:
                    snapshots[path] = None
                    continue
                try:
                    snapshots[path] = _read_regular_at(directory_fd, relative.name)
                except FileNotFoundError:
                    snapshots[path] = None
    return snapshots


def _atomic_write_bytes(path: pathlib.Path, content: bytes, mode: int) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.restore-", dir=path.parent)
    temporary = pathlib.Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _restore_legacy_snapshot(
    root: pathlib.Path,
    snapshots: Mapping[pathlib.Path, tuple[bytes, int] | None],
    backups: list[str],
    written: Mapping[pathlib.Path, tuple[int, int, int, str] | None] | None = None,
    layout: Mapping[pathlib.Path, tuple[int, int]] | None = None,
    handle: ProjectHandle | None = None,
) -> list[str]:
    conflicts: list[str] = []
    with _open_project_dirs(root, layout=layout, handle=handle) as directories:
        for path, snapshot in snapshots.items():
            if handle is not None:
                _assert_project_handle(handle)
            relative = path.relative_to(root)
            directory_fd = directories[str(relative.parent)]
            if directory_fd < 0:
                conflicts.append(str(path))
                continue
            if written is not None:
                if path not in written:
                    continue
                if _file_fingerprint_at(directory_fd, relative.name) != written[path]:
                    conflicts.append(str(path))
                    continue
            if snapshot is None:
                try:
                    info = os.stat(relative.name, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    os.unlink(relative.name, dir_fd=directory_fd)
                continue
            content, mode = snapshot
            _write_bytes_at(directory_fd, relative.name, content, mode)
        for raw in backups:
            backup = pathlib.Path(raw)
            try:
                relative = backup.relative_to(root)
            except ValueError:
                conflicts.append(str(backup))
                continue
            directory_fd = directories.get(str(relative.parent), -1)
            if directory_fd < 0:
                conflicts.append(str(backup))
                continue
            try:
                info = os.stat(relative.name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode):
                os.unlink(relative.name, dir_fd=directory_fd)
    return conflicts


def _write_migration_audit(
    root: pathlib.Path,
    stamp: str,
    *,
    before_digest: str,
    after_digest: str,
    changed_paths: list[str],
    backups: list[str],
    result: str,
    conflict_paths: list[str] | None = None,
    layout: Mapping[pathlib.Path, tuple[int, int]] | None = None,
    handle: ProjectHandle | None = None,
) -> pathlib.Path:
    audit = root / "codex" / "migration_audits" / f"legacy-hooks-{stamp}.json"
    document = {
            "root": str(root),
            "before_digest": before_digest,
            "after_digest": after_digest,
            "changed_paths": changed_paths,
            "backups": backups,
            "result": result,
            "conflict_paths": conflict_paths or [],
        }
    with _open_project_dirs(root, layout=layout, handle=handle) as directories:
        codex_fd = directories["codex"]
        if codex_fd < 0:
            raise ValueError("project codex directory is missing")
        audit_fd = directories["codex/migration_audits"]
        if audit_fd < 0:
            os.mkdir("migration_audits", 0o700, dir_fd=codex_fd)
            audit_fd = os.open(
                "migration_audits",
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=codex_fd,
            )
            try:
                _write_json_at(audit_fd, audit.name, document, 0o600)
            finally:
                os.close(audit_fd)
        else:
            _write_json_at(audit_fd, audit.name, document, 0o600)
    return audit


def _owned_legacy_script(
    root: pathlib.Path, script_path: pathlib.Path, agent: str
) -> tuple[bool, str | None]:
    if script_path.is_symlink():
        return False, "managed legacy script must not be a symlink"
    text, issue = _read_text(script_path)
    if issue is not None:
        return False, issue["error"]
    if text is None:
        return False, "referenced managed legacy script is missing"
    expected = memory_project.hook_script(root, agent)
    if text != expected:
        return False, "referenced script does not match a known manager-owned version"
    return True, None


def _legacy_hook_preview_for_project(
    root: pathlib.Path, handle: ProjectHandle | None = None
) -> dict[str, Any]:
    project_root = pathlib.Path(root).expanduser().absolute()
    managed: set[str] = set()
    custom: set[str] = set()
    targets: list[str] = []
    documents: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    script_targets: set[pathlib.Path] = set()
    try:
        directories_context = _open_project_dirs(project_root, handle=handle)
        directories = directories_context.__enter__()
    except (OSError, ValueError):
        return {
            "root": str(project_root),
            "status": "error",
            "managed_entries": 0,
            "custom_entries": 0,
            "targets": [],
            "script_targets": [],
            "documents": [],
            "errors": [_issue(project_root, ValueError("unsafe project directory layout"))],
        }
    try:
      for index, (document_relative, script_relative) in enumerate(_LEGACY_HOOK_DOCUMENTS):
        if handle is not None:
            _assert_project_handle(handle)
        document_path = project_root / document_relative
        script_path = project_root / script_relative
        document_fd = directories[str(document_relative.parent)]
        if document_fd < 0:
            documents.append({"path": str(document_path), "status": "missing"})
            continue
        try:
            raw, _mode = _read_regular_at(document_fd, document_relative.name)
            value = json.loads(raw.decode("utf-8"))
        except FileNotFoundError:
            documents.append({"path": str(document_path), "status": "missing"})
            continue
        except Exception as error:
            errors.append(_issue(document_path, error))
            documents.append({"path": str(document_path), "status": "error"})
            continue
        if not isinstance(value, dict):
            error = _issue(document_path, ValueError("JSON root must be an object"))
            errors.append(error)
            documents.append({"path": str(document_path), "status": "invalid"})
            continue
        has_reference = any(
            _is_legacy_memory_command(handler.get("command"))
            for groups in value.get("hooks", {}).values()
            if isinstance(groups, list)
            for group in groups
            if isinstance(group, dict) and isinstance(group.get("hooks"), list)
            for handler in group["hooks"]
            if isinstance(handler, dict) and handler.get("type") == "command"
        ) if isinstance(value.get("hooks"), dict) else False
        owned, ownership_error = (False, None)
        if has_reference:
            script_fd = directories[str(script_relative.parent)]
            if script_fd < 0:
                ownership_error = "referenced managed legacy script is missing"
                errors.append(_issue(script_path, ValueError(ownership_error)))
                script_fd = -1
            try:
                if script_fd < 0:
                    raise FileNotFoundError(script_relative.name)
                script_raw, _mode = _read_regular_at(script_fd, script_relative.name)
                expected = memory_project.hook_script(
                    project_root, "codex" if index == 0 else "claude"
                ).encode("utf-8")
                owned = script_raw == expected
                if not owned:
                    ownership_error = "referenced script does not match a known manager-owned version"
            except Exception as error:
                ownership_error = str(error)
        if has_reference and not owned:
            errors.append(_issue(script_path, ValueError(ownership_error or "unowned script")))
        try:
            document_managed, document_custom = _hook_entry_counts(
                value, owned_script=owned
            )
        except ValueError as error:
            errors.append(_issue(document_path, error))
            documents.append({"path": str(document_path), "status": "invalid"})
            continue
        managed.update(document_managed)
        custom.update(document_custom)
        if document_managed:
            targets.append(str(document_path))
            targets.append(str(script_path))
            script_targets.add(script_path)
        documents.append(
            {
                "path": str(document_path),
                "status": "ok",
                "managed_entries": len(document_managed),
                "custom_entries": len(document_custom),
            }
        )
    finally:
        directories_context.__exit__(None, None, None)
    return {
        "root": str(project_root),
        "status": "error" if errors else "preview",
        "managed_entries": len(managed),
        "custom_entries": len(custom),
        "targets": sorted(dict.fromkeys(targets)),
        "script_targets": sorted(str(path) for path in script_targets),
        "documents": documents,
        "errors": errors,
    }


def preview_legacy_hooks(
    project_roots: list[pathlib.Path], *, paths: RuntimePaths | None = None
) -> list[dict[str, Any]]:
    """Return a read-only legacy project memory-hook migration preview."""
    runtime_paths = paths or memory_project.RUNTIME_PATHS
    results: list[dict[str, Any]] = []
    for root in _selected_registered_roots(project_roots, runtime_paths):
        with _project_lock(root) as handle:
            results.append(_legacy_hook_preview_for_project(root, handle))
    return results


def apply_legacy_hooks(
    project_roots: list[pathlib.Path], *, paths: RuntimePaths | None = None
) -> dict[str, Any]:
    """Remove old per-project memory hook entries after backing up touched files."""
    runtime_paths = paths or memory_project.RUNTIME_PATHS
    try:
        selected = _selected_registered_roots(project_roots, runtime_paths)
    except Exception:
        return {
            "status": "failed",
            "projects": [
                {"root": str(pathlib.Path(root).expanduser().absolute()), "result": "failed", "error": "invalid_root"}
                for root in project_roots
            ],
        }
    stamp = _dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    results: list[dict[str, Any]] = []
    for project_root in selected:
        try:
            with _project_lock(project_root) as handle:
                preview = _legacy_hook_preview_for_project(project_root, handle)
                _assert_project_handle(handle)
                if preview["errors"]:
                    results.append({
                        "root": str(project_root),
                        "result": "failed",
                        "error": "ownership_conflict",
                        "audit": "",
                    })
                    continue
                snapshots = _snapshot_legacy_files(project_root, handle)
                layout = _layout_identities(project_root, handle)
                before_digest = _legacy_state_digest(project_root, handle)
                cleanup: dict[str, Any] = {
                    "backups": [], "changed_paths": [], "removed_handlers": 0
                }
                written_state: dict[
                    pathlib.Path, tuple[int, int, int, str] | None
                ] = {}
                if _snapshot_legacy_files(project_root, handle) != snapshots:
                    raise ValueError("legacy hook state changed before execution")
                _assert_project_handle(handle)
                _assert_layout_identities(layout)
                try:
                    cleanup = _archive_project_legacy_hooks(
                        project_root,
                        stamp=stamp,
                        layout=layout,
                        handle=handle,
                        written_state=written_state,
                    )
                    after_digest = _legacy_state_digest(project_root, handle)
                    result_status = "applied" if cleanup["changed_paths"] else "unchanged"
                    audit = _write_migration_audit(
                        project_root,
                        stamp,
                        before_digest=before_digest,
                        after_digest=after_digest,
                        changed_paths=cleanup["changed_paths"],
                        backups=cleanup["backups"],
                        result=result_status,
                        layout=layout,
                        handle=handle,
                    )
                except BaseException:
                    transaction_backups = _transaction_backups(
                        project_root, stamp, layout=layout, handle=handle
                    )
                    conflicts = _restore_legacy_snapshot(
                        project_root,
                        snapshots,
                        transaction_backups,
                        written_state,
                        layout,
                        handle,
                    )
                    audit_text = ""
                    try:
                        audit_text = str(_write_migration_audit(
                            project_root,
                            stamp,
                            before_digest=before_digest,
                            after_digest=_legacy_state_digest(project_root, handle),
                            changed_paths=[],
                            backups=[],
                            result="failed",
                            conflict_paths=conflicts,
                            layout=layout,
                            handle=handle,
                        ))
                    except Exception:
                        pass
                    results.append({
                        "root": str(project_root), "result": "failed",
                        "error": "conflict" if conflicts else "transaction_failed",
                        "conflict_paths": conflicts,
                        "audit": audit_text,
                    })
                    continue
            results.append({
                **preview,
                "status": result_status,
                "result": result_status,
                **cleanup,
                "audit": str(audit),
            })
        except BaseException:
            results.append({
                "root": str(project_root), "result": "failed",
                "error": "path_conflict", "audit": "",
            })
    failures = sum(item.get("result") == "failed" for item in results)
    status = "failed" if failures == len(results) else "partial" if failures else "applied"
    return {"status": status, "projects": results}


def _archive_project_legacy_hooks(
    project_root: pathlib.Path,
    *,
    stamp: str,
    layout: Mapping[pathlib.Path, tuple[int, int]] | None = None,
    handle: ProjectHandle | None = None,
    written_state: dict[
        pathlib.Path, tuple[int, int, int, str] | None
    ] | None = None,
) -> dict[str, Any]:
    backups: list[str] = []
    changed_paths: list[str] = []
    removed_handlers = 0
    written = written_state if written_state is not None else {}
    referenced_scripts: set[pathlib.Path] = set()
    with _open_project_dirs(project_root, layout=layout, handle=handle) as directories:
        for document_relative, script_relative in _LEGACY_HOOK_DOCUMENTS:
            if layout is not None:
                _assert_layout_identities(layout)
            if handle is not None:
                _assert_project_handle(handle)
            document_path = project_root / document_relative
            script_path = project_root / script_relative
            document_dir = str(document_relative.parent)
            document_fd = directories[document_dir]
            if document_fd < 0:
                continue
            try:
                raw, mode = _read_regular_at(document_fd, document_relative.name)
            except FileNotFoundError:
                continue
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError(f"legacy hook migration target must be an object: {document_path}")
            updated, removed = _remove_legacy_memory_entries(value)
            if not removed:
                continue
            backup_name = _backup_path(document_path, stamp).name
            _write_bytes_at(document_fd, backup_name, raw, mode)
            backups.append(str(document_path.with_name(backup_name)))
            _write_json_at(document_fd, document_relative.name, updated, mode)
            written[document_path] = _file_fingerprint_at(
                document_fd, document_relative.name
            )
            changed_paths.append(str(document_path))
            referenced_scripts.add(script_path)
            removed_handlers += removed
        for script_path in sorted(referenced_scripts):
            if layout is not None:
                _assert_layout_identities(layout)
            if handle is not None:
                _assert_project_handle(handle)
            script_relative = script_path.relative_to(project_root)
            script_fd = directories[str(script_relative.parent)]
            if script_fd < 0:
                raise ValueError(f"legacy script directory missing: {script_path.parent}")
            _read_regular_at(script_fd, script_relative.name)
            backup = _backup_path(script_path, stamp)
            os.replace(
                script_relative.name,
                backup.name,
                src_dir_fd=script_fd,
                dst_dir_fd=script_fd,
            )
            written[script_path] = None
            backups.append(str(backup))
            changed_paths.append(str(script_path))
    return {
        "backups": backups,
        "changed_paths": changed_paths,
        "removed_handlers": removed_handlers,
        "written": written,
    }


def prepare_legacy_hook_cleanup(project_root: pathlib.Path) -> dict[str, Any]:
    """Validate ownership and capture the active legacy-hook state."""
    root = pathlib.Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"project root is not a directory: {root}")
    with _project_lock(root) as handle:
        preview = _legacy_hook_preview_for_project(root, handle)
        if preview["errors"]:
            first = preview["errors"][0]
            raise ValueError(f"legacy hook cleanup preflight failed: {first['path']}: {first['error']}")
        snapshots = _snapshot_legacy_files(root, handle)
        layout = _layout_identities(root, handle)
    return {"root": root, "snapshots": snapshots, "layout": layout}


def execute_legacy_hook_cleanup(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Execute one prepared cleanup and restore the active state on failure."""
    root = pathlib.Path(plan["root"])
    snapshots = plan["snapshots"]
    layout = plan["layout"]
    stamp = _dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    with _project_lock(root) as handle:
        if _snapshot_legacy_files(root, handle) != snapshots:
            raise ValueError("legacy hook state changed after preflight")
        _assert_project_handle(handle)
        _assert_layout_identities(layout)
        cleanup: dict[str, Any] = {
            "backups": [], "changed_paths": [], "removed_handlers": 0,
            "written": {},
        }
        written_state: dict[
            pathlib.Path, tuple[int, int, int, str] | None
        ] = {}
        try:
            cleanup = _archive_project_legacy_hooks(
                root,
                stamp=stamp,
                layout=layout,
                handle=handle,
                written_state=written_state,
            )
            return cleanup
        except BaseException:
            transaction_backups = _transaction_backups(
                root, stamp, layout=layout, handle=handle
            )
            conflicts = _restore_legacy_snapshot(
                root, snapshots, transaction_backups, written_state, layout, handle
            )
            if conflicts:
                return {
                    "result": "failed",
                    "error": "conflict",
                    "conflict_paths": conflicts,
                    "backups": transaction_backups,
                    "changed_paths": [],
                    "removed_handlers": 0,
                }
            raise


def remove_managed_legacy_hooks(project_root: pathlib.Path) -> dict[str, Any]:
    return execute_legacy_hook_cleanup(prepare_legacy_hook_cleanup(project_root))


def _registry_projects(registry: Mapping[str, object]) -> list[dict[str, Any]]:
    projects = registry.get("projects", [])
    if not isinstance(projects, list):
        return []
    result: list[dict[str, Any]] = []
    for entry in projects:
        if isinstance(entry, dict):
            result.append(entry)
    return result


def valid_project_roots(registry: Mapping[str, object]) -> list[pathlib.Path]:
    roots: list[pathlib.Path] = []
    seen: set[str] = set()
    for entry in _registry_projects(registry):
        root = entry.get("root")
        if not isinstance(root, str) or not root.strip():
            continue
        resolved = pathlib.Path(root).expanduser().resolve()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        roots.append(resolved)
    return roots


def inspect_personal(personal_root: pathlib.Path) -> dict[str, Any]:
    files = {}
    errors: list[dict[str, str]] = []
    total_sections = 0
    for name in ("long.md", "short.md", "proposals.md"):
        summary = _markdown_summary(personal_root / name)
        files[name] = summary
        total_sections += int(summary.get("sections", 0))
        if summary.get("status") == "error":
            errors.append({"path": summary["path"], "error": summary["error"]})
    return {
        "root": str(personal_root),
        "files": files,
        "sections": total_sections,
        "errors": errors,
    }


def inspect_projects(
    project_roots: list[pathlib.Path], registry: Mapping[str, object]
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    schema_version = registry.get("schema_version", 1)
    projects_value = registry.get("projects", [])
    current_project = registry.get("current_project", "")
    if schema_version != 1:
        errors.append({"path": "projects.json", "error": "schema_version must be 1"})
    if not isinstance(projects_value, list):
        errors.append({"path": "projects.json", "error": "projects must be an array"})
    else:
        for index, record in enumerate(projects_value):
            if not isinstance(record, dict) or not isinstance(record.get("root"), str):
                errors.append({"path": "projects.json", "error": f"projects[{index}].root must be a string"})
    if not isinstance(current_project, str):
        errors.append({"path": "projects.json", "error": "current_project must be a string"})
    elif current_project and current_project not in {str(root) for root in project_roots}:
        errors.append({"path": "projects.json", "error": "current_project references an unregistered project"})
    for root in project_roots:
        config_path = root / ".loop" / "config.json"
        loop_summary = _json_summary(config_path)
        if loop_summary.get("status") == "error":
            errors.append({"path": loop_summary["path"], "error": loop_summary["error"]})
        if loop_summary.get("status") == "ok" and not isinstance(
            loop_summary.get("schema_version"), int
        ):
            errors.append(
                {
                    "path": str(config_path),
                    "error": "loop config schema_version must be an integer",
                }
            )
            loop_summary = {**loop_summary, "status": "invalid"}
        items.append(
            {
                "name": memory_project.repo_name(root),
                "root": str(root),
                "is_git_repo": (root / ".git").exists(),
                "has_memory": all(
                    path.exists()
                    for path in (
                        root / "codex" / "codex_long_memory.md",
                        root / "codex" / "codex_short_memory.md",
                        root / "codex" / "memory_proposals.md",
                    )
                ),
                "has_loop": config_path.exists(),
                "loop": loop_summary,
                "ui_design_status": memory_project.ui_design_status(root),
                "ui_design_config": _json_summary(root / "codex" / "ui_design" / "config.json"),
            }
        )
    return {
        "schema_version": schema_version,
        "current_project": current_project,
        "registered": len(project_roots),
        "projects": items,
        "errors": errors,
    }


def inspect_review_state(project_roots: list[pathlib.Path]) -> dict[str, Any]:
    projects: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    total_counts = {
        "pending": 0,
        "actionable_pending": 0,
        "checkpoint_pending": 0,
        "project_pending": 0,
        "personal_pending": 0,
        "approved": 0,
        "rejected": 0,
        "deferred": 0,
    }
    total_queue_items = 0
    total_state_items = 0
    total_proposal_sections = 0
    for root in project_roots:
        proposal_summary = _markdown_summary(root / "codex" / "memory_proposals.md")
        if proposal_summary.get("status") == "error":
            errors.append(
                {"path": proposal_summary["path"], "error": proposal_summary["error"]}
            )
        queue_path = root / "codex" / "memory_review_queue.json"
        queue_value, queue_issue = _read_json(queue_path)
        if queue_issue is not None:
            errors.append(queue_issue)
            queue_counts = {key: 0 for key in total_counts}
            queue_items = 0
        else:
            queue_items = 0
            if isinstance(queue_value, dict):
                items = queue_value.get("items", [])
                if isinstance(items, list):
                    queue_items = len(items)
                    queue_counts = count_items(
                        [item for item in items if isinstance(item, dict)]
                    )
                else:
                    queue_counts = {key: 0 for key in total_counts}
            else:
                queue_counts = {key: 0 for key in total_counts}
        state_path = root / "codex" / "memory_review_state.json"
        state_value, state_issue = _read_json(state_path)
        if state_issue is not None:
            errors.append(state_issue)
            state_items = 0
        elif isinstance(state_value, dict):
            items = state_value.get("items", {})
            state_items = len(items) if isinstance(items, dict) else 0
        else:
            state_items = 0
        total_queue_items += queue_items
        total_state_items += state_items
        total_proposal_sections += int(proposal_summary.get("sections", 0))
        for key in total_counts:
            total_counts[key] += int(queue_counts.get(key, 0))
        projects.append(
            {
                "root": str(root),
                "proposals": proposal_summary,
                "queue": {
                    "path": str(queue_path),
                    "items": queue_items,
                    "counts": queue_counts,
                },
                "state": {"path": str(state_path), "items": state_items},
            }
        )
    return {
        "projects": projects,
        "proposal_sections": total_proposal_sections,
        "queue_items": total_queue_items,
        "state_items": total_state_items,
        "counts": total_counts,
        "errors": errors,
    }


def inspect_design_preferences(
    paths: RuntimePaths, project_roots: list[pathlib.Path]
) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    global_path = paths.ui_design_home / "preferences.json"
    global_value, global_issue = _read_json(global_path)
    if global_issue is not None:
        errors.append(global_issue)
        global_summary = {"path": str(global_path), "status": "error", "schema_version": None}
    elif global_value is None:
        global_summary = {"path": str(global_path), "status": "missing", "schema_version": None}
    else:
        try:
            validated = ui_design_preferences.validate_global_preferences(global_value)
        except Exception as error:
            errors.append(_issue(global_path, error))
            global_summary = {"path": str(global_path), "status": "invalid", "schema_version": None}
        else:
            global_summary = {
                "path": str(global_path),
                "status": "ok",
                "schema_version": 1,
                "groups": len(validated),
            }

    project_items: list[dict[str, Any]] = []
    with_overrides = 0
    schema_versions: dict[str, int] = {}
    for root in project_roots:
        path = root / "codex" / "ui_design" / "preferences.json"
        value, issue = _read_json(path)
        if issue is not None:
            errors.append(issue)
            summary = {"path": str(path), "status": "error", "schema_version": None}
        elif value is None:
            summary = {"path": str(path), "status": "missing", "schema_version": None}
        else:
            overrides: Any
            schema_version = None
            if isinstance(value, dict) and "overrides" in value:
                schema_version = value.get("schema_version")
                overrides = value["overrides"]
            else:
                overrides = value
                schema_version = 1 if isinstance(value, dict) else None
            try:
                validated = ui_design_preferences.validate_project_overrides(overrides)
            except Exception as error:
                errors.append(_issue(path, error))
                summary = {"path": str(path), "status": "invalid", "schema_version": schema_version}
            else:
                if schema_version is not None:
                    schema_versions[str(schema_version)] = schema_versions.get(str(schema_version), 0) + 1
                if validated:
                    with_overrides += 1
                summary = {
                    "path": str(path),
                    "status": "ok",
                    "schema_version": schema_version,
                    "overrides": len(validated),
                }
        project_items.append({"root": str(root), "preferences": summary})

    return {
        "global": global_summary,
        "projects": {
            "items": project_items,
            "count": len(project_roots),
            "with_overrides": with_overrides,
            "schema_versions": schema_versions,
        },
        "errors": errors,
    }


def inspect_ui_design_approvals(project_roots: list[pathlib.Path]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    package_approvals = 0
    project_global_approvals = 0
    for root in project_roots:
        path = root / "codex" / "ui_design" / "approvals.json"
        value, issue = _read_json(path)
        if issue is not None:
            errors.append(issue)
            summary = {"path": str(path), "status": "error"}
        elif value is None:
            summary = {"path": str(path), "status": "missing"}
        elif not isinstance(value, dict):
            summary = {"path": str(path), "status": "invalid", "error": "JSON root must be an object"}
            errors.append(_issue(path, ValueError("JSON root must be an object")))
        else:
            if value.get("schema_version") != 1:
                errors.append(_issue(path, ValueError("approval schema_version must be 1")))
            packages = value.get("package_approvals", {})
            if not isinstance(packages, dict):
                errors.append(_issue(path, ValueError("package_approvals must be an object")))
                summary = {"path": str(path), "status": "invalid"}
            else:
                package_approvals += len(packages)
                package_root = root / "codex/ui_design/design-packages"
                for task_id, approval in packages.items():
                    if not isinstance(approval, dict) or not isinstance(approval.get("digest"), str):
                        errors.append(_issue(path, ValueError(f"invalid package approval {task_id}")))
                        continue
                    try:
                        package = ui_design_gate.get_design_package(root, str(task_id))
                    except Exception:
                        errors.append(_issue(package_root / str(task_id), FileNotFoundError("approval references missing design package")))
                    else:
                        if package["digest"] != approval["digest"]:
                            errors.append(_issue(package_root / str(task_id), ValueError("design approval digest mismatch")))
                idempotency = value.get("idempotency", {})
                if not isinstance(idempotency, dict):
                    errors.append(_issue(path, ValueError("approval idempotency must be an object")))
                project_global = value.get("project_global_approval")
                if project_global is not None:
                    project_global_approvals += 1
                summary = {
                    "path": str(path),
                    "status": "ok",
                    "schema_version": value.get("schema_version"),
                    "package_approvals": len(packages),
                    "project_global_approval": project_global is not None,
                }
        items.append({"root": str(root), "approvals": summary})
    return {
        "schema_version": 1,
        "projects": items,
        "package_approvals": package_approvals,
        "project_global_approvals": project_global_approvals,
        "errors": errors,
    }


def inspect_ui_skills(ui_design_home: pathlib.Path) -> dict[str, Any]:
    path = ui_design_home / "registry.json"
    value, issue = _read_json(path)
    if issue is not None:
        return {
            "path": str(path),
            "status": "error",
            "schema_version": None,
            "drafts": 0,
            "published": 0,
            "deployments": 0,
            "idempotency": 0,
            "errors": [issue],
        }
    if value is None:
        return {
            "path": str(path),
            "status": "missing",
            "schema_version": None,
            "drafts": 0,
            "published": 0,
            "deployments": 0,
            "idempotency": 0,
            "errors": [],
        }
    if not isinstance(value, dict):
        return {
            "path": str(path),
            "status": "invalid",
            "schema_version": None,
            "drafts": 0,
            "published": 0,
            "deployments": 0,
            "idempotency": 0,
            "errors": [_issue(path, ValueError("JSON root must be an object"))],
        }
    errors: list[dict[str, str]] = []
    drafts = value.get("drafts", {})
    packages = value.get("packages", {})
    deployments = value.get("deployments", {})
    idempotency = value.get("idempotency", {})
    for key, item in (
        ("drafts", drafts),
        ("packages", packages),
        ("deployments", deployments),
        ("idempotency", idempotency),
    ):
        if not isinstance(item, dict):
            errors.append(_issue(path, ValueError(f"{key} must be an object")))
    if errors:
        return {
            "path": str(path),
            "status": "invalid",
            "schema_version": value.get("schema_version"),
            "drafts": 0,
            "published": 0,
            "deployments": 0,
            "idempotency": 0,
            "errors": errors,
        }
    published = 0
    for versions in packages.values():
        if isinstance(versions, list):
            published += len(versions)
    return {
        "path": str(path),
        "status": "ok",
        "schema_version": value.get("schema_version"),
        "drafts": len(drafts),
        "published": published,
        "deployments": len(deployments),
        "idempotency": len(idempotency),
        "errors": [],
    }


def inspect_loop(project_roots: list[pathlib.Path]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    schema_versions: dict[str, int] = {}
    configured = 0
    for root in project_roots:
        path = root / ".loop" / "config.json"
        value, issue = _read_json(path)
        if issue is not None:
            errors.append(issue)
            summary = {"path": str(path), "status": "error", "schema_version": None}
        elif value is None:
            summary = {"path": str(path), "status": "missing", "schema_version": None}
        elif not isinstance(value, dict):
            summary = {"path": str(path), "status": "invalid", "schema_version": None}
            errors.append(_issue(path, ValueError("JSON root must be an object")))
        else:
            schema_version = value.get("schema_version")
            if isinstance(schema_version, int):
                configured += 1
                schema_versions[str(schema_version)] = schema_versions.get(str(schema_version), 0) + 1
                summary = {
                    "path": str(path),
                    "status": "ok",
                    "schema_version": schema_version,
                    "loop_enabled": bool(value.get("loop_enabled")),
                }
            else:
                summary = {"path": str(path), "status": "invalid", "schema_version": schema_version}
                errors.append(
                    _issue(path, ValueError("loop config schema_version must be an integer"))
                )
        items.append({"root": str(root), "config": summary})
    return {
        "projects": items,
        "configured": configured,
        "schema_versions": schema_versions,
        "errors": errors,
    }


def inspect_worktrees(worktree_manager: pathlib.Path) -> dict[str, Any]:
    path = worktree_manager / "tasks.json"
    value, issue = _read_json(path)
    if issue is not None:
        return {
            "path": str(path),
            "status": "error",
            "schema_version": None,
            "tasks": 0,
            "repositories": 0,
            "statuses": {},
            "errors": [issue],
        }
    if value is None:
        return {
            "path": str(path),
            "status": "missing",
            "schema_version": None,
            "tasks": 0,
            "repositories": 0,
            "statuses": {},
            "errors": [],
        }
    if not isinstance(value, dict):
        return {
            "path": str(path),
            "status": "invalid",
            "schema_version": None,
            "tasks": 0,
            "repositories": 0,
            "statuses": {},
            "errors": [_issue(path, ValueError("JSON root must be an object"))],
        }
    tasks = value.get("tasks", {})
    errors: list[dict[str, str]] = []
    if not isinstance(tasks, dict):
        errors.append(_issue(path, ValueError("tasks must be an object")))
        tasks = {}
    status_counts: dict[str, int] = {}
    repositories: set[str] = set()
    for item in tasks.values():
        if not isinstance(item, dict):
            errors.append(_issue(path, ValueError("worktree task must be an object")))
            continue
        status = str(item.get("status", "unknown"))
        if status not in _WORKTREE_STATUSES:
            errors.append(_issue(path, ValueError(f"unknown worktree task status: {status}")))
        status_counts[status] = status_counts.get(status, 0) + 1
        repository = item.get("repository")
        if isinstance(repository, str) and repository.strip():
            repositories.add(repository)
    return {
        "path": str(path),
        "status": "ok" if not errors else "invalid",
        "schema_version": value.get("schema_version"),
        "tasks": len(tasks),
        "repositories": len(repositories),
        "statuses": status_counts,
        "errors": errors,
    }


def inspect_legacy_hooks(project_roots: list[pathlib.Path]) -> dict[str, Any]:
    projects: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    script_files = 0
    config_documents = 0
    references = 0
    for root in project_roots:
        files = {
            ".codex/hooks/shared_memory_hook.py": root / ".codex" / "hooks" / "shared_memory_hook.py",
            ".claude/hooks/shared_memory_hook.py": root / ".claude" / "hooks" / "shared_memory_hook.py",
            ".codex/hooks/ui_design_gate_hook.py": root / ".codex" / "hooks" / "ui_design_gate_hook.py",
            ".claude/hooks/ui_design_gate_hook.py": root / ".claude" / "hooks" / "ui_design_gate_hook.py",
        }
        documents = {
            ".codex/hooks.json": root / ".codex" / "hooks.json",
            ".claude/settings.json": root / ".claude" / "settings.json",
        }
        file_statuses = {}
        document_statuses = {}
        project_script_files = 0
        project_references = 0
        for label, path in files.items():
            exists = path.exists()
            file_statuses[label] = {"path": str(path), "exists": exists}
            if exists:
                project_script_files += 1
        for label, path in documents.items():
            value, issue = _read_json(path)
            if issue is not None:
                errors.append(issue)
                document_statuses[label] = {"path": str(path), "status": "error"}
                continue
            if value is None:
                document_statuses[label] = {"path": str(path), "status": "missing"}
                continue
            if not isinstance(value, dict):
                document_statuses[label] = {"path": str(path), "status": "invalid"}
                errors.append(_issue(path, ValueError("JSON root must be an object")))
                continue
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
            reference_count = int("shared_memory_hook.py" in encoded) + int(
                "ui_design_gate_hook.py" in encoded
            )
            project_references += reference_count
            document_statuses[label] = {
                "path": str(path),
                "status": "ok",
                "references": reference_count,
            }
        script_files += project_script_files
        config_documents += sum(
            1 for item in document_statuses.values() if item.get("status") == "ok"
        )
        references += project_references
        projects.append(
            {
                "root": str(root),
                "script_files": file_statuses,
                "documents": document_statuses,
                "script_file_count": project_script_files,
                "reference_count": project_references,
            }
        )
    return {
        "projects": projects,
        "script_files": script_files,
        "documents": config_documents,
        "references": references,
        "errors": errors,
    }


def _messages(errors: object) -> list[str]:
    if not isinstance(errors, list):
        return ["errors must be an array"]
    return [
        f"{item.get('path')}: {item.get('error')}" if isinstance(item, dict) else str(item)
        for item in errors
    ]


def _area(value: dict[str, Any], records: list[dict[str, Any]], present: bool) -> dict[str, Any]:
    return {**value, "present": present, "errors": _messages(value.get("errors", [])), "records": records}


def inspect_policy(paths: RuntimePaths) -> dict[str, Any]:
    path = paths.install_root / "config.json"
    value, issue = _read_json(path)
    errors: list[dict[str, str]] = []
    records: list[dict[str, Any]] = []
    if issue:
        errors.append(issue)
    elif value is not None:
        try:
            settings = vibe_memory_settings.load_settings(paths)
        except (TypeError, ValueError) as error:
            errors.append(_issue(path, error))
        else:
            records.append({"path": str(path), "schema_version": settings["schema_version"], "formal_memory_requires_approval": settings["formal_memory_requires_approval"]})
    return _area({"path": str(path), "errors": errors}, records, value is not None and not errors)


def inspect_ui_design_packages(project_roots: list[pathlib.Path]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for root in project_roots:
        package_root = root / "codex/ui_design/design-packages"
        if not package_root.exists():
            continue
        if not package_root.is_dir():
            errors.append(_issue(package_root, ValueError("design-packages must be a directory")))
            continue
        for item in sorted(package_root.iterdir()):
            if not item.is_dir():
                errors.append(_issue(item, ValueError("design package must be a directory")))
                continue
            try:
                package = ui_design_gate.get_design_package(root, item.name)
            except Exception as error:
                errors.append(_issue(item, error))
            else:
                records.append({"root": str(root), "task_id": item.name, "path": str(item), "digest": package["digest"]})
    return _area({"projects": len(project_roots), "errors": errors}, records, bool(records))


def _inspect_jsonl(paths: list[pathlib.Path], label: str) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    present = False
    for path in paths:
        text_value, issue = _read_text(path)
        if issue:
            errors.append(issue)
            continue
        if text_value is None:
            continue
        present = True
        for number, line in enumerate(text_value.splitlines(), 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                errors.append(_issue(path, ValueError(f"{label} line {number}: {error}")))
                continue
            if not isinstance(value, dict):
                errors.append(_issue(path, ValueError(f"{label} line {number} must be an object")))
            else:
                records.append({"path": str(path), **value})
    return _area({"errors": errors}, records, present and not errors)


def inspect_ui_design_audit(project_roots: list[pathlib.Path]) -> dict[str, Any]:
    return _inspect_jsonl([root / "codex/ui_design/audit.jsonl" for root in project_roots], "UI design audit")


def _skill_registry(ui_design_home: pathlib.Path) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    path = ui_design_home / "registry.json"
    value, issue = _read_json(path)
    errors = [issue] if issue else []
    if value is None:
        return None, errors
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        errors.append(_issue(path, ValueError("UI skill registry schema_version must be 1")))
        return None, errors
    for key in ("drafts", "packages", "deployments", "idempotency"):
        if not isinstance(value.get(key), dict):
            errors.append(_issue(path, ValueError(f"{key} must be an object")))
    return value, errors


def inspect_ui_skill_digests(ui_design_home: pathlib.Path) -> dict[str, Any]:
    registry, errors = _skill_registry(ui_design_home)
    records: list[dict[str, Any]] = []
    if registry:
        for name, versions in registry["packages"].items():
            if not isinstance(versions, list):
                errors.append(_issue(ui_design_home / "registry.json", ValueError(f"packages.{name} must be an array")))
                continue
            for record in versions:
                if not isinstance(record, dict) or not isinstance(record.get("package_path"), str) or not isinstance(record.get("digest"), str):
                    errors.append(_issue(ui_design_home / "registry.json", ValueError(f"invalid package record for {name}")))
                    continue
                package_path = pathlib.Path(record["package_path"])
                if not package_path.is_dir():
                    errors.append(_issue(package_path, FileNotFoundError("referenced UI skill package is missing")))
                    continue
                actual = ui_design_store.tree_digest(package_path)
                if actual != record["digest"]:
                    errors.append(_issue(package_path, ValueError("UI skill package digest mismatch")))
                records.append({"name": name, "version_id": record.get("version_id"), "path": str(package_path), "digest": record["digest"], "actual_digest": actual})
    return _area({"errors": errors}, records, registry is not None and not errors)


def inspect_ui_skill_deployments(ui_design_home: pathlib.Path) -> dict[str, Any]:
    registry, errors = _skill_registry(ui_design_home)
    records: list[dict[str, Any]] = []
    packages: set[tuple[object, object]] = set()
    if registry:
        for name, versions in registry["packages"].items():
            if isinstance(versions, list):
                packages.update((name, item.get("version_id")) for item in versions if isinstance(item, dict))
        for transaction_id, record in registry["deployments"].items():
            if not isinstance(record, dict) or record.get("transaction_id") != transaction_id:
                errors.append(_issue(ui_design_home / "registry.json", ValueError(f"invalid deployment {transaction_id}")))
                continue
            if (record.get("name"), record.get("version_id")) not in packages:
                errors.append(_issue(ui_design_home / "registry.json", ValueError(f"deployment {transaction_id} references missing package")))
            report = ui_design_home / "deployments" / f"{transaction_id}.json"
            expected_package = next(
                (
                    item
                    for item in registry["packages"].get(record.get("name"), [])
                    if isinstance(item, dict) and item.get("version_id") == record.get("version_id")
                ),
                None,
            )
            if expected_package is not None and record.get("digest") != expected_package.get("digest"):
                errors.append(_issue(ui_design_home / "registry.json", ValueError(f"deployment {transaction_id} digest does not match package")))
            if report.is_symlink() or not report.is_file():
                errors.append(_issue(report, FileNotFoundError("referenced deployment report is missing")))
            else:
                report_value, report_issue = _read_json(report)
                if report_issue:
                    errors.append(report_issue)
                elif not isinstance(report_value, dict):
                    errors.append(_issue(report, ValueError("deployment report must be an object")))
                else:
                    for field in ("transaction_id", "name", "version_id", "digest", "status"):
                        if report_value.get(field) != record.get(field):
                            errors.append(_issue(report, ValueError(f"deployment report {field} mismatch")))
            records.append({"transaction_id": transaction_id, **record})
    return _area({"errors": errors}, records, registry is not None and not errors)


def inspect_ui_skill_audit(ui_design_home: pathlib.Path) -> dict[str, Any]:
    return _inspect_jsonl([ui_design_home / "audit.jsonl"], "UI skill audit")


_WORKTREE_STATUSES = {
    "developing",
    "ready_for_user_acceptance",
    "release_failed",
    "canonical_synced",
    "master_pushed",
    "staging_deployed",
    "verified",
    "cleaned",
    "failed",
}
_ACTIVE_WORKTREE_STATUSES = _WORKTREE_STATUSES - {"cleaned", "failed"}
_PENDING_WORKTREE_STATUSES = _ACTIVE_WORKTREE_STATUSES - {"developing"}


def _inspect_worktree_subset(worktree_manager: pathlib.Path, statuses: set[str]) -> dict[str, Any]:
    path = worktree_manager / "tasks.json"
    value, issue = _read_json(path)
    errors: list[dict[str, str]] = [issue] if issue else []
    records: list[dict[str, Any]] = []
    if value is not None:
        if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("tasks"), dict):
            errors.append(_issue(path, ValueError("worktree registry schema is invalid")))
        else:
            for task_id, record in value["tasks"].items():
                if not isinstance(record, dict):
                    errors.append(_issue(path, ValueError(f"task {task_id} must be an object")))
                    continue
                if record.get("status") in statuses:
                    worktree = record.get("worktree")
                    if not isinstance(worktree, str) or not pathlib.Path(worktree).is_dir():
                        errors.append(_issue(path, ValueError(f"task {task_id} status {record.get('status')} references missing worktree")))
                    records.append({"task_id": task_id, **record})
    return _area({"path": str(path), "errors": errors}, records, value is not None and not errors)


def inspect_active_worktrees(worktree_manager: pathlib.Path) -> dict[str, Any]:
    return _inspect_worktree_subset(worktree_manager, _ACTIVE_WORKTREE_STATUSES)


def inspect_pending_worktrees(worktree_manager: pathlib.Path) -> dict[str, Any]:
    return _inspect_worktree_subset(worktree_manager, _PENDING_WORKTREE_STATUSES)


def inventory(paths: RuntimePaths, registry: Mapping[str, object]) -> dict[str, Any]:
    project_roots = valid_project_roots(registry)
    snapshot = {
        "personal_memory": inspect_personal(paths.personal_memory),
        "projects": inspect_projects(project_roots, registry),
        "memory_review": inspect_review_state(project_roots),
        "policy": inspect_policy(paths),
        "design_preferences": inspect_design_preferences(paths, project_roots),
        "ui_design_packages": inspect_ui_design_packages(project_roots),
        "ui_design_approvals": inspect_ui_design_approvals(project_roots),
        "ui_design_audit": inspect_ui_design_audit(project_roots),
        "ui_skills": inspect_ui_skills(paths.ui_design_home),
        "ui_skill_digests": inspect_ui_skill_digests(paths.ui_design_home),
        "ui_skill_deployments": inspect_ui_skill_deployments(paths.ui_design_home),
        "ui_skill_audit": inspect_ui_skill_audit(paths.ui_design_home),
        "loop": inspect_loop(project_roots),
        "worktrees": inspect_worktrees(paths.worktree_manager),
        "active_worktrees": inspect_active_worktrees(paths.worktree_manager),
        "pending_worktrees": inspect_pending_worktrees(paths.worktree_manager),
        "legacy_hooks": inspect_legacy_hooks(project_roots),
    }
    for area, value in snapshot.items():
        if not isinstance(value, dict):
            snapshot[area] = {"present": False, "errors": ["inspector returned invalid value"], "records": []}
            continue
        value["errors"] = _messages(value.get("errors", []))
        value.setdefault("records", [])
        value.setdefault("present", bool(value.get("records")))
    return snapshot


def _control_plane_area_status(value: object) -> str:
    if not isinstance(value, dict):
        return "error"
    errors = value.get("errors", [])
    if isinstance(errors, list) and errors:
        return "error"
    return "ok"


def validate_control_plane(
    paths: RuntimePaths,
    registry: Mapping[str, object],
) -> dict[str, str]:
    """Validate that the installed runtime can read every control-plane area."""
    snapshot = inventory(paths, registry)
    return {
        area: _control_plane_area_status(value)
        for area, value in snapshot.items()
    }
