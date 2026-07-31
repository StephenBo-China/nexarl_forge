from __future__ import annotations

import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import public_release_check
import verify_release


class PublicReleaseCheckTest(unittest.TestCase):
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
