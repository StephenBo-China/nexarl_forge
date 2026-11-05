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
