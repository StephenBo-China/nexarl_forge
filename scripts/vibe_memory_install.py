"""Install and render the versioned local Vibe Memory macOS runtime."""

from __future__ import annotations

import ctypes
import json
import os
import pathlib
import plistlib
import re
import stat
from string import Template
import sys
from typing import Any
import uuid
from xml.sax.saxutils import escape

from vibe_memory_paths import RuntimePaths


_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
_LAUNCH_AGENT_TEMPLATE = _PROJECT_ROOT / "templates/macos/com.noema.vibe-memory.plist"
_REQUIRED_DIRECTORIES = ("scripts", "templates", "docs")
_REQUIRED_FILES = ("README.md", "release.json")
_OPTIONAL_FILES = ("LICENSE",)
_SEMVER_IDENTIFIER = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
_VERSION_PATTERN = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    rf"(?:-({_SEMVER_IDENTIFIER}(?:\.{_SEMVER_IDENTIFIER})*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_PYTHON_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+$")
RENAME_EXCL = 0x00000004
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_TRUSTED_SYSTEM_ALIASES = {
    pathlib.Path("/etc"): pathlib.Path("/private/etc"),
    pathlib.Path("/tmp"): pathlib.Path("/private/tmp"),
    pathlib.Path("/var"): pathlib.Path("/private/var"),
}


class _AnchoredPath:
    """A display path paired with a directory-fd-relative entry name."""

    def __init__(
        self,
        parent_fd: int,
        entry_name: str,
        display_path: pathlib.Path,
        directory_fd: int | None = None,
    ) -> None:
        self.parent_fd = parent_fd
        self.entry_name = entry_name
        self.display_path = display_path
        self.directory_fd = directory_fd
        self.creation_ledger: dict[str, tuple[int, int, int]] = {}

    @property
    def name(self) -> str:
        return self.entry_name

    def __fspath__(self) -> str:
        return str(self.display_path)

    def __str__(self) -> str:
        return str(self.display_path)

    def __truediv__(self, child: str) -> pathlib.Path:
        return self.display_path / child


class TemporaryCleanupConflict(RuntimeError):
    """Raised when a temporary install entry no longer has its created identity."""


def _validate_manifest(manifest: dict[str, Any]) -> str:
    required = {
        "app_version",
        "data_schema_version",
        "hook_protocol_version",
        "minimum_python",
        "platform",
    }
    missing = required.difference(manifest)
    if missing:
        raise ValueError(f"release manifest missing required fields: {', '.join(sorted(missing))}")
    version = manifest["app_version"]
    if not isinstance(version, str) or not _VERSION_PATTERN.fullmatch(version):
        raise ValueError("release app_version must be a semantic version")
    for field in ("data_schema_version", "hook_protocol_version"):
        value = manifest[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"release {field} must be a positive integer")
    minimum_python = manifest["minimum_python"]
    if not isinstance(minimum_python, str) or not _PYTHON_VERSION_PATTERN.fullmatch(minimum_python):
        raise ValueError("release minimum_python must use major.minor format")
    if manifest["platform"] != "macOS":
        raise ValueError("release platform must be macOS")
    return version


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _read_regular_file_at(parent_fd: int, name: str, display_path: pathlib.Path) -> bytes:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        raise ValueError(f"required release file is missing: {display_path}") from error
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"release source must contain regular files only: {display_path}")
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
    except OSError as error:
        raise ValueError(f"unsafe release source file: {display_path}") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
            raise ValueError(f"release source file changed while opening: {display_path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(descriptor)


def _open_source_directory_at(parent_fd: int, name: str, display_path: pathlib.Path) -> int:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        raise ValueError(f"required release directory is missing: {display_path}") from error
    if not stat.S_ISDIR(before.st_mode):
        raise ValueError(f"release source must contain real directories only: {display_path}")
    try:
        descriptor = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        raise ValueError(f"unsafe release source directory: {display_path}") from error
    try:
        opened = os.fstat(descriptor)
    except Exception:
        os.close(descriptor)
        raise
    if not stat.S_ISDIR(opened.st_mode) or _identity(opened) != _identity(before):
        os.close(descriptor)
        raise ValueError(f"release source directory changed while opening: {display_path}")
    return descriptor


def _snapshot_directory_fd(
    directory_fd: int,
    display_root: pathlib.Path,
    prefix: pathlib.Path,
) -> dict[str, tuple[str, bytes | None]]:
    entries: dict[str, tuple[str, bytes | None]] = {}
    for name in os.listdir(directory_fd):
        relative = prefix / name
        display_path = display_root / relative
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as error:
            raise ValueError(f"release source changed during enumeration: {display_path}") from error
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = _open_source_directory_at(directory_fd, name, display_path)
            try:
                entries[str(relative)] = ("directory", None)
                entries.update(_snapshot_directory_fd(child_fd, display_root, relative))
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(metadata.st_mode):
            entries[str(relative)] = (
                "file",
                _read_regular_file_at(directory_fd, name, display_path),
            )
        else:
            raise ValueError(f"release source must contain regular files only: {display_path}")
    return entries


def _snapshot_source_release(
    source_root: pathlib.Path,
) -> dict[str, tuple[str, bytes | None]]:
    try:
        before = os.lstat(source_root)
    except OSError as error:
        raise ValueError("release source root must be a real directory") from error
    if not stat.S_ISDIR(before.st_mode):
        raise ValueError("release source root must be a real directory")
    try:
        source_fd = os.open(source_root, _DIRECTORY_OPEN_FLAGS)
    except OSError as error:
        raise ValueError("release source root must be a real directory") from error
    try:
        opened = os.fstat(source_fd)
        if not stat.S_ISDIR(opened.st_mode) or _identity(opened) != _identity(before):
            raise ValueError("release source root changed while opening")
        entries: dict[str, tuple[str, bytes | None]] = {}
        for name in _REQUIRED_DIRECTORIES:
            display_path = source_root / name
            child_fd = _open_source_directory_at(source_fd, name, display_path)
            try:
                entries[name] = ("directory", None)
                entries.update(_snapshot_directory_fd(child_fd, source_root, pathlib.Path(name)))
            finally:
                os.close(child_fd)
        for name in _REQUIRED_FILES:
            entries[name] = (
                "file",
                _read_regular_file_at(source_fd, name, source_root / name),
            )
        for name in _OPTIONAL_FILES:
            try:
                os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as error:
                raise ValueError(f"unsafe optional release file: {source_root / name}") from error
            entries[name] = (
                "file",
                _read_regular_file_at(source_fd, name, source_root / name),
            )
        return entries
    finally:
        os.close(source_fd)


def _validate_install_ancestor_chain(path: pathlib.Path) -> None:
    """Reject unsafe components, allowing only exact macOS canonical aliases."""
    absolute = path.absolute()
    current = pathlib.Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            break
        if stat.S_ISLNK(metadata.st_mode):
            expected = _TRUSTED_SYSTEM_ALIASES.get(current)
            if expected is None or metadata.st_uid != 0:
                raise ValueError(f"untrusted symlink in install path: {current}")
            try:
                resolved = current.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise ValueError(f"broken system alias in install path: {current}") from error
            if resolved != expected or not resolved.is_dir():
                raise ValueError(f"system alias target is not trusted: {current}")
        elif not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"non-directory in install path: {current}")


def _canonical_install_path(path: pathlib.Path) -> pathlib.Path:
    absolute = path.absolute()
    for alias, target in _TRUSTED_SYSTEM_ALIASES.items():
        try:
            suffix = absolute.relative_to(alias)
        except ValueError:
            continue
        return target / suffix
    return absolute


def _open_or_create_directory_chain(path: pathlib.Path) -> int:
    descriptor = os.open(path.anchor, _DIRECTORY_OPEN_FLAGS)
    try:
        for component in path.parts[1:]:
            try:
                child = os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                try:
                    child = os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
                except OSError as error:
                    raise ValueError(f"unsafe install path component: {component}") from error
            except OSError as error:
                raise ValueError(f"unsafe install path component: {component}") from error
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_install_layout(paths: RuntimePaths) -> tuple[pathlib.Path, int, pathlib.Path, int]:
    requested = pathlib.Path(paths.install_root)
    _validate_install_ancestor_chain(requested)
    install_root = _canonical_install_path(requested)
    install_fd = _open_or_create_directory_chain(install_root)
    try:
        os.fchmod(install_fd, 0o700)
        try:
            os.mkdir("releases", 0o700, dir_fd=install_fd)
        except FileExistsError:
            pass
        try:
            releases_fd = os.open("releases", _DIRECTORY_OPEN_FLAGS, dir_fd=install_fd)
        except OSError as error:
            raise ValueError("unsafe releases directory") from error
        try:
            os.fchmod(releases_fd, 0o700)
        except Exception:
            os.close(releases_fd)
            raise
    except Exception:
        os.close(install_fd)
        raise
    return install_root, install_fd, install_root / "releases", releases_fd


def _darwin_rename(
    source: pathlib.Path | str,
    destination: pathlib.Path | str,
    flags: int,
) -> None:
    """Invoke Darwin's flagged atomic rename and preserve its errno."""
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


def _darwin_renameat(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
    flags: int,
) -> None:
    """Invoke Darwin's directory-fd-anchored flagged atomic rename."""
    if sys.platform != "darwin":
        raise NotImplementedError("flagged atomic rename is only supported on Darwin")
    libc = ctypes.CDLL(None, use_errno=True)
    renameatx_np = libc.renameatx_np
    renameatx_np.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameatx_np.restype = ctypes.c_int
    result = renameatx_np(
        source_parent_fd,
        os.fsencode(source_name),
        destination_parent_fd,
        os.fsencode(destination_name),
        flags,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), source_name, destination_name)


def _atomic_rename_exclusive(
    source: pathlib.Path | _AnchoredPath,
    destination: pathlib.Path | _AnchoredPath,
) -> None:
    """Atomically rename without replacing any destination entry."""
    if sys.platform != "darwin":
        raise NotImplementedError("atomic no-replace install requires Darwin renamex_np")
    if isinstance(source, _AnchoredPath) and isinstance(destination, _AnchoredPath):
        _darwin_renameat(
            source.parent_fd,
            source.entry_name,
            destination.parent_fd,
            destination.entry_name,
            RENAME_EXCL,
        )
        return
    _darwin_rename(source, destination, RENAME_EXCL)


def _write_file_at(
    content: bytes,
    parent_fd: int,
    name: str,
    creation_ledger: dict[str, tuple[int, int, int]] | None = None,
    relative: pathlib.Path | None = None,
) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise FileExistsError(f"created release file is unsafe: {relative or name}")
        if creation_ledger is not None and relative is not None:
            creation_ledger[str(relative)] = _temporary_identity(opened)
        with os.fdopen(descriptor, "wb", closefd=False) as target:
            target.write(content)
            target.flush()
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _copy_release_content(
    source_snapshot: dict[str, tuple[str, bytes | None]],
    temporary_release: pathlib.Path | _AnchoredPath,
) -> None:
    if isinstance(temporary_release, _AnchoredPath):
        owns_temporary_fd = temporary_release.directory_fd is None
        temporary_fd = temporary_release.directory_fd
        if temporary_fd is None:
            temporary_fd = os.open(
                temporary_release.entry_name,
                _DIRECTORY_OPEN_FLAGS,
                dir_fd=temporary_release.parent_fd,
            )
        try:
            directory_fds: dict[pathlib.Path, int] = {pathlib.Path(): temporary_fd}
            try:
                for raw_relative, (entry_type, _) in sorted(
                    source_snapshot.items(),
                    key=lambda item: (len(pathlib.Path(item[0]).parts), item[0]),
                ):
                    if entry_type != "directory":
                        continue
                    relative = pathlib.Path(raw_relative)
                    parent_fd = directory_fds[relative.parent]
                    os.mkdir(relative.name, 0o700, dir_fd=parent_fd)
                    descriptor = os.open(
                        relative.name,
                        _DIRECTORY_OPEN_FLAGS,
                        dir_fd=parent_fd,
                    )
                    try:
                        opened = os.fstat(descriptor)
                        active = os.stat(
                            relative.name,
                            dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                    except Exception:
                        os.close(descriptor)
                        raise
                    if (
                        not stat.S_ISDIR(opened.st_mode)
                        or _temporary_identity(opened) != _temporary_identity(active)
                    ):
                        os.close(descriptor)
                        raise FileExistsError(f"created release directory changed: {relative}")
                    temporary_release.creation_ledger[str(relative)] = _temporary_identity(opened)
                    directory_fds[relative] = descriptor
                for raw_relative, (entry_type, content) in source_snapshot.items():
                    if entry_type != "file":
                        continue
                    if content is None:
                        raise ValueError(f"source snapshot file has no content: {raw_relative}")
                    relative = pathlib.Path(raw_relative)
                    _write_file_at(
                        content,
                        directory_fds[relative.parent],
                        relative.name,
                        temporary_release.creation_ledger,
                        relative,
                    )
            finally:
                for relative, descriptor in reversed(list(directory_fds.items())):
                    if relative != pathlib.Path():
                        os.close(descriptor)
        finally:
            if owns_temporary_fd:
                os.close(temporary_fd)
        return
    for raw_relative, (entry_type, content) in sorted(
        source_snapshot.items(),
        key=lambda item: (len(pathlib.Path(item[0]).parts), item[0]),
    ):
        destination = temporary_release / raw_relative
        if entry_type == "directory":
            destination.mkdir(mode=0o700)
        elif content is not None:
            destination.write_bytes(content)
            destination.chmod(0o600)


def _verify_and_make_private_release(
    release: _AnchoredPath,
    expected_entries: dict[str, tuple[str, bytes | None]],
) -> None:
    try:
        directory_fd = os.open(
            release.entry_name,
            _DIRECTORY_OPEN_FLAGS,
            dir_fd=release.parent_fd,
        )
    except OSError as error:
        raise FileExistsError(f"existing release path is unsafe: {release}") from error
    try:
        opened = os.fstat(directory_fd)
        identity = opened.st_dev, opened.st_ino
        _verify_and_make_private_fd(directory_fd, expected_entries)
        try:
            active = os.stat(
                release.entry_name,
                dir_fd=release.parent_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            raise FileExistsError(
                f"release {release.entry_name} changed during verification"
            ) from error
        if not stat.S_ISDIR(active.st_mode) or (active.st_dev, active.st_ino) != identity:
            raise FileExistsError(
                f"release {release.entry_name} changed during verification"
            )
    finally:
        os.close(directory_fd)


def _pin_release_entries_fd(
    directory_fd: int,
    pinned: list[tuple[int, int, str, tuple[int, int, int], bool]],
    prefix: pathlib.Path = pathlib.Path(),
) -> dict[str, tuple[str, bytes | None]]:
    entries: dict[str, tuple[str, bytes | None]] = {}
    for name in os.listdir(directory_fd):
        relative = prefix / name
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode):
            raise FileExistsError(f"release contains a symlink: {relative}")
        if stat.S_ISDIR(before.st_mode):
            descriptor = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=directory_fd)
            try:
                opened = os.fstat(descriptor)
            except Exception:
                os.close(descriptor)
                raise
            if _temporary_identity(opened) != _temporary_identity(before):
                os.close(descriptor)
                raise FileExistsError(f"release directory changed while opening: {relative}")
            pinned.append(
                (descriptor, directory_fd, name, _temporary_identity(opened), True)
            )
            entries[str(relative)] = ("directory", None)
            entries.update(_pin_release_entries_fd(descriptor, pinned, relative))
        elif stat.S_ISREG(before.st_mode):
            descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            try:
                opened = os.fstat(descriptor)
            except Exception:
                os.close(descriptor)
                raise
            if _temporary_identity(opened) != _temporary_identity(before):
                os.close(descriptor)
                raise FileExistsError(f"release file changed while opening: {relative}")
            pinned.append(
                (descriptor, directory_fd, name, _temporary_identity(opened), False)
            )
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                entries[str(relative)] = ("file", handle.read())
        else:
            raise FileExistsError(f"release contains an unsafe file: {relative}")
    return entries


def _release_entries_from_pinned_fds(
    directory_fd: int,
    pinned: list[tuple[int, int, str, tuple[int, int, int], bool]],
    prefix: pathlib.Path = pathlib.Path(),
) -> dict[str, tuple[str, bytes | None]]:
    root_metadata = os.fstat(directory_fd)
    if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_IMODE(root_metadata.st_mode) != 0o700:
        raise FileExistsError(f"release directory permissions changed: {prefix or '.'}")
    children = {
        name: (descriptor, expected_identity, is_directory)
        for descriptor, parent_fd, name, expected_identity, is_directory in pinned
        if parent_fd == directory_fd
    }
    if set(os.listdir(directory_fd)) != set(children):
        raise FileExistsError(f"release directory inventory changed: {prefix or '.'}")
    entries: dict[str, tuple[str, bytes | None]] = {}
    for name, (descriptor, expected_identity, is_directory) in children.items():
        relative = prefix / name
        try:
            active = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as error:
            raise FileExistsError(f"release entry changed after verification: {relative}") from error
        if _temporary_identity(active) != expected_identity:
            raise FileExistsError(f"release entry changed after verification: {relative}")
        opened = os.fstat(descriptor)
        expected_mode = 0o700 if is_directory else 0o600
        expected_type = stat.S_ISDIR if is_directory else stat.S_ISREG
        if (
            _temporary_identity(opened) != expected_identity
            or not expected_type(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != expected_mode
        ):
            raise FileExistsError(f"release entry permissions changed: {relative}")
        if is_directory:
            entries[str(relative)] = ("directory", None)
            entries.update(_release_entries_from_pinned_fds(descriptor, pinned, relative))
        else:
            os.lseek(descriptor, 0, os.SEEK_SET)
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                entries[str(relative)] = ("file", handle.read())
    return entries


def _verify_and_make_private_fd(
    directory_fd: int,
    expected_entries: dict[str, tuple[str, bytes | None]],
) -> None:
    pinned: list[tuple[int, int, str, tuple[int, int, int], bool]] = []
    try:
        actual_entries = _pin_release_entries_fd(directory_fd, pinned)
        if actual_entries != expected_entries:
            raise FileExistsError("release content is incomplete or inconsistent")
        os.fchmod(directory_fd, 0o700)
        for descriptor, _, _, _, is_directory in pinned:
            os.fchmod(descriptor, 0o700 if is_directory else 0o600)
        final_entries = _release_entries_from_pinned_fds(directory_fd, pinned)
        if final_entries != expected_entries:
            raise FileExistsError("release content changed while making permissions private")
    finally:
        for descriptor, _, _, _, _ in reversed(pinned):
            os.close(descriptor)


def _entry_exists(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _temporary_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)


def _delete_tree_contents_fd(
    directory_fd: int,
    creation_ledger: dict[str, tuple[int, int, int]],
    prefix: pathlib.Path = pathlib.Path(),
) -> None:
    children = {
        relative.name: (relative, identity)
        for raw_relative, identity in creation_ledger.items()
        for relative in (pathlib.Path(raw_relative),)
        if relative.parent == prefix
    }
    for name, (relative, expected_identity) in children.items():
        try:
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as error:
            raise TemporaryCleanupConflict(
                f"temporary cleanup conflict while inspecting {relative}"
            ) from error
        if _temporary_identity(before) != expected_identity:
            raise TemporaryCleanupConflict(
                f"temporary cleanup conflict while inspecting {relative}"
            )
        if stat.S_ISDIR(expected_identity[2]):
            try:
                child_fd = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=directory_fd)
            except OSError as error:
                raise TemporaryCleanupConflict(
                    f"temporary cleanup conflict while opening {relative}"
                ) from error
            try:
                if _temporary_identity(os.fstat(child_fd)) != expected_identity:
                    raise TemporaryCleanupConflict(
                        f"temporary cleanup conflict while opening {relative}"
                    )
                _delete_tree_contents_fd(child_fd, creation_ledger, relative)
            finally:
                os.close(child_fd)
            try:
                active = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as error:
                raise TemporaryCleanupConflict(
                    f"temporary cleanup conflict before removing {relative}"
                ) from error
            if _temporary_identity(active) != expected_identity:
                raise TemporaryCleanupConflict(
                    f"temporary cleanup conflict before removing {relative}"
                )
            try:
                os.rmdir(name, dir_fd=directory_fd)
            except OSError as error:
                raise TemporaryCleanupConflict(
                    f"temporary cleanup conflict before removing {relative}"
                ) from error
        else:
            try:
                active = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as error:
                raise TemporaryCleanupConflict(
                    f"temporary cleanup conflict before unlinking {relative}"
                ) from error
            if _temporary_identity(active) != expected_identity:
                raise TemporaryCleanupConflict(
                    f"temporary cleanup conflict before unlinking {relative}"
                )
            os.unlink(name, dir_fd=directory_fd)


def _temporary_tree_inventory_fd(
    directory_fd: int,
    prefix: pathlib.Path = pathlib.Path(),
) -> dict[str, tuple[int, int, int]]:
    inventory: dict[str, tuple[int, int, int]] = {}
    try:
        names = os.listdir(directory_fd)
    except OSError as error:
        raise TemporaryCleanupConflict("temporary cleanup conflict while listing contents") from error
    for name in names:
        relative = prefix / name
        try:
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as error:
            raise TemporaryCleanupConflict(
                f"temporary cleanup conflict while inspecting {relative}"
            ) from error
        identity = _temporary_identity(before)
        inventory[str(relative)] = identity
        if stat.S_ISDIR(before.st_mode):
            try:
                child_fd = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=directory_fd)
            except OSError as error:
                raise TemporaryCleanupConflict(
                    f"temporary cleanup conflict while opening {relative}"
                ) from error
            try:
                if _temporary_identity(os.fstat(child_fd)) != identity:
                    raise TemporaryCleanupConflict(
                        f"temporary cleanup conflict while opening {relative}"
                    )
                inventory.update(_temporary_tree_inventory_fd(child_fd, relative))
            finally:
                os.close(child_fd)
    return inventory


def _restore_cleanup_claim(parent_fd: int, quarantine_name: str, temporary_name: str) -> None:
    try:
        _darwin_renameat(
            parent_fd,
            quarantine_name,
            parent_fd,
            temporary_name,
            RENAME_EXCL,
        )
    except OSError as error:
        raise TemporaryCleanupConflict(
            f"temporary cleanup conflict; unknown entry preserved as {quarantine_name}"
        ) from error


def _cleanup_owned_temporary(
    parent_fd: int,
    temporary_name: str,
    created_identity: tuple[int, int, int],
    creation_ledger: dict[str, tuple[int, int, int]],
    directory_fd: int,
) -> None:
    while True:
        quarantine_name = f".cleanup-{uuid.uuid4().hex}"
        try:
            _darwin_renameat(
                parent_fd,
                temporary_name,
                parent_fd,
                quarantine_name,
                RENAME_EXCL,
            )
            break
        except FileExistsError:
            continue
        except OSError as error:
            raise TemporaryCleanupConflict(
                f"temporary cleanup conflict; {temporary_name} could not be claimed"
            ) from error

    try:
        claimed = os.stat(quarantine_name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        raise TemporaryCleanupConflict(
            f"temporary cleanup conflict; claimed entry {quarantine_name} disappeared"
        ) from error
    if _temporary_identity(claimed) != created_identity:
        _restore_cleanup_claim(parent_fd, quarantine_name, temporary_name)
        raise TemporaryCleanupConflict(
            f"temporary cleanup conflict; unknown entry restored as {temporary_name}"
        )

    if stat.S_ISDIR(claimed.st_mode):
        try:
            if _temporary_identity(os.fstat(directory_fd)) != created_identity:
                raise TemporaryCleanupConflict(
                    f"temporary cleanup conflict while opening {quarantine_name}"
                )
            if _temporary_tree_inventory_fd(directory_fd) != creation_ledger:
                raise TemporaryCleanupConflict(
                    f"temporary cleanup conflict; unknown contents preserved as {temporary_name}"
                )
            _delete_tree_contents_fd(directory_fd, creation_ledger)
            active = os.stat(quarantine_name, dir_fd=parent_fd, follow_symlinks=False)
            if _temporary_identity(active) != created_identity:
                raise TemporaryCleanupConflict(
                    f"temporary cleanup conflict before removing {quarantine_name}"
                )
            try:
                os.rmdir(quarantine_name, dir_fd=parent_fd)
            except OSError as error:
                raise TemporaryCleanupConflict(
                    f"temporary cleanup conflict before removing {quarantine_name}"
                ) from error
        except Exception:
            _restore_cleanup_claim(parent_fd, quarantine_name, temporary_name)
            raise
    else:
        try:
            active = os.stat(quarantine_name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise TemporaryCleanupConflict(
                f"temporary cleanup conflict before unlinking {quarantine_name}"
            ) from error
        if _temporary_identity(active) != created_identity:
            raise TemporaryCleanupConflict(
                f"temporary cleanup conflict before unlinking {quarantine_name}"
            )
        os.unlink(quarantine_name, dir_fd=parent_fd)


def _managed_current_matches(install_fd: int, link_text: str) -> bool:
    try:
        metadata = os.stat("current", dir_fd=install_fd, follow_symlinks=False)
        return stat.S_ISLNK(metadata.st_mode) and os.readlink("current", dir_fd=install_fd) == link_text
    except OSError:
        return False


def _activate_release(install_root: pathlib.Path, install_fd: int, version: str) -> None:
    link_text = str(pathlib.Path("releases") / version)
    try:
        os.symlink(link_text, "current", dir_fd=install_fd)
    except FileExistsError as error:
        if not _managed_current_matches(install_fd, link_text):
            raise FileExistsError("current exists with unknown or different content") from error


def install_runtime(source_root: pathlib.Path | str, paths: RuntimePaths) -> dict[str, str]:
    """Install *source_root* as a private versioned release and activate it."""
    if sys.platform != "darwin":
        raise NotImplementedError("runtime installation requires Darwin atomic rename support")
    source = pathlib.Path(source_root)
    expected_entries = _snapshot_source_release(source)
    manifest_content = expected_entries["release.json"][1]
    if manifest_content is None:
        raise ValueError("release manifest snapshot is missing content")
    manifest = json.loads(manifest_content.decode("utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("release manifest must contain a JSON object")
    version = _validate_manifest(manifest)
    install_root, install_fd, releases, releases_fd = _open_install_layout(paths)
    try:
        destination = _AnchoredPath(releases_fd, version, releases / version)
        if _entry_exists(releases_fd, version):
            _verify_and_make_private_release(destination, expected_entries)
            _activate_release(install_root, install_fd, version)
            return {"version": version}

        while True:
            temporary_name = f".{version}.tmp-{uuid.uuid4().hex}"
            try:
                os.mkdir(temporary_name, 0o700, dir_fd=releases_fd)
                break
            except FileExistsError:
                continue
        temporary_release = _AnchoredPath(
            releases_fd,
            temporary_name,
            releases / temporary_name,
        )
        temporary_fd: int | None = None
        temporary_identity: tuple[int, int, int] | None = None
        published = False
        try:
            temporary_fd = os.open(temporary_name, _DIRECTORY_OPEN_FLAGS, dir_fd=releases_fd)
            try:
                opened_temporary = os.fstat(temporary_fd)
            except Exception as error:
                raise TemporaryCleanupConflict(
                    f"temporary cleanup conflict; {temporary_name} ownership could not be established"
                ) from error
            if (
                not stat.S_ISDIR(opened_temporary.st_mode)
                or stat.S_IMODE(opened_temporary.st_mode) != 0o700
            ):
                raise TemporaryCleanupConflict(
                    f"temporary cleanup conflict; {temporary_name} is not the created empty directory"
                )
            temporary_identity = _temporary_identity(opened_temporary)
            if os.listdir(temporary_fd):
                raise TemporaryCleanupConflict(
                    f"temporary cleanup conflict; {temporary_name} is not the created empty directory"
                )
            temporary_release.directory_fd = temporary_fd
            _copy_release_content(expected_entries, temporary_release)
            try:
                _verify_and_make_private_fd(temporary_fd, expected_entries)
            except FileExistsError as error:
                raise ValueError("copied release content is incomplete or inconsistent") from error
            try:
                active_before_publish = os.stat(
                    temporary_name,
                    dir_fd=releases_fd,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise TemporaryCleanupConflict(
                    f"temporary cleanup conflict; {temporary_name} changed before publication"
                ) from error
            if _temporary_identity(active_before_publish) != temporary_identity:
                raise TemporaryCleanupConflict(
                    f"temporary cleanup conflict; {temporary_name} changed before publication"
                )
            try:
                _atomic_rename_exclusive(temporary_release, destination)
                published = True
            except FileExistsError as error:
                try:
                    _verify_and_make_private_release(destination, expected_entries)
                except FileExistsError as conflict:
                    raise conflict from error
            try:
                active_temporary = os.stat(
                    temporary_name,
                    dir_fd=releases_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                active_temporary = None
            if not published and (
                active_temporary is None
                or _temporary_identity(active_temporary) != temporary_identity
            ):
                raise TemporaryCleanupConflict(
                    f"temporary cleanup conflict; {temporary_name} changed before publication"
                )
        except TemporaryCleanupConflict:
            raise
        except Exception as error:
            if temporary_fd is None:
                raise TemporaryCleanupConflict(
                    f"temporary cleanup conflict; {temporary_name} could not be opened safely"
                ) from error
            raise
        finally:
            if temporary_fd is not None:
                try:
                    if not published and temporary_identity is not None:
                        _cleanup_owned_temporary(
                            releases_fd,
                            temporary_name,
                            temporary_identity,
                            temporary_release.creation_ledger,
                            temporary_fd,
                        )
                finally:
                    os.close(temporary_fd)

        _activate_release(install_root, install_fd, version)
        return {"version": version}
    finally:
        os.close(releases_fd)
        os.close(install_fd)


def _read_launch_agent_template() -> str:
    return _LAUNCH_AGENT_TEMPLATE.read_text(encoding="utf-8")


def _validate_launch_agent(plist: object, runtime: str, port: int) -> None:
    if not isinstance(plist, dict):
        raise ValueError("launch agent template must contain a dictionary")
    expected_arguments = ["/usr/bin/python3", runtime + "/scripts/memory_review_server.py"]
    expected_environment = {
        "MEMORY_REVIEW_HOST": "127.0.0.1",
        "MEMORY_REVIEW_PORT": str(port),
    }
    if plist.get("Label") != "com.noema.vibe-memory":
        raise ValueError("launch agent Label is invalid")
    if plist.get("ProgramArguments") != expected_arguments:
        raise ValueError("launch agent ProgramArguments are invalid")
    if plist.get("EnvironmentVariables") != expected_environment:
        raise ValueError("launch agent environment is invalid")
    if plist.get("KeepAlive") is not True or plist.get("RunAtLoad") is not True:
        raise ValueError("launch agent lifecycle settings are invalid")


def render_launch_agent(paths: RuntimePaths, port: int = 8897) -> str:
    """Render and validate the loopback-only LaunchAgent property list."""
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be an integer from 1 through 65535")
    runtime = str(pathlib.Path(paths.install_root) / "current")
    rendered = Template(_read_launch_agent_template()).substitute(
        RUNTIME=escape(runtime),
        PORT=str(port),
    )
    try:
        plist = plistlib.loads(rendered.encode("utf-8"))
    except Exception as error:
        raise ValueError("rendered launch agent is not a valid plist") from error
    _validate_launch_agent(plist, runtime, port)
    return rendered
