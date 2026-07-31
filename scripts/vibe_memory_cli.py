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


def collect_status(paths: vibe_memory_paths.RuntimePaths) -> dict[str, dict[str, object]]:
    runtime = paths.install_root / "current"
    runtime_cli = runtime / "scripts" / "vibe_memory_cli.py"
    runtime_ok = runtime.is_symlink() and runtime_cli.is_file()
    home = _home(paths)
    codex = vibe_memory_hooks.status(home / ".codex/hooks.json", "codex", runtime)
    claude = vibe_memory_hooks.status(home / ".claude/settings.json", "claude-code", runtime)
    personal = paths.personal_memory
    data_files = (
        personal / "long.md",
        personal / "short.md",
        personal / "proposals.md",
        paths.project_registry,
    )
    data_ok = all(path.is_file() and not path.is_symlink() for path in data_files)
    service = health_status(paths)
    return {
        "runtime": {
            "ok": runtime_ok,
            "status": "current" if runtime_ok else "missing",
            "path": str(runtime),
            "action": None if runtime_ok else "run install",
        },
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


def _snapshot_managed_file(path: pathlib.Path) -> tuple[bytes, int] | None:
    if path.is_symlink():
        raise ValueError("managed path must not be a symlink")
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("managed path must be a regular file")
        return path.read_bytes(), stat.S_IMODE(metadata.st_mode)
    except FileNotFoundError:
        return None


def _restore_managed_file(path: pathlib.Path, snapshot: tuple[bytes, int] | None) -> None:
    if path.is_symlink():
        raise ValueError("managed path changed to a symlink during rollback")
    if snapshot is None:
        try:
            metadata = path.stat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("managed path changed type during rollback")
        path.unlink()
        return
    content, mode = snapshot
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
        raise ValueError("current runtime changed concurrently during rollback")
    path.symlink_to(before)


def install_command(args: argparse.Namespace) -> int:
    paths = vibe_memory_paths.for_home()
    runtime = paths.install_root / "current"
    home = _home(paths)
    hook_targets = [(home / ".codex/hooks.json", "codex", "codex")]
    if args.with_claude_hooks:
        hook_targets.append((home / ".claude/settings.json", "claude-code", "claude"))
    launch_agent = home / "Library/LaunchAgents/com.noema.vibe-memory.plist"
    runtime_config_path = paths.install_root / "config.json"
    try:
        validated = vibe_memory_install.validate_runtime_source(pathlib.Path(args.source_root))
        plist = vibe_memory_install.render_launch_agent(paths, port=args.port)
        vibe_memory_install.render_runtime_config(args.port, validated["version"])
        for target, agent, _name in hook_targets:
            vibe_memory_hooks.preview(target, agent, runtime)
        snapshots = {target: _snapshot_managed_file(target) for target, _agent, _name in hook_targets}
        hook_backups = {
            target: _hook_backup_artifacts(target)
            for target, _agent, _name in hook_targets
        }
        snapshots[launch_agent] = _snapshot_managed_file(launch_agent)
        snapshots[runtime_config_path] = _snapshot_managed_file(runtime_config_path)
        current_before = _snapshot_current(runtime)
    except Exception:
        _json({"status": "failed", "phase": "preflight", "error": "installation preflight failed"})
        return 1

    data: dict[str, object] | None = None
    hooks: dict[str, dict[str, object]] = {}
    rollback_paths: set[pathlib.Path] = set()
    attempted_hook_targets: set[pathlib.Path] = set()
    try:
        installed = vibe_memory_install.install_runtime(pathlib.Path(args.source_root), paths)
        data = vibe_memory_install.prepare_data(paths)
        rollback_paths.add(runtime_config_path)
        runtime_config = vibe_memory_install.install_runtime_config(
            paths, port=args.port, app_version=installed["version"]
        )
        control_plane = vibe_memory_migration.validate_control_plane(
            paths, _registry_snapshot(paths)
        )
        if any(status != "ok" for status in control_plane.values()):
            raise ValueError("control-plane compatibility validation failed")
        rollback_paths.add(launch_agent)
        plist_result = vibe_memory_install.install_launch_agent(paths, plist)
        for target, agent, name in hook_targets:
            rollback_paths.add(target)
            attempted_hook_targets.add(target)
            hooks[name] = vibe_memory_hooks.repair(target, agent, runtime)
        _json({
            "status": "installed",
            "runtime": installed,
            "runtime_config": runtime_config,
            "control_plane": control_plane,
            "data": data,
            "launch_agent": plist_result,
            "hooks": hooks,
        })
        return 0
    except Exception:
        rollback_errors = []
        for target in rollback_paths:
            try:
                _restore_managed_file(target, snapshots[target])
            except Exception:
                rollback_errors.append(str(target))
        try:
            _restore_current(runtime, current_before, validated["version"])
        except Exception:
            rollback_errors.append(str(runtime))
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
