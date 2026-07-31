"""Release-candidate verification gate for the local memory manager."""

from __future__ import annotations

import argparse
import ast
import json
import os
import pathlib
import plistlib
import py_compile
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import memory_review_server
import vibe_memory_hooks
import vibe_memory_install
import vibe_memory_migration
import vibe_memory_paths
import public_release_check
from tests.test_vibe_memory_migration import build_complete_legacy_fixture


CHECK_KEYS = (
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
)


def _command_status(command: list[str], *, cwd: pathlib.Path | None = None, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        command,
        cwd=str(cwd or ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode == 0:
        return "ok"
    detail = (completed.stderr or completed.stdout or "command failed").strip().splitlines()[-1]
    return f"failed: {detail[:240]}"


def _public_tree_status(base: pathlib.Path | str) -> str:
    """Summarize the public-tree gate without exposing matched secret text."""
    violations = public_release_check.scan_tree(base)
    if not violations:
        return "ok"
    first = violations[0]
    path = str(first.get("path") or "<unknown-path>")
    pattern = str(first.get("pattern") or "unknown")
    return f"failed: {path} [{pattern}]"


def _compile_python() -> str:
    try:
        for path in sorted((ROOT / "scripts").glob("*.py")):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        ast.parse(
            (ROOT / "tests" / "test_macos_install_e2e.py").read_text(encoding="utf-8"),
            filename=str(ROOT / "tests" / "test_macos_install_e2e.py"),
        )
    except Exception as error:
        return f"failed: {error}"
    return "ok"


def _install_source(root: pathlib.Path, version: str | None = None) -> pathlib.Path:
    source = pathlib.Path(tempfile.mkdtemp(prefix="verify-release-source-"))
    for name in ("scripts", "templates", "docs"):
        shutil.copytree(root / name, source / name)
    for name in ("README.md", "LICENSE", "SECURITY.md", "release.json"):
        candidate = root / name
        if candidate.exists():
            shutil.copy2(candidate, source / name)
    if version is not None:
        manifest = json.loads((source / "release.json").read_text(encoding="utf-8"))
        manifest["app_version"] = version
        (source / "release.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return source


def _install_smoke(root: pathlib.Path) -> tuple[pathlib.Path, vibe_memory_paths.RuntimePaths]:
    home = pathlib.Path(tempfile.mkdtemp(prefix="verify-release-home-"))
    home.mkdir(parents=True, exist_ok=True)
    paths = vibe_memory_paths.for_home(home)
    vibe_memory_install.install_runtime(root, paths)
    vibe_memory_install.install_runtime_config(paths, port=18997, app_version="1.0.0")
    vibe_memory_install.install_launcher(paths)
    vibe_memory_install.prepare_data(paths)
    return home, paths


def _permissions_check(root: pathlib.Path) -> str:
    try:
        home, paths = _install_smoke(root)
        release = paths.install_root / "releases/1.0.0"
        checks = [paths.install_root, paths.install_root / "releases", release]
        checks.extend(path for path in release.rglob("*") if path.is_file() or path.is_dir())
        for path in checks:
            if path.is_symlink():
                continue
            expected = 0o700 if path.is_dir() else 0o600
            if stat.S_IMODE(path.stat().st_mode) != expected:
                return f"failed: unexpected mode {path}"
        shutil.rmtree(home, ignore_errors=True)
    except Exception as error:
        return f"failed: {error}"
    return "ok"


def _hook_check(root: pathlib.Path, agent: str) -> str:
    try:
        home, paths = _install_smoke(root)
        target = home / (".codex/hooks.json" if agent == "codex" else ".claude/settings.json")
        result = vibe_memory_hooks.repair(target, agent, paths.launcher)
        if result.get("status") not in {"created", "updated", "current"}:
            return f"failed: unexpected hook status {result}"
        if agent == "codex":
            text = target.read_text(encoding="utf-8")
            if "--agent codex" not in text:
                return "failed: codex hook missing managed command"
        else:
            text = target.read_text(encoding="utf-8")
            if "--agent claude-code" not in text:
                return "failed: claude hook missing managed command"
        shutil.rmtree(home, ignore_errors=True)
    except Exception as error:
        return f"failed: {error}"
    return "ok"


def _control_plane_check(root: pathlib.Path) -> str:
    try:
        with tempfile.TemporaryDirectory() as value:
            fixture = build_complete_legacy_fixture(pathlib.Path(value))
            result = vibe_memory_migration.validate_control_plane(
                fixture.paths,
                json.loads(fixture.paths.project_registry.read_text(encoding="utf-8")),
            )
            if any(status != "ok" for status in result.values()):
                return f"failed: {result}"
    except Exception as error:
        return f"failed: {error}"
    return "ok"


def _rollback_check(root: pathlib.Path) -> str:
    try:
        with tempfile.TemporaryDirectory() as value:
            home = pathlib.Path(value) / "home"
            home.mkdir()
            paths = vibe_memory_paths.for_home(home)
            source_1 = _install_source(root, "1.0.0")
            source_2 = _install_source(root, "1.1.0")
            vibe_memory_install.update(source_1, paths, port=18997)
            vibe_memory_install.update(source_2, paths, port=18998)
            vibe_memory_install.prepare_data(paths)
            memory = paths.personal_memory / "long.md"
            memory.write_text("approved after upgrade\n", encoding="utf-8")
            result = vibe_memory_install.rollback(paths)
            if result["current_version"] != "1.0.0":
                return f"failed: {result}"
            if memory.read_text(encoding="utf-8") != "approved after upgrade\n":
                return "failed: rollback touched memory data"
    except Exception as error:
        return f"failed: {error}"
    return "ok"


def _uninstall_check(root: pathlib.Path) -> str:
    try:
        home, paths = _install_smoke(root)
        memory = paths.personal_memory / "long.md"
        memory.write_text("keep me\n", encoding="utf-8")
        result = vibe_memory_install.uninstall(paths, remove_data=False)
        if result["data_retained"] is not True:
            return f"failed: {result}"
        if not memory.exists():
            return "failed: uninstall removed memory data"
        if (paths.install_root / "current").exists():
            return "failed: uninstall kept runtime link"
        shutil.rmtree(home, ignore_errors=True)
    except Exception as error:
        return f"failed: {error}"
    return "ok"


def checks(root: pathlib.Path | str) -> dict[str, str]:
    return {key: "pending" for key in CHECK_KEYS}


def evaluate_checks(root: pathlib.Path | str) -> dict[str, str]:
    base = pathlib.Path(root).expanduser().resolve()
    template = base / "templates" / "macos" / "com.noema.vibe-memory.plist"
    results = {
        "manifest": "ok",
        "python": _compile_python(),
        "unit_tests": _command_status([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=base),
        "install_e2e": _command_status([sys.executable, "-m", "unittest", "tests.test_macos_install_e2e", "-v"], cwd=base),
        "public_tree": _public_tree_status(base),
        "plist": "ok",
        "loopback": "ok",
        "permissions": _permissions_check(base),
        "codex_hook": _hook_check(base, "codex"),
        "claude_hook": _hook_check(base, "claude-code"),
        "control_plane": _control_plane_check(base),
        "rollback": _rollback_check(base),
        "uninstall": _uninstall_check(base),
    }
    try:
        vibe_memory_paths.read_release_manifest(base / "release.json")
    except Exception as error:
        results["manifest"] = f"failed: {error}"
    try:
        plistlib.loads(template.read_text(encoding="utf-8").encode("utf-8"))
    except Exception as error:
        results["plist"] = f"failed: {error}"
    try:
        host, port = memory_review_server.server_address({"MEMORY_REVIEW_HOST": "127.0.0.1", "MEMORY_REVIEW_PORT": "18997"})
        if (host, port) != ("127.0.0.1", 18997):
            raise ValueError("unexpected loopback result")
    except Exception as error:
        results["loopback"] = f"failed: {error}"
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", default=".")
    args = parser.parse_args(argv)
    result = evaluate_checks(args.tree)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if all(value == "ok" for value in result.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
