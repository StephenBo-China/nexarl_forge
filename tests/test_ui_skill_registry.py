from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ui_skill_registry as registry


class UISkillRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.temp = pathlib.Path(self.temporary.name)
        self.fixture = ROOT / "tests/fixtures/ui_skills/minimal"
        self.environment = mock.patch.dict(
            os.environ, {"UI_DESIGN_HOME": str(self.temp / "ui-design-home")}
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def test_draft_approval_creates_immutable_version(self) -> None:
        draft = registry.create_draft(
            name="sample-ui",
            source={"type": "local", "path": "/fixture"},
            package_root=self.fixture,
            scope={"type": "global"},
            targets=["codex", "claude"],
        )

        approved = registry.approve_draft(
            draft["id"], expected_digest=draft["digest"]
        )

        self.assertEqual(approved["status"], "approved")
        package_path = pathlib.Path(approved["package_path"])
        self.assertTrue(package_path.exists())
        self.assertEqual(registry.package_digest(package_path), draft["digest"])
        with self.assertRaises(registry.InvalidTransition):
            registry.approve_draft(draft["id"], expected_digest=draft["digest"])

    def test_changed_draft_is_rejected_by_expected_digest(self) -> None:
        draft = registry.create_draft(
            name="sample-ui",
            source={"type": "local", "path": "/fixture"},
            package_root=self.fixture,
            scope={"type": "global"},
            targets=["codex"],
        )
        draft_root = pathlib.Path(draft["draft_path"]) / "content"
        (draft_root / "SKILL.md").write_text("changed", encoding="utf-8")

        with self.assertRaises(registry.DigestConflict):
            registry.approve_draft(draft["id"], expected_digest=draft["digest"])

        self.assertEqual(registry.get_draft(draft["id"])["status"], "validated")

    def test_registry_records_source_scope_targets_and_audit(self) -> None:
        draft = registry.create_draft(
            name="sample-ui",
            source={"type": "github", "revision": "abc123"},
            package_root=self.fixture,
            scope={"type": "project", "root": "/tmp/project"},
            targets=["claude", "codex"],
        )

        loaded = registry.get_draft(draft["id"])
        self.assertEqual(loaded["source"]["revision"], "abc123")
        self.assertEqual(loaded["scope"]["type"], "project")
        self.assertEqual(loaded["targets"], ["claude", "codex"])
        audit = (self.temp / "ui-design-home/audit.jsonl").read_text(encoding="utf-8")
        self.assertIn(draft["id"], audit)


if __name__ == "__main__":
    unittest.main()
