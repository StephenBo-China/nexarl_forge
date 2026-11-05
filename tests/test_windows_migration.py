import json
import os
import pathlib
import sys
import tempfile
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import memory_project
import vibe_memory_migration as migration
from unittest import mock

@unittest.skipUnless(os.name == "nt", "Windows legacy migration")
class WindowsMigrationTests(unittest.TestCase):
    def test_owned_legacy_hook_removed_custom_preserved_and_unregister_works(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "project"
            root.mkdir()
            registry = pathlib.Path(directory) / "registry.json"
            script = root / ".codex/hooks/shared_memory_hook.py"
            script.parent.mkdir(parents=True)
            script.write_text(memory_project.hook_script(root, "codex"), encoding="utf-8", newline="\n")
            document = root / ".codex/hooks.json"
            document.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": 'python ".codex/hooks/shared_memory_hook.py"'}, {"type": "command", "command": "echo custom"}]}]}}), encoding="utf-8")
            with mock.patch.object(memory_project, "REGISTRY_PATH", registry):
                memory_project.register_project(root)
                plan = migration.prepare_legacy_hook_cleanup(root)
                import windows_migration
                import runtime_platform
                original_write = runtime_platform.atomic_write
                before = document.read_bytes()
                def fail_audit(path, content, *args, **kwargs):
                    if pathlib.Path(path).parent.name == "migration_audits":
                        raise OSError("injected audit failure")
                    return original_write(path, content, *args, **kwargs)
                with mock.patch.object(runtime_platform, "atomic_write", side_effect=fail_audit):
                    with self.assertRaisesRegex(OSError, "injected audit failure"):
                        windows_migration.execute(plan, audit=True)
                self.assertEqual(document.read_bytes(), before)
                self.assertTrue(script.exists())
                result = migration.execute_legacy_hook_cleanup(plan)
                self.assertGreater(result["removed_handlers"], 0)
                self.assertIn("echo custom", document.read_text(encoding="utf-8"))
                self.assertNotIn("shared_memory_hook.py", document.read_text(encoding="utf-8"))
                self.assertFalse(script.exists())
                self.assertTrue(result["backups"])
                memory_project.unregister_project(root)
                self.assertEqual(memory_project.registry()["projects"], [])

    def test_safe_tree_digest_matches_package_protocol_and_enforces_limit(self):
        import ui_design_store
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "nested").mkdir()
            (root / "nested/SKILL.md").write_bytes(b"# A skill\n")
            (root / "SKILL.md").write_bytes(b"# Root\n")
            (root / "nested.txt").write_bytes(b"sibling file")
            self.assertEqual(migration.safe_tree_digest(root), ui_design_store.tree_digest(root))
            with self.assertRaises(ValueError):
                migration.safe_tree_digest(root, max_file_bytes=2)

    def test_skill_digest_inventory_and_traversal_rejection(self):
        import ui_design_store
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            package = home / "packages/example/v1"
            package.mkdir(parents=True)
            (package / "SKILL.md").write_bytes(b"# Example\n")
            record = {"version_id": "v1", "package_path": str(package), "digest": ui_design_store.tree_digest(package)}
            document = {"schema_version": 1, "drafts": {}, "packages": {"example": [record]}, "deployments": {}, "idempotency": {}}
            (home / "registry.json").write_text(json.dumps(document), encoding="utf-8")
            result = migration.inspect_ui_skill_digests(home)
            self.assertFalse(result["errors"])
            record["version_id"] = "..\\outside"
            (home / "registry.json").write_text(json.dumps(document), encoding="utf-8")
            self.assertTrue(migration.inspect_ui_skill_digests(home)["errors"])

    def test_upgrade_hooks_keeps_universal_native_entry_and_generated_script_compiles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            registry = root / "registry.json"
            project = root / "project"
            project.mkdir()
            with mock.patch.object(memory_project, "REGISTRY_PATH", registry):
                memory_project.register_project(project)
                memory_project.init_project(project)
                self.assertTrue(memory_project.upgrade_memory_hooks(project)["ok"])
                self.assertFalse((project / ".codex/hooks.json").exists())
                compile(memory_project.hook_script(project, "codex"), "generated_hook", "exec")

    def test_preflight_is_readonly_and_root_rebind_is_rejected(self):
        import windows_migration
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "project"
            root.mkdir()
            plan = windows_migration.prepare(root)
            self.assertEqual(list(root.iterdir()), [])
            self.assertFalse(windows_migration.preview(root)["errors"])
            self.assertEqual(list(root.iterdir()), [])
            root.rename(root.with_name("original"))
            root.mkdir()
            with self.assertRaisesRegex(ValueError, "binding"):
                windows_migration.execute(plan)
            self.assertEqual(list(root.iterdir()), [])

    def test_skill_import_rejects_windows_alias_and_junction_paths(self):
        import subprocess
        import ui_skill_sources
        import ui_skill_publisher
        import ui_skill_validator
        import zipfile
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for member in ("C:/outside/SKILL.md", "asset.txt:stream", "NUL.txt", "folder./SKILL.md"):
                archive = root / "unsafe.zip"
                with zipfile.ZipFile(archive, "w") as handle:
                    handle.writestr(member, "unsafe")
                with self.subTest(member=member), self.assertRaises(ui_skill_sources.SourceError):
                    ui_skill_sources.import_zip(archive, root / "destination")
                self.assertFalse((root / "destination").exists())
            actual = root / "actual"
            actual.mkdir()
            (actual / "SKILL.md").write_text("---\nname: example\ndescription: example\n---\n", encoding="utf-8")
            alias = root / "junction"
            result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(actual)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            try:
                with self.assertRaises(ValueError):
                    ui_skill_sources.import_local(alias, root / "destination")
                with self.assertRaises(ui_skill_publisher.ScopeConflict):
                    ui_skill_publisher._reject_symlink_ancestors(alias / "child", root)
                report = ui_skill_validator.validate_package(alias, installed_names=set())
                self.assertFalse(report["valid"])
                self.assertEqual(report["digest"], "")
                import public_release_check
                scanroot = root / "release"
                scanroot.mkdir()
                scan_alias = scanroot / "scripts"
                made = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(scan_alias), str(actual)], capture_output=True)
                self.assertEqual(made.returncode, 0, made.stderr)
                try:
                    violations = public_release_check.scan_tree(scanroot)
                    self.assertTrue(any(item["path"] == "scripts" and item["pattern"] == "release_asset_symlink" for item in violations), violations)
                finally:
                    scan_alias.rmdir()
            finally:
                alias.rmdir()
