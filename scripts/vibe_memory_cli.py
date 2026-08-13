#!/usr/bin/env python3
"""Fail-open command line entry point for shared memory hooks."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from collections.abc import Sequence

# Hook mode must remain independently fail-open when only this entry point is
# present. Lifecycle dependencies are optional at import time for that reason.
try:
    import memory_project
    import memory_review_queue
    import vibe_memory_hooks
    import vibe_memory_install
    import vibe_memory_migration
    import vibe_memory_paths
except ImportError:
    pass


DOCTOR_KEYS = ("runtime", "codex_hooks", "claude_hooks", "service", "data")


class LifecycleError(RuntimeError):
    """An intentionally safe lifecycle error suitable for terminal output."""


def _home(paths: vibe_memory_paths.RuntimePaths) -> pathlib.Path:
    return paths.personal_memory.parents[1]


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: object, fp: object, code: int, msg: str, headers: object, newurl: str) -> None:
        return None


def health_status(
    paths: vibe_memory_paths.RuntimePaths, timeout: float = 0.6
) -> dict[str, object]:
    try:
        config = vibe_memory_install.read_runtime_config(paths)
        port = config["port"]
        url = f"http://127.0.0.1:{port}/"
    except Exception:
        return {
            "ok": False,
            "status": "config_invalid",
            "url": None,
            "action": "run install",
        }
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(url + "health", timeout=timeout) as response:
            if response.status != 200 or response.geturl() != url + "health":
                raise ValueError("unexpected health response")
            raw = response.read(4097)
            if len(raw) > 4096:
                raise ValueError("health response is too large")
            payload = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError, urllib.error.URLError):
        return {
            "ok": False,
            "status": "unreachable",
            "url": url,
            "action": "start the LaunchAgent service",
        }
    identity_ok = (
        isinstance(payload, dict)
        and payload.get("ok") is True
        and payload.get("service") == "vibe-memory"
        and payload.get("app_version") == config["app_version"]
    )
    return {
        "ok": identity_ok,
        "status": "healthy" if identity_ok else "wrong_service",
        "url": url,
        "action": None if identity_ok else "stop the conflicting service and start Vibe Memory",
    }


def health_ok(paths: vibe_memory_paths.RuntimePaths, timeout: float = 0.6) -> bool:
    return bool(health_status(paths, timeout=timeout)["ok"])


def _persisted_python_status(
    paths: vibe_memory_paths.RuntimePaths,
) -> tuple[str | None, str | None]:
    """Return the recorded interpreter or a doctor-safe Python diagnostic."""
    try:
        value = vibe_memory_install._read_runtime_config_document(paths)
    except (OSError, UnicodeDecodeError, ValueError, OverflowError, vibe_memory_install.InstallError):
        return None, "python: persisted runtime configuration is invalid"
    if not isinstance(value, dict):
        return None, "python: persisted runtime configuration is invalid"
    executable = value.get("python_executable")
    version = value.get("python_version")
    if not isinstance(executable, str) or not executable:
        return None, "python: persisted interpreter is missing"
    if not isinstance(version, str):
        return None, "python: persisted interpreter version is missing"
    parts = version.split(".")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None, "python: persisted interpreter version is invalid"
    try:
        recorded = (int(parts[0]), int(parts[1]))
    except (ValueError, OverflowError):
        return None, "python: persisted interpreter version is invalid"
    if recorded < vibe_memory_install.MINIMUM_PYTHON:
        return None, "python: persisted interpreter must be Python 3.10 or newer"
    try:
        validated = vibe_memory_install.validate_python(executable)
        actual = vibe_memory_install.probe_python(validated)
    except (OSError, ValueError, OverflowError, vibe_memory_install.InstallError):
        return None, "python: persisted interpreter is unavailable or below Python 3.10"
    if actual != recorded:
        return None, "python: persisted interpreter version does not match the executable"
    return validated, None


def collect_status(paths: vibe_memory_paths.RuntimePaths) -> dict[str, dict[str, object]]:
    runtime = paths.install_root / "current"
    runtime_cli = runtime / "scripts" / "vibe_memory_cli.py"
    launcher = paths.launcher
    current_ok = runtime.is_symlink() and runtime_cli.is_file()
    launcher_present = False
    try:
        launcher_metadata = launcher.stat(follow_symlinks=False)
        launcher_present = True
        launcher_ok = (
            stat.S_ISREG(launcher_metadata.st_mode)
            and not launcher.is_symlink()
            and bool(launcher_metadata.st_mode & stat.S_IXUSR)
            and stat.S_IMODE(launcher_metadata.st_mode) == 0o700
            and os.access(launcher, os.X_OK)
        )
    except FileNotFoundError:
        launcher_ok = False
    persisted_python, python_error = _persisted_python_status(paths)
    if python_error is None:
        try:
            vibe_memory_install.read_runtime_config(paths)
        except (OSError, ValueError, vibe_memory_install.InstallError):
            python_error = "python: persisted runtime configuration is invalid"
    interpreter_ok = python_error is None
    if launcher_ok and persisted_python is not None:
        try:
            launcher_ok = launcher.read_text(encoding="utf-8") == vibe_memory_install.render_launcher(
                paths, python_executable=persisted_python
            )
        except (OSError, ValueError, vibe_memory_install.InstallError):
            launcher_ok = False
    if not current_ok:
        runtime_status = "missing"
    elif not interpreter_ok:
        runtime_status = "python_error"
    elif not launcher_ok:
        runtime_status = "launcher_invalid" if launcher_present else "launcher_missing"
    else:
        runtime_status = "current"
    runtime_ok = runtime_status == "current"
    home = _home(paths)
    codex = vibe_memory_hooks.status(home / ".codex/hooks.json", "codex", launcher)
    claude = vibe_memory_hooks.status(home / ".claude/settings.json", "claude-code", launcher)
    personal = paths.personal_memory
    data_files = (
        personal / "long.md",
        personal / "short.md",
        personal / "proposals.md",
        paths.project_registry,
    )
    data_ok = all(path.is_file() and not path.is_symlink() for path in data_files)
    service = health_status(paths)
    runtime_entry: dict[str, object] = {
        "ok": runtime_ok,
        "status": runtime_status,
        "path": str(runtime),
        "launcher": str(launcher),
        "action": None if runtime_ok else "run install",
    }
    if python_error is not None:
        runtime_entry["error"] = python_error
    return {
        "runtime": runtime_entry,
        "codex_hooks": {
            "ok": codex["status"] == "current",
            "status": codex["status"],
            "path": codex["path"],
            "action": None if codex["status"] == "current" else "run install",
        },
        "claude_hooks": {
            "ok": claude["status"] in {"current", "missing"},
            "status": claude["status"],
            "path": claude["path"],
            "action": None if claude["status"] in {"current", "missing"} else "run install --with-claude-hooks",
        },
        "service": service,
        "data": {
            "ok": data_ok,
            "status": "ready" if data_ok else "missing",
            "personal_memory": str(personal),
            "project_registry": str(paths.project_registry),
            "action": None if data_ok else "run install",
        },
    }


ManagedFileSnapshot = tuple[tuple[int, int], bytes, int]


def _snapshot_managed_file(path: pathlib.Path) -> ManagedFileSnapshot | None:
    if path.is_symlink():
        raise ValueError("managed path must not be a symlink")
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("managed path must be a regular file")
        content = path.read_bytes()
        after = path.stat(follow_symlinks=False)
        if (after.st_dev, after.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ValueError("managed path changed while snapshotting")
        return (after.st_dev, after.st_ino), content, stat.S_IMODE(after.st_mode)
    except FileNotFoundError:
        return None


def _restore_managed_file(
    path: pathlib.Path,
    snapshot: ManagedFileSnapshot | None,
    expected_current: ManagedFileSnapshot | None,
) -> None:
    if path.is_symlink():
        raise ValueError("managed path changed to a symlink during rollback")
    if _snapshot_managed_file(path) != expected_current:
        raise ValueError("managed path changed concurrently during rollback")
    if snapshot is None:
        try:
            metadata = path.stat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("managed path changed type during rollback")
        path.unlink()
        return
    _before_identity, content, mode = snapshot
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.rollback-", dir=path.parent)
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        if path.is_symlink():
            raise ValueError("managed path changed to a symlink during rollback")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _hook_backup_artifacts(path: pathlib.Path) -> set[pathlib.Path]:
    return set(path.parent.glob(f"{path.name}.bak.*"))


def _snapshot_current(path: pathlib.Path) -> str | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISLNK(metadata.st_mode):
        raise ValueError("current runtime must be a managed symlink")
    return os.readlink(path)


def _restore_current(path: pathlib.Path, before: str | None, installed_version: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        metadata = None
    if before is None:
        if metadata is None:
            return
        if not stat.S_ISLNK(metadata.st_mode) or os.readlink(path) != f"releases/{installed_version}":
            raise ValueError("current runtime changed concurrently during rollback")
        path.unlink()
        return
    if metadata is not None and stat.S_ISLNK(metadata.st_mode) and os.readlink(path) == before:
        return
    if metadata is not None:
        if not stat.S_ISLNK(metadata.st_mode) or os.readlink(path) != f"releases/{installed_version}":
            raise ValueError("current runtime changed concurrently during rollback")
        temporary = path.with_name(f".current.restore-{uuid.uuid4().hex}")
        try:
            temporary.symlink_to(before)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return
    path.symlink_to(before)


def install_command(args: argparse.Namespace) -> int:
    paths = vibe_memory_paths.for_home()
    runtime = paths.install_root / "current"
    launcher = paths.launcher
    home = _home(paths)
    hook_targets = [(home / ".codex/hooks.json", "codex", "codex")]
    if args.with_claude_hooks:
        hook_targets.append((home / ".claude/settings.json", "claude-code", "claude"))
    launch_agent = paths.launch_agent
    runtime_config_path = paths.install_root / "config.json"
    install_state_path = vibe_memory_install.install_state_path(paths)
    existing_install_state: dict[str, object] = {}
    try:
        python_executable = vibe_memory_install.discover_python()
        validated = vibe_memory_install.validate_runtime_source(pathlib.Path(args.source_root))
        release_path = paths.install_root / "releases" / validated["version"]
        release_preexisted = release_path.exists()
        plist = vibe_memory_install.render_launch_agent(
            paths, port=args.port, python_executable=python_executable
        )
        vibe_memory_install.render_runtime_config(
            args.port, validated["version"], python_executable=python_executable
        )
        vibe_memory_install.render_launcher(paths, python_executable=python_executable)
        for target, agent, _name in hook_targets:
            vibe_memory_hooks.preview(target, agent, launcher)
        existing_install_state = vibe_memory_install.read_install_state(paths)
        snapshots = {target: _snapshot_managed_file(target) for target, _agent, _name in hook_targets}
        hook_backups = {
            target: _hook_backup_artifacts(target)
            for target, _agent, _name in hook_targets
        }
        snapshots[launch_agent] = _snapshot_managed_file(launch_agent)
        snapshots[runtime_config_path] = _snapshot_managed_file(runtime_config_path)
        snapshots[launcher] = _snapshot_managed_file(launcher)
        snapshots[install_state_path] = _snapshot_managed_file(install_state_path)
        current_before = _snapshot_current(runtime)
    except Exception:
        _json({"status": "failed", "phase": "preflight", "error": "installation preflight failed"})
        return 1

    data: dict[str, object] | None = None
    hooks: dict[str, dict[str, object]] = {}
    rollback_paths: set[pathlib.Path] = set()
    attempted_hook_targets: set[pathlib.Path] = set()
    lifecycle_attempted = False
    release_identity: tuple[int, int] | None = None
    written: dict[pathlib.Path, ManagedFileSnapshot | None] = {}
    try:
        installed = vibe_memory_install.install_runtime(
            pathlib.Path(args.source_root), paths, activate=False
        )
        if not release_preexisted and release_path.exists():
            metadata = release_path.stat(follow_symlinks=False)
            release_identity = (metadata.st_dev, metadata.st_ino)
        vibe_memory_install._activate_managed_version(paths, installed["version"])
        data = vibe_memory_install.prepare_data(paths)
        rollback_paths.add(runtime_config_path)
        try:
            runtime_config = vibe_memory_install.install_runtime_config(
                paths,
                port=args.port,
                app_version=installed["version"],
                python_executable=python_executable,
            )
        finally:
            written[runtime_config_path] = _snapshot_managed_file(runtime_config_path)
        rollback_paths.add(launcher)
        try:
            launcher_result = vibe_memory_install.install_launcher(
                paths, python_executable=python_executable
            )
        finally:
            written[launcher] = _snapshot_managed_file(launcher)
        control_plane = vibe_memory_migration.validate_control_plane(
            paths, _registry_snapshot(paths)
        )
        if any(status != "ok" for status in control_plane.values()):
            raise ValueError("control-plane compatibility validation failed")
        rollback_paths.add(launch_agent)
        try:
            plist_result = vibe_memory_install.install_launch_agent(paths, plist)
        finally:
            written[launch_agent] = _snapshot_managed_file(launch_agent)
        for target, agent, name in hook_targets:
            rollback_paths.add(target)
            attempted_hook_targets.add(target)
            try:
                hooks[name] = vibe_memory_hooks.repair(target, agent, launcher)
            finally:
                written[target] = _snapshot_managed_file(target)
        existing_clients = existing_install_state.get("installed_clients", [])
        clients = ["codex"]
        if args.with_claude_hooks or "claude-code" in existing_clients:
            clients.append("claude-code")
        previous_version = existing_install_state.get("previous_version")
        rollback_paths.add(install_state_path)
        try:
            vibe_memory_install.write_install_state(
                paths,
                vibe_memory_install._install_state_document(
                    current_version=installed["version"],
                    previous_version=previous_version if isinstance(previous_version, str) else None,
                    port=args.port,
                    installed_clients=clients,
                    python_executable=python_executable,
                ),
            )
        finally:
            written[install_state_path] = _snapshot_managed_file(install_state_path)
        lifecycle_attempted = True
        service = vibe_memory_install.activate_launch_agent(
            paths, expected_version=installed["version"]
        )
        smoke = vibe_memory_install.smoke_managed_hooks(paths, clients)
        _json({
            "status": "installed",
            "runtime": installed,
            "runtime_config": runtime_config,
            "launcher": launcher_result,
            "install_state": {"path": str(install_state_path)},
            "control_plane": control_plane,
            "data": data,
            "launch_agent": plist_result,
            "hooks": hooks,
            "service": service,
            "hook_smoke": smoke,
        })
        return 0
    except Exception:
        rollback_errors = []
        if lifecycle_attempted:
            try:
                vibe_memory_install.bootout_launch_agent()
            except Exception:
                rollback_errors.append("launchctl bootout")
        for target in rollback_paths:
            try:
                _restore_managed_file(target, snapshots[target], written.get(target))
            except Exception:
                rollback_errors.append(str(target))
        try:
            _restore_current(runtime, current_before, validated["version"])
        except Exception:
            rollback_errors.append(str(runtime))
        if current_before is not None:
            previous_version = current_before.removeprefix("releases/")
            try:
                vibe_memory_install.activate_launch_agent(
                    paths, expected_version=previous_version
                )
            except Exception:
                rollback_errors.append("previous launch agent restart")
        if release_identity is not None:
            try:
                vibe_memory_install._remove_new_managed_release(
                    release_path,
                    preexisted=release_preexisted,
                    expected_identity=release_identity,
                )
            except Exception:
                rollback_errors.append(str(release_path))
        if not rollback_errors:
            for target in attempted_hook_targets:
                artifacts = _hook_backup_artifacts(target) - hook_backups[target]
                for artifact in artifacts:
                    try:
                        if artifact.is_symlink() or not artifact.is_file():
                            raise ValueError("hook backup changed type during rollback")
                        artifact.unlink()
                    except Exception:
                        rollback_errors.append(str(artifact))
        _json({
            "status": "failed",
            "phase": "commit",
            "error": "installation commit failed",
            "rollback": {
                "ok": not rollback_errors,
                "failed_paths": rollback_errors,
                "data_retained": True,
            },
        })
        return 1


def status_command(_args: argparse.Namespace) -> int:
    _json(collect_status(vibe_memory_paths.for_home()))
    return 0


def doctor_command(_args: argparse.Namespace) -> int:
    result = collect_status(vibe_memory_paths.for_home())
    _json(result)
    return 0 if all(result[key]["ok"] for key in DOCTOR_KEYS) else 1


def open_command(_args: argparse.Namespace) -> int:
    paths = vibe_memory_paths.for_home()
    health = health_status(paths)
    if not health["ok"]:
        raise LifecycleError("local health endpoint is unavailable")
    url = str(health["url"])
    completed = subprocess.run(["/usr/bin/open", url], check=False)
    if completed.returncode:
        raise LifecycleError("open command failed")
    _json({"status": "opened", "url": url})
    return 0


def project_command(args: argparse.Namespace) -> int:
    if args.project_command == "register":
        value = memory_project.register_project(args.project_root)
    elif args.project_command == "unregister":
        value = memory_project.unregister_project(args.project_root)
    elif args.project_command == "list":
        value = memory_project.list_projects()
    else:
        value = memory_project.init_project(args.project_root)
    _json(value)
    return 0


def memory_command(args: argparse.Namespace) -> int:
    if args.memory_command == "propose":
        value = memory_review_queue.create_agent_candidate(
            args.scope, args.target, args.category, args.title, args.summary,
            args.source_event, source_agent=args.source_agent,
            policy_version=args.policy_version,
        )
    elif args.memory_command == "list":
        queue = memory_review_queue.load_queue(refresh=True)
        value = {"items": [item for item in queue.get("items", []) if args.status is None or item.get("status") == args.status]}
    elif args.memory_command == "show":
        value = memory_review_queue.find_item(args.candidate_id)
    else:
        content = pathlib.Path(args.content_file).read_text(encoding="utf-8") if args.content_file else None
        item = memory_review_queue.approve(args.candidate_id, target=args.target, content=content)
        value = {"status": "approved", "item": item}
    _json(value)
    return 0


def _migration_project_roots(args: argparse.Namespace) -> list[pathlib.Path]:
    raw_roots = getattr(args, "project_root", None) or []
    if not raw_roots:
        return [memory_project.current_project().resolve()]
    return [pathlib.Path(root).expanduser().resolve() for root in raw_roots]


def _registry_snapshot(paths: vibe_memory_paths.RuntimePaths) -> dict[str, object]:
    try:
        raw = paths.project_registry.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"current_project": "", "projects": []}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("project registry must be an object")
    if not isinstance(value.get("current_project", ""), str) or not isinstance(
        value.get("projects", []), list
    ):
        raise ValueError("project registry has an invalid structure")
    value.setdefault("current_project", "")
    value.setdefault("projects", [])
    return value


def migrate_command(args: argparse.Namespace) -> int:
    roots = _migration_project_roots(args)
    if args.migrate_command == "preview":
        value = vibe_memory_migration.preview_legacy_hooks(roots)
    else:
        if not args.approved:
            raise LifecycleError("migrate apply requires --approved")
        value = vibe_memory_migration.apply_legacy_hooks(roots)
    _json(value)
    return 0


def update_command(args: argparse.Namespace) -> int:
    value = vibe_memory_install.update(pathlib.Path(args.source_root), vibe_memory_paths.for_home())
    _json(value)
    return 0


def rollback_command(_args: argparse.Namespace) -> int:
    _json(vibe_memory_install.rollback(vibe_memory_paths.for_home()))
    return 0


def repair_command(_args: argparse.Namespace) -> int:
    _json(vibe_memory_install.repair(vibe_memory_paths.for_home()))
    return 0


def uninstall_command(args: argparse.Namespace) -> int:
    if args.remove_data and not args.approved_data_deletion:
        raise LifecycleError("uninstall --remove-data requires --approved-data-deletion")
    value = vibe_memory_install.uninstall(
        vibe_memory_paths.for_home(),
        remove_data=args.remove_data,
        approved_data_deletion=args.approved_data_deletion,
        data_paths=[pathlib.Path(path).expanduser().resolve() for path in args.data_path],
    )
    _json(value)
    return 0


def hooks_command(args: argparse.Namespace) -> int:
    paths = vibe_memory_paths.for_home()
    state = vibe_memory_install.read_install_state(paths)
    clients = [
        client for client in state.get("installed_clients", ["codex"])
        if client in {"codex", "claude-code"}
    ] or ["codex"]
    results: dict[str, object] = {}
    for client in clients:
        target, agent, label = vibe_memory_install._hook_target_for_client(paths, client)
        if args.hooks_command == "status":
            results[label] = vibe_memory_hooks.status(target, agent, paths.launcher)
        else:
            results[label] = vibe_memory_hooks.repair(target, agent, paths.launcher)
    if args.hooks_command == "repair":
        vibe_memory_install.smoke_managed_hooks(paths, clients)
    current = all(
        isinstance(result, dict) and result.get("status") == "current"
        for result in results.values()
    )
    _json({
        "status": "current" if args.hooks_command == "status" and current else (
            "needs_repair" if args.hooks_command == "status" else "repaired"
        ),
        "hooks": results,
    })
    return 0 if args.hooks_command == "repair" or current else 1


def hook_command(args: argparse.Namespace) -> int:
    try:
        router = importlib.import_module("vibe_memory_router")
        payload = json.loads(sys.stdin.read() or "{}")
        result = router.handle_event(
            args.agent,
            args.event,
            payload,
            pathlib.Path.cwd(),
        )
        print(json.dumps(result, ensure_ascii=False))
    except Exception:
        print(
            json.dumps(
                {"status": "degraded", "error": "钩子处理失败"},
                ensure_ascii=False,
            )
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Vibe Memory shared runtime CLI")
    subcommands = parser.add_subparsers(dest="command", required=True)
    hook = subcommands.add_parser("hook", help="Route a Codex or Claude Code hook")
    hook.add_argument("--agent", choices=("codex", "claude-code"), required=True)
    hook.add_argument("--event", required=True)
    hook.set_defaults(command_handler=hook_command)

    install = subcommands.add_parser("install", help="Install or repair the local runtime")
    install.add_argument("--source-root", required=True)
    install.add_argument("--port", type=int, default=8897)
    install.add_argument("--with-claude-hooks", action="store_true")
    install.set_defaults(command_handler=install_command)

    status = subcommands.add_parser("status", help="Print stable machine-readable status")
    status.set_defaults(command_handler=status_command)
    doctor = subcommands.add_parser("doctor", help="Diagnose the local installation")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(command_handler=doctor_command)
    open_parser = subcommands.add_parser("open", help="Open the healthy local review console")
    open_parser.set_defaults(command_handler=open_command)

    project = subcommands.add_parser("project", help="Manage registered projects")
    project_sub = project.add_subparsers(dest="project_command", required=True)
    for name in ("register", "unregister", "init"):
        command_parser = project_sub.add_parser(name)
        command_parser.add_argument("project_root")
    project_sub.add_parser("list")
    project.set_defaults(command_handler=project_command)

    memory = subcommands.add_parser("memory", help="Manage approval-gated memory candidates")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
    propose = memory_sub.add_parser("propose")
    propose.add_argument("--scope", choices=("personal", "project"), required=True)
    propose.add_argument("--target", choices=("long", "short"), required=True)
    propose.add_argument("--category", required=True)
    propose.add_argument("--title", required=True)
    propose.add_argument("--summary", required=True)
    propose.add_argument("--source-event", default="agent_summary")
    propose.add_argument("--source-agent", choices=("codex", "claude-code", "unknown"), default="unknown")
    propose.add_argument("--policy-version", type=int, default=1)
    list_parser = memory_sub.add_parser("list")
    list_parser.add_argument("--status", default="pending")
    show = memory_sub.add_parser("show")
    show.add_argument("candidate_id")
    approve = memory_sub.add_parser("approve")
    approve.add_argument("candidate_id")
    approve.add_argument("--target", choices=("project_long", "personal_long", "personal_short"))
    approve.add_argument("--content-file")
    memory.set_defaults(command_handler=memory_command)

    migrate = subcommands.add_parser("migrate", help="Preview or apply safe legacy migrations")
    migrate_sub = migrate.add_subparsers(dest="migrate_command", required=True)
    preview = migrate_sub.add_parser("preview", help="Preview legacy project hook migration")
    preview.add_argument("project_root", nargs="*")
    apply = migrate_sub.add_parser("apply", help="Apply approved legacy project hook migration")
    apply.add_argument("--approved", action="store_true")
    apply.add_argument("project_root", nargs="*")
    migrate.set_defaults(command_handler=migrate_command)

    update = subcommands.add_parser("update", help="Install a new runtime release")
    update.add_argument("--source-root", required=True)
    update.set_defaults(command_handler=update_command)

    rollback = subcommands.add_parser("rollback", help="Switch back to the previous runtime")
    rollback.set_defaults(command_handler=rollback_command)

    repair = subcommands.add_parser("repair", help="Repair managed runtime assets")
    repair.set_defaults(command_handler=repair_command)

    uninstall = subcommands.add_parser("uninstall", help="Remove the managed runtime")
    uninstall.add_argument("--remove-data", action="store_true")
    uninstall.add_argument("--approved-data-deletion", action="store_true")
    uninstall.add_argument("--data-path", action="append", default=[])
    uninstall.set_defaults(command_handler=uninstall_command)

    hooks = subcommands.add_parser("hooks", help="Inspect or repair managed hooks")
    hooks_sub = hooks.add_subparsers(dest="hooks_command", required=True)
    hooks_sub.add_parser("status")
    hooks_sub.add_parser("repair")
    hooks.set_defaults(command_handler=hooks_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "hook":
        return args.command_handler(args)
    try:
        return args.command_handler(args)
    except LifecycleError as error:
        print(f"{args.command} failed: {error}", file=sys.stderr)
        return 1
    except Exception:
        print(f"{args.command} failed; run doctor for actionable status", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
