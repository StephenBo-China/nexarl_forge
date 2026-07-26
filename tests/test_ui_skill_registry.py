from __future__ import annotations

import contextlib
import io
import json
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
import ui_skill_publisher as publisher
import memory_review


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

    def test_owned_workflow_skill_contains_required_approval_gate(self) -> None:
        root = ROOT / "templates/ui_design/skills/ui-design-workflow"
        report = validator.validate_package(root, installed_names=set())

        self.assertTrue(report["valid"], report)
        text = (root / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("frontend-design", text)
        self.assertIn("ui-ux-pro-max", text)
        self.assertIn("Do not modify formal frontend business code", text)
        self.assertIn("design-package-schema.md", text)
        self.assertIn("preference-schema.md", text)
        self.assertIn("codex/ui_design/effective-context.json", text)
        self.assertIn("pure_backend", text)
        self.assertIn("explicit user approval", text)
        self.assertLess(len(text.splitlines()), 500)
        self.assertTrue((root / "agents/openai.yaml").is_file())

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

    def test_frontend_design_bootstrap_is_pinned_and_common(self) -> None:
        destination = self.temp / "frontend-design"
        requests = []

        def downloader(request: dict[str, str], target: pathlib.Path) -> None:
            requests.append(request)
            shutil.copytree(self.fixture, target)

        result = sources.bootstrap_frontend_design(
            destination,
            revision=sources.FRONTEND_DESIGN_REVISION,
            downloader=downloader,
        )

        self.assertEqual(requests[0]["repository"], "anthropics/skills")
        self.assertEqual(requests[0]["path"], "skills/frontend-design")
        self.assertEqual(requests[0]["revision"], sources.FRONTEND_DESIGN_REVISION)
        self.assertEqual(result["variants"]["common"]["path"], ".")
        self.assertEqual(len(result["variants"]["common"]["digest"]), 64)

    def test_ui_ux_bootstrap_stages_variants_and_publication_never_runs_cli(self) -> None:
        bundle = self.temp / "ui-ux-pro-max"
        calls = []

        def runner(request: dict[str, str], target: pathlib.Path) -> dict:
            calls.append({"request": request, "target": str(target)})
            self.assertNotEqual(target.parent, bundle)
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text(
                "---\nname: ui-ux-pro-max\ndescription: Generated UI UX guidance.\n"
                f"---\n# {request['agent']} variant\n",
                encoding="utf-8",
            )
            return {"command": ["npx", "pinned-cli"], "stdout_summary": "generated"}

        source = sources.bootstrap_ui_ux_pro_max(
            bundle,
            release=sources.UI_UX_PRO_MAX_RELEASE,
            revision=sources.UI_UX_PRO_MAX_REVISION,
            cli_version=sources.UI_UX_PRO_MAX_CLI_VERSION,
            expected_npm_shasum=sources.UI_UX_PRO_MAX_NPM_SHASUM,
            npm_metadata=lambda package, version: {
                "name": package,
                "version": version,
                "dist": {"shasum": sources.UI_UX_PRO_MAX_NPM_SHASUM},
            },
            runner=runner,
        )
        draft = registry.create_draft(
            name="ui-ux-pro-max",
            source=source,
            package_root=bundle,
            scope={"type": "global"},
            targets=["codex", "claude"],
            version_label=sources.UI_UX_PRO_MAX_CLI_VERSION,
        )
        approved = registry.approve_draft(draft["id"], expected_digest=draft["digest"])
        before_publish = len(calls)
        targets = {
            "codex": self.temp / "published/codex/ui-ux-pro-max",
            "claude": self.temp / "published/claude/ui-ux-pro-max",
        }

        publisher.publish(
            approved,
            targets=targets,
            idempotency_key="variant-publish-001",
        )

        self.assertEqual(len(calls), before_publish)
        self.assertEqual({item["request"]["agent"] for item in calls}, {"codex", "claude"})
        self.assertEqual(source["release"], "v2.11.0")
        self.assertEqual(source["revision"], sources.UI_UX_PRO_MAX_REVISION)
        self.assertEqual(source["cli_version"], "2.11.0")
        self.assertEqual(source["npm_shasum"], sources.UI_UX_PRO_MAX_NPM_SHASUM)
        self.assertIn("# codex variant", (targets["codex"] / "SKILL.md").read_text(encoding="utf-8"))
        self.assertIn("# claude variant", (targets["claude"] / "SKILL.md").read_text(encoding="utf-8"))
        self.assertNotIn("variants", {path.name for path in targets["codex"].iterdir()})

    def test_bootstrap_cli_creates_validated_manager_workflow_draft(self) -> None:
        code, draft = self.run_cli(
            [
                "ui-skill",
                "bootstrap",
                "ui-design-workflow",
                "--idempotency-key",
                "bootstrap-workflow-001",
            ]
        )

        self.assertEqual(code, 0)
        self.assertEqual(draft["name"], "ui-design-workflow")
        self.assertEqual(draft["status"], "validated")
        self.assertEqual(draft["targets"], ["claude", "codex"])

    def run_cli(self, arguments: list[str]) -> tuple[int, dict]:
        original = sys.argv
        output = io.StringIO()
        try:
            sys.argv = ["memory_review.py", *arguments]
            with contextlib.redirect_stdout(output):
                code = memory_review.main()
        finally:
            sys.argv = original
        return code, json.loads(output.getvalue())

    def test_nested_cli_parses_pinned_github_import_and_requires_idempotency(self) -> None:
        parser = memory_review.build_parser()
        args = parser.parse_args(
            [
                "ui-skill",
                "import",
                "--github",
                "owner/repo",
                "--path",
                "skills/sample",
                "--revision",
                "abc123",
                "--scope",
                "global",
                "--targets",
                "codex,claude",
                "--idempotency-key",
                "import-001",
            ]
        )

        self.assertEqual(args.command, "ui-skill")
        self.assertEqual(args.ui_skill_command, "import")
        self.assertEqual(args.github, "owner/repo")
        self.assertEqual(args.idempotency_key, "import-001")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    ["ui-skill", "approve", "draft-1", "--digest", "0" * 64]
                )

    def test_cli_imports_local_skill_as_visible_draft_and_lists_it(self) -> None:
        code, imported = self.run_cli(
            [
                "ui-skill",
                "import",
                "--local",
                str(self.fixture),
                "--scope",
                "global",
                "--targets",
                "codex,claude",
                "--idempotency-key",
                "local-import-001",
            ]
        )
        list_code, listed = self.run_cli(["ui-skill", "list"])

        self.assertEqual(code, 0)
        self.assertEqual(list_code, 0)
        self.assertEqual(imported["status"], "validated")
        self.assertEqual(listed["items"][0]["id"], imported["id"])
        self.assertTrue(listed["items"][0]["validation_report"]["valid"])

    def test_cli_sets_project_preferences_and_shows_effective_value(self) -> None:
        project = self.temp / "project"
        override_file = self.temp / "override.json"
        override_file.write_text(
            json.dumps(
                {"visual.radius": {"mode": "replace", "value": "3px"}}
            ),
            encoding="utf-8",
        )

        set_code, saved = self.run_cli(
            [
                "ui-design",
                "preferences",
                "set-project",
                "--project",
                str(project),
                "--json-file",
                str(override_file),
                "--idempotency-key",
                "project-pref-001",
            ]
        )
        show_code, shown = self.run_cli(
            ["ui-design", "preferences", "show", "--project", str(project)]
        )

        self.assertEqual(set_code, 0)
        self.assertEqual(show_code, 0)
        self.assertEqual(saved["status"], "saved")
        self.assertEqual(shown["effective"]["value"]["visual"]["radius"], "3px")

    def test_cli_publish_uses_project_root_recorded_in_draft_scope(self) -> None:
        project = self.temp / "project-target"
        draft = registry.create_draft(
            name="sample-ui",
            source={"type": "local"},
            package_root=self.fixture,
            scope={"type": "project", "root": str(project)},
            targets=["codex", "claude"],
        )
        approved = registry.approve_draft(draft["id"], expected_digest=draft["digest"])

        code, published = self.run_cli(
            [
                "ui-skill",
                "publish",
                draft["id"],
                "--digest",
                approved["digest"],
                "--idempotency-key",
                "project-publish-001",
            ]
        )

        self.assertEqual(code, 0)
        self.assertEqual(published["status"], "published")
        self.assertTrue((project / ".agents/skills/sample-ui/SKILL.md").exists())
        self.assertTrue((project / ".claude/skills/sample-ui/SKILL.md").exists())

        disable_code, disabled = self.run_cli(
            [
                "ui-skill",
                "disable",
                "sample-ui",
                "--idempotency-key",
                "project-disable-001",
            ]
        )
        self.assertEqual(disable_code, 0)
        self.assertEqual(disabled["status"], "disabled")
        self.assertFalse((project / ".agents/skills/sample-ui").exists())
        self.assertFalse((project / ".claude/skills/sample-ui").exists())


if __name__ == "__main__":
    unittest.main()
