"""Scan the distributable tree for private-path and secret hygiene issues."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
from collections.abc import Iterable

import vibe_memory_install

_ROOT_FILES = (*vibe_memory_install._REQUIRED_FILES, *vibe_memory_install._OPTIONAL_FILES)
_RELEASE_DIRECTORIES = vibe_memory_install._REQUIRED_DIRECTORIES
_LOCAL_CLIENT_RUNTIME_CONFIGS = frozenset({".codex/hooks.json", ".claude/settings.json"})
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_IEND = b"\x00\x00\x00\x00IEND\xaeB`\x82"
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("personal_path", re.compile(r"/U" + r"sers/")),
    (
        "credential_assignment",
        re.compile(
            r"(?i)\b(?:api[-_ ]?key|access[-_ ]?key|secret|password|token)\b\s*[:=]\s*(?:['\"][A-Za-z0-9+/=_-]{12,}['\"]?|[A-Za-z0-9+/=_-]{12,})"
        ),
    ),
    (
        "verification_code_assignment",
        re.compile(
            r"(?i)\bverification[_-]?code\b\s*[:=]\s*(?:['\"]?\d{4,8}['\"]?|[A-Za-z0-9+/=_-]{4,8})"
        ),
    ),
    (
        "private_memory_heading",
        re.compile(
            r"(?im)^#{1,6}\s+Personal Codex (?:Long|Short) Memory\b|^#{1,6}\s+Approved Memories\b"
        ),
    ),
)


def _file_candidates(root: pathlib.Path) -> Iterable[pathlib.Path]:
    for name in _ROOT_FILES:
        candidate = root / name
        if candidate.is_symlink() or candidate.is_file():
            yield candidate

    for name in _RELEASE_DIRECTORIES:
        directory = root / name
        if directory.is_symlink():
            yield directory
        elif directory.is_dir():
            yield from _iter_release_tree(
                directory, root_relative=pathlib.PurePosixPath(name)
            )


def _iter_release_tree(
    directory: pathlib.Path,
    *,
    root_relative: pathlib.PurePosixPath,
) -> Iterable[pathlib.Path]:
    """Walk release assets without following directory symlinks."""
    for path in sorted(directory.iterdir()):
        relative = root_relative / path.name
        if vibe_memory_install._is_release_path_excluded(relative):
            continue
        if path.is_symlink():
            yield path
        elif path.is_dir():
            yield from _iter_release_tree(path, root_relative=relative)
        elif path.is_file():
            yield path


def _client_asset_candidates(root: pathlib.Path) -> Iterable[pathlib.Path]:
    tracked = _tracked_client_assets(root)
    candidates = tracked if tracked is not None else _client_assets_without_git(root)
    yield from (path for path in candidates if not _is_local_client_runtime_config(path, root))


def _tracked_client_assets(root: pathlib.Path) -> list[pathlib.Path] | None:
    try:
        top_level = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            check=False,
        )
        if top_level.returncode:
            return None
        discovered_root = pathlib.Path(os.fsdecode(top_level.stdout.rstrip(b"\n"))).resolve()
        if discovered_root != root:
            return None
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--", ".claude", ".codex"],
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode:
        return None
    return [
        root / os.fsdecode(path)
        for path in result.stdout.split(b"\0")
        if path
        and (
            (root / os.fsdecode(path)).is_file()
            or (root / os.fsdecode(path)).is_symlink()
        )
    ]


def _client_assets_without_git(root: pathlib.Path) -> list[pathlib.Path]:
    candidates: list[pathlib.Path] = []
    for directory in (root / ".claude", root / ".codex"):
        if directory.is_symlink():
            candidates.append(directory)
            continue
        if directory.is_dir():
            candidates.extend(
                path
                for path in sorted(directory.rglob("*"))
                if path.is_file() or path.is_symlink()
            )
    return candidates


def _is_local_client_runtime_config(path: pathlib.Path, root: pathlib.Path) -> bool:
    return path.relative_to(root).as_posix() in _LOCAL_CLIENT_RUNTIME_CONFIGS


def _display_path(path: pathlib.Path, root: pathlib.Path) -> str:
    """Return a root-relative POSIX path without exposing local absolutes."""
    try:
        # ``absolute`` normalizes ``..`` lexically without following symlinks,
        # so client-asset symlink diagnostics still point at the link itself.
        absolute = pathlib.Path(os.path.abspath(os.fspath(path)))
        return absolute.relative_to(root).as_posix()
    except (OSError, ValueError):
        # A path outside the scanned tree must fail closed without leaking the
        # caller's local root or home directory.
        name = pathlib.Path(path).name or "unknown"
        return f"<outside-root>/{name}"


def _release_asset_safety_violation(
    path: pathlib.Path, root: pathlib.Path
) -> dict[str, str] | None:
    try:
        lexical = pathlib.Path(os.path.abspath(os.fspath(path)))
        relative = lexical.relative_to(root)
    except (OSError, ValueError):
        return {
            "path": _display_path(path, root),
            "pattern": "release_asset_outside_root",
            "match": "release asset is outside the scanned root",
        }

    current = root
    try:
        for component in relative.parts:
            current /= component
            if current.is_symlink():
                return {
                    "path": _display_path(path, root),
                    "pattern": "release_asset_symlink",
                    "match": "symlink release asset is not allowed",
                }
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except ValueError:
        return {
            "path": _display_path(path, root),
            "pattern": "release_asset_outside_root",
            "match": "release asset is outside the scanned root",
        }
    except OSError:
        return {
            "path": _display_path(path, root),
            "pattern": "release_asset_unreadable",
            "match": "release asset could not be safely resolved",
        }
    return None


def _scan_content(
    text: str, path: pathlib.Path, root: pathlib.Path
) -> list[dict[str, str]]:
    violations: list[dict[str, str]] = []
    for name, pattern in _PATTERNS:
        match = pattern.search(text)
        if match:
            violations.append(
                {
                    "path": _display_path(path, root),
                    "pattern": name,
                    "match": (
                        "[redacted]"
                        if name in {"credential_assignment", "verification_code_assignment"}
                        else match.group(0)
                    ),
                }
            )
    return violations


def _scan_text(path: pathlib.Path, root: pathlib.Path) -> list[dict[str, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return [
            {
                "path": _display_path(path, root),
                "pattern": "unreadable",
                "match": type(error).__name__,
            }
        ]
    return _scan_content(text, path, root)


def _scan_png(path: pathlib.Path, root: pathlib.Path) -> list[dict[str, str]]:
    try:
        content = path.read_bytes()
    except OSError as error:
        return [
            {
                "path": _display_path(path, root),
                "pattern": "unreadable",
                "match": type(error).__name__,
            }
        ]
    if not content.startswith(_PNG_SIGNATURE) or not content.endswith(_PNG_IEND):
        return [
            {
                "path": _display_path(path, root),
                "pattern": "invalid_binary_asset",
                "match": "invalid PNG signature or trailer",
            }
        ]
    return _scan_content(content.decode("utf-8", errors="ignore"), path, root)


def _scan_client_asset(path: pathlib.Path, root: pathlib.Path) -> list[dict[str, str]]:
    if path.is_symlink():
        return [
            {
                "path": _display_path(path, root),
                "pattern": "client_asset_symlink",
                "match": "symlink client asset is not allowed",
            }
        ]
    return _scan_text(path, root)


def _scan_release_asset(path: pathlib.Path, root: pathlib.Path) -> list[dict[str, str]]:
    safety_violation = _release_asset_safety_violation(path, root)
    if safety_violation:
        return [safety_violation]
    if path.suffix.lower() == ".png":
        return _scan_png(path, root)
    return _scan_text(path, root)


def _safe_cli_path(value: object, root: pathlib.Path) -> str:
    try:
        candidate = pathlib.Path(str(value))
    except (TypeError, ValueError):
        return "<unknown-path>"
    if not candidate.is_absolute():
        candidate = root / candidate
    return _display_path(candidate, root)


def scan_tree(root: pathlib.Path | str) -> list[dict[str, str]]:
    base = pathlib.Path(root).expanduser().resolve()
    violations: list[dict[str, str]] = []
    for path in _file_candidates(base):
        violations.extend(_scan_release_asset(path, base))
    for path in _client_asset_candidates(base):
        violations.extend(_scan_client_asset(path, base))
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", default=".")
    args = parser.parse_args(argv)
    base = pathlib.Path(args.tree).expanduser().resolve()
    violations = scan_tree(base)
    if violations:
        safe_violations = [
            {
                "path": _safe_cli_path(violation.get("path", "<unknown-path>"), base),
                "pattern": violation.get("pattern", "unknown"),
            }
            for violation in violations
        ]
        print(json.dumps({"status": "failed", "violations": safe_violations}, ensure_ascii=False, indent=2))
        return 1
    print("public release tree check: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
