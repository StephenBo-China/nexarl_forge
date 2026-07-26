"""Safe source adapters for UI skill draft imports."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable
from typing import Any


DEFAULT_MAX_FILES = 2_000
DEFAULT_MAX_BYTES = 100 * 1024 * 1024

FRONTEND_DESIGN_REPOSITORY = "anthropics/skills"
FRONTEND_DESIGN_PATH = "skills/frontend-design"
FRONTEND_DESIGN_REVISION = "b29e7cf65e5cb78a5ac33d582270551bc74a14eb"

UI_UX_PRO_MAX_REPOSITORY = "nextlevelbuilder/ui-ux-pro-max-skill"
UI_UX_PRO_MAX_RELEASE = "v2.11.0"
UI_UX_PRO_MAX_REVISION = "6142b073958df645d0fb27e682428e69599386dc"
UI_UX_PRO_MAX_CLI_PACKAGE = "ui-ux-pro-max-cli"
UI_UX_PRO_MAX_CLI_VERSION = "2.11.0"
UI_UX_PRO_MAX_NPM_SHASUM = "2ff4d811cf1dded593b9d1f37bad65ffa80cb87c"


class SourceError(ValueError):
    pass


def _safe_relative(value: str) -> pathlib.PurePosixPath:
    path = pathlib.PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise SourceError(f"unsafe package path: {value}")
    return path


def _check_destination(destination: pathlib.Path) -> pathlib.Path:
    destination = pathlib.Path(destination)
    if destination.exists():
        raise SourceError(f"import destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def _validate_source_tree(
    root: pathlib.Path,
    *,
    max_files: int,
    max_bytes: int,
) -> None:
    root = pathlib.Path(root)
    if not root.is_dir() or not (root / "SKILL.md").is_file():
        raise SourceError(f"skill root must contain SKILL.md: {root}")
    count = 0
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            raise SourceError(f"symlinks are not allowed: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise SourceError(f"non-regular file is not allowed: {path}")
        count += 1
        total += path.stat().st_size
    if count > max_files:
        raise SourceError(f"package file count {count} exceeds limit {max_files}")
    if total > max_bytes:
        raise SourceError(f"package size {total} exceeds limit {max_bytes}")


def _copy_tree_atomically(
    source: pathlib.Path,
    destination: pathlib.Path,
    *,
    max_files: int,
    max_bytes: int,
) -> None:
    destination = _check_destination(destination)
    _validate_source_tree(source, max_files=max_files, max_bytes=max_bytes)
    stage_parent = pathlib.Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.import-", dir=destination.parent)
    )
    stage = stage_parent / "content"
    try:
        shutil.copytree(source, stage)
        _validate_source_tree(stage, max_files=max_files, max_bytes=max_bytes)
        os.replace(stage, destination)
    finally:
        shutil.rmtree(stage_parent, ignore_errors=True)


def import_local(
    source: pathlib.Path,
    destination: pathlib.Path,
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    source = pathlib.Path(source)
    _copy_tree_atomically(
        source, destination, max_files=max_files, max_bytes=max_bytes
    )
    return {"type": "local", "path": str(source.resolve())}


def _zip_member_kind(info: zipfile.ZipInfo) -> int:
    return stat.S_IFMT(info.external_attr >> 16)


def _validate_zip(
    handle: zipfile.ZipFile,
    *,
    max_files: int,
    max_bytes: int,
) -> list[tuple[zipfile.ZipInfo, pathlib.PurePosixPath]]:
    members: list[tuple[zipfile.ZipInfo, pathlib.PurePosixPath]] = []
    count = 0
    total = 0
    for info in handle.infolist():
        relative = _safe_relative(info.filename)
        kind = _zip_member_kind(info)
        if stat.S_ISLNK(kind):
            raise SourceError(f"ZIP symlink is not allowed: {info.filename}")
        if kind and not (stat.S_ISREG(kind) or stat.S_ISDIR(kind)):
            raise SourceError(f"ZIP special file is not allowed: {info.filename}")
        if not info.is_dir():
            count += 1
            total += info.file_size
        members.append((info, relative))
    if count > max_files:
        raise SourceError(f"ZIP file count {count} exceeds limit {max_files}")
    if total > max_bytes:
        raise SourceError(f"ZIP size {total} exceeds limit {max_bytes}")
    return members


def _extract_validated_zip(
    archive: pathlib.Path,
    destination: pathlib.Path,
    *,
    max_files: int,
    max_bytes: int,
) -> None:
    with zipfile.ZipFile(archive) as handle:
        members = _validate_zip(handle, max_files=max_files, max_bytes=max_bytes)
        destination.mkdir(parents=True, exist_ok=False)
        for info, relative in members:
            target = destination.joinpath(*relative.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with handle.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def import_zip(
    archive: pathlib.Path,
    destination: pathlib.Path,
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    archive = pathlib.Path(archive)
    destination = _check_destination(destination)
    if not archive.is_file():
        raise SourceError(f"ZIP archive does not exist: {archive}")
    with tempfile.TemporaryDirectory(prefix="ui-skill-zip-") as value:
        extracted = pathlib.Path(value) / "extracted"
        try:
            _extract_validated_zip(
                archive, extracted, max_files=max_files, max_bytes=max_bytes
            )
        except (zipfile.BadZipFile, OSError) as error:
            raise SourceError(f"invalid ZIP archive: {error}") from error
        roots = sorted(path.parent for path in extracted.rglob("SKILL.md") if path.is_file())
        if len(roots) != 1:
            raise SourceError(f"ZIP must contain exactly one SKILL.md root; found {len(roots)}")
        _copy_tree_atomically(
            roots[0], destination, max_files=max_files, max_bytes=max_bytes
        )
    return {"type": "zip", "path": str(archive.resolve())}


def import_editor(
    files: dict[str, str | bytes],
    destination: pathlib.Path,
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    destination = _check_destination(destination)
    if len(files) > max_files:
        raise SourceError(f"editor file count {len(files)} exceeds limit {max_files}")
    normalized: list[tuple[pathlib.PurePosixPath, bytes]] = []
    total = 0
    for name, content in files.items():
        relative = _safe_relative(name)
        data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        total += len(data)
        normalized.append((relative, data))
    if total > max_bytes:
        raise SourceError(f"editor size {total} exceeds limit {max_bytes}")
    with tempfile.TemporaryDirectory(prefix="ui-skill-editor-") as value:
        root = pathlib.Path(value) / "content"
        root.mkdir()
        for relative, data in normalized:
            target = root.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        _copy_tree_atomically(root, destination, max_files=max_files, max_bytes=max_bytes)
    return {"type": "editor"}


def _download_github(request: dict[str, str], target: pathlib.Path) -> None:
    url = f"https://github.com/{request['repository']}/archive/{request['revision']}.zip"
    with tempfile.TemporaryDirectory(prefix="ui-skill-github-") as value:
        archive = pathlib.Path(value) / "repository.zip"
        with urllib.request.urlopen(url, timeout=30) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        extracted = pathlib.Path(value) / "extracted"
        _extract_validated_zip(
            archive,
            extracted,
            max_files=DEFAULT_MAX_FILES * 10,
            max_bytes=DEFAULT_MAX_BYTES * 5,
        )
        top_levels = [path for path in extracted.iterdir() if path.is_dir()]
        if len(top_levels) != 1:
            raise SourceError("GitHub archive has an unexpected root layout")
        candidate = top_levels[0].joinpath(*_safe_relative(request["path"]).parts)
        shutil.copytree(candidate, target)


def import_github(
    repository: str,
    skill_path: str,
    revision: str,
    destination: pathlib.Path,
    *,
    downloader: Callable[[dict[str, str], pathlib.Path], None] | None = None,
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    if not revision.strip():
        raise SourceError("GitHub import requires a pinned revision")
    if len(repository.split("/")) != 2 or any(not part for part in repository.split("/")):
        raise SourceError(f"invalid GitHub repository: {repository}")
    _safe_relative(skill_path)
    destination = _check_destination(destination)
    request = {"repository": repository, "path": skill_path, "revision": revision}
    with tempfile.TemporaryDirectory(prefix="ui-skill-github-stage-") as value:
        downloaded = pathlib.Path(value) / "downloaded"
        (downloader or _download_github)(request, downloaded)
        _copy_tree_atomically(
            downloaded, destination, max_files=max_files, max_bytes=max_bytes
        )
    return {
        "type": "github",
        "repository": repository,
        "path": skill_path,
        "revision": revision,
    }


def _require_pin(label: str, actual: str, expected: str) -> None:
    if actual != expected:
        raise SourceError(f"{label} must be pinned to {expected}; received {actual}")


def bootstrap_frontend_design(
    destination: pathlib.Path,
    *,
    revision: str,
    downloader: Callable[[dict[str, str], pathlib.Path], None] | None = None,
) -> dict[str, Any]:
    """Stage the reviewed Anthropic frontend-design revision as one common variant."""
    _require_pin("frontend-design revision", revision, FRONTEND_DESIGN_REVISION)
    source = import_github(
        FRONTEND_DESIGN_REPOSITORY,
        FRONTEND_DESIGN_PATH,
        revision,
        destination,
        downloader=downloader,
    )
    source.update(
        {
            "type": "bootstrap",
            "source_type": "github",
            "url": (
                f"https://github.com/{FRONTEND_DESIGN_REPOSITORY}/tree/"
                f"{revision}/{FRONTEND_DESIGN_PATH}"
            ),
            "variants": {
                "common": {"path": ".", "digest": _tree_digest(destination)}
            },
        }
    )
    return source


def _tree_digest(root: pathlib.Path) -> str:
    import ui_design_store as store

    return store.tree_digest(pathlib.Path(root))


def _npm_metadata(package: str, version: str) -> dict[str, Any]:
    url = f"https://registry.npmjs.org/{package}/{version}"
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def _run_ui_ux_generator(request: dict[str, str], target: pathlib.Path) -> dict[str, Any]:
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "npx",
        "--yes",
        f"{request['package']}@{request['cli_version']}",
        "init",
        "--ai",
        request["agent"],
        "--output",
        str(target),
    ]
    completed = subprocess.run(
        command,
        cwd=target.parent,
        check=True,
        capture_output=True,
        text=True,
    )
    return {
        "command": command,
        "stdout_summary": completed.stdout[-2_000:],
    }


def bootstrap_ui_ux_pro_max(
    destination: pathlib.Path,
    *,
    release: str,
    revision: str,
    cli_version: str,
    expected_npm_shasum: str,
    npm_metadata: Callable[[str, str], dict[str, Any]] | None = None,
    runner: Callable[[dict[str, str], pathlib.Path], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Generate immutable Codex and Claude variants using reviewed upstream pins."""
    _require_pin("UI UX Pro Max release", release, UI_UX_PRO_MAX_RELEASE)
    _require_pin("UI UX Pro Max revision", revision, UI_UX_PRO_MAX_REVISION)
    _require_pin("UI UX Pro Max CLI version", cli_version, UI_UX_PRO_MAX_CLI_VERSION)
    _require_pin(
        "UI UX Pro Max npm shasum",
        expected_npm_shasum,
        UI_UX_PRO_MAX_NPM_SHASUM,
    )
    metadata = (npm_metadata or _npm_metadata)(UI_UX_PRO_MAX_CLI_PACKAGE, cli_version)
    actual_shasum = metadata.get("dist", {}).get("shasum")
    if actual_shasum != expected_npm_shasum:
        raise SourceError(
            "UI UX Pro Max npm metadata shasum mismatch: "
            f"expected {expected_npm_shasum}, received {actual_shasum}"
        )

    destination = _check_destination(destination)
    generate = runner or _run_ui_ux_generator
    with tempfile.TemporaryDirectory(prefix="ui-ux-pro-max-generate-") as value:
        generated_root = pathlib.Path(value) / "bundle"
        variants_root = generated_root / "variants"
        variants: dict[str, dict[str, str]] = {}
        generation: dict[str, dict[str, Any]] = {}
        for agent in ("codex", "claude"):
            target = variants_root / agent
            request = {
                "agent": agent,
                "package": UI_UX_PRO_MAX_CLI_PACKAGE,
                "cli_version": cli_version,
                "release": release,
                "revision": revision,
            }
            result = generate(request, target)
            _validate_source_tree(
                target, max_files=DEFAULT_MAX_FILES, max_bytes=DEFAULT_MAX_BYTES
            )
            variants[agent] = {
                "path": f"variants/{agent}",
                "digest": _tree_digest(target),
            }
            generation[agent] = {
                "command": list(result.get("command", [])),
                "stdout_summary": str(result.get("stdout_summary", ""))[-2_000:],
            }
        generated_root.mkdir(parents=True, exist_ok=True)
        (generated_root / "SKILL.md").write_text(
            "---\n"
            "name: ui-ux-pro-max\n"
            "description: Use when reviewing the generated Codex and Claude UI UX Pro Max variants.\n"
            "---\n\n"
            "# UI UX Pro Max\n\n"
            "This approval bundle contains pinned, agent-specific generated variants.\n",
            encoding="utf-8",
        )
        _copy_tree_atomically(
            generated_root,
            destination,
            max_files=DEFAULT_MAX_FILES,
            max_bytes=DEFAULT_MAX_BYTES,
        )

    return {
        "type": "bootstrap",
        "source_type": "github+npm",
        "repository": UI_UX_PRO_MAX_REPOSITORY,
        "url": (
            f"https://github.com/{UI_UX_PRO_MAX_REPOSITORY}/tree/{revision}"
        ),
        "release": release,
        "revision": revision,
        "cli_package": UI_UX_PRO_MAX_CLI_PACKAGE,
        "cli_version": cli_version,
        "npm_shasum": expected_npm_shasum,
        "variants": variants,
        "generation": generation,
    }
