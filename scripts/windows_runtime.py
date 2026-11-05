"""Windows installation and lifecycle integration using Python and Git only."""
from __future__ import annotations
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import uuid
import webbrowser

import runtime_platform as storage
import vibe_memory_hooks as hooks
import vibe_memory_paths
import windows_service


def _rename_directory(source, target):
    # Filesystem filters can briefly retain a stopped Python runtime directory.
    deadline = time.monotonic() + 2
    while True:
        try:
            return source.rename(target)
        except PermissionError:
            if target.exists() or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _restore_owned(target, expected, original):
    # Match hooks.repair/uninstall and settings.save_settings lock names.
    # Noncooperating editors retain the platform's userspace CAS limitation.
    with hooks.exclusive_lock(target.with_name(f".{target.name}.vibe-memory.lock")):
        actual = storage.read_regular(target) if target.exists() else None
        if actual != expected:
            return False
        if original is None:
            storage.validate_path(target).unlink(missing_ok=True)
        else:
            storage.atomic_write(target, original)
        return True


def config(paths):
    value = json.loads(storage.read_regular(pathlib.Path(paths.install_root) / "config.json").decode("utf-8"))
    if not isinstance(value, dict) or value.get("service") != "vibe-memory":
        raise ValueError("invalid Windows runtime config")
    return value


def _write_json(path, value):
    storage.atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _targets(paths):
    home = pathlib.Path(paths.personal_memory).parents[1]
    return [(home / ".codex/hooks.json", "codex"), (home / ".claude/settings.json", "claude-code")]


def repair_hooks(paths, *, on_change=None, expected_sources=None):
    settings = config(paths)
    results = {}
    for target, agent in _targets(paths):
        enabled = settings.get("codex_hooks_enabled" if agent == "codex" else "claude_hooks_enabled", False)
        options = {}
        if expected_sources is not None:
            options.update(_expected_source=expected_sources[target], _include_commit_snapshot=True)
        result = hooks.repair(target, agent, paths.launcher, **options) if enabled else hooks.uninstall(target, paths.launcher, **options)
        committed = result.pop("_commit_snapshot", None)
        results[agent] = result
        if on_change is not None and result["changed"]:
            on_change(target, committed[1] if committed is not None else storage.read_regular(target))
    return results


def _validate_tree(root):
    storage.validate_path(root)
    for path in root.rglob("*"):
        storage.validate_path(path)
        if not path.is_file() and not path.is_dir():
            raise ValueError(f"unsupported runtime entry: {path}")


def install(paths, source, *, port=8897, with_claude_hooks=False):
    if not storage.IS_WINDOWS or sys.version_info < (3, 10):
        raise RuntimeError("Windows Python 3.10+ required")
    if not shutil.which("git"):
        raise RuntimeError("Git is required")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("invalid service port")
    source = storage.validate_path(pathlib.Path(source).absolute())
    manifest = vibe_memory_paths.read_release_manifest(source / "release.json")
    root = storage.private_directory(pathlib.Path(paths.install_root))
    marker = root / "windows-install.json"
    current = root / "current"
    if current.exists():
        if not marker.exists() or json.loads(storage.read_regular(marker).decode("utf-8")).get("managed_by") != "nexarl-forge-windows":
            raise ValueError("refusing to overwrite an unmanaged runtime")
    with storage.file_lock(root / ".windows-lifecycle.lock"):
        staging = root / (".install-" + uuid.uuid4().hex)
        staging.mkdir()
        before_running = windows_service.status(paths)["ok"]
        snapshots = {}
        for target in (root / "config.json", marker, pathlib.Path(paths.launcher), pathlib.Path(paths.launcher).with_suffix(""), *[target for target, _ in _targets(paths)]):
            snapshots[target] = storage.read_regular(target) if target.exists() else None
        committed = {}
        def record_change(target, content=hooks._NO_SOURCE_CHECK):
            committed[target] = (storage.read_regular(target) if target.exists() else None) if content is hooks._NO_SOURCE_CHECK else content
        def write_managed(target, content):
            actual = storage.read_regular(target) if target.exists() else None
            if actual != snapshots[target]:
                raise RuntimeError(f"installation conflict: {target}")
            storage.atomic_write(target, content)
            record_change(target)
        replaced = False
        moved_old = False
        prior_previous = root / (".saved-previous-" + uuid.uuid4().hex)
        try:
            for name in ("scripts", "templates", "docs"):
                tree = source / name
                _validate_tree(tree)
                shutil.copytree(tree, staging / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            for name in ("README.md", "release.json", "LICENSE"):
                if (source / name).exists():
                    storage.atomic_write(staging / name, storage.read_regular(source / name))
            old = config(paths) if (root / "config.json").exists() else {}
            import vibe_memory_settings
            selected = {**vibe_memory_settings.default_settings(), **old}
            selected.update(app_version=manifest["app_version"], port=port, service_port=port,
                python_executable=sys.executable, python_version=f"{sys.version_info.major}.{sys.version_info.minor}",
                service="vibe-memory", claude_hooks_enabled=with_claude_hooks or old.get("claude_hooks_enabled", False))
            windows_service.stop(paths)
            previous = root / "previous"
            if current.exists():
                if previous.exists():
                    _validate_tree(previous)
                    _rename_directory(previous, prior_previous)
                _rename_directory(current, previous)
                moved_old = True
            _rename_directory(staging, current)
            replaced = True
            write_managed(root / "config.json", (json.dumps(selected, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            write_managed(marker, (json.dumps({"managed_by": "nexarl-forge-windows", "version": manifest["app_version"]}) + "\n").encode("utf-8"))
            storage.private_directory(pathlib.Path(paths.personal_memory))
            storage.private_directory(pathlib.Path(paths.project_registry).parent)
            for path, content in (
                (paths.personal_memory / "long.md", "# Personal Long Memory\n"),
                (paths.personal_memory / "short.md", "# Personal Short Memory\n"),
                (paths.personal_memory / "proposals.md", "# Personal Memory Proposals\n"),
                (paths.project_registry, '{"current_project":"","projects":[]}\n'),
            ):
                storage.validate_path(path)
                if not path.exists():
                    storage.atomic_write(path, content.encode("utf-8"))
            cli = current / "scripts/vibe_memory_cli.py"
            launcher = pathlib.Path(paths.launcher)
            # Paths in batch files must not expand percent characters.
            line = batch_command([sys.executable, str(cli)])
            write_managed(launcher, ("@echo off\r\n@chcp 65001 >nul\r\nsetlocal DisableDelayedExpansion\r\n" + line + " %*\r\n").encode("utf-8"))
            # Bash launcher delegates with an argv array through the Python CLI.
            import shlex
            bash = "#!/usr/bin/env bash\nexec " + shlex.join([sys.executable.replace("\\", "/"), str(cli).replace("\\", "/")]) + ' "$@"\n'
            write_managed(launcher.with_suffix(""), bash.encode("utf-8"))
            for target, _ in _targets(paths):
                actual = storage.read_regular(target) if target.exists() else None
                if actual != snapshots[target]:
                    raise RuntimeError(f"installation hook conflict: {target}")
            repaired = repair_hooks(paths, on_change=record_change, expected_sources=snapshots)
            service = windows_service.start(paths, port=port)
            if prior_previous.exists():
                _validate_tree(prior_previous)
                shutil.rmtree(prior_previous)
            return {"ok": True, "runtime": str(current), "launcher": str(launcher), "hooks": repaired, "service": service}
        except Exception as original_error:
            if replaced:
                windows_service.stop(paths)
                _validate_tree(current)
                failed = root / (".failed-" + uuid.uuid4().hex)
                _rename_directory(current, failed)
                if moved_old:
                    _rename_directory((root / "previous"), current)
                if prior_previous.exists():
                    _rename_directory(prior_previous, root / "previous")
                conflicts = []
                for target, expected in committed.items():
                    if not _restore_owned(target, expected, snapshots[target]):
                        conflicts.append(str(target))
                _validate_tree(failed)
                shutil.rmtree(failed)
                if conflicts:
                    raise RuntimeError("installation rollback preserved concurrent edits: " + ", ".join(conflicts)) from original_error
                if before_running and moved_old:
                    try:
                        windows_service.start(paths, port=config(paths)["port"])
                    except Exception as restart_error:
                        raise RuntimeError(f"installation failed: {original_error}; restored service failed: {restart_error}") from original_error
            elif moved_old:
                _rename_directory((root / "previous"), current)
                if prior_previous.exists():
                    _rename_directory(prior_previous, root / "previous")
            raise
        finally:
            if staging.exists():
                _validate_tree(staging)
                shutil.rmtree(staging)


def uninstall(paths):
    root = storage.validate_path(pathlib.Path(paths.install_root))
    marker = json.loads(storage.read_regular(root / "windows-install.json").decode("utf-8"))
    if marker.get("managed_by") != "nexarl-forge-windows":
        raise ValueError("unmanaged Windows installation")
    with storage.file_lock(root / ".windows-lifecycle.lock"):
        windows_service.stop(paths)
        reconcile_startup(paths, False)
        for target, agent in _targets(paths):
            hooks.uninstall(target, paths.launcher)
        for target in (pathlib.Path(paths.launcher), pathlib.Path(paths.launcher).with_suffix("")):
            storage.validate_path(target).unlink(missing_ok=True)
        for name in ("current", "previous"):
            target = storage.validate_path(root / name)
            if target.exists():
                _validate_tree(target)
                shutil.rmtree(target)
    return {"ok": True, "data_preserved": True}


def dispatch(args, paths):
    name = args.command
    if name in {"install", "update"}:
        return install(paths, pathlib.Path(args.source_root), port=getattr(args, "port", None) or config(paths)["port"],
            with_claude_hooks=getattr(args, "with_claude_hooks", False))
    if name == "rollback":
        return rollback(paths)
    if name == "repair":
        return repair(paths)
    if name in {"status", "doctor"}:
        return doctor(paths)
    if name == "stop":
        return windows_service.stop(paths)
    if name in {"start", "restart"}:
        if name == "restart":
            windows_service.stop(paths)
        return windows_service.start(paths, port=config(paths)["port"])
    if name == "open":
        service = windows_service.status(paths)
        if not service["ok"]:
            raise RuntimeError("service is not running; run start")
        webbrowser.open(f"http://127.0.0.1:{service['port']}/")
        return {"ok": True}
    if name == "uninstall":
        if args.remove_data:
            raise ValueError("Windows uninstall preserves memory; data deletion is a separate explicit action")
        return uninstall(paths)
    if name == "hooks":
        if args.hooks_command == "repair":
            return repair_hooks(paths)
        return {agent: hooks.status(target, agent, paths.launcher) for target, agent in _targets(paths)}
    raise ValueError(f"Windows lifecycle command not yet implemented: {name}")


STARTUP_MARKER = "rem Nexarl Forge managed login startup"


def reconcile_startup(paths, enabled):
    target = storage.validate_path(pathlib.Path(paths.launch_agent))
    existing = storage.read_regular(target) if target.exists() else None
    if existing is not None and STARTUP_MARKER.encode() not in existing.splitlines():
        raise ValueError("refusing to change an unmanaged Startup entry")
    if not enabled:
        target.unlink(missing_ok=True)
        return {"changed": existing is not None, "path": str(target)}
    value = config(paths)
    command = batch_command([value["python_executable"], str(pathlib.Path(paths.install_root) / "current/scripts/vibe_memory_cli.py"), "start"])
    content = ("@echo off\r\n@chcp 65001 >nul\r\nsetlocal DisableDelayedExpansion\r\n" + STARTUP_MARKER + "\r\n" + command + "\r\n").encode("utf-8")
    if existing != content:
        storage.atomic_write(target, content)
    return {"changed": existing != content, "path": str(target)}


def apply_first_run(paths, request, *, manager_source_root, register_workspace):
    import vibe_memory_settings as settings
    normalized = settings.normalize_first_run_request(paths, request, manager_source_root=manager_source_root)
    selected = normalized["settings"]
    root = pathlib.Path(paths.install_root)
    with storage.file_lock(root / ".windows-lifecycle.lock"):
        # Refuse an unmanaged Startup file before touching other settings.
        startup = storage.validate_path(pathlib.Path(paths.launch_agent))
        if startup.exists() and STARTUP_MARKER.encode() not in storage.read_regular(startup).splitlines():
            raise ValueError("unmanaged Startup entry")
        originals = {}
        committed = {}
        def snapshot(path):
            try:
                return storage.read_regular(path)
            except FileNotFoundError:
                return None
        def run(operation, targets):
            for target in targets:
                originals.setdefault(target, snapshot(target))
            result = operation()
            for target in targets:
                committed[target] = snapshot(target)
            return result
        old = config(paths)
        try:
            saved = run(lambda: settings.save_settings(paths, selected), [root / "config.json"])
            for target, agent in _targets(paths):
                enabled = saved["codex_hooks_enabled" if agent == "codex" else "claude_hooks_enabled"]
                run(lambda target=target, agent=agent, enabled=enabled:
                    hooks.repair(target, agent, paths.launcher) if enabled else hooks.uninstall(target, paths.launcher), [target])
            short = pathlib.Path(paths.personal_memory) / "short.md"
            run(lambda: settings.prune_personal_short(short, retention_days=saved["personal_short_retention_days"]), [short])
            registered = None
            if normalized["workspace"]:
                registered = run(lambda: register_workspace(normalized["workspace"]), [pathlib.Path(paths.project_registry)])
            launch = run(lambda: reconcile_startup(paths, saved["start_at_login"]), [startup])
            action = run(lambda: settings.write_service_action(paths, desired_start_at_login=saved["start_at_login"],
                status="active" if saved["start_at_login"] else "bootout_pending"), [root / "state/service-action.json"])
            return {"settings": saved, "registered_project": registered, "launch_agent": launch,
                "bootout_after_response": not saved["start_at_login"],
                "restart_after_response": saved["start_at_login"] and old["port"] != saved["service_port"],
                "service_action_generation": action["generation"]}
        except Exception as error:
            conflicts = []
            for target, content in reversed(list(committed.items())):
                if snapshot(target) != content:
                    conflicts.append(str(target))
                    continue
                original = originals[target]
                if original is None:
                    storage.validate_path(target).unlink(missing_ok=True)
                else:
                    storage.atomic_write(target, original)
            if conflicts:
                raise RuntimeError("first-run rollback conflicts: " + ", ".join(conflicts)) from error
            raise


def batch_command(arguments):
    """Quote literal batch-file argv, including shell metacharacters."""
    result = []
    for value in arguments:
        value = str(value)
        if any(character in value for character in ('"', "\r", "\n")):
            raise ValueError("invalid batch command argument")
        result.append('"' + value.replace("%", "%%") + '"')
    return " ".join(result)


def rollback(paths):
    root = storage.validate_path(pathlib.Path(paths.install_root))
    current = root / "current"
    previous = root / "previous"
    with storage.file_lock(root / ".windows-lifecycle.lock"):
        _validate_tree(current)
        _validate_tree(previous)
        manifest = vibe_memory_paths.read_release_manifest(previous / "release.json")
        settings = config(paths)
        before = dict(settings)
        was_running = windows_service.status(paths)["ok"]
        windows_service.stop(paths)
        temporary = root / (".rollback-" + uuid.uuid4().hex)
        promoted = False
        archived = False
        originals = {target: storage.read_regular(target) if target.exists() else None
            for target in (root / "config.json", root / "windows-install.json", *[path for path, _ in _targets(paths)])}
        committed = {}
        def record_change(target, content=hooks._NO_SOURCE_CHECK):
            committed[target] = (storage.read_regular(target) if target.exists() else None) if content is hooks._NO_SOURCE_CHECK else content
        try:
            _rename_directory(current, temporary)
            _rename_directory(previous, current)
            promoted = True
            _rename_directory(temporary, previous)
            archived = True
            settings["app_version"] = manifest["app_version"]
            for target in originals:
                actual = storage.read_regular(target) if target.exists() else None
                if actual != originals[target]:
                    raise RuntimeError(f"rollback conflict: {target}")
            _write_json(root / "config.json", settings)
            record_change(root / "config.json")
            _write_json(root / "windows-install.json", {"managed_by": "nexarl-forge-windows", "version": manifest["app_version"]})
            record_change(root / "windows-install.json")
            repair_hooks(paths, on_change=record_change, expected_sources=originals)
            service = windows_service.start(paths, port=settings["port"]) if was_running else {"ok": True, "status": "stopped"}
            return {"ok": True, "app_version": manifest["app_version"], "service": service}
        except Exception as original_error:
            windows_service.stop(paths)
            if promoted:
                failed = root / (".rollback-failed-" + uuid.uuid4().hex)
                _rename_directory(current, failed)
                (previous if archived else temporary).rename(current)
                _rename_directory(failed, previous)
            elif temporary.exists():
                _rename_directory(temporary, current)
            conflicts = []
            for target, expected in committed.items():
                if not _restore_owned(target, expected, originals[target]):
                    conflicts.append(str(target))
            if conflicts:
                raise RuntimeError("rollback preserved concurrent edits: " + ", ".join(conflicts)) from original_error
            if was_running:
                windows_service.start(paths, port=before["port"])
            raise


def repair(paths):
    root = storage.validate_path(pathlib.Path(paths.install_root))
    settings = config(paths)
    manifest = vibe_memory_paths.read_release_manifest(root / "current/release.json")
    if settings["app_version"] != manifest["app_version"]:
        raise ValueError("runtime version does not match configuration")
    _validate_tree(root / "current")
    with storage.file_lock(root / ".windows-lifecycle.lock"):
        cli = root / "current/scripts/vibe_memory_cli.py"
        line = batch_command([settings["python_executable"], str(cli)])
        storage.atomic_write(pathlib.Path(paths.launcher), ("@echo off\r\n@chcp 65001 >nul\r\nsetlocal DisableDelayedExpansion\r\n" + line + " %*\r\n").encode("utf-8"))
        import shlex
        bash = "#!/usr/bin/env bash\nexec " + shlex.join([settings["python_executable"].replace("\\", "/"), str(cli).replace("\\", "/")]) + ' "$@"\n'
        storage.atomic_write(pathlib.Path(paths.launcher).with_suffix(""), bash.encode("utf-8"))
        repaired = repair_hooks(paths)
        startup = reconcile_startup(paths, bool(settings["start_at_login"] and settings["first_run_complete"]))
        return {"ok": True, "hooks": repaired, "startup": startup}


def doctor(paths):
    """Diagnose independent areas without hiding malformed installation state."""
    result = {}
    settings = None
    try:
        settings = config(paths)
        root = storage.validate_path(pathlib.Path(paths.install_root))
        manifest = vibe_memory_paths.read_release_manifest(root / "current/release.json")
        marker = json.loads(storage.read_regular(root / "windows-install.json").decode("utf-8"))
        if marker.get("managed_by") != "nexarl-forge-windows" or marker.get("version") != manifest["app_version"]:
            raise ValueError("installation ownership/version mismatch")
        if settings["app_version"] != manifest["app_version"]:
            raise ValueError("configuration version mismatch")
        for name in ("vibe_memory_cli.py", "windows_service.py", "windows_runtime.py"):
            storage.read_regular(root / "current/scripts" / name)
        interpreter = settings["python_executable"]
        probe = subprocess.run([interpreter, "-c", "import sys;raise SystemExit(sys.version_info < (3,10))"], capture_output=True, timeout=5)
        if probe.returncode:
            raise ValueError("Windows Python 3.10+ interpreter unavailable")
        launcher = storage.read_regular(pathlib.Path(paths.launcher)).decode("utf-8")
        if batch_command([interpreter, str(root / "current/scripts/vibe_memory_cli.py")]) not in launcher:
            raise ValueError("launcher drifted; run repair")
        result["runtime"] = {"ok": True, "status": "current"}
    except Exception as error:
        result["runtime"] = {"ok": False, "status": "invalid", "error": str(error)}
    for target, agent in _targets(paths):
        key = "codex_hooks" if agent == "codex" else "claude_hooks"
        try:
            enabled = settings is not None and settings.get("codex_hooks_enabled" if agent == "codex" else "claude_hooks_enabled", False)
            if enabled:
                status = hooks.status(target, agent, paths.launcher)
                result[key] = {**status, "ok": status["status"] == "current"}
            else:
                document = hooks.load_document(target) if target.exists() else {"hooks": {}}
                clean = hooks.remove_managed_entries(document, paths.launcher)
                result[key] = {"ok": clean == document, "status": "disabled" if clean == document else "drifted"}
        except Exception as error:
            result[key] = {"ok": False, "status": "invalid", "error": str(error)}
    try:
        result["service"] = windows_service.status(paths)
    except Exception as error:
        result["service"] = {"ok": False, "status": "invalid", "error": str(error)}
    try:
        for name in ("long.md", "short.md", "proposals.md"):
            storage.read_regular(pathlib.Path(paths.personal_memory) / name).decode("utf-8")
        registry = json.loads(storage.read_regular(pathlib.Path(paths.project_registry)).decode("utf-8"))
        if not isinstance(registry, dict) or not isinstance(registry.get("projects"), list) or not isinstance(registry.get("current_project"), str):
            raise ValueError("malformed project registry")
        result["data"] = {"ok": True, "status": "available"}
    except Exception as error:
        result["data"] = {"ok": False, "status": "invalid", "error": str(error)}
    return result
