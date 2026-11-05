"""Path-based Windows legacy-hook migration with snapshots and backups."""
from __future__ import annotations
import json
import pathlib
import uuid
import hashlib
import tempfile
import runtime_platform as storage
import vibe_memory_hooks as hooks


def _snapshot(root):
    import vibe_memory_migration as migration
    root = storage.validate_path(pathlib.Path(root).absolute())
    if not root.is_dir():
        raise ValueError("project directory does not exist")
    result = {}
    for document, script in migration._LEGACY_HOOK_DOCUMENTS:
        for relative in (document, script):
            path = storage.validate_path(root / relative)
            result[str(relative)] = storage.read_regular(path) if path.exists() else None
    return result


def _identity(root):
    metadata = storage.validate_path(root).stat(follow_symlinks=False)
    return metadata.st_dev, metadata.st_ino


def _lock_path(root):
    locks = storage.private_directory(pathlib.Path(tempfile.gettempdir()) / "NexarlForge/migration-locks")
    return locks / (hashlib.sha256(str(root).casefold().encode("utf-8")).hexdigest() + ".lock")


def prepare(root):
    root = storage.validate_path(pathlib.Path(root).absolute())
    identity = _identity(root)
    snapshots = _snapshot(root)
    if _identity(root) != identity:
        raise ValueError("project root changed during migration preflight")
    return {"root": root, "snapshots": snapshots, "identity": identity}


def preview(root, *, _plan=None):
    import vibe_memory_migration as migration
    import memory_project
    errors = []
    managed, custom, documents, targets, scripts = set(), set(), [], [], []
    root = pathlib.Path(root).absolute()
    try:
        plan = prepare(root) if _plan is None else _plan
        root, snapshots = plan["root"], plan["snapshots"]
        for relative, script_relative in migration._LEGACY_HOOK_DOCUMENTS:
            raw = snapshots[str(relative)]
            if raw is None:
                documents.append({"path": str(root / relative), "status": "missing"})
                continue
            try:
                value = json.loads(raw.decode("utf-8"))
                if not isinstance(value, dict) or not isinstance(value.get("hooks", {}), dict):
                    raise ValueError("hook document and hooks must be objects")
                agent = "codex" if relative.parts[0] == ".codex" else "claude"
                script = snapshots[str(script_relative)]
                owned = script is not None and script.decode("utf-8").replace("\r\n", "\n") == memory_project.hook_script(root, agent)
                has_reference = any(migration._is_legacy_memory_command(handler.get("command"))
                    for groups in value.get("hooks", {}).values() if isinstance(groups, list)
                    for group in groups if isinstance(group, dict) and isinstance(group.get("hooks"), list)
                    for handler in group["hooks"] if isinstance(handler, dict) and handler.get("type") == "command")
                if has_reference and not owned:
                    errors.append(migration._issue(root / script_relative, ValueError("referenced script is missing or not manager-owned")))
                found, other = migration._hook_entry_counts(value, owned_script=owned)
                managed.update(found)
                custom.update(other)
                documents.append({"path": str(root / relative), "status": "ok", "managed_entries": len(found), "custom_entries": len(other)})
                if found:
                    targets.extend((str(root / relative), str(root / script_relative)))
                    scripts.append(str(root / script_relative))
            except (ValueError, UnicodeError) as error:
                errors.append(migration._issue(root / relative, error))
    except (OSError, ValueError) as error:
        errors.append(migration._issue(root, error))
    return {"root": str(root), "status": "error" if errors else "preview", "managed_entries": len(managed), "custom_entries": len(custom),
        "targets": sorted(set(targets)), "script_targets": sorted(set(scripts)), "documents": documents, "errors": errors}


def execute(plan, *, audit=False):
    import vibe_memory_migration as migration
    import memory_project
    root = storage.validate_path(pathlib.Path(plan["root"]))
    backups, changed, committed, archived = [], [], {}, []
    removed = 0
    with storage.file_lock(_lock_path(root)):
        if _identity(root) != plan["identity"] or _snapshot(root) != plan["snapshots"]:
            raise ValueError("legacy project binding or files changed after preflight")
        inspection = preview(root, _plan=plan)
        if inspection["errors"]:
            raise ValueError("legacy hook ownership conflict")
        try:
            for relative, script_relative in migration._LEGACY_HOOK_DOCUMENTS:
                raw = plan["snapshots"][str(relative)]
                script = plan["snapshots"][str(script_relative)]
                if raw is None or script is None:
                    continue
                agent = "codex" if relative.parts[0] == ".codex" else "claude"
                if script.decode("utf-8").replace("\r\n", "\n") != memory_project.hook_script(root, agent):
                    continue
                updated, count = migration._remove_legacy_memory_entries(json.loads(raw.decode("utf-8")), owned_script=True)
                if not count:
                    continue
                if _identity(root) != plan["identity"]:
                    raise ValueError("legacy project root changed during migration")
                target = root / relative
                with hooks.exclusive_lock(target.with_name(f".{target.name}.vibe-memory.lock")):
                    result = hooks.write_with_backup(target, updated, expected_source=raw, _include_commit_snapshot=True)
                    if result["changed"]:
                        committed[target] = result["_commit_snapshot"][1]
                backups.append(result["backup"])
                changed.append(str(target))
                script_path = storage.validate_path(root / script_relative)
                if storage.read_regular(script_path) != script:
                    raise ValueError("legacy script changed during migration")
                archive = script_path.with_name(script_path.name + ".archived." + uuid.uuid4().hex)
                script_path.rename(archive)
                archived.append((script_path, archive))
                backups.append(str(archive))
                changed.append(str(script_path))
                removed += count
            result = {"result": "ok", "changed_paths": changed, "backups": backups, "removed_handlers": removed}
            if audit:
                import hashlib
                def state_digest(snapshot):
                    digest = hashlib.sha256()
                    for name, content in sorted(snapshot.items()):
                        raw = json.dumps([name, content.hex() if content is not None else None], ensure_ascii=False).encode("utf-8")
                        digest.update(len(raw).to_bytes(8, "big"))
                        digest.update(raw)
                    return digest.hexdigest()
                inspection = preview(root, _plan=plan)
                status = "applied" if changed else "unchanged"
                audit_path = storage.validate_path(root / "codex/migration_audits" / ("legacy-hooks-" + uuid.uuid4().hex + ".json"))
                document = {"root": str(root), "before_digest": state_digest(plan["snapshots"]),
                    "after_digest": state_digest(_snapshot(root)), "changed_paths": changed,
                    "backups": backups, "result": status, "conflict_paths": []}
                storage.atomic_write(audit_path, (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
                result.update(result=status, audit=str(audit_path), managed_entries=inspection["managed_entries"], custom_entries=inspection["custom_entries"])
            return result
        except Exception as error:
            if _identity(root) != plan["identity"]:
                raise RuntimeError("migration root changed; recovery artifacts preserved") from error
            conflicts = []
            for path, archive in reversed(archived):
                if path.exists():
                    conflicts.append(str(path))
                else:
                    archive.rename(path)
            for target, content in committed.items():
                with hooks.exclusive_lock(target.with_name(f".{target.name}.vibe-memory.lock")):
                    if storage.read_regular(target) != content:
                        conflicts.append(str(target))
                    else:
                        storage.atomic_write(target, plan["snapshots"][str(target.relative_to(root))])
            if conflicts:
                raise RuntimeError("migration rollback conflict: " + ", ".join(conflicts)) from error
            raise


def safe_tree_digest(root, *, max_files=4096, max_total_bytes=64 * 1024 * 1024, max_file_bytes=16 * 1024 * 1024):
    import hashlib
    import os
    root = storage.validate_path(pathlib.Path(root))
    files = []
    total = 0
    def walk(directory):
        nonlocal total
        with os.scandir(directory) as entries:
            for entry in entries:
                path = storage.validate_path(pathlib.Path(entry.path))
                if entry.is_dir(follow_symlinks=False):
                    walk(path)
                elif entry.is_file(follow_symlinks=False):
                    size = entry.stat(follow_symlinks=False).st_size
                    if size > max_file_bytes:
                        raise ValueError("package file exceeds size limit")
                    total += size
                    if total > max_total_bytes or len(files) >= max_files:
                        raise ValueError("package exceeds size or file count limit")
                    files.append((path.relative_to(root).as_posix(), path, size))
                else:
                    raise ValueError("package contains a special file")
    walk(root)
    digest = hashlib.sha256()
    for relative, path, size in sorted(files, key=lambda item: pathlib.PurePosixPath(item[0]).parts):
        raw = storage.read_regular(path, max_file_bytes)
        if len(raw) != size:
            raise ValueError("package changed during inspection")
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def inspect_ui_skill_digests(home):
    import vibe_memory_migration as migration
    import ui_skill_registry
    registry, errors = migration._skill_registry(home)
    records = []
    packages = home / "packages"
    if registry:
        for name, versions in registry["packages"].items():
            if not ui_skill_registry.NAME_PATTERN.fullmatch(str(name)) or not isinstance(versions, list):
                errors.append(migration._issue(packages, ValueError("invalid package name or versions")))
                continue
            for record in versions:
                try:
                    if not isinstance(record, dict):
                        raise ValueError("invalid package record")
                    version = record.get("version_id")
                    if (not isinstance(version, str) or not version or version in {".", ".."}
                            or any(c in version for c in '/\\:') or version.rstrip(' .') != version):
                        raise ValueError("invalid package version component")
                    recorded = record.get("package_path")
                    if not isinstance(recorded, str) or not isinstance(record.get("digest"), str):
                        raise ValueError("invalid package path or digest")
                    expected = packages / name / version
                    if pathlib.Path(recorded).parts[-3:] != ("packages", name, version):
                        raise ValueError("UI skill package path does not match canonical storage")
                    actual = safe_tree_digest(expected)
                    if actual != record["digest"]:
                        errors.append(migration._issue(expected, ValueError("UI skill package digest mismatch")))
                    records.append({"name": name, "version_id": version, "path": str(expected),
                        "digest": record["digest"], "actual_digest": actual})
                except (OSError, ValueError) as error:
                    errors.append(migration._issue(packages, error))
    return migration._area({"errors": errors}, records, registry is not None and not errors)


def apply(project_roots, paths):
    import vibe_memory_migration as migration
    try:
        roots = migration._selected_registered_roots(project_roots, paths)
    except (OSError, ValueError):
        return {"status": "failed", "projects": [{"root": str(root), "result": "failed", "error": "invalid_root", "audit": ""} for root in project_roots]}
    results = []
    for root in roots:
        try:
            plan = prepare(root)
            if preview(root, _plan=plan)["errors"]:
                raise ValueError("legacy hook ownership conflict")
            result = execute(plan, audit=True)
            results.append({"root": str(root), **result})
        except (OSError, ValueError, RuntimeError) as error:
            results.append({"root": str(root), "result": "failed", "error": str(error), "audit": ""})
    failures = sum(item["result"] == "failed" for item in results)
    status = "failed" if failures and failures == len(results) else "partial" if failures else "applied"
    return {"status": status, "projects": results}
