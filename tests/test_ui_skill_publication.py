from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ui_skill_publisher as publisher
import ui_skill_discovery as discovery
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

    def test_project_scope_rejects_targets_for_a_different_project_before_writes(self) -> None:
        project_a = self.temp / "project-a"
        project_b = self.temp / "project-b"
        self.approved["scope"] = {"type": "project", "root": str(project_a)}
        targets = {
            "codex": project_b / ".agents/skills/sample-ui",
            "claude": project_b / ".claude/skills/sample-ui",
        }

        with self.assertRaises(publisher.ScopeConflict):
            publisher.publish(
                self.approved,
                targets=targets,
                project_root=project_b,
                idempotency_key="scope-conflict",
            )

        self.assertFalse(project_b.exists())
        self.assertFalse((self.temp / "ui-design-home/registry.json").exists())

    def test_global_scope_rejects_explicit_project_publication_before_writes(self) -> None:
        project = self.temp / "project"
        targets = {
            "codex": project / ".agents/skills/sample-ui",
            "claude": project / ".claude/skills/sample-ui",
        }

        with self.assertRaises(publisher.ScopeConflict):
            publisher.publish(
                {**self.approved, "scope": {"type": "global"}},
                targets=targets,
                project_root=project,
                idempotency_key="global-project-conflict",
            )

        self.assertFalse(project.exists())
        self.assertFalse((self.temp / "ui-design-home/registry.json").exists())

    def test_personal_scope_rejects_publication_before_writes(self) -> None:
        project = self.temp / "project"
        targets = {"codex": project / ".agents/skills/sample-ui"}

        with self.assertRaises(publisher.ScopeConflict):
            publisher.publish(
                {**self.approved, "scope": {"type": "personal"}},
                targets=targets,
                idempotency_key="personal-scope-conflict",
            )

        self.assertFalse(project.exists())
        self.assertFalse((self.temp / "ui-design-home/registry.json").exists())

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

    def test_publication_audit_failure_restores_registry_and_targets(self) -> None:
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
        idempotency_key = "audit-failure"

        original_audit = registry._audit

        def fail_publication_audit(event: str, record: dict[str, object]) -> None:
            if event == "draft_published":
                raise OSError("audit unavailable")
            original_audit(event, record)

        with mock.patch.object(registry, "_audit", side_effect=fail_publication_audit):
            with self.assertRaises(publisher.PublishError):
                publisher.publish(
                    approved,
                    targets=targets,
                    idempotency_key=idempotency_key,
                )

        current = registry.load_registry()
        self.assertEqual(registry.get_draft(draft["id"])["status"], "publish_failed")
        self.assertNotIn(idempotency_key, current["idempotency"])
        self.assertFalse(current["deployments"])
        self.assertFalse(self.codex_dir.exists())
        self.assertFalse(self.claude_dir.exists())

        retried = publisher.publish(
            approved,
            targets=targets,
            idempotency_key=idempotency_key,
        )
        self.assertEqual(retried["status"], "published")
        self.assertEqual(registry.get_draft(draft["id"])["status"], "published")
        self.assertIn(idempotency_key, registry.load_registry()["idempotency"])

    def test_all_publication_audits_failing_still_returns_publish_error_and_retries(self) -> None:
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
        idempotency_key = "all-audits-failure"

        with mock.patch.object(registry, "_audit", side_effect=OSError("audit unavailable")):
            with self.assertRaises(publisher.PublishError) as raised:
                publisher.publish(
                    approved,
                    targets=targets,
                    idempotency_key=idempotency_key,
                )
        self.assertEqual(str(raised.exception), "audit unavailable")

        current = registry.load_registry()
        self.assertEqual(registry.get_draft(draft["id"])["status"], "publish_failed")
        self.assertNotIn(idempotency_key, current["idempotency"])
        self.assertFalse(current["deployments"])
        self.assertFalse(self.codex_dir.exists())
        self.assertFalse(self.claude_dir.exists())

        retried = publisher.publish(
            approved,
            targets=targets,
            idempotency_key=idempotency_key,
        )
        self.assertEqual(retried["status"], "published")

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

    def test_discovery_classifies_managed_unknown_ignored_and_drifted(self) -> None:
        root = self.temp / "scan/codex"
        fixture = ROOT / "tests/fixtures/ui_skills/minimal"
        managed_path = root / "managed-ui"
        unknown_path = root / "unknown-ui"
        ignored_path = root / "ignored-ui"
        drifted_path = root / "drifted-ui"
        for path in (managed_path, unknown_path, ignored_path, drifted_path):
            shutil.copytree(fixture, path)
        (unknown_path / "extra").write_text("unknown", encoding="utf-8")
        (ignored_path / "extra").write_text("ignored", encoding="utf-8")
        (drifted_path / "extra").write_text("changed", encoding="utf-8")
        managed_digest = registry.package_digest(managed_path)
        ignored_digest = registry.package_digest(ignored_path)
        before = {path: path.stat().st_mtime_ns for path in root.rglob("*")}

        results = discovery.scan(
            {"codex": [root]},
            {
                "targets": {
                    "codex": {
                        "managed-ui": managed_digest,
                        "drifted-ui": "0" * 64,
                    }
                },
                "ignored_fingerprints": [ignored_digest],
            },
        )

        statuses = {item["name"]: item["status"] for item in results}
        self.assertEqual(statuses["managed-ui"], "managed")
        self.assertEqual(statuses["unknown-ui"], "unmanaged_discovered")
        self.assertEqual(statuses["ignored-ui"], "unmanaged_ignored")
        self.assertEqual(statuses["drifted-ui"], "drifted")
        after = {path: path.stat().st_mtime_ns for path in root.rglob("*")}
        self.assertEqual(before, after)

    def test_changed_ignored_fingerprint_is_visible_again(self) -> None:
        root = self.temp / "scan/claude"
        skill = root / "sample-ui"
        shutil.copytree(ROOT / "tests/fixtures/ui_skills/minimal", skill)
        old_digest = registry.package_digest(skill)
        discovery.ignore_fingerprint(old_digest)
        (skill / "changed").write_text("new", encoding="utf-8")

        results = discovery.scan_and_persist({"claude": [root]}, {"targets": {}})

        self.assertEqual(results[0]["status"], "unmanaged_discovered")
        state = discovery.load_discovery_state()
        self.assertEqual(state["ignored_fingerprints"], [old_digest])

    def test_duplicate_name_with_different_digest_is_flagged(self) -> None:
        first_root = self.temp / "scan/first"
        second_root = self.temp / "scan/second"
        first = first_root / "same-name"
        second = second_root / "same-name"
        fixture = ROOT / "tests/fixtures/ui_skills/minimal"
        shutil.copytree(fixture, first)
        shutil.copytree(fixture, second)
        (second / "changed").write_text("different", encoding="utf-8")

        results = discovery.scan(
            {"codex": [first_root, second_root]}, {"targets": {}}
        )

        self.assertEqual(len(results), 2)
        self.assertTrue(all(item["name_conflict"] for item in results))

    def test_discovery_ignores_malformed_managed_target_entries(self) -> None:
        root = self.temp / "scan/malformed"
        shutil.copytree(
            ROOT / "tests/fixtures/ui_skills/minimal", root / "sample-ui"
        )

        results = discovery.scan(
            {"codex": [root]}, {"targets": {"codex": ["not", "a", "mapping"]}}
        )

        self.assertEqual(results[0]["status"], "unmanaged_discovered")


if __name__ == "__main__":
    unittest.main()
