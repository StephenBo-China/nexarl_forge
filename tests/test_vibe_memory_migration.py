from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from dataclasses import dataclass
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import memory_project
import ui_design_preferences as preferences
import ui_skill_registry as skills
import vibe_memory_paths
import vibe_memory_migration as migration


@dataclass(frozen=True)
class LegacyFixture:
    paths: vibe_memory_paths.RuntimePaths
    registry: dict[str, object]


def _write_text(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: pathlib.Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_complete_legacy_fixture(base: pathlib.Path) -> LegacyFixture:
    home = base / "home"
    home.mkdir()
    paths = vibe_memory_paths.for_home(home)

    _write_text(paths.personal_memory / "long.md", "## Long\n\n### Memory\nApproved.\n")
    _write_text(paths.personal_memory / "short.md", "## Short\n\n### Note\nTemporary.\n")
    _write_text(paths.personal_memory / "proposals.md", "# Proposals\n\n### Personal candidate\n- content\n")

    _write_json(paths.ui_design_home / "preferences.json", preferences.default_global_preferences())

    fixture_skill = ROOT / "tests/fixtures/ui_skills/minimal"
    project_root = base / "projects" / "alpha"
    second_root = base / "projects" / "beta"
    project_root.mkdir(parents=True)
    second_root.mkdir(parents=True)

    original_registry = memory_project.REGISTRY_PATH
    original_worktree_root = memory_project.DEFAULT_WORKTREE_ROOT
    memory_project.REGISTRY_PATH = paths.project_registry
    memory_project.DEFAULT_WORKTREE_ROOT = paths.worktree_root
    try:
        memory_project.register_project(project_root)
        memory_project.register_project(second_root, make_current=False)

        for root in (project_root, second_root):
            (root / ".git").mkdir()
            codex = root / "codex"
            ui_design = codex / "ui_design"
            hooks_codex = root / ".codex" / "hooks"
            hooks_claude = root / ".claude" / "hooks"
            codex.mkdir()
            ui_design.mkdir(parents=True)
            hooks_codex.mkdir(parents=True)
            hooks_claude.mkdir(parents=True)
            _write_text(codex / "codex_long_memory.md", "# Long\n\n### Approved memory\nStable.\n")
            _write_text(codex / "codex_short_memory.md", "# Short\n\n### Short memory\nStable.\n")
            _write_text(codex / "memory_proposals.md", "# Proposals\n\n### Pending memory\n- item\n")
            _write_json(
                codex / "memory_review_queue.json",
                {
                    "generated_at": "2026-07-31T00:00:00Z",
                    "review_url": "http://127.0.0.1:8897",
                    "items": [
                        {
                            "id": f"{root.name}-1",
                            "scope": "personal",
                            "target": "personal_long",
                            "review_kind": "memory",
                            "actionable": True,
                            "status": "pending",
                        },
                        {
                            "id": f"{root.name}-2",
                            "scope": "project",
                            "target": "project_long",
                            "review_kind": "memory",
                            "actionable": True,
                            "status": "approved",
                        },
                    ],
                    "counts": {
                        "pending": 1,
                        "approved": 1,
                        "rejected": 0,
                        "deferred": 0,
                    },
                },
            )
            _write_json(
                codex / "memory_review_state.json",
                {
                    "items": {
                        f"{root.name}-1": {"status": "pending"},
                        f"{root.name}-2": {"status": "approved"},
                    },
                    "last_reminder_at": "",
                },
            )
            _write_json(
                ui_design / "config.json",
                {
                    **memory_project.ui_design_config(root),
                    "hard_gate_enabled": True,
                    "formal_frontend_paths": ["web/src/**"],
                    "relocked": False,
                },
            )
            _write_json(
                ui_design / "active-skills.json",
                {
                    "schema_version": 1,
                    "execution_order": ["frontend-design"],
                    "skills": [{"name": "frontend-design", "version": "pinned"}],
                },
            )
            _write_json(
                ui_design / "approvals.json",
                {
                    "schema_version": 1,
                    "package_approvals": {
                        "design-1": {"digest": "a" * 64, "status": "approved"}
                    },
                    "project_global_approval": {
                        "digest": "b" * 64,
                        "status": "approved",
                    },
                },
            )
            _write_json(
                ui_design / "preferences.json",
                {
                    "visual.radius": {"mode": "replace", "value": "4px"},
                },
            )
            _write_json(
                root / ".loop" / "config.json",
                memory_project.loop_config(root, 8082),
            )
            _write_json(
                root / ".codex" / "hooks.json",
                {
                    "schema_version": 1,
                    "hooks": {
                        "SessionStart": [
                            {"hooks": [{"type": "command", "command": "shared_memory_hook.py"}]}
                        ],
                        "Stop": [
                            {"hooks": [{"type": "command", "command": "ui_design_gate_hook.py"}]}
                        ],
                    },
                },
            )
            _write_json(
                root / ".claude" / "settings.json",
                {
                    "schema_version": 1,
                    "hooks": {
                        "SessionStart": [
                            {"hooks": [{"type": "command", "command": "shared_memory_hook.py"}]}
                        ],
                        "Stop": [
                            {"hooks": [{"type": "command", "command": "ui_design_gate_hook.py"}]}
                        ],
                    },
                },
            )
            _write_text(root / ".codex" / "hooks" / "shared_memory_hook.py", "# legacy codex hook\n")
            _write_text(root / ".claude" / "hooks" / "shared_memory_hook.py", "# legacy claude hook\n")
            _write_text(root / ".codex" / "hooks" / "ui_design_gate_hook.py", "# ui design gate hook\n")
            _write_text(root / ".claude" / "hooks" / "ui_design_gate_hook.py", "# ui design gate hook\n")

        _write_json(
            paths.worktree_manager / "tasks.json",
            {
                "schema_version": 1,
                "tasks": {
                    "task-alpha": {
                        "repository": str(project_root),
                        "status": "developing",
                    },
                    "task-beta": {
                        "repository": str(second_root),
                        "status": "released",
                    },
                },
            },
        )

        with mock.patch.dict(
            os.environ, {"UI_DESIGN_HOME": str(paths.ui_design_home)}
        ):
            draft = skills.create_draft(
                name="sample-ui",
                source={"type": "local", "path": str(fixture_skill)},
                package_root=fixture_skill,
                scope={"type": "global"},
                targets=["codex"],
            )
            skills.approve_draft(draft["id"], expected_digest=draft["digest"])

            preferences.save_global_preferences(preferences.default_global_preferences())
            preferences.save_project_overrides(
                project_root,
                {"visual.radius": {"mode": "replace", "value": "2px"}},
            )
            preferences.save_project_overrides(
                second_root,
                {"visual.radius": {"mode": "replace", "value": "6px"}},
            )

        registry = json.loads(paths.project_registry.read_text(encoding="utf-8"))
    finally:
        memory_project.REGISTRY_PATH = original_registry
        memory_project.DEFAULT_WORKTREE_ROOT = original_worktree_root
    return LegacyFixture(paths=paths, registry=registry)


class VibeMemoryMigrationTest(unittest.TestCase):
    def test_inventory_covers_every_control_plane(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            fixture = build_complete_legacy_fixture(pathlib.Path(value))
            result = migration.inventory(fixture.paths, fixture.registry)

        self.assertEqual(
            set(result),
            {
                "personal_memory",
                "projects",
                "memory_review",
                "design_preferences",
                "ui_design_approvals",
                "ui_skills",
                "loop",
                "worktrees",
                "legacy_hooks",
            },
        )
        self.assertEqual(result["projects"]["registered"], 2)
        self.assertEqual(result["ui_skills"]["published"], 1)


if __name__ == "__main__":
    unittest.main()
