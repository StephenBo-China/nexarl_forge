from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ui_skill_registry as registry
import ui_skill_sources as sources
import ui_skill_validator as validator


class UISkillRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.temp = pathlib.Path(self.temporary.name)
        self.fixture = ROOT / "tests/fixtures/ui_skills/minimal"
        self.with_script = ROOT / "tests/fixtures/ui_skills/with-script"
        self.broken = self.temp / "broken"
        self.broken.mkdir()
        (self.broken / "SKILL.md").write_text(
            """---
name: sample-ui
description: Broken duplicate skill.
---

[Missing reference](references/nope.md)
""",
            encoding="utf-8",
        )
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

    def test_validator_reports_scripts_without_running_them(self) -> None:
        marker = self.with_script / "executed"

        report = validator.validate_package(self.with_script, installed_names=set())

        self.assertTrue(report["valid"], report)
        self.assertEqual(report["scripts"][0]["path"], "scripts/build.py")
        self.assertFalse(marker.exists())
        self.assertEqual(report["license"], "MIT")
        self.assertEqual(len(report["digest"]), 64)

    def test_validator_rejects_missing_reference_and_name_conflict(self) -> None:
        report = validator.validate_package(
            self.broken, installed_names={"sample-ui"}
        )

        self.assertFalse(report["valid"])
        codes = {item["code"] for item in report["errors"]}
        self.assertIn("name_conflict", codes)
        self.assertIn("missing_reference", codes)

    def test_validator_rejects_symlinks_and_invalid_metadata(self) -> None:
        package = self.temp / "unsafe"
        package.mkdir()
        (package / "SKILL.md").write_text(
            "---\nname: Bad Name\ndescription:\n---\n", encoding="utf-8"
        )
        (package / "escape").symlink_to(self.fixture / "SKILL.md")

        report = validator.validate_package(package, installed_names=set())

        codes = {item["code"] for item in report["errors"]}
        self.assertIn("invalid_name", codes)
        self.assertIn("missing_description", codes)
        self.assertIn("symlink", codes)

    def test_validator_checks_references_in_nested_markdown_files(self) -> None:
        package = self.temp / "nested-reference"
        (package / "references").mkdir(parents=True)
        (package / "SKILL.md").write_text(
            "---\nname: nested-ui\ndescription: Nested reference test.\n---\n"
            "[Guide](references/guide.md)\n",
            encoding="utf-8",
        )
        (package / "references/guide.md").write_text(
            "[Missing](details.md)\n", encoding="utf-8"
        )

        report = validator.validate_package(package, installed_names=set())

        self.assertIn(
            "missing_reference", {item["code"] for item in report["errors"]}
        )

    def test_local_editor_and_valid_zip_sources_create_normalized_packages(self) -> None:
        local_destination = self.temp / "local-import"
        editor_destination = self.temp / "editor-import"
        zip_destination = self.temp / "zip-import"
        archive = self.temp / "skill.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("package/SKILL.md", (self.fixture / "SKILL.md").read_bytes())

        local = sources.import_local(self.fixture, local_destination)
        editor = sources.import_editor(
            {"SKILL.md": (self.fixture / "SKILL.md").read_text(encoding="utf-8")},
            editor_destination,
        )
        zipped = sources.import_zip(archive, zip_destination)

        self.assertEqual(local["type"], "local")
        self.assertEqual(editor["type"], "editor")
        self.assertEqual(zipped["type"], "zip")
        for destination in (local_destination, editor_destination, zip_destination):
            self.assertTrue((destination / "SKILL.md").is_file())

    def test_zip_slip_symlink_and_limits_are_rejected_before_extraction(self) -> None:
        bad_zip = self.temp / "bad.zip"
        with zipfile.ZipFile(bad_zip, "w") as handle:
            handle.writestr("../../escape", "bad")
            handle.writestr("SKILL.md", "---\nname: bad\ndescription: bad\n---\n")
        with self.assertRaises(sources.SourceError):
            sources.import_zip(bad_zip, self.temp / "bad-output")
        self.assertFalse((self.temp / "escape").exists())

        symlink_zip = self.temp / "symlink.zip"
        symlink = zipfile.ZipInfo("link")
        symlink.create_system = 3
        symlink.external_attr = 0o120777 << 16
        with zipfile.ZipFile(symlink_zip, "w") as handle:
            handle.writestr("SKILL.md", "---\nname: bad\ndescription: bad\n---\n")
            handle.writestr(symlink, "SKILL.md")
        with self.assertRaises(sources.SourceError):
            sources.import_zip(symlink_zip, self.temp / "symlink-output")

        with self.assertRaises(sources.SourceError):
            sources.import_zip(
                self.temp / "skill.zip", self.temp / "limited-output", max_files=0
            )

    def test_github_adapter_records_pinned_revision_with_injected_downloader(self) -> None:
        destination = self.temp / "github-import"

        result = sources.import_github(
            "owner/repo",
            "skills/sample",
            "abc123",
            destination,
            downloader=lambda request, target: shutil.copytree(self.fixture, target),
        )

        self.assertEqual(result["revision"], "abc123")
        self.assertEqual(result["repository"], "owner/repo")
        self.assertTrue((destination / "SKILL.md").exists())


if __name__ == "__main__":
    unittest.main()
