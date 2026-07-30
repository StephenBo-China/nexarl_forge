from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import vibe_memory_paths


class RuntimePathsTest(unittest.TestCase):
    def test_paths_are_derived_from_supplied_home(self) -> None:
        home = pathlib.Path("/portable/home")

        paths = vibe_memory_paths.for_home(home)

        self.assertEqual(paths.personal_memory, home / ".codex/personal_memory")
        self.assertEqual(paths.project_registry, home / ".codex/memory_review/projects.json")
        self.assertEqual(paths.install_root, home / "Library/Application Support/VibeMemory")
        self.assertEqual(paths.ui_design_home, home / ".codex/ui_design")
        self.assertEqual(paths.worktree_manager, home / ".codex/worktree_manager")
        self.assertEqual(paths.worktree_root, home / "Projects/worktrees")
        with self.assertRaises(AttributeError):
            paths.personal_memory = home

    def test_release_manifest_is_supported_on_macos(self) -> None:
        manifest = vibe_memory_paths.read_release_manifest(ROOT / "release.json")

        self.assertEqual(manifest["app_version"], "1.0.0")
        self.assertEqual(manifest["data_schema_version"], 1)
        self.assertEqual(manifest["hook_protocol_version"], 1)
        self.assertEqual(manifest["minimum_python"], "3.10")
        self.assertEqual(manifest["platform"], "macOS")

    def test_release_manifest_rejects_non_object_root(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            manifest_path = pathlib.Path(value) / "release.json"
            manifest_path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")

            with self.assertRaises(ValueError):
                vibe_memory_paths.read_release_manifest(manifest_path)

    def test_release_manifest_rejects_missing_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            manifest_path = pathlib.Path(value) / "release.json"
            manifest_path.write_text(json.dumps({"app_version": "1.0.0"}), encoding="utf-8")

            with self.assertRaises(ValueError):
                vibe_memory_paths.read_release_manifest(manifest_path)
