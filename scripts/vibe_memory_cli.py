#!/usr/bin/env python3
"""Fail-open command line entry point for shared memory hooks."""

from __future__ import annotations

import argparse
import importlib
import json
import pathlib
import subprocess
import sys
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
    import vibe_memory_paths
except ImportError:
    pass


REVIEW_URL = "http://127.0.0.1:8897/"
DOCTOR_KEYS = ("runtime", "codex_hooks", "claude_hooks", "service", "data")


class LifecycleError(RuntimeError):
    """An intentionally safe lifecycle error suitable for terminal output."""


def _home(paths: vibe_memory_paths.RuntimePaths) -> pathlib.Path:
    return paths.personal_memory.parents[1]


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def health_ok(timeout: float = 0.6) -> bool:
    try:
        with urllib.request.urlopen(REVIEW_URL + "health", timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


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
    service_ok = health_ok()
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
        "service": {
            "ok": service_ok,
            "status": "healthy" if service_ok else "unreachable",
            "url": REVIEW_URL,
            "action": None if service_ok else "start the LaunchAgent service",
        },
        "data": {
            "ok": data_ok,
            "status": "ready" if data_ok else "missing",
            "personal_memory": str(personal),
            "project_registry": str(paths.project_registry),
            "action": None if data_ok else "run install",
        },
    }


def install_command(args: argparse.Namespace) -> int:
    paths = vibe_memory_paths.for_home()
    installed = vibe_memory_install.install_runtime(pathlib.Path(args.source_root), paths)
    data = vibe_memory_install.prepare_data(paths)
    plist = vibe_memory_install.render_launch_agent(paths, port=args.port)
    plist_result = vibe_memory_install.install_launch_agent(paths, plist)
    runtime = paths.install_root / "current"
    hooks = {
        "codex": vibe_memory_hooks.repair(
            _home(paths) / ".codex/hooks.json", "codex", runtime
        )
    }
    if args.with_claude_hooks:
        hooks["claude"] = vibe_memory_hooks.repair(
            _home(paths) / ".claude/settings.json", "claude-code", runtime
        )
    _json({"status": "installed", "runtime": installed, "data": data, "launch_agent": plist_result, "hooks": hooks})
    return 0


def status_command(_args: argparse.Namespace) -> int:
    _json(collect_status(vibe_memory_paths.for_home()))
    return 0


def doctor_command(_args: argparse.Namespace) -> int:
    result = collect_status(vibe_memory_paths.for_home())
    _json(result)
    return 0 if all(result[key]["ok"] for key in DOCTOR_KEYS) else 1


def open_command(_args: argparse.Namespace) -> int:
    if not health_ok():
        raise LifecycleError("local health endpoint is unavailable")
    completed = subprocess.run(["/usr/bin/open", REVIEW_URL], check=False)
    if completed.returncode:
        raise LifecycleError("open command failed")
    _json({"status": "opened", "url": REVIEW_URL})
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
