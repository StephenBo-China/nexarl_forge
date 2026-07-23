#!/usr/bin/env python3
"""Safe multi-conversation worktree and release workflow.

Runtime state lives outside Git under ~/.codex/worktree_manager by default.
The script deliberately refuses force pushes, automatic stashes, resets, and
overwriting dirty canonical files.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import uuid
from typing import Any, Iterator


MANAGER_ROOT = pathlib.Path(
    os.environ.get("CODEX_WORKTREE_MANAGER_ROOT", "~/.codex/worktree_manager")
).expanduser()
REGISTRY_PATH = MANAGER_ROOT / "tasks.json"
LOCK_ROOT = MANAGER_ROOT / "locks"
DEFAULT_WORKTREE_ROOT = pathlib.Path(
    os.environ.get("CODEX_WORKTREE_ROOT", "/Users/stephenbo/Noema/Projects/worktrees")
).expanduser()


class WorkflowError(RuntimeError):
    pass


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path: pathlib.Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def registry() -> dict[str, Any]:
    value = read_json(REGISTRY_PATH, {"schema_version": 1, "tasks": {}})
    if not isinstance(value, dict):
        value = {"schema_version": 1, "tasks": {}}
    value.setdefault("schema_version", 1)
    value.setdefault("tasks", {})
    return value


def save_registry(value: dict[str, Any]) -> None:
    write_json(REGISTRY_PATH, value)


def run(
    args: list[str],
    *,
    cwd: pathlib.Path | None = None,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=False,
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "command failed").strip()
        raise WorkflowError(f"{shlex.join(args)}: {detail}")
    return result


def run_user_command(command: str, cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
    args = shlex.split(command)
    if not args:
        raise WorkflowError("empty command is not allowed")
    result = subprocess.run(
        args,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "command failed").strip()
        raise WorkflowError(f"command failed ({command}): {detail}")
    return result


def git(root: pathlib.Path, *args: str, check: bool = True) -> str:
    return run(["git", "-C", str(root), *args], check=check).stdout.strip()


def canonical_root(value: str | pathlib.Path) -> pathlib.Path:
    root = pathlib.Path(value).expanduser().resolve()
    actual = git(root, "rev-parse", "--show-toplevel")
    if pathlib.Path(actual).resolve() != root:
        raise WorkflowError(f"canonical root must be the Git top-level: {actual}")
    return root


def config(root: pathlib.Path) -> dict[str, Any]:
    value = read_json(root / ".loop" / "config.json", {})
    return value if isinstance(value, dict) else {}


def workflow_settings(root: pathlib.Path) -> dict[str, Any]:
    value = config(root)
    repository = value.get("repository", {})
    worktree = value.get("worktree", {})
    branch = value.get("branch", {})
    main = repository.get("main_branch") or branch.get("main_branch") or "master"
    remote = repository.get("remote") or "origin"
    configured_root = worktree.get("root") or worktree.get("default_root")
    base_root = pathlib.Path(configured_root).expanduser() if configured_root else DEFAULT_WORKTREE_ROOT
    return {
        "main_branch": str(main),
        "remote": str(remote),
        "worktree_root": base_root.resolve(),
        "branch_format": branch.get("name_format", "loop/<project>-<date>-<slug>"),
        "finish_validation_commands": worktree.get("finish_validation_commands", []),
        "verification_commands": value.get("verification", {}).get("commands", []),
    }


def slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", value.lower().replace("_", "-"))
    return cleaned.strip("-") or "task"


def repository_id(root: pathlib.Path) -> str:
    return hashlib.sha256(str(root).encode()).hexdigest()[:16]


def task_key(root: pathlib.Path, task: str) -> str:
    return f"{repository_id(root)}:{slug(task)}"


def task_entry(root: pathlib.Path, task: str) -> dict[str, Any]:
    entry = registry()["tasks"].get(task_key(root, task))
    if not entry:
        raise WorkflowError(f"unknown worktree task: {task}")
    return entry


@contextlib.contextmanager
def repository_lock(root: pathlib.Path, kind: str, task: str) -> Iterator[pathlib.Path]:
    lock = LOCK_ROOT / f"{repository_id(root)}.{kind}.lock"
    try:
        lock.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        metadata = read_json(lock / "owner.json", {})
        raise WorkflowError(f"{kind} lock is already held: {metadata}") from exc
    write_json(
        lock / "owner.json",
        {
            "repository": str(root),
            "task": task,
            "kind": kind,
            "pid": os.getpid(),
            "created_at": now(),
        },
    )
    try:
        yield lock
    finally:
        try:
            (lock / "owner.json").unlink(missing_ok=True)
            lock.rmdir()
        except OSError:
            pass


def branch_name(root: pathlib.Path, task: str, pattern: str) -> str:
    name = root.name.replace("_", "-")
    date = dt.datetime.now().strftime("%Y%m%d")
    value = (
        pattern.replace("<project>", slug(name))
        .replace("<date>", date)
        .replace("<slug>", slug(task))
    )
    suffix = uuid.uuid4().hex[:6]
    if git(root, "show-ref", "--verify", "--quiet", f"refs/heads/{value}", check=False) == "":
        probe = run(
            ["git", "-C", str(root), "show-ref", "--verify", "--quiet", f"refs/heads/{value}"],
            check=False,
        )
        if probe.returncode == 0:
            value = f"{value}-{suffix}"
    return value


def fetch_main(root: pathlib.Path, settings: dict[str, Any]) -> str:
    remote, main = settings["remote"], settings["main_branch"]
    git(root, "fetch", remote, main)
    return git(root, "rev-parse", f"{remote}/{main}")


def dirty_paths(root: pathlib.Path) -> set[str]:
    paths: set[str] = set()
    output = run(
        ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"]
    ).stdout
    for line in output.splitlines():
        if not line:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.add(path)
    return paths


def incoming_paths(root: pathlib.Path, target: str) -> set[str]:
    return set(filter(None, git(root, "diff", "--name-only", f"HEAD..{target}").splitlines()))


def start(root_value: str, task: str, conversation: str) -> dict[str, Any]:
    root = canonical_root(root_value)
    settings = workflow_settings(root)
    remote_head = fetch_main(root, settings)
    data = registry()
    key = task_key(root, task)
    existing = data["tasks"].get(key)
    if existing and existing.get("status") not in {"cleaned", "failed"}:
        raise WorkflowError(f"task already registered: {existing}")
    worktree_root = settings["worktree_root"]
    if root == worktree_root or root in worktree_root.parents:
        raise WorkflowError("worktree root must be outside the canonical repository")
    path = worktree_root / root.name / slug(task)
    if path.exists():
        raise WorkflowError(f"worktree path already exists: {path}")
    branch = branch_name(root, task, settings["branch_format"])
    path.parent.mkdir(parents=True, exist_ok=True)
    git(root, "worktree", "add", "-b", branch, str(path), f"{settings['remote']}/{settings['main_branch']}")
    entry = {
        "repository": str(root),
        "task": slug(task),
        "conversation_id": conversation,
        "worktree": str(path),
        "branch": branch,
        "base_commit": remote_head,
        "status": "developing",
        "created_at": now(),
        "updated_at": now(),
    }
    data["tasks"][key] = entry
    save_registry(data)
    return entry


def finish(root_value: str, task: str) -> dict[str, Any]:
    root = canonical_root(root_value)
    settings = workflow_settings(root)
    entry = task_entry(root, task)
    worktree = pathlib.Path(entry["worktree"])
    if dirty_paths(worktree):
        raise WorkflowError("feature worktree is not clean")
    branch = git(worktree, "branch", "--show-current")
    if branch != entry["branch"]:
        raise WorkflowError(f"worktree branch mismatch: {branch}")
    feature_commit = git(worktree, "rev-parse", "HEAD")
    upstream = git(worktree, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False)
    if not upstream:
        raise WorkflowError("feature branch has no upstream; push it before finish")
    if git(worktree, "rev-parse", upstream) != feature_commit:
        raise WorkflowError("feature branch is not fully pushed")
    for command in settings["finish_validation_commands"]:
        run_user_command(command, worktree)
    if dirty_paths(worktree):
        raise WorkflowError("finish validation commands left the feature worktree dirty")
    data = registry()
    value = data["tasks"][task_key(root, task)]
    value.update(
        {
            "feature_commit": feature_commit,
            "status": "ready_for_user_acceptance",
            "updated_at": now(),
        }
    )
    save_registry(data)
    return value


def sync_canonical(root_value: str, *, fetch: bool = True) -> dict[str, Any]:
    root = canonical_root(root_value)
    settings = workflow_settings(root)
    if fetch:
        fetch_main(root, settings)
    main_ref = f"{settings['remote']}/{settings['main_branch']}"
    current_branch = git(root, "branch", "--show-current")
    head = git(root, "rev-parse", "HEAD")
    target = git(root, "rev-parse", main_ref)
    result = {
        "canonical_root": str(root),
        "current_branch": current_branch,
        "canonical_commit_before": head,
        "remote_main_commit": target,
    }
    if current_branch != settings["main_branch"]:
        result.update({"ok": False, "status": "blocked_by_current_branch"})
        return result
    if head == target:
        result.update({"ok": True, "status": "already_current", "canonical_commit_after": head})
        return result
    ancestor = run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", head, target],
        check=False,
    ).returncode == 0
    if not ancestor:
        result.update({"ok": False, "status": "blocked_by_non_fast_forward"})
        return result
    dirty = dirty_paths(root)
    incoming = incoming_paths(root, main_ref)
    overlap = sorted(dirty & incoming)
    result.update({"dirty_paths": sorted(dirty), "incoming_paths": sorted(incoming), "overlap": overlap})
    if overlap:
        result.update({"ok": False, "status": "blocked_by_dirty_overlap"})
        return result
    git(root, "merge", "--ff-only", main_ref)
    after = git(root, "rev-parse", "HEAD")
    result.update({"ok": after == target, "status": "synced", "canonical_commit_after": after})
    return result


def release(
    root_value: str,
    task: str,
    *,
    approved: bool,
    test_commands: list[str],
) -> dict[str, Any]:
    if not approved:
        raise WorkflowError("release requires explicit --approved user authorization")
    root = canonical_root(root_value)
    settings = workflow_settings(root)
    entry = task_entry(root, task)
    if entry.get("status") != "ready_for_user_acceptance":
        raise WorkflowError("task must pass finish before release")
    with repository_lock(root, "release", task):
        remote_head = fetch_main(root, settings)
        feature_commit = entry["feature_commit"]
        current_feature_commit = git(root, "rev-parse", entry["branch"])
        if current_feature_commit != feature_commit:
            raise WorkflowError(
                "feature branch changed after finish; run verification and finish again before release"
            )
        remote_feature = git(
            root,
            "ls-remote",
            settings["remote"],
            f"refs/heads/{entry['branch']}",
        )
        remote_feature_commit = remote_feature.split()[0] if remote_feature else ""
        if remote_feature_commit != feature_commit:
            raise WorkflowError("approved feature commit is not the current remote feature branch")
        remote_ref = f"{settings['remote']}/{settings['main_branch']}"
        already_merged = run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", feature_commit, remote_ref],
            check=False,
        ).returncode == 0
        release_commit = remote_head
        release_path: pathlib.Path | None = None
        if not already_merged:
            commands = test_commands or list(settings["verification_commands"])
            if not commands:
                raise WorkflowError(
                    "release requires at least one --test-command or verification.commands entry"
                )
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            release_branch = f"release/{slug(root.name)}-{stamp}-{slug(task)}"
            release_path = settings["worktree_root"] / root.name / f"_release-{stamp}-{slug(task)}"
            release_path.parent.mkdir(parents=True, exist_ok=True)
            git(root, "worktree", "add", "-b", release_branch, str(release_path), remote_ref)
            try:
                git(
                    release_path,
                    "merge",
                    "--no-ff",
                    entry["branch"],
                    "-m",
                    f"merge: release {slug(task)}",
                )
                for command in commands:
                    run_user_command(command, release_path)
                release_commit = git(release_path, "rev-parse", "HEAD")
                git(release_path, "push", settings["remote"], f"HEAD:{settings['main_branch']}")
            except Exception:
                data = registry()
                value = data["tasks"][task_key(root, task)]
                value.update(
                    {
                        "status": "release_failed",
                        "release_worktree": str(release_path),
                        "updated_at": now(),
                    }
                )
                save_registry(data)
                raise
        sync = sync_canonical(str(root), fetch=True)
        if release_path and sync.get("ok"):
            release_branch = git(release_path, "branch", "--show-current")
            git(root, "worktree", "remove", str(release_path))
            git(root, "branch", "-d", release_branch)
            release_path = None
        data = registry()
        value = data["tasks"][task_key(root, task)]
        value.update(
            {
                "release_commit": release_commit,
                "release_worktree": str(release_path) if release_path else "",
                "canonical_sync": sync,
                "status": "canonical_synced" if sync.get("ok") else "master_pushed",
                "updated_at": now(),
            }
        )
        save_registry(data)
        return value


def deploy_staging(
    root_value: str,
    task: str,
    *,
    approved: bool,
    commands: list[str],
    deployed_commit_command: str,
) -> dict[str, Any]:
    if not approved:
        raise WorkflowError("staging deployment requires explicit --approved authorization")
    if not commands:
        raise WorkflowError("at least one --command is required")
    root = canonical_root(root_value)
    settings = workflow_settings(root)
    entry = task_entry(root, task)
    if entry.get("status") not in {"canonical_synced", "master_pushed", "staging_deployed", "verified"}:
        raise WorkflowError("master staging deploy requires a completed release first")
    with repository_lock(root, "staging", task):
        expected = fetch_main(root, settings)
        for command in commands:
            run_user_command(command, root)
        deployed = ""
        if deployed_commit_command:
            deployed = run_user_command(deployed_commit_command, root).stdout.strip()
            if deployed != expected:
                raise WorkflowError(
                    f"deployed commit mismatch: expected {expected}, received {deployed or '<empty>'}"
                )
        data = registry()
        value = data["tasks"][task_key(root, task)]
        value.update(
            {
                "deployed_commit": deployed,
                "expected_deployed_commit": expected,
                "status": "verified" if deployed else "staging_deployed",
                "updated_at": now(),
            }
        )
        save_registry(data)
        return value


def verify(root_value: str, task: str | None, deployed_commit: str = "") -> dict[str, Any]:
    root = canonical_root(root_value)
    settings = workflow_settings(root)
    fetch_main(root, settings)
    remote_ref = f"{settings['remote']}/{settings['main_branch']}"
    remote_commit = git(root, "rev-parse", remote_ref)
    canonical_commit = git(root, "rev-parse", "HEAD")
    current_branch = git(root, "branch", "--show-current")
    result: dict[str, Any] = {
        "repository": str(root),
        "main_branch": settings["main_branch"],
        "remote_main_commit": remote_commit,
        "canonical_branch": current_branch,
        "canonical_commit": canonical_commit,
        "canonical_matches_remote": current_branch == settings["main_branch"] and canonical_commit == remote_commit,
    }
    if task:
        entry = task_entry(root, task)
        feature_commit = entry.get("feature_commit") or git(pathlib.Path(entry["worktree"]), "rev-parse", "HEAD")
        result["task"] = task
        result["feature_commit"] = feature_commit
        result["feature_is_ancestor"] = run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", feature_commit, remote_ref],
            check=False,
        ).returncode == 0
    if deployed_commit:
        result["deployed_commit"] = deployed_commit
        result["deployment_matches_remote"] = deployed_commit == remote_commit
    checks = [result["canonical_matches_remote"]]
    if task:
        checks.append(result["feature_is_ancestor"])
    if deployed_commit:
        checks.append(result["deployment_matches_remote"])
    result["ok"] = all(checks)
    return result


def cleanup(root_value: str, task: str, *, approved: bool) -> dict[str, Any]:
    if not approved:
        raise WorkflowError("cleanup requires explicit --approved authorization")
    root = canonical_root(root_value)
    settings = workflow_settings(root)
    entry = task_entry(root, task)
    fetch_main(root, settings)
    feature_commit = entry.get("feature_commit") or git(pathlib.Path(entry["worktree"]), "rev-parse", "HEAD")
    remote_ref = f"{settings['remote']}/{settings['main_branch']}"
    if run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", feature_commit, remote_ref],
        check=False,
    ).returncode:
        raise WorkflowError("feature commit is not in remote main")
    worktree = pathlib.Path(entry["worktree"])
    if dirty_paths(worktree):
        raise WorkflowError("feature worktree is not clean")
    git(root, "worktree", "remove", str(worktree))
    git(root, "branch", "-d", entry["branch"])
    data = registry()
    value = data["tasks"][task_key(root, task)]
    value.update({"status": "cleaned", "cleaned_at": now(), "updated_at": now()})
    save_registry(data)
    return value


def status(root_value: str) -> dict[str, Any]:
    root = canonical_root(root_value)
    settings = workflow_settings(root)
    data = registry()
    tasks = [value for value in data["tasks"].values() if value.get("repository") == str(root)]
    return {
        "repository": str(root),
        "settings": settings,
        "canonical_branch": git(root, "branch", "--show-current"),
        "canonical_commit": git(root, "rev-parse", "HEAD"),
        "canonical_dirty_paths": sorted(dirty_paths(root)),
        "worktrees": git(root, "worktree", "list", "--porcelain"),
        "tasks": tasks,
    }


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="Safe multi-conversation worktree workflow")
    sub = parser.add_subparsers(dest="command", required=True)

    start_parser = sub.add_parser("start")
    start_parser.add_argument("project_root")
    start_parser.add_argument("--task", required=True)
    start_parser.add_argument("--conversation", required=True)

    status_parser = sub.add_parser("status")
    status_parser.add_argument("project_root")

    finish_parser = sub.add_parser("finish")
    finish_parser.add_argument("project_root")
    finish_parser.add_argument("--task", required=True)

    release_parser = sub.add_parser("release")
    release_parser.add_argument("project_root")
    release_parser.add_argument("--task", required=True)
    release_parser.add_argument("--approved", action="store_true")
    release_parser.add_argument("--test-command", action="append", default=[])

    sync_parser = sub.add_parser("sync-canonical")
    sync_parser.add_argument("project_root")
    sync_parser.add_argument("--no-fetch", action="store_true")

    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("project_root")
    verify_parser.add_argument("--task")
    verify_parser.add_argument("--deployed-commit", default="")

    deploy_parser = sub.add_parser("deploy-staging")
    deploy_parser.add_argument("project_root")
    deploy_parser.add_argument("--task", required=True)
    deploy_parser.add_argument("--approved", action="store_true")
    deploy_parser.add_argument("--command", action="append", default=[])
    deploy_parser.add_argument("--deployed-commit-command", default="")

    cleanup_parser = sub.add_parser("cleanup")
    cleanup_parser.add_argument("project_root")
    cleanup_parser.add_argument("--task", required=True)
    cleanup_parser.add_argument("--approved", action="store_true")

    args = parser.parse_args()
    try:
        if args.command == "start":
            value = start(args.project_root, args.task, args.conversation)
        elif args.command == "status":
            value = status(args.project_root)
        elif args.command == "finish":
            value = finish(args.project_root, args.task)
        elif args.command == "release":
            value = release(
                args.project_root,
                args.task,
                approved=args.approved,
                test_commands=args.test_command,
            )
        elif args.command == "sync-canonical":
            value = sync_canonical(args.project_root, fetch=not args.no_fetch)
        elif args.command == "verify":
            value = verify(args.project_root, args.task, args.deployed_commit)
        elif args.command == "deploy-staging":
            value = deploy_staging(
                args.project_root,
                args.task,
                approved=args.approved,
                commands=args.command,
                deployed_commit_command=args.deployed_commit_command,
            )
        elif args.command == "cleanup":
            value = cleanup(args.project_root, args.task, approved=args.approved)
        else:
            raise WorkflowError("unknown command")
        print_json(value)
        return 0 if value.get("ok", True) else 2
    except WorkflowError as exc:
        print_json({"ok": False, "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
