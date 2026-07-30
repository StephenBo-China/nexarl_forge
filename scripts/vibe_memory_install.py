"""Install and render the versioned local Vibe Memory macOS runtime."""

from __future__ import annotations

import os
import pathlib
import plistlib
import re
import shutil
from string import Template
import tempfile
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


def _ensure_private_directory(path: pathlib.Path, *, parents: bool = False) -> None:
    if not os.path.lexists(str(path)):
        try:
            path.mkdir(mode=0o700, parents=parents)
        except FileExistsError:
            pass
    if _is_symlink(path) or not path.is_dir():
        raise ValueError(f"unsafe install directory: {path}")
    path.chmod(0o700)


def _validate_install_layout(paths: RuntimePaths) -> pathlib.Path:
    install_root = pathlib.Path(paths.install_root)
    for ancestor in tuple(install_root.parents)[:3]:
        if os.path.lexists(str(ancestor)) and (_is_symlink(ancestor) or not ancestor.is_dir()):
            raise ValueError(f"unsafe install path component: {ancestor}")
    _ensure_private_directory(install_root, parents=True)
    releases = install_root / "releases"
    _ensure_private_directory(releases)
    current = install_root / "current"
    if os.path.lexists(str(current)) and not current.is_symlink():
        raise ValueError("current must be a symlink when it already exists")
    return releases


def _copy_release_content(source_root: pathlib.Path, temporary_release: pathlib.Path) -> None:
    for name in _REQUIRED_DIRECTORIES:
        shutil.copytree(str(source_root / name), str(temporary_release / name))
    for name in (*_REQUIRED_FILES, *_OPTIONAL_FILES):
        source = source_root / name
        if source.is_file():
            shutil.copy2(str(source), str(temporary_release / name))


def _make_private(tree: pathlib.Path) -> None:
    tree.chmod(0o700)
    for current, directory_names, file_names in os.walk(str(tree), followlinks=False):
        current_path = pathlib.Path(current)
        for name in directory_names:
            (current_path / name).chmod(0o700)
        for name in file_names:
            (current_path / name).chmod(0o600)


def _release_entries(root: pathlib.Path) -> dict[str, tuple[str, bytes | None]]:
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


def _activate_release(install_root: pathlib.Path, version: str) -> None:
    current = install_root / "current"
    if os.path.lexists(str(current)) and not current.is_symlink():
        raise ValueError("current must be a symlink when it already exists")
    temporary_link = install_root / f".current.tmp-{uuid.uuid4().hex}"
    try:
        os.symlink(str(pathlib.Path("releases") / version), str(temporary_link))
        os.replace(str(temporary_link), str(current))
    finally:
        if os.path.lexists(str(temporary_link)):
            temporary_link.unlink()


def install_runtime(source_root: pathlib.Path | str, paths: RuntimePaths) -> dict[str, str]:
    """Install *source_root* as a private versioned release and activate it."""
    source = pathlib.Path(source_root)
    _validate_source_tree(source)
    manifest = read_release_manifest(source / "release.json")
    version = _validate_manifest(manifest)
    expected_entries = _source_entries(source)
    releases = _validate_install_layout(paths)
    destination = releases / version

    if os.path.lexists(str(destination)):
        if _release_entries(destination) != expected_entries:
            raise FileExistsError(f"release {version} already exists with different content")
        _make_private(destination)
        _activate_release(pathlib.Path(paths.install_root), version)
        return {"version": version}

    temporary_release = pathlib.Path(tempfile.mkdtemp(prefix=f".{version}.tmp-", dir=str(releases)))
    try:
        _copy_release_content(source, temporary_release)
        if _release_entries(temporary_release) != expected_entries:
            raise ValueError("copied release content is incomplete or inconsistent")
        _make_private(temporary_release)
        try:
            os.rename(str(temporary_release), str(destination))
        except OSError as error:
            if not os.path.lexists(str(destination)):
                raise
            if _release_entries(destination) != expected_entries:
                raise FileExistsError(
                    f"release {version} already exists with different content"
                ) from error
    finally:
        if os.path.lexists(str(temporary_release)):
            shutil.rmtree(str(temporary_release))

    _activate_release(pathlib.Path(paths.install_root), version)
    return {"version": version}


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
