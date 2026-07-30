"""Resolve workspace directories to registered memory projects."""

from __future__ import annotations

import pathlib


def resolve_registered_project(
    cwd: pathlib.Path, projects: list[dict[str, object]]
) -> pathlib.Path | None:
    """Return the deepest registered root containing *cwd*, if any."""
    current = cwd.expanduser().resolve()
    matches: list[pathlib.Path] = []
    for entry in projects:
        raw_root = entry.get("root") if isinstance(entry, dict) else None
        if not isinstance(raw_root, str) or not raw_root:
            continue
        root = pathlib.Path(raw_root).expanduser().resolve()
        if current == root or root in current.parents:
            matches.append(root)
    return max(matches, key=lambda root: len(root.parts)) if matches else None
