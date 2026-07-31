from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import public_release_check
import verify_release


class PublicReleaseCheckTest(unittest.TestCase):
    def test_scan_tree_checks_legacy_codex_assets_without_git_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            legacy_hook = root / ".codex" / "hooks" / "legacy.py"
            legacy_hook.parent.mkdir(parents=True)
            legacy_hook.write_text("HOME = '/Users/example'\n", encoding="utf-8")

            violations = public_release_check.scan_tree(root)

        self.assertIn(
            {
                "path": str(legacy_hook.resolve()),
                "pattern": "personal_path",
                "match": "/Users/",
            },
            violations,
        )

    def test_scan_tree_falls_back_when_git_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            legacy_hook = root / ".codex" / "hooks" / "legacy.py"
            legacy_hook.parent.mkdir(parents=True)
            legacy_hook.write_text("HOME = '/Users/example'\n", encoding="utf-8")

            with mock.patch.object(
                public_release_check.subprocess,
                "run",
                side_effect=FileNotFoundError("git"),
            ):
                try:
                    violations = public_release_check.scan_tree(root)
                except OSError as error:
                    self.fail(f"scan_tree did not use the filesystem fallback: {error}")

        self.assertIn(
            {
                "path": str(legacy_hook.resolve()),
                "pattern": "personal_path",
                "match": "/Users/",
            },
            violations,
        )

    def test_scan_tree_falls_back_when_root_is_nested_in_parent_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = pathlib.Path(temporary)
            root = parent / "public-tree"
            legacy_hook = root / ".codex" / "hooks" / "legacy.py"
            legacy_hook.parent.mkdir(parents=True)
            legacy_hook.write_text("HOME = '/Users/example'\n", encoding="utf-8")
            subprocess.run(["git", "init", "--quiet", str(parent)], check=True)

            violations = public_release_check.scan_tree(root)

        self.assertIn(
            {
                "path": str(legacy_hook.resolve()),
                "pattern": "personal_path",
                "match": "/Users/",
            },
            violations,
        )

    def test_scan_tree_checks_tracked_claude_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            legacy_rule = root / ".claude" / "rules" / "legacy.md"
            legacy_rule.parent.mkdir(parents=True)
            legacy_rule.write_text("Path: /Users/example\n", encoding="utf-8")
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "add", str(legacy_rule.relative_to(root))],
                check=True,
            )

            violations = public_release_check.scan_tree(root)

        self.assertIn(
            {
                "path": str(legacy_rule.resolve()),
                "pattern": "personal_path",
                "match": "/Users/",
            },
            violations,
        )

    def test_scan_tree_checks_tracked_client_asset_with_special_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            rule = root / ".claude" / "rules" / "记忆\nrule.md"
            rule.parent.mkdir(parents=True)
            rule.write_text("Path: /Users/example\n", encoding="utf-8")
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "add", str(rule.relative_to(root))],
                check=True,
            )

            violations = public_release_check.scan_tree(root)

        self.assertIn(
            {
                "path": str(rule.resolve()),
                "pattern": "personal_path",
                "match": "/Users/",
            },
            violations,
        )

    def test_scan_tree_ignores_local_client_runtime_configs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            for path in (root / ".codex" / "hooks.json", root / ".claude" / "settings.json"):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("path = '/Users/example'\n", encoding="utf-8")

            violations = public_release_check.scan_tree(root)

        self.assertEqual(violations, [])

    def test_scan_tree_reports_external_client_asset_symlink_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            root = base / "public-tree"
            external = base / "external.md"
            external.write_text("Path: /Users/example\n", encoding="utf-8")
            link = root / ".claude" / "rules" / "external.md"
            link.parent.mkdir(parents=True)
            link.symlink_to(external)

            violations = public_release_check.scan_tree(root)

        self.assertEqual(
            violations,
            [
                {
                    "path": str(root.resolve() / ".claude" / "rules" / "external.md"),
                    "pattern": "client_asset_symlink",
                    "match": "symlink client asset is not allowed",
                }
            ],
        )

    def test_scan_tree_reports_broken_client_asset_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            link = root / ".codex" / "hooks" / "broken.py"
            link.parent.mkdir(parents=True)
            link.symlink_to(root / "missing-target")

            violations = public_release_check.scan_tree(root)

        self.assertEqual(
            violations,
            [
                {
                    "path": str(root.resolve() / ".codex" / "hooks" / "broken.py"),
                    "pattern": "client_asset_symlink",
                    "match": "symlink client asset is not allowed",
                }
            ],
        )

    def test_active_release_files_contain_no_personal_absolute_path(self) -> None:
        violations = public_release_check.scan_tree(ROOT)
        self.assertEqual(violations, [])

    def test_runtime_state_patterns_are_ignored(self) -> None:
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for pattern in (
            "codex/memory_review_queue.json",
            "codex/memory_review_state.json",
            "*.bak.*",
        ):
            self.assertIn(pattern, text)

    def test_release_gate_has_all_required_checks(self) -> None:
        result = verify_release.checks(ROOT)
        self.assertEqual(
            set(result),
            {
                "manifest",
                "python",
                "unit_tests",
                "install_e2e",
                "public_tree",
                "plist",
                "loopback",
                "permissions",
                "codex_hook",
                "claude_hook",
                "control_plane",
                "rollback",
                "uninstall",
            },
        )


if __name__ == "__main__":
    unittest.main()
