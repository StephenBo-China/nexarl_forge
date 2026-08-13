from __future__ import annotations

import json
import os
import pathlib
import shutil
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


EXPECTED_AREAS = {
    "personal_memory", "projects", "memory_review", "policy",
    "design_preferences", "ui_design_packages", "ui_design_approvals",
    "ui_design_audit", "ui_skills", "ui_skill_digests",
    "ui_skill_deployments", "ui_skill_audit", "loop", "worktrees",
    "active_worktrees", "pending_worktrees", "legacy_hooks",
}


@dataclass(frozen=True)
class LegacyFixture:
    paths: vibe_memory_paths.RuntimePaths
    registry: dict[str, object]
    project_roots: tuple[pathlib.Path, ...]


def _write_text(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: pathlib.Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _snapshot_tree(root: pathlib.Path) -> list[tuple[str, str, object]]:
    items: list[tuple[str, str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item.relative_to(root))):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            items.append((relative, "symlink", os.readlink(path)))
        elif path.is_dir():
            items.append((relative, "dir", ""))
        else:
            items.append((relative, "file", path.read_bytes()))
    return items


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
                    "package_approvals": {},
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
                json.loads(memory_project.codex_hooks_json()),
            )
            _write_json(
                root / ".claude" / "settings.json",
                json.loads(memory_project.claude_settings_json()),
            )
            _write_text(
                root / ".codex" / "hooks" / "shared_memory_hook.py",
                memory_project.hook_script(root, "codex"),
            )
            _write_text(
                root / ".claude" / "hooks" / "shared_memory_hook.py",
                memory_project.hook_script(root, "claude"),
            )
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
                        "worktree": str(base / "worktrees" / "alpha"),
                    },
                    "task-beta": {
                        "repository": str(second_root),
                        "status": "released",
                    },
                },
            },
        )
        (base / "worktrees" / "alpha").mkdir(parents=True)

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
    return LegacyFixture(paths=paths, registry=registry, project_roots=(project_root, second_root))


class VibeMemoryMigrationTest(unittest.TestCase):
    def test_inventory_covers_every_control_plane(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            fixture = build_complete_legacy_fixture(pathlib.Path(value))
            result = migration.inventory(fixture.paths, fixture.registry)

        self.assertEqual(set(result), EXPECTED_AREAS)
        for area in EXPECTED_AREAS:
            self.assertIsInstance(result[area]["present"], bool, area)
            self.assertIsInstance(result[area]["errors"], list, area)
            self.assertIsInstance(result[area]["records"], list, area)
        self.assertEqual(result["projects"]["registered"], 2)
        self.assertEqual(result["ui_skills"]["published"], 1)

    def test_empty_optional_control_plane_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            home = pathlib.Path(value) / "home"
            home.mkdir()
            paths = vibe_memory_paths.for_home(home)
            result = migration.validate_control_plane(
                paths, {"schema_version": 1, "projects": [], "current_project": ""}
            )
        self.assertEqual(set(result), EXPECTED_AREAS)
        self.assertTrue(all(status == "ok" for status in result.values()), result)

    def test_dangling_design_approval_and_skill_deployment_are_errors(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            fixture = build_complete_legacy_fixture(pathlib.Path(value))
            project = fixture.project_roots[0]
            _write_json(project / "codex/ui_design/approvals.json", {
                "schema_version": 1,
                "package_approvals": {"missing": {"status": "approved", "digest": "a" * 64}},
                "project_global_approval": None,
            })
            registry_path = fixture.paths.ui_design_home / "registry.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            registry["deployments"]["dangling"] = {
                "transaction_id": "dangling", "name": "missing", "version_id": "1.0.0",
                "digest": "b" * 64, "status": "published",
            }
            _write_json(registry_path, registry)
            result = migration.validate_control_plane(fixture.paths, fixture.registry)
        self.assertEqual(result["ui_design_approvals"], "error")
        self.assertEqual(result["ui_skill_deployments"], "error")

    def test_digest_mismatch_and_malformed_canonical_json_are_errors(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            fixture = build_complete_legacy_fixture(pathlib.Path(value))
            registry_path = fixture.paths.ui_design_home / "registry.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            package = next(iter(registry["packages"].values()))[0]
            package["digest"] = "0" * 64
            _write_json(registry_path, registry)
            (fixture.project_roots[0] / ".loop/config.json").write_text("{bad", encoding="utf-8")
            result = migration.validate_control_plane(fixture.paths, fixture.registry)
        self.assertEqual(result["ui_skill_digests"], "error")
        self.assertEqual(result["loop"], "error")

    def test_worktree_registry_status_must_reference_existing_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            home = pathlib.Path(value) / "home"
            home.mkdir()
            paths = vibe_memory_paths.for_home(home)
            _write_json(paths.worktree_manager / "tasks.json", {
                "schema_version": 1,
                "tasks": {"task": {"status": "verified", "worktree": str(home / "missing")}},
            })
            result = migration.validate_control_plane(
                paths, {"schema_version": 1, "projects": [], "current_project": ""}
            )
        self.assertEqual(result["active_worktrees"], "error")
        self.assertEqual(result["pending_worktrees"], "error")

    def test_preview_legacy_hooks_is_read_only_and_reports_targets(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            fixture = build_complete_legacy_fixture(pathlib.Path(value))
            project = fixture.project_roots[0]
            before = _snapshot_tree(project)

            preview = migration.preview_legacy_hooks([project], paths=fixture.paths)

            self.assertEqual(_snapshot_tree(project), before)
            self.assertEqual(len(preview), 1)
            self.assertEqual(preview[0]["managed_entries"], 5)
            self.assertEqual(preview[0]["custom_entries"], 1)
            resolved = project.resolve()
            self.assertIn(str(resolved / ".codex/hooks.json"), preview[0]["targets"])
            self.assertIn(str(resolved / ".claude/settings.json"), preview[0]["targets"])
            self.assertIn(str(resolved / ".codex/hooks/shared_memory_hook.py"), preview[0]["targets"])
            self.assertIn(str(resolved / ".claude/hooks/shared_memory_hook.py"), preview[0]["targets"])

    def test_apply_legacy_hooks_preserves_ui_gate_and_backs_up_managed_assets(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            fixture = build_complete_legacy_fixture(pathlib.Path(value))
            project = fixture.project_roots[0]

            envelope = migration.apply_legacy_hooks([project], paths=fixture.paths)
            result = envelope["projects"]
            self.assertEqual(envelope["status"], "applied")

            codex_hooks = (project / ".codex" / "hooks.json").read_text(encoding="utf-8")
            claude_hooks = (project / ".claude" / "settings.json").read_text(encoding="utf-8")
            self.assertNotIn("shared_memory_hook.py", codex_hooks)
            self.assertNotIn("shared_memory_hook.py", claude_hooks)
            self.assertIn("ui_design_gate_hook.py", codex_hooks)
            self.assertIn("ui_design_gate_hook.py", claude_hooks)
            self.assertFalse((project / ".codex" / "hooks" / "shared_memory_hook.py").exists())
            self.assertFalse((project / ".claude" / "hooks" / "shared_memory_hook.py").exists())
            self.assertTrue((project / ".codex" / "hooks" / "ui_design_gate_hook.py").exists())
            self.assertTrue((project / ".claude" / "hooks" / "ui_design_gate_hook.py").exists())
            self.assertTrue(result[0]["backups"])
            self.assertEqual(result[0]["managed_entries"], 5)
            self.assertEqual(result[0]["custom_entries"], 1)
            audit_path = pathlib.Path(result[0]["audit"])
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual(audit["root"], str(project.resolve()))
            self.assertNotEqual(audit["before_digest"], audit["after_digest"])
            self.assertEqual(audit["changed_paths"], result[0]["changed_paths"])
            self.assertEqual(audit["backups"], result[0]["backups"])
            self.assertEqual(audit["result"], "applied")

    def test_two_registered_roots_receive_separate_audit_records(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            fixture = build_complete_legacy_fixture(pathlib.Path(value))

            envelope = migration.apply_legacy_hooks(
                list(fixture.project_roots), paths=fixture.paths
            )
            results = envelope["projects"]

            audits = [pathlib.Path(item["audit"]) for item in results]
            self.assertEqual(len(set(audits)), 2)
            self.assertTrue(all(path.exists() for path in audits))
            self.assertEqual(
                {json.loads(path.read_text(encoding="utf-8"))["root"] for path in audits},
                {str(root.resolve()) for root in fixture.project_roots},
            )

    def test_preview_and_apply_refuse_unregistered_outside_and_symlink_roots(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            fixture = build_complete_legacy_fixture(pathlib.Path(value))
            unregistered = pathlib.Path(value) / "projects" / "other"
            unregistered.mkdir()
            outside = pathlib.Path(value) / "outside"
            outside.mkdir()
            alias = pathlib.Path(value) / "alias"
            alias.symlink_to(fixture.project_roots[0], target_is_directory=True)

            for root in (unregistered, outside, alias):
                with self.subTest(root=root):
                    with self.assertRaises(ValueError):
                        migration.preview_legacy_hooks([root], paths=fixture.paths)
                    result = migration.apply_legacy_hooks([root], paths=fixture.paths)
                    self.assertEqual(result["status"], "failed")

    def test_custom_command_with_same_basename_is_not_manager_owned(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            fixture = build_complete_legacy_fixture(pathlib.Path(value))
            project = fixture.project_roots[0]
            path = project / ".codex/hooks.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            document["hooks"]["Stop"].append({
                "hooks": [{
                    "type": "command",
                    "command": "python3 /opt/custom/shared_memory_hook.py",
                }]
            })
            _write_json(path, document)

            self.assertEqual(
                migration.apply_legacy_hooks([project], paths=fixture.paths)["status"],
                "applied",
            )

            current = path.read_text(encoding="utf-8")
            self.assertIn("/opt/custom/shared_memory_hook.py", current)

    def test_same_project_path_with_custom_script_is_preserved_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            fixture = build_complete_legacy_fixture(pathlib.Path(value))
            project = fixture.project_roots[0]
            script = project / ".codex/hooks/shared_memory_hook.py"
            script.write_text("# custom project hook\n", encoding="utf-8")
            before = _snapshot_tree(project)

            preview = migration.preview_legacy_hooks([project], paths=fixture.paths)
            self.assertEqual(
                migration.apply_legacy_hooks([project], paths=fixture.paths)["status"],
                "failed",
            )

            self.assertEqual(_snapshot_tree(project), before)
            self.assertTrue(preview[0]["errors"])

    def test_missing_owned_script_preserves_document_and_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            fixture = build_complete_legacy_fixture(pathlib.Path(value))
            project = fixture.project_roots[0]
            (project / ".codex/hooks/shared_memory_hook.py").unlink()
            before = _snapshot_tree(project)

            preview = migration.preview_legacy_hooks([project], paths=fixture.paths)
            self.assertEqual(
                migration.apply_legacy_hooks([project], paths=fixture.paths)["status"],
                "failed",
            )

            self.assertEqual(_snapshot_tree(project), before)
            self.assertTrue(preview[0]["errors"])

    def test_script_symlink_preflight_leaves_zero_changes_or_backups(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            fixture = build_complete_legacy_fixture(pathlib.Path(value))
            project = fixture.project_roots[0]
            script = project / ".codex/hooks/shared_memory_hook.py"
            target = pathlib.Path(value) / "outside.py"
            target.write_text(memory_project.hook_script(project, "codex"), encoding="utf-8")
            script.unlink()
            script.symlink_to(target)
            before = _snapshot_tree(project)
            before_backups = set(project.rglob("*.bak.*"))

            self.assertEqual(
                migration.apply_legacy_hooks([project], paths=fixture.paths)["status"],
                "failed",
            )

            self.assertEqual(_snapshot_tree(project), before)
            self.assertEqual(set(project.rglob("*.bak.*")), before_backups)

    def test_rename_failure_restores_documents_scripts_and_transaction_backups(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            fixture = build_complete_legacy_fixture(pathlib.Path(value))
            project = fixture.project_roots[0]
            active_paths = [
                project / ".codex/hooks.json",
                project / ".claude/settings.json",
                project / ".codex/hooks/shared_memory_hook.py",
                project / ".claude/hooks/shared_memory_hook.py",
            ]
            before = {path: (path.read_bytes(), path.stat().st_mode) for path in active_paths}
            real_replace = migration.os.replace

            def fail_second_script(source: object, target: object, *args: object, **kwargs: object):
                if str(source).endswith("shared_memory_hook.py") and kwargs.get("src_dir_fd") is not None and calls[0] == 1:
                    raise OSError("injected rename failure")
                if str(source).endswith("shared_memory_hook.py") and kwargs.get("src_dir_fd") is not None:
                    calls[0] += 1
                return real_replace(source, target, *args, **kwargs)

            calls = [0]

            with mock.patch.object(migration.os, "replace", side_effect=fail_second_script):
                result = migration.apply_legacy_hooks([project], paths=fixture.paths)
                self.assertEqual(result["status"], "failed")

            self.assertEqual(
                {path: (path.read_bytes(), path.stat().st_mode) for path in active_paths},
                before,
            )
            self.assertFalse([
                path for path in project.rglob("*.bak.*")
                if "ui_design" not in str(path)
            ])
            failed = list((project / "codex/migration_audits").glob("*.json"))
            self.assertEqual(len(failed), 1)
            self.assertEqual(
                json.loads(failed[0].read_text(encoding="utf-8"))["result"], "failed"
            )

    def test_audit_write_failure_restores_active_resources(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            fixture = build_complete_legacy_fixture(pathlib.Path(value))
            project = fixture.project_roots[0]
            active_paths = [
                project / ".codex/hooks.json",
                project / ".claude/settings.json",
                project / ".codex/hooks/shared_memory_hook.py",
                project / ".claude/hooks/shared_memory_hook.py",
            ]
            before = {path: path.read_bytes() for path in active_paths}
            with mock.patch.object(
                migration, "_write_migration_audit", side_effect=OSError("audit failure")
            ):
                result = migration.apply_legacy_hooks([project], paths=fixture.paths)
                self.assertEqual(result["status"], "failed")

            self.assertEqual({path: path.read_bytes() for path in active_paths}, before)

    def test_multi_root_returns_applied_and_failed_projects(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            fixture = build_complete_legacy_fixture(pathlib.Path(value))
            alpha, beta = fixture.project_roots
            (beta / ".codex/hooks/shared_memory_hook.py").write_text(
                "# custom\n", encoding="utf-8"
            )

            result = migration.apply_legacy_hooks([alpha, beta], paths=fixture.paths)

            self.assertEqual(result["status"], "partial")
            self.assertEqual(
                [item["root"] for item in result["projects"]],
                [str(alpha.resolve()), str(beta.resolve())],
            )
            self.assertEqual([item["result"] for item in result["projects"]], ["applied", "failed"])

    def test_intermediate_symlink_targets_are_rejected_without_outside_write(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            fixture = build_complete_legacy_fixture(pathlib.Path(value))
            project = fixture.project_roots[0]
            outside = pathlib.Path(value) / "outside"
            outside.mkdir()
            shutil.rmtree(project / ".codex")
            (project / ".codex").symlink_to(outside, target_is_directory=True)

            result = migration.apply_legacy_hooks([project], paths=fixture.paths)

            self.assertEqual(result["status"], "failed")
            self.assertEqual(list(outside.iterdir()), [])

    def test_audit_directory_symlink_is_rejected_without_outside_write(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            fixture = build_complete_legacy_fixture(pathlib.Path(value))
            project = fixture.project_roots[0]
            outside = pathlib.Path(value) / "outside"
            outside.mkdir()
            audits = project / "codex/migration_audits"
            audits.symlink_to(outside, target_is_directory=True)

            result = migration.apply_legacy_hooks([project], paths=fixture.paths)

            self.assertEqual(result["status"], "failed")
            self.assertEqual(list(outside.iterdir()), [])

    def test_lock_window_custom_replacement_is_preserved_as_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            fixture = build_complete_legacy_fixture(pathlib.Path(value))
            project = fixture.project_roots[0]
            script = project / ".codex/hooks/shared_memory_hook.py"
            real_snapshot = migration._snapshot_legacy_files
            calls = 0

            def replace_after_snapshot(root: pathlib.Path, *args: object):
                nonlocal calls
                value = real_snapshot(root, *args)
                calls += 1
                if calls == 1:
                    script.write_text("# concurrent custom replacement\n", encoding="utf-8")
                return value

            with mock.patch.object(
                migration, "_snapshot_legacy_files", side_effect=replace_after_snapshot
            ):
                result = migration.apply_legacy_hooks([project], paths=fixture.paths)

            self.assertEqual(result["status"], "failed")
            self.assertEqual(script.read_text(encoding="utf-8"), "# concurrent custom replacement\n")

    def test_root_rebind_during_lock_window_fails_without_touching_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            fixture = build_complete_legacy_fixture(pathlib.Path(value))
            project = fixture.project_roots[0]
            replacement = pathlib.Path(value) / "replacement"
            replacement.mkdir()
            marker = replacement / "sentinel"
            marker.write_text("keep\n", encoding="utf-8")
            original = project.with_name("alpha-original")
            real_snapshot = migration._snapshot_legacy_files
            calls = 0

            def rebind_after_snapshot(root: pathlib.Path, *args: object):
                nonlocal calls
                result = real_snapshot(root, *args)
                calls += 1
                if calls == 1:
                    project.rename(original)
                    replacement.rename(project)
                return result

            with mock.patch.object(
                migration, "_snapshot_legacy_files", side_effect=rebind_after_snapshot
            ):
                result = migration.apply_legacy_hooks([project], paths=fixture.paths)

            self.assertEqual(result["status"], "failed")
            self.assertEqual((project / "sentinel").read_text(encoding="utf-8"), "keep\n")

    def test_root_rebind_after_preview_does_not_migrate_valid_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            fixture = build_complete_legacy_fixture(pathlib.Path(value))
            project, replacement = fixture.project_roots
            original = project.with_name("alpha-original")
            before_project = _snapshot_tree(project)
            before_replacement = _snapshot_tree(replacement)
            real_preview = migration._legacy_hook_preview_for_project

            def rebind_after_preview(root: pathlib.Path, *args: object, **kwargs: object):
                result = real_preview(root, *args, **kwargs)
                project.rename(original)
                replacement.rename(project)
                return result

            with mock.patch.object(
                migration,
                "_legacy_hook_preview_for_project",
                side_effect=rebind_after_preview,
            ):
                result = migration.apply_legacy_hooks([project], paths=fixture.paths)

            self.assertEqual(result["status"], "failed")
            self.assertEqual(_snapshot_tree(original), before_project)
            self.assertEqual(_snapshot_tree(project), before_replacement)


if __name__ == "__main__":
    unittest.main()
