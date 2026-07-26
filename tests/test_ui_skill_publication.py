from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ui_skill_publisher as publisher
import ui_skill_registry as registry


class UISkillPublicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.temp = pathlib.Path(self.temporary.name)
        self.environment = mock.patch.dict(
            os.environ,
            {
                "UI_DESIGN_HOME": str(self.temp / "ui-design-home"),
                "CODEX_UI_SKILLS_DIR": str(self.temp / "global-codex"),
                "CLAUDE_UI_SKILLS_DIR": str(self.temp / "global-claude"),
            },
        )
        self.environment.start()
        self.package = self.temp / "approved-package"
        self.package.mkdir()
        (self.package / "SKILL.md").write_text("new package", encoding="utf-8")
        (self.package / "VERSION").write_text("new", encoding="utf-8")
        self.codex_dir = self.temp / "targets/codex/sample-ui"
        self.claude_dir = self.temp / "targets/claude/sample-ui"
        self.approved = {
            "id": "draft-test",
            "name": "sample-ui",
            "status": "approved",
            "version_id": "2.0.0+new",
            "package_path": str(self.package),
            "digest": registry.package_digest(self.package),
        }

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def seed_target(self, path: pathlib.Path, version: str) -> str:
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(f"{version} package", encoding="utf-8")
        (path / "VERSION").write_text(version, encoding="utf-8")
        return registry.package_digest(path)

    def test_second_target_failure_restores_both_previous_versions(self) -> None:
        old_codex = self.seed_target(self.codex_dir, "old")
        old_claude = self.seed_target(self.claude_dir, "old")
        self.approved["previous_target_digests"] = {
            "codex": old_codex,
            "claude": old_claude,
        }

        def fail_claude(source: pathlib.Path, target: pathlib.Path) -> None:
            if "claude" in str(target):
                raise OSError("boom")
            os.replace(source, target)

        with self.assertRaises(publisher.PublishError):
            publisher.publish(
                self.approved,
                targets={"codex": self.codex_dir, "claude": self.claude_dir},
                idempotency_key="publish-failure",
                replace=fail_claude,
            )

        self.assertEqual((self.codex_dir / "VERSION").read_text(), "old")
        self.assertEqual((self.claude_dir / "VERSION").read_text(), "old")

    def test_successful_publish_is_idempotent_and_updates_draft_status(self) -> None:
        fixture = ROOT / "tests/fixtures/ui_skills/minimal"
        draft = registry.create_draft(
            name="sample-ui",
            source={"type": "local"},
            package_root=fixture,
            scope={"type": "global"},
            targets=["codex", "claude"],
        )
        approved = registry.approve_draft(draft["id"], expected_digest=draft["digest"])
        targets = {"codex": self.codex_dir, "claude": self.claude_dir}

        first = publisher.publish(
            approved, targets=targets, idempotency_key="publish-success"
        )
        second = publisher.publish(
            approved, targets=targets, idempotency_key="publish-success"
        )

        self.assertEqual(first, second)
        self.assertEqual(registry.get_draft(draft["id"])["status"], "published")
        self.assertEqual(registry.package_digest(self.codex_dir), approved["digest"])
        self.assertEqual(registry.package_digest(self.claude_dir), approved["digest"])

    def test_external_target_change_blocks_publish_and_disable(self) -> None:
        self.seed_target(self.codex_dir, "unmanaged")
        targets = {"codex": self.codex_dir, "claude": self.claude_dir}

        with self.assertRaises(publisher.TargetDigestConflict):
            publisher.publish(
                self.approved, targets=targets, idempotency_key="conflict"
            )
        with self.assertRaises(publisher.TargetDigestConflict):
            publisher.disable(
                name="sample-ui",
                targets=targets,
                expected_target_digests={},
                idempotency_key="disable-conflict",
            )
        self.assertTrue(self.codex_dir.exists())

    def test_disable_and_rollback_use_the_same_two_target_transaction(self) -> None:
        current_codex = self.seed_target(self.codex_dir, "current")
        current_claude = self.seed_target(self.claude_dir, "current")
        targets = {"codex": self.codex_dir, "claude": self.claude_dir}
        expected = {"codex": current_codex, "claude": current_claude}

        disabled = publisher.disable(
            name="sample-ui",
            targets=targets,
            expected_target_digests=expected,
            idempotency_key="disable-managed",
        )

        self.assertEqual(disabled["status"], "disabled")
        self.assertFalse(self.codex_dir.exists())
        self.assertFalse(self.claude_dir.exists())

        rolled_back = publisher.rollback(
            self.approved,
            targets=targets,
            expected_target_digests={"codex": None, "claude": None},
            idempotency_key="rollback-managed",
        )
        self.assertEqual(rolled_back["operation"], "rollback")
        self.assertTrue(self.codex_dir.exists())
        self.assertTrue(self.claude_dir.exists())

    def test_resolve_targets_separates_global_and_project_scopes(self) -> None:
        global_targets = publisher.resolve_targets({"type": "global"})
        project = self.temp / "project"
        project_targets = publisher.resolve_targets(
            {"type": "project"}, project_root=project
        )

        self.assertEqual(global_targets["codex"], self.temp / "global-codex")
        self.assertEqual(global_targets["claude"], self.temp / "global-claude")
        self.assertEqual(project_targets["codex"], project / ".agents/skills")
        self.assertEqual(project_targets["claude"], project / ".claude/skills")

    def test_registry_commit_failure_restores_both_previous_targets(self) -> None:
        old_codex = self.seed_target(self.codex_dir, "old")
        old_claude = self.seed_target(self.claude_dir, "old")
        self.approved["previous_target_digests"] = {
            "codex": old_codex,
            "claude": old_claude,
        }
        targets = {"codex": self.codex_dir, "claude": self.claude_dir}

        with mock.patch.object(
            registry, "record_deployment", side_effect=OSError("registry unavailable")
        ):
            with self.assertRaises(publisher.PublishError):
                publisher.publish(
                    self.approved,
                    targets=targets,
                    idempotency_key="registry-failure",
                )

        self.assertEqual((self.codex_dir / "VERSION").read_text(), "old")
        self.assertEqual((self.claude_dir / "VERSION").read_text(), "old")

    def test_idempotency_key_cannot_be_reused_for_different_targets(self) -> None:
        targets = {"codex": self.codex_dir, "claude": self.claude_dir}
        publisher.publish(
            self.approved, targets=targets, idempotency_key="stable-key"
        )
        different = {
            "codex": self.temp / "other/codex/sample-ui",
            "claude": self.temp / "other/claude/sample-ui",
        }

        with self.assertRaises(publisher.IdempotencyConflict):
            publisher.publish(
                self.approved, targets=different, idempotency_key="stable-key"
            )


if __name__ == "__main__":
    unittest.main()
