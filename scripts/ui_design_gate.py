"""Digest-bound design packages and project UI approval decisions."""

from __future__ import annotations

import copy
import datetime as dt
import fnmatch
import hashlib
import json
import os
import pathlib
import re
import shutil
import shlex
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
    if (
        path.is_absolute()
        or re.match(r"^[A-Za-z]:/", value.replace("\\", "/"))
        or ".." in path.parts
        or not path.parts
    ):
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
    try:
        config = _config(project_root)
    except GateValidationError:
        config = {}
    if config.get("gate_mode") == "project_global":
        return _project_global_gate_status(project_root, config)
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


PATCH_PATH = re.compile(
    r"^\*\*\* (?:Add|Update|Delete) File:\s*(.+?)\s*$", re.MULTILINE
)
UNIFIED_PATCH_PATH = re.compile(r"^\+\+\+\s+(?:b/)?(.+?)\s*$", re.MULTILINE)
DIRECT_PATH_KEYS = {
    "file_path",
    "filePath",
    "path",
    "paths",
    "target",
    "target_path",
    "destination",
    "destination_path",
    "filename",
}
READ_ONLY_SHELL_COMMANDS = {
    "cat",
    "cut",
    "env",
    "find",
    "git",
    "grep",
    "head",
    "ls",
    "pwd",
    "rg",
    "sed",
    "sort",
    "tail",
    "wc",
    "which",
}
MUTATING_SHELL_COMMANDS = {
    "cp",
    "install",
    "mkdir",
    "mv",
    "rm",
    "rmdir",
    "touch",
    "truncate",
}


def _shell_tokens(tool_input: dict[str, Any]) -> list[str]:
    command = tool_input.get("command", tool_input.get("cmd", ""))
    if not isinstance(command, str) or not command.strip():
        return []
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return []


def _shell_command_name(tokens: list[str]) -> str:
    if not tokens:
        return ""
    index = 0
    if tokens[0] == "env":
        index = 1
        while index < len(tokens) and "=" in tokens[index] and not tokens[index].startswith("-"):
            index += 1
    if index >= len(tokens):
        return ""
    return pathlib.PurePosixPath(tokens[index]).name


def _shell_paths(tokens: list[str]) -> list[str]:
    command = _shell_command_name(tokens)
    if command not in MUTATING_SHELL_COMMANDS:
        return []
    command_index = next(
        (index for index, token in enumerate(tokens) if pathlib.PurePosixPath(token).name == command),
        0,
    )
    arguments = [token for token in tokens[command_index + 1 :] if not token.startswith("-")]
    if command in {"cp", "install", "mv"}:
        return arguments[-1:] if arguments else []
    return arguments


def _collect_direct_paths(value: Any, *, key: str = "") -> list[str]:
    if isinstance(value, dict):
        result: list[str] = []
        for child_key, child in value.items():
            if child_key in DIRECT_PATH_KEYS:
                result.extend(_collect_direct_paths(child, key=child_key))
        return result
    if isinstance(value, list):
        result = []
        for child in value:
            result.extend(_collect_direct_paths(child, key=key))
        return result
    if isinstance(value, str) and key in DIRECT_PATH_KEYS:
        return [value]
    return []


def extract_candidate_paths(tool_name: str, tool_input: dict[str, Any]) -> list[str]:
    lowered = tool_name.lower()
    paths: list[str] = []
    if lowered == "apply_patch" or "patch" in tool_input:
        patch = tool_input.get("patch", tool_input.get("input", ""))
        if isinstance(patch, str):
            paths.extend(PATCH_PATH.findall(patch))
            paths.extend(
                path for path in UNIFIED_PATCH_PATH.findall(patch) if path != "/dev/null"
            )
    if lowered in {"bash", "exec_command"}:
        paths.extend(_shell_paths(_shell_tokens(tool_input)))
    else:
        paths.extend(_collect_direct_paths(tool_input))
    return list(dict.fromkeys(path.strip() for path in paths if path.strip()))


def classify_mutation(tool_name: str, tool_input: dict[str, Any]) -> str:
    lowered = tool_name.lower()
    if lowered in {"edit", "write", "apply_patch"}:
        return "mutation"
    if lowered.startswith("mcp__filesystem__"):
        operation = lowered.rsplit("__", 1)[-1]
        if operation.startswith(("read", "list", "get", "stat", "search")):
            return "read_only"
        return "mutation"
    if lowered not in {"bash", "exec_command"}:
        return "read_only"
    raw_command = tool_input.get("command", tool_input.get("cmd", ""))
    if isinstance(raw_command, str) and any(
        operator in raw_command for operator in ("&&", "||", ";", "|", ">", "<")
    ):
        return "unresolved_mutation"
    tokens = _shell_tokens(tool_input)
    command = _shell_command_name(tokens)
    if not command:
        return "unresolved_mutation"
    if command in MUTATING_SHELL_COMMANDS:
        return "mutation"
    if command == "git":
        subcommand = next((token for token in tokens[1:] if not token.startswith("-")), "")
        return (
            "read_only"
            if subcommand in {"status", "diff", "log", "show", "rev-parse", "branch"}
            else "unresolved_mutation"
        )
    if command == "find" and "-delete" in tokens:
        return "unresolved_mutation"
    if command == "sed" and any(token.startswith("-i") for token in tokens[1:]):
        return "unresolved_mutation"
    if command in READ_ONLY_SHELL_COMMANDS:
        return "read_only"
    return "unresolved_mutation"


def _normalize_candidate(project_root: pathlib.Path, value: str) -> str:
    root = pathlib.Path(project_root).expanduser().resolve()
    raw = pathlib.Path(value).expanduser()
    candidate = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
    if not candidate.is_relative_to(root):
        raise GateValidationError(f"mutation path escapes project root: {value}")
    return candidate.relative_to(root).as_posix()


def _matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _safe_patterns(value: Any, *, label: str) -> list[str]:
    patterns = _string_list(value, label=label)
    for pattern in patterns:
        _safe_relative(pattern, label=label)
    return patterns


def _load_config_for_decision(
    project_root: pathlib.Path,
) -> tuple[dict[str, Any], str | None]:
    path = _ui_root(project_root) / "config.json"
    try:
        value = store.read_json_strict(path)
    except (OSError, json.JSONDecodeError) as error:
        return {}, f"invalid UI gate config {path}: {error}"
    if not isinstance(value, dict):
        return {}, f"invalid UI gate config object: {path}"
    try:
        if value.get("schema_version") != 1:
            raise GateValidationError("schema_version must be 1")
        if value.get("gate_mode") not in {"design_package", "project_global"}:
            raise GateValidationError("unsupported gate_mode")
        for key in (
            "formal_frontend_paths",
            "design_artifact_paths",
            "generated_paths",
            "test_artifact_paths",
        ):
            _safe_patterns(value.get(key, []), label=key)
    except GateValidationError as error:
        return value, f"invalid UI gate config {path}: {error}"
    return value, None


def _looks_frontend(path: str) -> bool:
    parts = set(pathlib.PurePosixPath(path).parts)
    suffix = pathlib.PurePosixPath(path).suffix.lower()
    return bool(
        parts & {"web", "frontend", "app", "pages", "components", "mini-program", "miniprogram"}
        and suffix
        in {".css", ".html", ".js", ".jsx", ".less", ".scss", ".swift", ".tsx", ".ts", ".vue", ".wxml", ".wxss"}
    )


def _project_global_gate_status(
    project_root: pathlib.Path, config: dict[str, Any]
) -> dict[str, Any]:
    if config.get("relocked", True):
        return {
            "decision": "deny_pending_approval",
            "status": "relocked",
            "reason": "Project-global UI approval is relocked.",
        }
    task_id = config.get("project_global_baseline_task")
    approvals = _load_approvals(project_root)
    approval = approvals.get("project_global_approval")
    if not isinstance(task_id, str) or not isinstance(approval, dict):
        return {
            "decision": "deny_pending_approval",
            "status": "pending_approval",
            "reason": "Project-global UI baseline approval is required.",
        }
    if (
        approval.get("status") != "approved"
        or approval.get("task_id") != task_id
        or approval.get("gate_mode") != "project_global"
    ):
        return {
            "decision": "deny_pending_approval",
            "status": approval.get("status", "pending_approval"),
            "reason": "Project-global UI baseline approval is not active.",
        }
    try:
        package = get_design_package(project_root, task_id)
    except (DesignPackageNotFound, GateValidationError):
        return {
            "decision": "deny_invalidated_approval",
            "status": "invalidated",
            "reason": "The approved project UI baseline is missing or invalid.",
        }
    if package["digest"] != approval.get("digest"):
        return {
            "decision": "deny_invalidated_approval",
            "status": "invalidated",
            "current_digest": package["digest"],
            "approval_digest": approval.get("digest"),
            "reason": "The approved project UI baseline has changed.",
        }
    return {
        "decision": "allow_approved_frontend_scope",
        "status": "approved",
        "current_digest": package["digest"],
        "approval_digest": approval["digest"],
    }


def _design_package_path_decision(
    project_root: pathlib.Path, formal_paths: list[str]
) -> dict[str, Any]:
    approvals = _load_approvals(project_root).get("package_approvals", {})
    approved = [
        item
        for item in approvals.values()
        if isinstance(item, dict)
        and item.get("status") == "approved"
        and item.get("gate_mode") == "design_package"
    ]
    if not approved:
        return {
            "decision": "deny_pending_approval",
            "status": "pending_approval",
            "reason": "Approve a design package before modifying formal frontend code.",
        }
    for path in formal_paths:
        matching = [
            item
            for item in approved
            if _matches(path, item.get("allowed_file_patterns", []))
        ]
        if not matching:
            return {
                "decision": "deny_scope_mismatch",
                "status": "approved",
                "path": path,
                "reason": "The frontend path is outside every approved design package.",
            }
        valid = False
        invalidated: dict[str, Any] | None = None
        for item in matching:
            try:
                package = get_design_package(project_root, item["task_id"])
            except (DesignPackageNotFound, GateValidationError):
                invalidated = item
                continue
            if package["digest"] == item.get("digest"):
                valid = True
                break
            invalidated = {
                **item,
                "current_digest": package["digest"],
            }
        if not valid:
            return {
                "decision": "deny_invalidated_approval",
                "status": "invalidated",
                "path": path,
                "current_digest": (invalidated or {}).get("current_digest"),
                "approval_digest": (invalidated or {}).get("digest"),
                "reason": "The design package changed after approval.",
            }
    return {
        "decision": "allow_approved_frontend_scope",
        "status": "approved",
        "paths": formal_paths,
    }


def decide_tool_use(
    project_root: pathlib.Path, tool_name: str, tool_input: dict[str, Any]
) -> dict[str, Any]:
    mutation = classify_mutation(tool_name, tool_input)
    if mutation == "read_only":
        return {"decision": "allow_non_visual", "reason": "Read-only operation."}
    config, config_error = _load_config_for_decision(project_root)
    if config.get("enabled") is False or config.get("hard_gate_enabled") is False:
        return {"decision": "allow_non_visual", "reason": "UI hard gate is disabled."}

    raw_paths = extract_candidate_paths(tool_name, tool_input)
    if mutation == "unresolved_mutation" and not raw_paths:
        if config.get("gate_mode") == "project_global" and config_error is None:
            global_status = _project_global_gate_status(project_root, config)
            if global_status["decision"] == "allow_approved_frontend_scope":
                return global_status
        return {
            "decision": "deny_invalid_configuration"
            if config_error
            else "deny_pending_approval",
            "reason": (
                "Mutating shell command paths cannot be resolved while the UI gate is locked; "
                "use apply_patch or a file-specific write tool."
            ),
        }
    try:
        paths = [_normalize_candidate(project_root, path) for path in raw_paths]
    except GateValidationError as error:
        return {"decision": "deny_invalid_configuration", "reason": str(error)}
    if not paths:
        return {"decision": "allow_non_visual", "reason": "No project mutation path."}

    formal_patterns = config.get("formal_frontend_paths", [])
    formal = [
        path
        for path in paths
        if _matches(path, formal_patterns) or (config_error and _looks_frontend(path))
    ]
    if not formal:
        design_patterns = config.get("design_artifact_paths", [])
        if paths and all(_matches(path, design_patterns) for path in paths):
            return {
                "decision": "allow_design_artifact",
                "paths": paths,
                "reason": "The mutation is inside configured design-artifact paths.",
            }
        return {
            "decision": "allow_non_visual",
            "paths": paths,
            "reason": "The mutation does not touch formal frontend paths.",
        }
    if config_error:
        return {
            "decision": "deny_invalid_configuration",
            "paths": formal,
            "reason": config_error,
        }
    if config.get("gate_mode") == "project_global":
        return _project_global_gate_status(project_root, config)
    return _design_package_path_decision(project_root, formal)


def approve_project_baseline(
    project_root: pathlib.Path,
    task_id: str,
    *,
    expected_digest: str,
    idempotency_key: str,
) -> dict[str, Any]:
    config = _config(project_root)
    if config.get("gate_mode") != "project_global":
        raise GateValidationError("project baseline approval requires project_global mode")
    if config.get("project_global_baseline_task") != task_id:
        raise GateValidationError("task does not match project_global_baseline_task")

    def operation(approvals: dict[str, Any]) -> dict[str, Any]:
        package = get_design_package(project_root, task_id)
        if package["digest"] != expected_digest:
            raise DigestConflict(
                f"design package digest changed: expected {expected_digest}, current {package['digest']}"
            )
        record = {
            "task_id": task_id,
            "status": "approved",
            "digest": package["digest"],
            "gate_mode": "project_global",
            "at": _now(),
        }
        approvals["project_global_approval"] = record
        current_config = _config(project_root)
        current_config["relocked"] = False
        store.atomic_write_json(_ui_root(project_root) / "config.json", current_config)
        return copy.deepcopy(record)

    result, changed = _mutate(
        project_root,
        idempotency_key=idempotency_key,
        operation="project-baseline.approve",
        payload={"task_id": task_id, "expected_digest": expected_digest},
        callback=operation,
    )
    if changed:
        _audit(project_root, "project_baseline_approved", result)
        _refresh_context(project_root)
    return result
