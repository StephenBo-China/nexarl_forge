from __future__ import annotations

import contextlib
import errno
import io
import json
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
    def test_docs_scan_preserves_markdown_boundary_for_symlinks_and_nested_plans(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            root = base / "public-tree"
            docs = root / "docs"
            docs.mkdir(parents=True)
            external = base / "external"
            external.mkdir()
            (external / "note.txt").write_text(
                "Path: /Users/example\n", encoding="utf-8"
            )
            (external / "guide.md").write_text(
                "Path: /Users/example\n", encoding="utf-8"
            )
            (docs / "note.txt").symlink_to(external / "note.txt")
            (docs / "guide.md").symlink_to(external / "guide.md")
            nested = docs / "nested" / "plans"
            nested.mkdir(parents=True)
            (nested / "secret.md").write_text(
                "token = 'unique-nested-secret'\n", encoding="utf-8"
            )

            violations = public_release_check.scan_tree(root)

        self.assertEqual(
            [(violation["path"], violation["pattern"]) for violation in violations],
            [("docs/guide.md", "release_asset_symlink")],
        )

    def test_scan_tree_rejects_external_release_file_symlink_without_reading_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            root = base / "public-tree"
            root.mkdir()
            external = base / "external-readme.md"
            external.write_text("Path: /Users/example\n", encoding="utf-8")
            (root / "README.md").symlink_to(external)

            violations = public_release_check.scan_tree(root)

        self.assertEqual(
            [(violation["path"], violation["pattern"]) for violation in violations],
            [("README.md", "release_asset_symlink")],
        )

    def test_scan_tree_rejects_release_directory_symlinks_without_recursing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            root = base / "public-tree"
            root.mkdir()
            expected: list[tuple[str, str]] = []
            for directory_name in ("scripts", "templates"):
                directory = root / directory_name
                directory.mkdir()
                external = base / f"external-{directory_name}"
                external.mkdir()
                (external / "leaked.py").write_text(
                    "token = 'unique-external-secret'\n", encoding="utf-8"
                )
                (directory / "external").symlink_to(external, target_is_directory=True)
                expected.append((f"{directory_name}/external", "release_asset_symlink"))

            violations = public_release_check.scan_tree(root)

        self.assertEqual(
            [(violation["path"], violation["pattern"]) for violation in violations],
            expected,
        )

    def test_scan_tree_rejects_broken_release_file_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            link = root / "README.md"
            link.symlink_to(root / "missing-readme.md")

            violations = public_release_check.scan_tree(root)

        self.assertEqual(
            [(violation["path"], violation["pattern"]) for violation in violations],
            [("README.md", "release_asset_symlink")],
        )

    def test_unreadable_violation_redacts_absolute_error_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            blocked = root / "docs" / "blocked.md"
            blocked.parent.mkdir(parents=True)
            blocked.write_text("documentation\n", encoding="utf-8")
            original_read_text = pathlib.Path.read_text

            def read_text_with_permission_error(
                path: pathlib.Path, *args: object, **kwargs: object
            ) -> str:
                if path == blocked.resolve():
                    raise PermissionError(errno.EACCES, "denied", str(blocked))
                return original_read_text(path, *args, **kwargs)

            with mock.patch.object(
                pathlib.Path,
                "read_text",
                new=read_text_with_permission_error,
            ):
                violations = public_release_check.scan_tree(root)

        serialized = json.dumps(violations, ensure_ascii=False)
        self.assertNotIn(str(root), serialized)
        self.assertEqual(violations[0]["path"], "docs/blocked.md")
        self.assertEqual(violations[0]["pattern"], "unreadable")
        self.assertEqual(violations[0]["match"], "PermissionError")

    def test_scan_tree_redacts_credential_and_verification_matches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            readme = root / "README.md"
            readme.write_text(
                "token = 'unique-credential-secret'\nverification_code = 123456\n",
                encoding="utf-8",
            )

            violations = public_release_check.scan_tree(root)

        by_pattern = {violation["pattern"]: violation for violation in violations}
        self.assertEqual(by_pattern["credential_assignment"]["match"], "[redacted]")
        self.assertEqual(
            by_pattern["verification_code_assignment"]["match"], "[redacted]"
        )

    def test_public_release_cli_omits_match_and_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            secret = "unique-cli-credential-secret"
            (root / "README.md").write_text(
                f"token = '{secret}'\n", encoding="utf-8"
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = public_release_check.main(["--tree", str(root)])

        rendered = output.getvalue()
        self.assertEqual(status, 1)
        self.assertNotIn(secret, rendered)
        self.assertNotIn(str(root), rendered)
        self.assertNotIn('"match"', rendered)
        self.assertEqual(
            json.loads(rendered),
            {
                "status": "failed",
                "violations": [
                    {"path": "README.md", "pattern": "credential_assignment"}
                ],
            },
        )

    def test_evaluate_checks_wires_public_tree_status_helper(self) -> None:
        status = "failed: README.md [personal_path]"
        patches = {
            "_command_status": mock.Mock(return_value="ok"),
            "_compile_python": mock.Mock(return_value="ok"),
            "_permissions_check": mock.Mock(return_value="ok"),
            "_hook_check": mock.Mock(return_value="ok"),
            "_control_plane_check": mock.Mock(return_value="ok"),
            "_rollback_check": mock.Mock(return_value="ok"),
            "_uninstall_check": mock.Mock(return_value="ok"),
            "_public_tree_status": mock.Mock(return_value=status),
        }
        with mock.patch.multiple(verify_release, **patches):
            result = verify_release.evaluate_checks(ROOT)

        self.assertEqual(result["public_tree"], status)
        patches["_public_tree_status"].assert_called_once_with(ROOT.resolve())

    def test_scan_tree_reports_root_relative_path_for_unreadable_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            blocked = root / "docs" / "blocked.md"
            blocked.parent.mkdir(parents=True)
            blocked.write_text("documentation\n", encoding="utf-8")
            original_read_text = pathlib.Path.read_text

            def read_text_with_permission_error(
                path: pathlib.Path, *args: object, **kwargs: object
            ) -> str:
                if path == blocked.resolve():
                    raise PermissionError("permission denied")
                return original_read_text(path, *args, **kwargs)

            with mock.patch.object(
                pathlib.Path,
                "read_text",
                new=read_text_with_permission_error,
            ):
                violations = public_release_check.scan_tree(root)

        self.assertIn(
            {"path": "docs/blocked.md", "pattern": "unreadable"},
            [
                {"path": violation["path"], "pattern": violation["pattern"]}
                for violation in violations
            ],
        )

    def test_scan_tree_reports_root_relative_path_for_public_file_violation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            guide = root / "docs" / "guide.md"
            guide.parent.mkdir(parents=True)
            guide.write_text("Example home: /Users/example\n", encoding="utf-8")

            violations = public_release_check.scan_tree(root)

        self.assertIn(
            {
                "path": "docs/guide.md",
                "pattern": "personal_path",
                "match": "/Users/",
            },
            violations,
        )

    def test_public_tree_status_exposes_first_relative_path_and_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            guide = root / "docs" / "guide.md"
            guide.parent.mkdir(parents=True)
            guide.write_text("Example home: /Users/example\n", encoding="utf-8")

            status = verify_release._public_tree_status(root)

        self.assertEqual(status, "failed: docs/guide.md [personal_path]")

    def test_scan_tree_checks_legacy_codex_assets_without_git_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            legacy_hook = root / ".codex" / "hooks" / "legacy.py"
            legacy_hook.parent.mkdir(parents=True)
            legacy_hook.write_text("HOME = '/Users/example'\n", encoding="utf-8")

            violations = public_release_check.scan_tree(root)

        self.assertIn(
            {
                "path": ".codex/hooks/legacy.py",
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
                "path": ".codex/hooks/legacy.py",
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
                "path": ".codex/hooks/legacy.py",
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
                "path": ".claude/rules/legacy.md",
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
                "path": ".claude/rules/记忆\nrule.md",
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
                    "path": ".claude/rules/external.md",
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
                    "path": ".codex/hooks/broken.py",
                    "pattern": "client_asset_symlink",
                    "match": "symlink client asset is not allowed",
                }
            ],
        )

    def test_scan_tree_reports_symlinked_client_root_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            root = base / "public-tree"
            external = base / "external-client"
            external.mkdir(parents=True)
            (external / "legacy.md").write_text("Path: /Users/example\n", encoding="utf-8")
            root.mkdir()
            client_root = root / ".claude"
            client_root.symlink_to(external, target_is_directory=True)

            violations = public_release_check.scan_tree(root)

        self.assertEqual(
            violations,
            [
                {
                    "path": ".claude",
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
