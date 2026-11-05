"""Path-based Windows legacy-hook migration with snapshots and backups."""
from __future__ import annotations
import json
import pathlib
import uuid
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


def prepare(root):
    root = storage.validate_path(pathlib.Path(root).absolute())
    with storage.file_lock(root / ".nexarl-forge-migration.lock"):
        return {"root": root, "snapshots": _snapshot(root)}


def preview(root):
    import vibe_memory_migration as migration
    import memory_project
    plan = prepare(root)
    root, snapshots = plan["root"], plan["snapshots"]
    managed, custom, documents, targets, scripts = set(), set(), [], [], []
    for relative, script_relative in migration._LEGACY_HOOK_DOCUMENTS:
        raw = snapshots[str(relative)]
        if raw is None:
            documents.append({"path": str(root / relative), "status": "missing"})
            continue
        value = json.loads(raw.decode("utf-8"))
        agent = "codex" if relative.parts[0] == ".codex" else "claude"
        script = snapshots[str(script_relative)]
        owned = script is not None and script.decode("utf-8").replace("\r\n", "\n") == memory_project.hook_script(root, agent)
        found, other = migration._hook_entry_counts(value, owned_script=owned)
        managed.update(found)
        custom.update(other)
        documents.append({"path": str(root / relative), "status": "ok", "managed_entries": len(found), "custom_entries": len(other)})
        if found:
            targets.append(str(root / relative))
            scripts.append(str(root / script_relative))
    return {"root": str(root), "status": "preview", "managed_entries": len(managed), "custom_entries": len(custom),
        "targets": targets, "script_targets": scripts, "documents": documents, "errors": []}


def execute(plan):
    import vibe_memory_migration as migration
    import memory_project
    root = storage.validate_path(pathlib.Path(plan["root"]))
    backups, changed, committed, archived = [], [], {}, []
    removed = 0
    with storage.file_lock(root / ".nexarl-forge-migration.lock"):
        if _snapshot(root) != plan["snapshots"]:
            raise ValueError("legacy hook files changed after preflight")
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
                target = root / relative
                result = hooks.write_with_backup(target, updated, expected_source=raw)
                committed[target] = storage.read_regular(target)
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
            return {"result": "ok", "changed_paths": changed, "backups": backups, "removed_handlers": removed}
        except Exception as error:
            conflicts = []
            for path, archive in reversed(archived):
                if path.exists():
                    conflicts.append(str(path))
                else:
                    archive.rename(path)
            for target, content in committed.items():
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
    for relative, path, size in sorted(files):
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
