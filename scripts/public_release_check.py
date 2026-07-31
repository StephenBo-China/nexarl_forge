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


_ROOT_FILES = (
    "README.md",
    "AGENTS.md",
    "CLAUDE.md",
    "LICENSE",
    "SECURITY.md",
    "release.json",
    "install.sh",
)
_DOC_FILES = ("docs/*.md",)
_LOCAL_CLIENT_RUNTIME_CONFIGS = frozenset({".codex/hooks.json", ".claude/settings.json"})
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
        if candidate.is_file():
            yield candidate
    for pattern in _DOC_FILES:
        yield from sorted(path for path in root.glob(pattern) if path.is_file())
    scripts = root / "scripts"
    if scripts.is_dir():
        yield from sorted(path for path in scripts.rglob("*.py") if path.is_file())
    templates = root / "templates"
    if templates.is_dir():
        yield from sorted(path for path in templates.rglob("*") if path.is_file())


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


def _scan_text(path: pathlib.Path, root: pathlib.Path) -> list[dict[str, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return [{"path": _display_path(path, root), "pattern": "unreadable", "match": str(error)}]
    violations: list[dict[str, str]] = []
    for name, pattern in _PATTERNS:
        match = pattern.search(text)
        if match:
            violations.append(
                {
                    "path": _display_path(path, root),
                    "pattern": name,
                    "match": match.group(0),
                }
            )
    return violations


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


def scan_tree(root: pathlib.Path | str) -> list[dict[str, str]]:
    base = pathlib.Path(root).expanduser().resolve()
    violations: list[dict[str, str]] = []
    for path in _file_candidates(base):
        violations.extend(_scan_text(path, base))
    for path in _client_asset_candidates(base):
        violations.extend(_scan_client_asset(path, base))
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", default=".")
    args = parser.parse_args(argv)
    violations = scan_tree(pathlib.Path(args.tree))
    if violations:
        print(json.dumps({"status": "failed", "violations": violations}, ensure_ascii=False, indent=2))
        return 1
    print("public release tree check: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
