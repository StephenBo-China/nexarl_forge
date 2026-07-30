"""Install and render the versioned local Vibe Memory macOS runtime."""

from __future__ import annotations

import ctypes
import os
import pathlib
import plistlib
import re
import shutil
import stat
from string import Template
import sys
from typing import Any
import uuid
from xml.sax.saxutils import escape

from vibe_memory_paths import RuntimePaths, read_release_manifest


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

    def __init__(self, parent_fd: int, entry_name: str, display_path: pathlib.Path) -> None:
        self.parent_fd = parent_fd
        self.entry_name = entry_name
        self.display_path = display_path

    @property
    def name(self) -> str:
        return self.entry_name

    def __fspath__(self) -> str:
        return str(self.display_path)

    def __str__(self) -> str:
        return str(self.display_path)

    def __truediv__(self, child: str) -> pathlib.Path:
        return self.display_path / child


def _is_symlink(path: pathlib.Path) -> bool:
    try:
        return path.is_symlink()
    except OSError:
        return True


def _validate_manifest(manifest: dict[str, Any]) -> str:
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


def _validate_source_entry(path: pathlib.Path, *, directory: bool) -> None:
    if _is_symlink(path):
        raise ValueError(f"release source may not contain symlinks: {path}")
    if directory:
        if not path.is_dir():
            raise ValueError(f"required release directory is missing: {path.name}")
    elif not path.is_file():
        raise ValueError(f"required release file is missing: {path.name}")


def _validate_source_tree(source_root: pathlib.Path) -> None:
    if _is_symlink(source_root) or not source_root.is_dir():
        raise ValueError("release source root must be a real directory")
    for name in _REQUIRED_DIRECTORIES:
        _validate_source_entry(source_root / name, directory=True)
    for name in _REQUIRED_FILES:
        _validate_source_entry(source_root / name, directory=False)
    for name in _OPTIONAL_FILES:
        path = source_root / name
        if os.path.lexists(str(path)):
            _validate_source_entry(path, directory=False)

    selected = [source_root / name for name in _REQUIRED_DIRECTORIES]
    for tree_root in selected:
        for current, directory_names, file_names in os.walk(str(tree_root), followlinks=False):
            current_path = pathlib.Path(current)
            for name in directory_names:
                child = current_path / name
                if _is_symlink(child):
                    raise ValueError(f"release source may not contain symlinks: {child}")
            for name in file_names:
                child = current_path / name
                if _is_symlink(child) or not child.is_file():
                    raise ValueError(f"release source must contain regular files only: {child}")


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
    os.fchmod(install_fd, 0o700)
    try:
        try:
            os.mkdir("releases", 0o700, dir_fd=install_fd)
        except FileExistsError:
            pass
        try:
            releases_fd = os.open("releases", _DIRECTORY_OPEN_FLAGS, dir_fd=install_fd)
        except OSError as error:
            raise ValueError("unsafe releases directory") from error
        os.fchmod(releases_fd, 0o700)
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


def _copy_tree_at(source: pathlib.Path, parent_fd: int, name: str) -> None:
    os.mkdir(name, 0o700, dir_fd=parent_fd)
    directory_fd = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
    try:
        for child in source.iterdir():
            if child.is_symlink():
                raise ValueError(f"release source may not contain symlinks: {child}")
            if child.is_dir():
                _copy_tree_at(child, directory_fd, child.name)
            elif child.is_file():
                _copy_file_at(child, directory_fd, child.name)
            else:
                raise ValueError(f"release source must contain regular files only: {child}")
    finally:
        os.close(directory_fd)


def _copy_file_at(source: pathlib.Path, parent_fd: int, name: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
    try:
        with source.open("rb") as source_handle, os.fdopen(descriptor, "wb", closefd=False) as target:
            shutil.copyfileobj(source_handle, target)
            target.flush()
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _copy_release_content(
    source_root: pathlib.Path,
    temporary_release: pathlib.Path | _AnchoredPath,
) -> None:
    if isinstance(temporary_release, _AnchoredPath):
        temporary_fd = os.open(
            temporary_release.entry_name,
            _DIRECTORY_OPEN_FLAGS,
            dir_fd=temporary_release.parent_fd,
        )
        try:
            for name in _REQUIRED_DIRECTORIES:
                _copy_tree_at(source_root / name, temporary_fd, name)
            for name in (*_REQUIRED_FILES, *_OPTIONAL_FILES):
                source = source_root / name
                if source.is_file():
                    _copy_file_at(source, temporary_fd, name)
        finally:
            os.close(temporary_fd)
        return
    for name in _REQUIRED_DIRECTORIES:
        shutil.copytree(str(source_root / name), str(temporary_release / name))
    for name in (*_REQUIRED_FILES, *_OPTIONAL_FILES):
        source = source_root / name
        if source.is_file():
            shutil.copy2(str(source), str(temporary_release / name))


def _make_private_fd(directory_fd: int) -> None:
    os.fchmod(directory_fd, 0o700)
    for name in os.listdir(directory_fd):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            raise FileExistsError(f"installed release contains a symlink: {name}")
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=directory_fd)
            try:
                _make_private_fd(child_fd)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(metadata.st_mode):
            file_fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
            try:
                os.fchmod(file_fd, 0o600)
            finally:
                os.close(file_fd)
        else:
            raise FileExistsError(f"installed release contains an unsafe file: {name}")


def _make_private(tree: pathlib.Path | _AnchoredPath) -> None:
    if isinstance(tree, _AnchoredPath):
        directory_fd = os.open(tree.entry_name, _DIRECTORY_OPEN_FLAGS, dir_fd=tree.parent_fd)
        try:
            _make_private_fd(directory_fd)
        finally:
            os.close(directory_fd)
        return
    tree.chmod(0o700)
    for current, directory_names, file_names in os.walk(str(tree), followlinks=False):
        current_path = pathlib.Path(current)
        for name in directory_names:
            (current_path / name).chmod(0o700)
        for name in file_names:
            (current_path / name).chmod(0o600)


def _release_entries_fd(
    directory_fd: int,
    prefix: pathlib.Path = pathlib.Path(),
) -> dict[str, tuple[str, bytes | None]]:
    entries: dict[str, tuple[str, bytes | None]] = {}
    for name in os.listdir(directory_fd):
        relative = prefix / name
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            raise FileExistsError(f"existing release contains a symlink: {relative}")
        if stat.S_ISDIR(metadata.st_mode):
            entries[str(relative)] = ("directory", None)
            child_fd = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=directory_fd)
            try:
                entries.update(_release_entries_fd(child_fd, relative))
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(metadata.st_mode):
            file_fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
            try:
                with os.fdopen(file_fd, "rb", closefd=False) as handle:
                    entries[str(relative)] = ("file", handle.read())
            finally:
                os.close(file_fd)
        else:
            raise FileExistsError(f"existing release contains an unsafe file: {relative}")
    return entries


def _release_entries(
    root: pathlib.Path | _AnchoredPath,
) -> dict[str, tuple[str, bytes | None]]:
    if isinstance(root, _AnchoredPath):
        try:
            directory_fd = os.open(root.entry_name, _DIRECTORY_OPEN_FLAGS, dir_fd=root.parent_fd)
        except OSError as error:
            raise FileExistsError(f"existing release path is unsafe: {root}") from error
        try:
            return _release_entries_fd(directory_fd)
        finally:
            os.close(directory_fd)
    entries: dict[str, tuple[str, bytes | None]] = {}
    if _is_symlink(root) or not root.is_dir():
        raise FileExistsError(f"existing release path is unsafe: {root}")
    for current, directory_names, file_names in os.walk(str(root), followlinks=False):
        current_path = pathlib.Path(current)
        for name in directory_names:
            child = current_path / name
            if _is_symlink(child):
                raise FileExistsError(f"existing release contains a symlink: {child}")
            entries[str(child.relative_to(root))] = ("directory", None)
        for name in file_names:
            child = current_path / name
            if _is_symlink(child) or not child.is_file():
                raise FileExistsError(f"existing release contains an unsafe file: {child}")
            entries[str(child.relative_to(root))] = ("file", child.read_bytes())
    return entries


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
        if _release_entries_fd(directory_fd) != expected_entries:
            raise FileExistsError(
                f"release {release.entry_name} already exists with different content"
            )
        _make_private_fd(directory_fd)
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


def _source_entries(source_root: pathlib.Path) -> dict[str, tuple[str, bytes | None]]:
    entries: dict[str, tuple[str, bytes | None]] = {}
    for name in _REQUIRED_DIRECTORIES:
        tree_root = source_root / name
        entries[name] = ("directory", None)
        for current, directory_names, file_names in os.walk(str(tree_root), followlinks=False):
            current_path = pathlib.Path(current)
            for child_name in directory_names:
                child = current_path / child_name
                entries[str(child.relative_to(source_root))] = ("directory", None)
            for child_name in file_names:
                child = current_path / child_name
                entries[str(child.relative_to(source_root))] = ("file", child.read_bytes())
    for name in (*_REQUIRED_FILES, *_OPTIONAL_FILES):
        child = source_root / name
        if child.is_file():
            entries[name] = ("file", child.read_bytes())
    return entries


def _entry_exists(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _managed_current_matches(install_fd: int, link_text: str) -> bool:
    try:
        metadata = os.stat("current", dir_fd=install_fd, follow_symlinks=False)
        return stat.S_ISLNK(metadata.st_mode) and os.readlink("current", dir_fd=install_fd) == link_text
    except OSError:
        return False


def _activate_release(install_root: pathlib.Path, install_fd: int, version: str) -> None:
    link_text = str(pathlib.Path("releases") / version)
    if _entry_exists(install_fd, "current"):
        if _managed_current_matches(install_fd, link_text):
            return
        raise FileExistsError("current already exists and is not the requested managed link")
    while True:
        temporary_name = f".current.tmp-{uuid.uuid4().hex}"
        try:
            os.symlink(link_text, temporary_name, dir_fd=install_fd)
            break
        except FileExistsError:
            continue
    temporary_link = _AnchoredPath(install_fd, temporary_name, install_root / temporary_name)
    current = _AnchoredPath(install_fd, "current", install_root / "current")
    try:
        _atomic_rename_exclusive(temporary_link, current)
    except FileExistsError as error:
        if not _managed_current_matches(install_fd, link_text):
            raise FileExistsError("current appeared concurrently with unknown content") from error


def install_runtime(source_root: pathlib.Path | str, paths: RuntimePaths) -> dict[str, str]:
    """Install *source_root* as a private versioned release and activate it."""
    if sys.platform != "darwin":
        raise NotImplementedError("runtime installation requires Darwin atomic rename support")
    source = pathlib.Path(source_root)
    _validate_source_tree(source)
    manifest = read_release_manifest(source / "release.json")
    version = _validate_manifest(manifest)
    expected_entries = _source_entries(source)
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
        temporary_fd = os.open(temporary_name, _DIRECTORY_OPEN_FLAGS, dir_fd=releases_fd)
        try:
            _copy_release_content(source, temporary_release)
            if _release_entries_fd(temporary_fd) != expected_entries:
                raise ValueError("copied release content is incomplete or inconsistent")
            _make_private_fd(temporary_fd)
            try:
                _atomic_rename_exclusive(temporary_release, destination)
            except FileExistsError as error:
                try:
                    _verify_and_make_private_release(destination, expected_entries)
                except FileExistsError as conflict:
                    raise conflict from error
        except Exception:
            _make_private_fd(temporary_fd)
            raise
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
