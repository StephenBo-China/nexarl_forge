"""Digest-bound design packages and project UI approval decisions."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import tempfile
from collections.abc import Callable
from typing import Any

import ui_design_store as store


TASK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
CLASSIFICATIONS = {
    "non_visual",
    "visual_new",
    "visual_change",
    "visual_maintenance",
}
REQUIRED_DESIGN_FILES = {
    "design-brief.md",
    "interaction-spec.md",
    "responsive-spec.md",
}
MANIFEST_FIELDS = {
    "schema_version",
    "task_id",
    "title",
    "classification",
    "pages",
    "components",
    "allowed_file_patterns",
    "design_files",
    "status",
}


class GateError(RuntimeError):
    pass


class GateValidationError(GateError, ValueError):
    pass


class DigestConflict(GateError):
    pass


class DesignPackageNotFound(GateError):
    pass


class IdempotencyConflict(GateError):
    pass


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _ui_root(project_root: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(project_root).expanduser().resolve() / "codex" / "ui_design"


def _package_root(project_root: pathlib.Path, task_id: str) -> pathlib.Path:
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise GateValidationError(f"invalid design task ID: {task_id}")
    return _ui_root(project_root) / "design-packages" / task_id


def _safe_relative(value: str, *, label: str) -> pathlib.PurePosixPath:
    if not isinstance(value, str) or not value.strip():
        raise GateValidationError(f"{label} must be a non-empty project-relative path")
    path = pathlib.PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise GateValidationError(f"unsafe {label}: {value}")
    return path


def _string_list(value: Any, *, label: str, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise GateValidationError(f"{label} must be a list")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise GateValidationError(f"{label} must contain non-empty strings")
    return sorted(set(value))


def normalize_manifest(task_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise GateValidationError("design package manifest must be an object")
    unknown = sorted(set(manifest) - MANIFEST_FIELDS)
    missing = sorted(MANIFEST_FIELDS - set(manifest))
    if unknown:
        raise GateValidationError(f"unknown manifest field: {unknown[0]}")
    if missing:
        raise GateValidationError(f"missing manifest field: {missing[0]}")
    if manifest.get("schema_version") != 1:
        raise GateValidationError("design package schema_version must be 1")
    if manifest.get("task_id") != task_id or not TASK_ID_PATTERN.fullmatch(task_id):
        raise GateValidationError("manifest task_id must match the requested task")
    if not isinstance(manifest.get("title"), str) or not manifest["title"].strip():
        raise GateValidationError("manifest title is required")
    if manifest.get("classification") not in CLASSIFICATIONS:
        raise GateValidationError("unsupported design package classification")
    if manifest.get("status") != "pending_approval":
        raise GateValidationError("manifest status must be pending_approval")

    pages = _string_list(manifest["pages"], label="pages")
    components = _string_list(manifest["components"], label="components")
    patterns = _string_list(
        manifest["allowed_file_patterns"],
        label="allowed_file_patterns",
        allow_empty=False,
    )
    design_files = _string_list(
        manifest["design_files"], label="design_files", allow_empty=False
    )
    for pattern in patterns:
        _safe_relative(pattern, label="allowed file pattern")
    for filename in design_files:
        _safe_relative(filename, label="design file")
    if not REQUIRED_DESIGN_FILES.issubset(design_files):
        missing_files = sorted(REQUIRED_DESIGN_FILES - set(design_files))
        raise GateValidationError(f"missing required design file: {missing_files[0]}")

    return {
        "schema_version": 1,
        "task_id": task_id,
        "title": manifest["title"].strip(),
        "classification": manifest["classification"],
        "pages": pages,
        "components": components,
        "allowed_file_patterns": patterns,
        "design_files": design_files,
        "status": "pending_approval",
    }


def _manifest_path(package_root: pathlib.Path) -> pathlib.Path:
    return package_root / "manifest.json"


def _read_manifest(package_root: pathlib.Path) -> dict[str, Any]:
    path = _manifest_path(package_root)
    try:
        value = store.read_json_strict(path)
    except FileNotFoundError as error:
        raise DesignPackageNotFound(str(package_root)) from error
    except (OSError, json.JSONDecodeError) as error:
        raise GateValidationError(f"invalid design package manifest: {path}: {error}") from error
    return normalize_manifest(str(value.get("task_id", "")), value)


def _declared_files(package_root: pathlib.Path, manifest: dict[str, Any]) -> list[pathlib.Path]:
    declared = {
        package_root.joinpath(*_safe_relative(name, label="design file").parts)
        for name in manifest["design_files"]
    }
    for path in package_root.rglob("*"):
        if path.is_symlink():
            raise GateValidationError(f"design package symlinks are not allowed: {path}")
        if path.is_dir():
            continue
        if path == _manifest_path(package_root):
            continue
        if path not in declared:
            raise GateValidationError(
                f"design package contains undeclared file: {path.relative_to(package_root)}"
            )
    for path in declared:
        if not path.is_file() or path.is_symlink():
            raise GateValidationError(
                f"declared design file is missing or invalid: {path.relative_to(package_root)}"
            )
    return sorted(declared)


def design_package_digest(package_root: pathlib.Path) -> str:
    package_root = pathlib.Path(package_root)
    manifest = _read_manifest(package_root)
    declared = _declared_files(package_root, manifest)
    digest = hashlib.sha256()
    manifest_data = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    digest.update(len(manifest_data).to_bytes(8, "big"))
    digest.update(manifest_data)
    for path in declared:
        relative = path.relative_to(package_root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _approvals_path(project_root: pathlib.Path) -> pathlib.Path:
    return _ui_root(project_root) / "approvals.json"


def _load_approvals(project_root: pathlib.Path) -> dict[str, Any]:
    path = _approvals_path(project_root)
    if not path.exists():
        return {
            "schema_version": 1,
            "package_approvals": {},
            "project_global_approval": None,
            "idempotency": {},
        }
    try:
        value = store.read_json_strict(path)
    except (OSError, json.JSONDecodeError) as error:
        raise GateValidationError(f"invalid UI approvals state: {path}: {error}") from error
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise GateValidationError(f"invalid UI approvals state: {path}")
    if not isinstance(value.get("package_approvals"), dict):
        raise GateValidationError(f"invalid package approvals state: {path}")
    value.setdefault("project_global_approval", None)
    value.setdefault("idempotency", {})
    if not isinstance(value["idempotency"], dict):
        raise GateValidationError(f"invalid approval idempotency state: {path}")
    return value


def _fingerprint(operation: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        {"operation": operation, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _mutate(
    project_root: pathlib.Path,
    *,
    idempotency_key: str,
    operation: str,
    payload: dict[str, Any],
    callback: Callable[[dict[str, Any]], dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    if not idempotency_key:
        raise GateValidationError("idempotency_key is required")
    fingerprint = _fingerprint(operation, payload)
    ui_root = _ui_root(project_root)
    with store.exclusive_lock(ui_root / "gate.lock", timeout=30):
        approvals = _load_approvals(project_root)
        existing = approvals["idempotency"].get(idempotency_key)
        if existing is not None:
            if existing.get("fingerprint") != fingerprint:
                raise IdempotencyConflict(
                    f"idempotency key reused with different arguments: {idempotency_key}"
                )
            return copy.deepcopy(existing["result"]), False
        result = callback(approvals)
        approvals["idempotency"][idempotency_key] = {
            "fingerprint": fingerprint,
            "result": copy.deepcopy(result),
        }
        store.atomic_write_json(_approvals_path(project_root), approvals)
    return result, True


def _audit(project_root: pathlib.Path, event: str, result: dict[str, Any]) -> None:
    store.append_jsonl(
        _ui_root(project_root) / "audit.jsonl",
        {
            "at": _now(),
            "event": event,
            "task_id": result.get("task_id"),
            "digest": result.get("digest"),
            "status": result.get("status"),
        },
    )


def _refresh_context(project_root: pathlib.Path) -> None:
    import memory_project

    memory_project.publish_effective_ui_context(project_root)


def create_design_package(
    project_root: pathlib.Path,
    task_id: str,
    manifest: dict[str, Any],
    *,
    idempotency_key: str,
) -> dict[str, Any]:
    normalized = normalize_manifest(task_id, manifest)
    package_root = _package_root(project_root, task_id)

    def operation(_approvals: dict[str, Any]) -> dict[str, Any]:
        if package_root.exists():
            raise GateValidationError(f"design package already exists: {task_id}")
        package_root.parent.mkdir(parents=True, exist_ok=True)
        stage = pathlib.Path(
            tempfile.mkdtemp(prefix=f".{task_id}.stage-", dir=package_root.parent)
        )
        try:
            store.atomic_write_json(_manifest_path(stage), normalized)
            for name in normalized["design_files"]:
                target = stage.joinpath(*pathlib.PurePosixPath(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("", encoding="utf-8")
            digest = design_package_digest(stage)
            os.replace(stage, package_root)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        return {
            "task_id": task_id,
            "root": str(package_root),
            "manifest": copy.deepcopy(normalized),
            "digest": digest,
            "status": "pending_approval",
        }

    result, created = _mutate(
        project_root,
        idempotency_key=idempotency_key,
        operation="design-package.create",
        payload={"task_id": task_id, "manifest": normalized},
        callback=operation,
    )
    if created:
        _audit(project_root, "design_package_created", result)
        _refresh_context(project_root)
    return result


def get_design_package(project_root: pathlib.Path, task_id: str) -> dict[str, Any]:
    package_root = _package_root(project_root, task_id)
    if not package_root.is_dir():
        raise DesignPackageNotFound(task_id)
    manifest = _read_manifest(package_root)
    digest = design_package_digest(package_root)
    approval = _load_approvals(project_root)["package_approvals"].get(task_id)
    return {
        "task_id": task_id,
        "root": str(package_root),
        "manifest": manifest,
        "digest": digest,
        "status": approval.get("status", "pending_approval")
        if isinstance(approval, dict)
        else "pending_approval",
        "approval": copy.deepcopy(approval),
    }


def list_design_packages(project_root: pathlib.Path) -> list[dict[str, Any]]:
    root = _ui_root(project_root) / "design-packages"
    if not root.exists():
        return []
    return [
        get_design_package(project_root, path.name)
        for path in sorted(root.iterdir())
        if path.is_dir()
    ]


def revise_design_package(
    project_root: pathlib.Path,
    task_id: str,
    manifest: dict[str, Any],
    *,
    idempotency_key: str,
) -> dict[str, Any]:
    normalized = normalize_manifest(task_id, manifest)
    package_root = _package_root(project_root, task_id)

    def operation(approvals: dict[str, Any]) -> dict[str, Any]:
        if not package_root.is_dir():
            raise DesignPackageNotFound(task_id)
        current = _read_manifest(package_root)
        current_files = set(current["design_files"])
        revised_files = set(normalized["design_files"])
        if current_files - revised_files:
            raise GateValidationError("revision cannot leave undeclared design files")
        store.atomic_write_json(_manifest_path(package_root), normalized)
        for name in revised_files - current_files:
            target = package_root.joinpath(*pathlib.PurePosixPath(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("", encoding="utf-8")
        digest = design_package_digest(package_root)
        prior = approvals["package_approvals"].get(task_id)
        approvals["package_approvals"][task_id] = {
            "task_id": task_id,
            "status": "pending_approval",
            "digest": digest,
            "superseded_digest": prior.get("digest") if isinstance(prior, dict) else None,
            "at": _now(),
        }
        return {
            "task_id": task_id,
            "root": str(package_root),
            "manifest": copy.deepcopy(normalized),
            "digest": digest,
            "status": "pending_approval",
        }

    result, changed = _mutate(
        project_root,
        idempotency_key=idempotency_key,
        operation="design-package.revise",
        payload={"task_id": task_id, "manifest": normalized},
        callback=operation,
    )
    if changed:
        _audit(project_root, "design_package_revised", result)
        _refresh_context(project_root)
    return result


def _config(project_root: pathlib.Path) -> dict[str, Any]:
    path = _ui_root(project_root) / "config.json"
    try:
        value = store.read_json_strict(path)
    except (OSError, json.JSONDecodeError) as error:
        raise GateValidationError(f"invalid UI gate config: {path}: {error}") from error
    if not isinstance(value, dict) or value.get("gate_mode") not in {
        "design_package",
        "project_global",
    }:
        raise GateValidationError(f"invalid UI gate config: {path}")
    return value


def approve_design_package(
    project_root: pathlib.Path,
    task_id: str,
    *,
    expected_digest: str,
    idempotency_key: str,
) -> dict[str, Any]:
    package = get_design_package(project_root, task_id)

    def operation(approvals: dict[str, Any]) -> dict[str, Any]:
        current = get_design_package(project_root, task_id)
        if current["digest"] != expected_digest:
            raise DigestConflict(
                f"design package digest changed: expected {expected_digest}, current {current['digest']}"
            )
        config = _config(project_root)
        prior = approvals["package_approvals"].get(task_id)
        record = {
            "task_id": task_id,
            "status": "approved",
            "digest": current["digest"],
            "gate_mode": config["gate_mode"],
            "pages": current["manifest"]["pages"],
            "components": current["manifest"]["components"],
            "allowed_file_patterns": current["manifest"]["allowed_file_patterns"],
            "superseded_digest": prior.get("digest") if isinstance(prior, dict) else None,
            "at": _now(),
        }
        approvals["package_approvals"][task_id] = record
        return copy.deepcopy(record)

    result, changed = _mutate(
        project_root,
        idempotency_key=idempotency_key,
        operation="design-package.approve",
        payload={"task_id": task_id, "expected_digest": expected_digest},
        callback=operation,
    )
    if changed:
        _audit(project_root, "design_package_approved", result)
        _refresh_context(project_root)
    return result


def _record_decision(
    project_root: pathlib.Path,
    task_id: str,
    *,
    status: str,
    reason: str,
    idempotency_key: str,
) -> dict[str, Any]:
    package = get_design_package(project_root, task_id)

    def operation(approvals: dict[str, Any]) -> dict[str, Any]:
        prior = approvals["package_approvals"].get(task_id)
        record = {
            "task_id": task_id,
            "status": status,
            "digest": package["digest"],
            "reason": reason,
            "superseded_digest": prior.get("digest") if isinstance(prior, dict) else None,
            "at": _now(),
        }
        approvals["package_approvals"][task_id] = record
        return copy.deepcopy(record)

    result, changed = _mutate(
        project_root,
        idempotency_key=idempotency_key,
        operation=f"design-package.{status}",
        payload={"task_id": task_id, "reason": reason},
        callback=operation,
    )
    if changed:
        _audit(project_root, f"design_package_{status}", result)
        _refresh_context(project_root)
    return result


def reject_design_package(
    project_root: pathlib.Path,
    task_id: str,
    *,
    reason: str,
    idempotency_key: str,
) -> dict[str, Any]:
    return _record_decision(
        project_root,
        task_id,
        status="rejected",
        reason=reason,
        idempotency_key=idempotency_key,
    )


def request_design_revision(
    project_root: pathlib.Path,
    task_id: str,
    *,
    reason: str,
    idempotency_key: str,
) -> dict[str, Any]:
    return _record_decision(
        project_root,
        task_id,
        status="revision_requested",
        reason=reason,
        idempotency_key=idempotency_key,
    )


def invalidate_design_package(
    project_root: pathlib.Path,
    task_id: str,
    *,
    reason: str,
    idempotency_key: str,
) -> dict[str, Any]:
    return _record_decision(
        project_root,
        task_id,
        status="invalidated",
        reason=reason,
        idempotency_key=idempotency_key,
    )


def gate_status(project_root: pathlib.Path, *, task_id: str | None = None) -> dict[str, Any]:
    if not task_id:
        return {"decision": "deny_missing_design", "status": "missing"}
    try:
        package = get_design_package(project_root, task_id)
    except DesignPackageNotFound:
        return {
            "decision": "deny_missing_design",
            "status": "missing",
            "task_id": task_id,
        }
    approval = package.get("approval")
    if not isinstance(approval, dict):
        return {
            "decision": "deny_pending_approval",
            "status": "pending_approval",
            "task_id": task_id,
            "current_digest": package["digest"],
        }
    if approval.get("status") != "approved":
        return {
            "decision": "deny_pending_approval",
            "status": approval.get("status", "pending_approval"),
            "task_id": task_id,
            "current_digest": package["digest"],
            "approval_digest": approval.get("digest"),
        }
    if approval.get("digest") != package["digest"]:
        return {
            "decision": "deny_invalidated_approval",
            "status": "invalidated",
            "task_id": task_id,
            "current_digest": package["digest"],
            "approval_digest": approval.get("digest"),
        }
    return {
        "decision": "allow_approved_frontend_scope",
        "status": "approved",
        "task_id": task_id,
        "current_digest": package["digest"],
        "approval_digest": approval["digest"],
        "allowed_file_patterns": approval.get("allowed_file_patterns", []),
    }
