from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import memory_project
import loop_superpowers
import memory_review_server


EXPECTED_SKILLS = {
    "brainstorming",
    "dispatching-parallel-agents",
    "executing-plans",
    "finishing-a-development-branch",
    "receiving-code-review",
    "requesting-code-review",
    "subagent-driven-development",
    "systematic-debugging",
    "test-driven-development",
    "using-git-worktrees",
    "using-superpowers",
    "verification-before-completion",
    "writing-plans",
    "writing-skills",
}


class LoopSuperpowersRolloutTest(unittest.TestCase):
    def test_managed_validator_template_is_valid_python(self) -> None:
        compile(
            loop_superpowers.validator_text(),
            str(loop_superpowers.VALIDATOR_TEMPLATE),
            "exec",
        )

    def test_new_loop_contract_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            project = pathlib.Path(value) / "sample"
            project.mkdir()
            config = memory_project.loop_config(project, 8123)

        self.assertEqual(config["schema_version"], 3)
        self.assertIn("methodology", config)
        method = config["methodology"]["superpowers"]
        self.assertEqual(config["methodology"]["provider"], "superpowers")
        self.assertTrue(method["enabled"])
        self.assertEqual(set(method["declared_skills"]), EXPECTED_SKILLS)
        self.assertEqual(method["authority"]["orchestrator"], "loop")
        self.assertEqual(method["authority"]["worktree"], "loop_worktree_flow_only")
        self.assertFalse(method["evaluator"]["may_modify_product_source"])
        self.assertFalse(method["subagents"]["default_enabled"])
        self.assertTrue(method["subagents"]["requires_explicit_user_authorization"])
        self.assertEqual(
            config["worktree"]["finish_validation_commands"],
            ["python3 scripts/validate_loop_methodology.py --phase completion"],
        )

    def test_init_loop_installs_managed_validator(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            project = pathlib.Path(value) / "sample"
            project.mkdir()
            (project / ".git").mkdir()
            original_registry = memory_project.REGISTRY_PATH
            memory_project.REGISTRY_PATH = pathlib.Path(value) / "projects.json"
            try:
                first = memory_project.init_loop(project, 8123)
                validator = project.resolve() / "scripts" / "validate_loop_methodology.py"
                self.assertTrue(validator.exists())
                first_text = validator.read_text(encoding="utf-8")
            finally:
                memory_project.REGISTRY_PATH = original_registry

            self.assertIn(
                "Validate the repository's Loop + Superpowers workflow contract",
                first_text,
            )
            self.assertIn(loop_superpowers.MANAGED_VALIDATOR_MARKER, first_text)
            self.assertTrue(
                any(
                    item["status"] == "created" and item["path"] == str(validator)
                    for item in first["changes"]
                ),
                first["changes"],
            )

    def test_preview_and_upgrade_preserve_custom_values_and_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            project = (pathlib.Path(value) / "sample").resolve()
            project.mkdir()
            (project / ".git").mkdir()
            config_path = project / ".loop" / "config.json"
            config_path.parent.mkdir()
            original = {
                "schema_version": 2,
                "project_repo_name": "sample",
                "repository": {
                    "canonical_root": "/old",
                    "main_branch": "main",
                    "remote": "upstream",
                },
                "worktree": {"finish_validation_commands": ["make preflight"]},
                "staging": {
                    "port": 9191,
                    "database": "offline",
                    "oss_bucket": "owned",
                    "remote_path": "/srv/sample",
                },
                "verification": {"commands": ["make test"]},
                "custom_extension": {"keep": True},
            }
            config_path.write_text(json.dumps(original), encoding="utf-8")

            self.assertTrue(hasattr(memory_project, "preview_loop_upgrade"))
            self.assertTrue(hasattr(memory_project, "upgrade_loop"))
            preview = memory_project.preview_loop_upgrade(project, 8123)
            self.assertEqual(json.loads(config_path.read_text(encoding="utf-8")), original)
            self.assertIn("methodology", preview["added_paths"])

            first = memory_project.upgrade_loop(project, 8123)
            upgraded = json.loads(config_path.read_text(encoding="utf-8"))
            for key, expected in original["staging"].items():
                self.assertEqual(upgraded["staging"][key], expected)
            self.assertEqual(upgraded["verification"]["commands"], ["make test"])
            self.assertEqual(upgraded["custom_extension"], {"keep": True})
            self.assertEqual(upgraded["repository"]["canonical_root"], str(project))
            self.assertEqual(
                upgraded["worktree"]["finish_validation_commands"],
                ["make preflight", loop_superpowers.COMPLETION_COMMAND],
            )
            self.assertEqual(len(list(config_path.parent.glob("config.json.bak.*"))), 1)
            self.assertEqual(first["config_status"], "upgraded")

            second = memory_project.upgrade_loop(project, 8123)
            self.assertEqual(second["config_status"], "existing")
            self.assertEqual(len(list(config_path.parent.glob("config.json.bak.*"))), 1)

    def test_invalid_loop_json_is_not_rewritten_or_backed_up(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            project = pathlib.Path(value).resolve()
            config_path = project / ".loop" / "config.json"
            config_path.parent.mkdir()
            config_path.write_text("{broken", encoding="utf-8")

            self.assertTrue(hasattr(memory_project, "preview_loop_upgrade"))
            self.assertTrue(hasattr(memory_project, "upgrade_loop"))
            with self.assertRaisesRegex(ValueError, "Invalid loop config JSON"):
                memory_project.preview_loop_upgrade(project, 8123)
            with self.assertRaisesRegex(ValueError, "Invalid loop config JSON"):
                memory_project.upgrade_loop(project, 8123)

            self.assertEqual(config_path.read_text(encoding="utf-8"), "{broken")
            self.assertEqual(list(config_path.parent.glob("config.json.bak.*")), [])

    def test_upgrade_reports_custom_validator_conflict_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            project = pathlib.Path(value).resolve()
            config_path = project / ".loop" / "config.json"
            config_path.parent.mkdir()
            config_path.write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
            validator = project / loop_superpowers.VALIDATOR_RELATIVE_PATH
            validator.parent.mkdir()
            validator.write_text("custom validator\n", encoding="utf-8")

            self.assertTrue(hasattr(memory_project, "upgrade_loop"))
            result = memory_project.upgrade_loop(project, 8123)

            self.assertEqual(validator.read_text(encoding="utf-8"), "custom validator\n")
            self.assertEqual(result["methodology_status"]["status"], "custom_conflict")

    def test_project_entry_reports_loop_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            project = (pathlib.Path(value) / "sample").resolve()
            project.mkdir()
            (project / ".git").mkdir()
            empty = memory_project.project_entry(project)
            self.assertIn("loop_status", empty)
            self.assertEqual(empty["loop_status"], "not_initialized")
            self.assertEqual(empty["completion_gate"], "not_applicable")

            original_registry = memory_project.REGISTRY_PATH
            memory_project.REGISTRY_PATH = pathlib.Path(value) / "projects.json"
            try:
                memory_project.init_loop(project, 8123)
            finally:
                memory_project.REGISTRY_PATH = original_registry
            ready = memory_project.project_entry(project)
            self.assertEqual(ready["loop_status"], "superpowers_ready")
            self.assertEqual(ready["completion_gate"], "configured")
            self.assertIn(ready["plugin_status"], {"installed", "partial", "missing"})

    def test_project_operation_requires_git_and_explicit_upgrade_confirmation(self) -> None:
        self.assertTrue(hasattr(memory_review_server, "project_operation"))
        with tempfile.TemporaryDirectory() as value:
            project = pathlib.Path(value).resolve()
            with self.assertRaisesRegex(ValueError, "Git repository"):
                memory_review_server.project_operation(
                    "init-loop", {"project_root": str(project), "port": 8123}
                )

            (project / ".git").mkdir()
            config_path = project / ".loop" / "config.json"
            config_path.parent.mkdir()
            config_path.write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
            with self.assertRaisesRegex(PermissionError, "confirmation"):
                memory_review_server.project_operation(
                    "upgrade-loop",
                    {"project_root": str(project), "port": 8123, "confirmed": False},
                )

            preview = memory_review_server.project_operation(
                "preview-loop-upgrade",
                {"project_root": str(project), "port": 8123},
            )
            self.assertTrue(preview["config_will_change"])

    def test_init_loop_operation_rejects_existing_loop_config(self) -> None:
        self.assertTrue(hasattr(memory_review_server, "project_operation"))
        with tempfile.TemporaryDirectory() as value:
            project = pathlib.Path(value).resolve()
            (project / ".git").mkdir()
            config_path = project / ".loop" / "config.json"
            config_path.parent.mkdir()
            config_path.write_text(json.dumps({"schema_version": 2}), encoding="utf-8")

            with self.assertRaisesRegex(FileExistsError, "preview-loop-upgrade"):
                memory_review_server.project_operation(
                    "init-loop", {"project_root": str(project), "port": 8123}
                )

    def test_init_loop_cli_boundary_rejects_existing_config_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            project = pathlib.Path(value).resolve()
            (project / ".git").mkdir()
            config_path = project / ".loop" / "config.json"
            config_path.parent.mkdir()
            original = '{"schema_version": 2, "sentinel": true}\n'
            config_path.write_text(original, encoding="utf-8")

            with self.assertRaisesRegex(FileExistsError, "upgrade-loop"):
                memory_project.init_loop(project, 8123)

            self.assertEqual(config_path.read_text(encoding="utf-8"), original)

    def test_project_operation_rejects_missing_project_root(self) -> None:
        with self.assertRaisesRegex(ValueError, "project_root is required"):
            memory_review_server.project_operation("init-loop", {"port": 8123})

    def test_backup_failure_does_not_install_validator_or_rewrite_config(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            project = pathlib.Path(value).resolve()
            config_path = project / ".loop" / "config.json"
            config_path.parent.mkdir()
            original = json.dumps({"schema_version": 2})
            config_path.write_text(original, encoding="utf-8")
            validator = project / loop_superpowers.VALIDATOR_RELATIVE_PATH

            with mock.patch.object(
                loop_superpowers,
                "timestamped_backup",
                side_effect=OSError("backup denied"),
            ):
                with self.assertRaisesRegex(OSError, "backup denied"):
                    memory_project.upgrade_loop(project, 8123)

            self.assertFalse(validator.exists())
            self.assertEqual(config_path.read_text(encoding="utf-8"), original)

    def test_project_api_errors_have_specific_http_statuses(self) -> None:
        self.assertEqual(memory_review_server.project_error_status(ValueError("bad")), 400)
        self.assertEqual(
            memory_review_server.project_error_status(PermissionError("confirm")), 403
        )
        self.assertEqual(
            memory_review_server.project_error_status(FileExistsError("exists")), 409
        )

    def test_console_renders_latest_loop_superpowers_actions_and_boundaries(self) -> None:
        html = memory_review_server.page()
        self.assertIn("初始化 Loop × Superpowers", html)
        self.assertIn("预览升级 Loop", html)
        self.assertIn("升级记忆规则/钩子", html)
        self.assertIn("Superpowers 是阶段内工程方法", html)
        self.assertIn("Loop 是唯一生命周期编排器", html)
        self.assertIn("子代理和并行代理必须获得用户明确授权", html)
        self.assertIn("preview-loop-upgrade", html)
        self.assertIn("confirmed: true", html)

    def test_documentation_lists_latest_initialization_and_upgrade_commands(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        workflow = (ROOT / "docs" / "worktree_loop_workflow.md").read_text(
            encoding="utf-8"
        )
        for command in (
            "init-loop",
            "preview-loop-upgrade",
            "upgrade-loop",
            "upgrade-memory",
        ):
            self.assertIn(command, readme)
        self.assertIn("Superpowers", workflow)
        self.assertIn("Loop remains", workflow)


if __name__ == "__main__":
    unittest.main()
