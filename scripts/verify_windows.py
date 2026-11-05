"""Verify Windows-native runtime and shared business workflows in an isolated profile."""
from __future__ import annotations
import argparse
import ast
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
# Native alternatives cover macOS lifecycle, dir_fd, flock and launchctl tests.
# Shared business modules execute unchanged; explicit platform prerequisites
# are recorded by unittest as skips, never converted from arbitrary failures.
PATTERNS = (
    "test_windows*.py", "test_runtime_platform.py", "test_ui*.py",
    "test_worktree_flow.py", "test_loop_superpowers_rollout.py",
    "test_vibe_memory_paths.py", "test_vibe_memory_events.py",
    "test_public_release_check.py",
)


def worker(root):
    sys.path[:0] = [str(root / "scripts"), str(root / "tests")]
    suite = unittest.TestSuite()
    for pattern in PATTERNS:
        suite.addTests(unittest.TestLoader().discover(str(root / "tests"), pattern=pattern))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", default=str(ROOT))
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = pathlib.Path(args.tree).resolve()
    if os.name != "nt":
        parser.error("Windows Python is required; use verify_release.py on macOS")
    if args.worker:
        return worker(root)
    checks = {}
    try:
        for folder in ("scripts", "tests"):
            for path in (root / folder).rglob("*.py"):
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        checks["python_sources"] = "ok"
        sys.path.insert(0, str(root / "scripts"))
        import vibe_memory_paths
        manifest = vibe_memory_paths.read_release_manifest(root / "release.json")
        if "Windows" not in manifest.get("supported_platforms", []):
            raise ValueError("release does not declare Windows support")
        checks["manifest"] = "ok"
        import public_release_check
        violations = public_release_check.scan_tree(root)
        checks["public_tree"] = "ok" if not violations else "failed"
    except (OSError, ValueError, SyntaxError) as error:
        print(json.dumps({"preflight": "failed", "error": str(error)}, ensure_ascii=False))
        return 1
    with tempfile.TemporaryDirectory(prefix="forge-windows-gate-") as directory:
        home = pathlib.Path(directory)
        environment = os.environ.copy()
        environment.update(HOME=str(home), USERPROFILE=str(home),
            LOCALAPPDATA=str(home / "AppData/Local"), APPDATA=str(home / "AppData/Roaming"),
            PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
            GIT_CONFIG_GLOBAL=str(home / "gitconfig"))
        for name in ("MEMORY_REVIEW_PROJECT_ROOT", "MEMORY_REVIEW_PROJECT_REGISTRY", "UI_DESIGN_HOME",
                     "CODEX_UI_SKILLS_DIR", "CLAUDE_UI_SKILLS_DIR", "CODEX_WORKTREE_MANAGER_ROOT", "CODEX_WORKTREE_ROOT"):
            environment.pop(name, None)
        command = [sys.executable, "-B", str(root / "scripts/verify_windows.py"), "--tree", str(root), "--worker"]
        result = subprocess.run(command, cwd=root, env=environment, check=False)
        checks["native_and_shared_tests"] = "ok" if result.returncode == 0 else "failed"
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if all(value == "ok" for value in checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
