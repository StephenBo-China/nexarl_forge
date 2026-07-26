from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import memory_project
import memory_review_queue as review
import loop_superpowers
import ui_design_preferences as preferences


class MemoryReviewQualityTest(unittest.TestCase):
    def test_init_project_creates_safe_ui_design_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value) / "project"
            root.mkdir()
            (root / ".git").mkdir()
            original_registry = memory_project.REGISTRY_PATH
            try:
                memory_project.REGISTRY_PATH = pathlib.Path(value) / "projects.json"

                result = memory_project.init_project(root)
            finally:
                memory_project.REGISTRY_PATH = original_registry

            config = json.loads(
                (root / "codex/ui_design/config.json").read_text(encoding="utf-8")
            )
            self.assertEqual(config["gate_mode"], "design_package")
            self.assertFalse(config["hard_gate_enabled"])
            self.assertEqual(config["schema_version"], 1)
            self.assertTrue((root / "codex/ui_design/active-skills.json").exists())
            self.assertTrue((root / "codex/ui_design/preferences.json").exists())
            self.assertTrue((root / "codex/ui_design/approvals.json").exists())
            self.assertTrue(
                (root / ".codex/hooks/ui_design_gate_hook.py").exists()
            )
            self.assertTrue(
                (root / ".claude/hooks/ui_design_gate_hook.py").exists()
            )
            for settings_path in (
                root / ".codex/hooks.json",
                root / ".claude/settings.json",
            ):
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
                pre_tool = settings["hooks"]["PreToolUse"]
                self.assertTrue(
                    any(
                        "ui_design_gate_hook.py" in hook["command"]
                        for entry in pre_tool
                        for hook in entry["hooks"]
                    )
                )
            context_path = root / "codex/ui_design/effective-context.json"
            self.assertTrue(context_path.exists())
            context = json.loads(context_path.read_text(encoding="utf-8"))
            self.assertEqual(context["gate"]["mode"], "design_package")
            self.assertEqual(context["active_skills"]["skills"], [])
            for instructions in (
                root / "AGENTS.md",
                root / "CLAUDE.md",
                root / ".claude/rules/shared-memory.md",
            ):
                text = instructions.read_text(encoding="utf-8")
                self.assertIn("codex/ui_design/config.json", text)
                self.assertIn("codex/ui_design/effective-context.json", text)
                self.assertIn("codex/ui_design/active-skills.json", text)
                self.assertIn("codex/ui_design/approvals.json", text)
                self.assertIn("visible-interface", text)
            self.assertEqual(
                result["project"]["ui_design_status"], "configuration_required"
            )

    def test_effective_ui_context_merges_preferences_skills_and_gate_state(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            temp = pathlib.Path(value)
            root = temp / "project"
            root.mkdir()
            (root / ".git").mkdir()
            original_registry = memory_project.REGISTRY_PATH
            try:
                memory_project.REGISTRY_PATH = temp / "projects.json"
                with mock.patch.dict(
                    os.environ, {"UI_DESIGN_HOME": str(temp / "ui-design-home")}
                ):
                    memory_project.init_project(root)
                    global_value = preferences.default_global_preferences()
                    global_value["design_principles"] = ["calm hierarchy"]
                    preferences.save_global_preferences(global_value)
                    preferences.save_project_overrides(
                        root,
                        {
                            "design_principles": {
                                "mode": "append",
                                "value": ["clear primary action"],
                            }
                        },
                    )
                    memory_project.write_json(
                        root / "codex/ui_design/active-skills.json",
                        {
                            "schema_version": 1,
                            "execution_order": ["frontend-design", "ui-ux-pro-max"],
                            "skills": [
                                {"name": "frontend-design", "version": "pinned"}
                            ],
                        },
                    )
                    config = memory_project.ui_design_config(root)
                    config.update({"hard_gate_enabled": True, "relocked": False})
                    memory_project.write_json(
                        root / "codex/ui_design/config.json", config
                    )
                    memory_project.write_json(
                        root / "codex/ui_design/approvals.json",
                        {
                            "schema_version": 1,
                            "package_approvals": {"task-1": {"digest": "a" * 64}},
                            "project_global_approval": None,
                        },
                    )

                    snapshot = memory_project.publish_effective_ui_context(root)
            finally:
                memory_project.REGISTRY_PATH = original_registry

            self.assertEqual(
                snapshot["preferences"]["effective"]["value"]["design_principles"],
                ["calm hierarchy", "clear primary action"],
            )
            self.assertEqual(
                snapshot["active_skills"]["execution_order"],
                ["frontend-design", "ui-ux-pro-max"],
            )
            self.assertFalse(snapshot["gate"]["relocked"])
            self.assertIn("task-1", snapshot["gate"]["approvals"]["package_approvals"])

    def test_personal_noise_rejects_project_tasks_and_memory_console_commands(self) -> None:
        base = {
            "scope": "personal",
            "status": "pending",
            "content": "",
        }
        for content in (
            "用户偏好/工作方式：隔离审核台候选记忆并标记为拒绝。",
            "用户偏好/工作方式：更新项目员工开发文档和服务代码。",
        ):
            item = dict(base, content=content)
            self.assertTrue(review.is_noise_personal_candidate(item), content)

        durable = dict(
            base,
            content="**分类：协作偏好**\n\n用户希望跨项目每次修改代码前先确认修改计划。",
        )
        self.assertFalse(review.is_noise_personal_candidate(durable))

    def test_generated_project_hook_summarizes_short_memory(self) -> None:
        hook = memory_project.hook_script(pathlib.Path("/tmp/project"), "codex")
        self.assertIn('entry += "- summary: " + compact', hook)
        self.assertNotIn('prompt[:3000]', hook)
        self.assertNotIn("append_project_candidate", hook)
        self.assertIn("conversation model reviews memory candidates", hook)

    def test_hook_context_is_conditional_and_safe(self) -> None:
        hook = memory_project.hook_script(pathlib.Path("/tmp/project"), "codex")
        self.assertIn("def loop_context()", hook)
        self.assertIn("Loop × Superpowers", hook)
        self.assertIn("explicit user authorization", hook)
        self.assertIn("Loop configuration is invalid", hook)
        self.assertNotIn("oss_access_key", hook)
        self.assertNotIn("database_password", hook)

    def test_agent_candidate_is_structured_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            originals = {
                name: getattr(review, name)
                for name in (
                    "PERSONAL_PROPOSALS", "PERSONAL_LONG", "PERSONAL_SHORT",
                    "PROJECT_PROPOSALS", "PROJECT_LONG", "CODEX_DIR",
                )
            }
            original_build = review.build_queue
            try:
                review.PERSONAL_PROPOSALS = temp / "personal_proposals.md"
                review.PERSONAL_PROPOSALS.write_text("# Proposals\n", encoding="utf-8")
                review.PERSONAL_LONG = temp / "personal_long.md"
                review.PERSONAL_SHORT = temp / "personal_short.md"
                review.PROJECT_PROPOSALS = temp / "project_proposals.md"
                review.PROJECT_LONG = temp / "project_long.md"
                review.CODEX_DIR = temp / "codex"
                review.build_queue = lambda: {}
                first = review.create_agent_candidate(
                    "personal", "long", "collaboration_preference", "修改前确认计划",
                    "用户希望修改代码前先确认修改计划和不确定事项。",
                )
                second = review.create_agent_candidate(
                    "personal", "long", "collaboration_preference", "重复标题",
                    "用户希望修改代码前先确认修改计划和不确定事项。",
                )
            finally:
                review.build_queue = original_build
                for name, value in originals.items():
                    setattr(review, name, value)
            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            text = (temp / "personal_proposals.md").read_text(encoding="utf-8")
            self.assertIn("status: pending", text)
            self.assertIn("**标题：修改前确认计划**", text)

    def test_upgrade_memory_hooks_preserves_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            project = pathlib.Path(temp_value)
            old_hook = project / ".codex" / "hooks" / "shared_memory_hook.py"
            old_hook.parent.mkdir(parents=True)
            old_hook.write_text("old managed hook\n", encoding="utf-8")
            result = memory_project.upgrade_memory_hooks(project)
            self.assertIn("- summary: ", old_hook.read_text(encoding="utf-8"))
            backups = list(old_hook.parent.glob("shared_memory_hook.py.bak.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "old managed hook\n")
            self.assertTrue(any(item["status"] == "backup" for item in result["changes"]))

    def test_hook_upgrade_merges_ui_gate_entry_and_preserves_unrelated_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            project = pathlib.Path(temp_value)
            settings_path = project / ".codex/hooks.json"
            settings_path.parent.mkdir(parents=True)
            custom = {
                "custom": "keep",
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "CustomTool",
                            "hooks": [{"type": "command", "command": "custom-hook"}],
                        }
                    ],
                    "PostToolUse": [
                        {"hooks": [{"type": "command", "command": "post-hook"}]}
                    ],
                },
            }
            settings_path.write_text(json.dumps(custom), encoding="utf-8")

            first = memory_project.upgrade_memory_hooks(project)
            merged = json.loads(settings_path.read_text(encoding="utf-8"))
            second = memory_project.upgrade_memory_hooks(project)

            self.assertEqual(merged["custom"], "keep")
            self.assertEqual(merged["hooks"]["PostToolUse"], custom["hooks"]["PostToolUse"])
            commands = [
                hook["command"]
                for entry in merged["hooks"]["PreToolUse"]
                for hook in entry["hooks"]
            ]
            self.assertIn("custom-hook", commands)
            self.assertTrue(any("ui_design_gate_hook.py" in item for item in commands))
            self.assertTrue(any(item["status"] == "backup" for item in first["changes"]))
            self.assertFalse(any(item["status"] == "backup" for item in second["changes"]))

    def test_hook_upgrade_reports_malformed_settings_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            project = pathlib.Path(temp_value)
            settings_path = project / ".claude/settings.json"
            settings_path.parent.mkdir(parents=True)
            original = "{not-json\n"
            settings_path.write_text(original, encoding="utf-8")

            result = memory_project.upgrade_memory_hooks(project)

            self.assertEqual(settings_path.read_text(encoding="utf-8"), original)
            self.assertTrue(
                any(
                    item["path"] == str(settings_path.resolve())
                    and item["status"] == "conflict"
                    for item in result["changes"]
                )
            )

    def test_upgrade_managed_rules_preserves_user_text_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            project = pathlib.Path(temp_value)
            agents = project / "AGENTS.md"
            agents.write_text("# User rules\n\nKeep this exact text.\n", encoding="utf-8")
            self.assertTrue(hasattr(memory_project, "upgrade_memory_rules"))

            first = memory_project.upgrade_memory_rules(project)
            updated = agents.read_text(encoding="utf-8")
            second = memory_project.upgrade_memory_rules(project)

            self.assertIn("Keep this exact text.", updated)
            self.assertIn("codex/ui_design/effective-context.json", updated)
            self.assertIn("visible-interface", updated)
            self.assertIn(loop_superpowers.MANAGED_RULE_START, updated)
            self.assertEqual(updated, agents.read_text(encoding="utf-8"))
            self.assertTrue(any(item["status"] == "backup" for item in first["changes"]))
            self.assertFalse(any(item["status"] == "backup" for item in second["changes"]))

    def test_upgrade_managed_rules_reports_unmatched_marker_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            project = pathlib.Path(temp_value)
            agents = project / "AGENTS.md"
            original = f"user text\n{loop_superpowers.MANAGED_RULE_START}\nbroken\n"
            agents.write_text(original, encoding="utf-8")
            self.assertTrue(hasattr(memory_project, "upgrade_memory_rules"))

            result = memory_project.upgrade_memory_rules(project)

            self.assertEqual(agents.read_text(encoding="utf-8"), original)
            self.assertTrue(any(item["status"] == "conflict" for item in result["changes"]))

    def test_quarantine_preserves_source_and_marks_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            source = temp / "proposals.md"
            source.write_text("original candidate source\n", encoding="utf-8")
            review.CODEX_DIR = temp / "codex"
            review.PROJECT_STATE = review.CODEX_DIR / "memory_review_state.json"
            item = {
                "id": "M-20260719-000001",
                "scope": "personal",
                "status": "pending",
                "target": "long",
                "source_path": str(source),
                "content": "审核台候选记忆管理命令",
            }
            original_loader = review.load_queue
            original_record = review.record_decision
            decisions: dict[str, dict] = {}
            try:
                review.load_queue = lambda refresh=True: {"items": [item]}
                review.record_decision = lambda candidate_id, decision: decisions.setdefault(candidate_id, decision)
                ids = review.reject_noise_personal_candidates(dry_run=False)
            finally:
                review.load_queue = original_loader
                review.record_decision = original_record

            self.assertEqual(ids, [item["id"]])
            self.assertEqual(source.read_text(encoding="utf-8"), "original candidate source\n")
            self.assertEqual(decisions[item["id"]]["status"], "rejected")
            archive = review.CODEX_DIR / "memory_review_noise_personal.md"
            self.assertIn(item["id"], archive.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
