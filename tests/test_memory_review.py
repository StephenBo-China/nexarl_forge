from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import memory_project
import memory_review_queue as review


class MemoryReviewQualityTest(unittest.TestCase):
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
