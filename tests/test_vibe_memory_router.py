from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import memory_project
from vibe_memory_router import resolve_registered_project


class VibeMemoryRouterTest(unittest.TestCase):
    def test_resolve_registered_project_prefers_deepest_matching_root(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = pathlib.Path(value)
            outer = base / "workspace"
            inner = outer / "nested-project"
            cwd = inner / "src" / "feature"
            cwd.mkdir(parents=True)

            resolved = resolve_registered_project(
                cwd,
                [{"root": str(outer)}, {"root": str(inner)}],
            )

            self.assertEqual(resolved, inner.resolve())

    def test_resolve_registered_project_returns_none_for_unregistered_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = pathlib.Path(value)
            registered = base / "registered"
            cwd = base / "unregistered"
            registered.mkdir()
            cwd.mkdir()

            self.assertIsNone(resolve_registered_project(cwd, [{"root": str(registered)}]))
            self.assertFalse((cwd / "codex").exists())

    def test_resolve_registered_project_ignores_malformed_entries(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value) / "project"
            cwd = root / "src"
            cwd.mkdir(parents=True)

            resolved = resolve_registered_project(
                cwd,
                [None, "not a project", {}, {"root": None}, {"root": ""}, {"root": str(root)}],
            )

            self.assertEqual(resolved, root.resolve())

    def test_resolve_registered_project_uses_canonical_symlink_paths(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = pathlib.Path(value)
            actual = base / "actual-project"
            actual.mkdir()
            link = base / "linked-project"
            try:
                link.symlink_to(actual, target_is_directory=True)
            except (NotImplementedError, OSError) as error:
                self.skipTest(f"symlinks unavailable: {error}")
            cwd = link / "src"
            cwd.mkdir()

            resolved = resolve_registered_project(cwd, [{"root": str(actual)}])

            self.assertEqual(resolved, actual.resolve())

    def test_register_project_allows_non_git_directory_and_makes_it_current(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = pathlib.Path(value)
            project = base / "ordinary-directory"
            project.mkdir()
            registry_path = base / "projects.json"
            original_registry = memory_project.REGISTRY_PATH
            memory_project.REGISTRY_PATH = registry_path
            try:
                data = memory_project.register_project(project)
            finally:
                memory_project.REGISTRY_PATH = original_registry

            self.assertEqual(data["current_project"], str(project.resolve()))
            self.assertEqual(data["projects"][0]["root"], str(project.resolve()))
            self.assertFalse(data["projects"][0]["is_git_repo"])
            self.assertEqual(json.loads(registry_path.read_text(encoding="utf-8")), data)
            self.assertFalse((project / "codex").exists())

    def test_register_project_rejects_invalid_roots_without_mutating_registry(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = pathlib.Path(value)
            registry_path = base / "projects.json"
            original = {"current_project": "/existing", "projects": [{"root": "/existing"}]}
            registry_path.write_text(json.dumps(original), encoding="utf-8")
            regular_file = base / "not-a-directory"
            regular_file.write_text("nope\n", encoding="utf-8")
            original_registry = memory_project.REGISTRY_PATH
            memory_project.REGISTRY_PATH = registry_path
            try:
                for invalid in (base / "does-not-exist", regular_file):
                    with self.subTest(invalid=invalid):
                        with self.assertRaisesRegex(ValueError, "existing directory"):
                            memory_project.register_project(invalid)
                        self.assertEqual(
                            json.loads(registry_path.read_text(encoding="utf-8")), original
                        )
            finally:
                memory_project.REGISTRY_PATH = original_registry


if __name__ == "__main__":
    unittest.main()
