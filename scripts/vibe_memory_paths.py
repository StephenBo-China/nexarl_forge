"""Portable runtime paths and release metadata for Vibe Memory."""

from __future__ import annotations

from dataclasses import dataclass
import json
import pathlib
from typing import Any


@dataclass(frozen=True)
class RuntimePaths:
    personal_memory: pathlib.Path
    project_registry: pathlib.Path
    install_root: pathlib.Path
    ui_design_home: pathlib.Path
    worktree_manager: pathlib.Path
    worktree_root: pathlib.Path
    launcher: pathlib.Path
    launch_agent: pathlib.Path


def for_home(home: pathlib.Path | None = None) -> RuntimePaths:
    """Return Vibe Memory runtime paths rooted at *home* or the current home."""
    resolved_home = pathlib.Path.home() if home is None else pathlib.Path(home)
    codex_home = resolved_home / ".codex"
    return RuntimePaths(
        personal_memory=codex_home / "personal_memory",
        project_registry=codex_home / "memory_review" / "projects.json",
        install_root=resolved_home / "Library" / "Application Support" / "VibeMemory",
        ui_design_home=codex_home / "ui_design",
        worktree_manager=codex_home / "worktree_manager",
        worktree_root=resolved_home / "Projects" / "worktrees",
        launcher=resolved_home / ".local" / "bin" / "vibe-memory",
        launch_agent=(
            resolved_home
            / "Library"
            / "LaunchAgents"
            / "com.noema.vibe-memory.plist"
        ),
    )


def read_release_manifest(path: pathlib.Path | str) -> dict[str, Any]:
    """Read a release manifest with the required portable-release fields."""
    value = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("release manifest root must be an object")
    required = {
        "app_version",
        "data_schema_version",
        "hook_protocol_version",
        "minimum_python",
        "platform",
    }
    missing = required.difference(value)
    if missing:
        raise ValueError(f"release manifest missing required fields: {', '.join(sorted(missing))}")
    return value
