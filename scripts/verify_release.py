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
import socket
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any
import urllib.error
import urllib.request
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import memory_review_server
import memory_project
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
            non_ok = sorted(area for area, status in result.items() if status != "ok")
            if non_ok:
                return "failed: non-ok areas: " + ", ".join(non_ok)
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
            with mock.patch.object(
                vibe_memory_install,
                "activate_launch_agent",
                return_value={"status": "healthy"},
            ), mock.patch.object(
                vibe_memory_install, "bootout_launch_agent", return_value={"status": "absent"}
            ), mock.patch.object(
                vibe_memory_install, "smoke_managed_hooks", return_value={}
            ):
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


_INSTALLED_E2E_SERVICE = f"gui/{os.getuid()}/com.noema.vibe-memory"


def _installed_e2e_process(
    command: list[str | os.PathLike[str]],
    *,
    home: pathlib.Path,
    cwd: pathlib.Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [os.fspath(item) for item in command],
        cwd=cwd,
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _installed_e2e_require_zero(
    completed: subprocess.CompletedProcess[str], label: str
) -> None:
    if completed.returncode:
        detail = " | ".join(
            value.strip()
            for value in (completed.stdout, completed.stderr)
            if value and value.strip()
        ) or "command failed"
        raise RuntimeError(f"{label} failed: {detail[-500:]}")


def _installed_e2e_http(
    port: int,
    path: str,
    body: dict[str, object] | None = None,
) -> tuple[int, str, object]:
    url = f"http://127.0.0.1:{port}{path}"
    request = urllib.request.Request(
        url,
        data=(json.dumps(body).encode("utf-8") if body is not None else None),
        headers=({"Content-Type": "application/json"} if body is not None else {}),
        method=("POST" if body is not None else "GET"),
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        raw = response.read()
        content_type = response.headers.get_content_type()
        value: object = (
            json.loads(raw.decode("utf-8"))
            if content_type == "application/json"
            else raw.decode("utf-8")
        )
        return response.status, response.geturl(), value


def _installed_e2e_bootout() -> None:
    subprocess.run(
        ["/bin/launchctl", "bootout", _INSTALLED_E2E_SERVICE],
        capture_output=True,
        text=True,
        check=False,
    )


def _run_installed_release_e2e(root: pathlib.Path | str) -> str:
    """Exercise the public release exclusively through install.sh and its launcher."""
    if sys.platform != "darwin":
        return "skipped: macOS installed-runtime E2E"
    base = pathlib.Path(root).expanduser().resolve()
    existing = subprocess.run(
        ["/bin/launchctl", "print", _INSTALLED_E2E_SERVICE],
        capture_output=True,
        text=True,
        check=False,
    )
    if existing.returncode == 0:
        return "skipped: fixed product LaunchAgent label is already loaded"
    home: pathlib.Path | None = None
    launcher: pathlib.Path | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="vibe-memory-installed-e2e-") as value:
            fixture_base = pathlib.Path(value)
            fixture = build_complete_legacy_fixture(fixture_base)
            home = fixture.paths.personal_memory.parents[1]
            workspace = fixture.project_roots[0]
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = int(reservation.getsockname()[1])
            installed = _installed_e2e_process(
                [base / "install.sh", "--port", str(port)], home=home, cwd=base
            )
            _installed_e2e_require_zero(installed, "install.sh")
            launcher = home / ".local/bin/vibe-memory"
            if not launcher.is_file():
                raise RuntimeError("installed launcher is missing")
            installed_runtime = (
                home / "Library/Application Support/VibeMemory/current"
            ).resolve(strict=True)
            fixture_paths = vibe_memory_paths.for_home(home)
            with mock.patch.object(
                memory_project, "APP_ROOT", installed_runtime
            ), mock.patch.object(
                memory_project, "RUNTIME_PATHS", fixture_paths
            ), mock.patch.object(
                memory_project,
                "CODEX_LOOP_DIR",
                home / ".codex/loop_engineering",
            ), mock.patch.object(
                memory_project,
                "CLAUDE_LOOP_DIR",
                home / ".claude/loop_engineering",
            ):
                for agent, legacy_script in (
                    ("codex", workspace / ".codex/hooks/shared_memory_hook.py"),
                    ("claude", workspace / ".claude/hooks/shared_memory_hook.py"),
                ):
                    legacy_script.write_text(
                        memory_project.hook_script(workspace, agent), encoding="utf-8"
                    )
            doctor = _installed_e2e_process(
                [launcher, "doctor", "--json"], home=home, cwd=workspace
            )
            _installed_e2e_require_zero(doctor, "installed doctor")
            doctor_payload = json.loads(doctor.stdout)
            if doctor_payload.get("control_plane", {}).get("ok") is not True:
                raise RuntimeError("installed doctor rejected the control plane")

            status, url, health = _installed_e2e_http(port, "/health")
            release = json.loads((base / "release.json").read_text(encoding="utf-8"))
            if (
                status != 200
                or url != f"http://127.0.0.1:{port}/health"
                or not isinstance(health, dict)
                or health.get("ok") is not True
                or health.get("service") != "vibe-memory"
                or health.get("app_version") != release.get("app_version")
            ):
                raise RuntimeError("installed health identity is invalid")
            page_status, _, page = _installed_e2e_http(port, "/")
            if page_status != 200 or not isinstance(page, str) or 'id="first-run"' not in page:
                page_kind = type(page).__name__
                page_title = ""
                if isinstance(page, str) and "<title>" in page and "</title>" in page:
                    page_title = page.split("<title>", 1)[1].split("</title>", 1)[0]
                raise RuntimeError(
                    f"first-run wizard was not served: status={page_status} "
                    f"type={page_kind} title={page_title!r}"
                )
            for endpoint in ("/api/settings", "/api/projects", "/api/queue"):
                api_status, _, payload = _installed_e2e_http(port, endpoint)
                if api_status != 200 or not isinstance(payload, dict):
                    raise RuntimeError(f"installed endpoint failed: {endpoint}")
            _, _, first_run = _installed_e2e_http(
                port,
                "/api/settings/first-run",
                {
                    "codex_hooks": True,
                    "claude_hooks": False,
                    "automatic_candidate_checks": True,
                    "personal_short_retention_days": 30,
                    "start_at_login": True,
                    "service_port": port,
                    "workspace": str(workspace),
                },
            )
            if (
                not isinstance(first_run, dict)
                or first_run.get("settings", {}).get("first_run_complete") is not True
                or first_run.get("settings", {}).get("start_at_login") is not True
            ):
                raise RuntimeError("first-run settings were not applied")
            _, _, projects = _installed_e2e_http(port, "/api/projects")
            roots = {
                item.get("root")
                for item in projects.get("registry", {}).get("projects", [])
                if isinstance(item, dict)
            } if isinstance(projects, dict) else set()
            if str(workspace.resolve()) not in roots:
                raise RuntimeError("fixture workspace is not registered")

            preview = _installed_e2e_process(
                [launcher, "migrate", "preview", "--project-root", workspace],
                home=home,
                cwd=workspace,
            )
            _installed_e2e_require_zero(preview, "installed migration preview")
            preview_payload = json.loads(preview.stdout)
            if not preview_payload or preview_payload[0].get("managed_entries", 0) < 1:
                errors = preview_payload[0].get("errors", []) if preview_payload else []
                raise RuntimeError(
                    f"legacy fixture has no managed migration boundary: {errors!r}"
                )
            migrated = _installed_e2e_process(
                [
                    launcher,
                    "migrate",
                    "apply",
                    "--approved",
                    "--project-root",
                    workspace,
                ],
                home=home,
                cwd=workspace,
            )
            _installed_e2e_require_zero(migrated, "installed migration apply")
            migrated_payload = json.loads(migrated.stdout)
            item = migrated_payload.get("projects", [{}])[0]
            audit_path = pathlib.Path(str(item.get("audit", "")))
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            if (
                migrated_payload.get("status") != "applied"
                or audit.get("root") != str(workspace.resolve())
                or audit.get("result") != "applied"
                or not audit.get("changed_paths")
                or not (workspace / ".codex/hooks/ui_design_gate_hook.py").is_file()
                or (workspace / ".codex/hooks/shared_memory_hook.py").exists()
            ):
                raise RuntimeError("migration audit or ownership boundary is invalid")
            return "ok"
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError, urllib.error.URLError, subprocess.SubprocessError) as error:
        return f"failed: {str(error)[:500]}"
    finally:
        if launcher is not None and home is not None and launcher.is_file():
            try:
                _installed_e2e_process([launcher, "uninstall"], home=home, cwd=base)
            except (OSError, subprocess.SubprocessError):
                pass
        _installed_e2e_bootout()
        for _attempt in range(20):
            printed = subprocess.run(
                ["/bin/launchctl", "print", _INSTALLED_E2E_SERVICE],
                capture_output=True,
                text=True,
                check=False,
            )
            if printed.returncode != 0:
                break
            time.sleep(0.05)


def checks(root: pathlib.Path | str) -> dict[str, str]:
    return {key: "pending" for key in CHECK_KEYS}


def evaluate_checks(root: pathlib.Path | str) -> dict[str, str]:
    base = pathlib.Path(root).expanduser().resolve()
    template = base / "templates" / "macos" / "com.noema.vibe-memory.plist"
    results = {
        "manifest": "ok",
        "python": _compile_python(),
        "unit_tests": _command_status([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=base),
        "install_e2e": _run_installed_release_e2e(base),
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
