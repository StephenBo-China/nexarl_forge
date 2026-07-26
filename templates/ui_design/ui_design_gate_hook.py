#!/usr/bin/env python3
"""Managed, dependency-free PreToolUse gate for visible-interface mutations."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import pathlib
import re
import shlex
import sys
from typing import Any


MANAGED_UI_DESIGN_GATE_HOOK_VERSION = 1
ROOT = pathlib.Path(__file__).resolve().parents[2]
UI_ROOT = ROOT / "codex" / "ui_design"
PATCH_PATH = re.compile(
    r"^\*\*\* (?:Add|Update|Delete) File:\s*(.+?)\s*$", re.MULTILINE
)
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
    "cat", "cut", "env", "find", "git", "grep", "head", "ls", "pwd",
    "rg", "sed", "sort", "tail", "wc", "which",
}
MUTATING_SHELL_COMMANDS = {
    "cp", "install", "mkdir", "mv", "rm", "rmdir", "touch", "truncate",
}


def read_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def shell_tokens(tool_input: dict[str, Any]) -> list[str]:
    command = tool_input.get("command", tool_input.get("cmd", ""))
    if not isinstance(command, str) or not command.strip():
        return []
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return []


def shell_command(tokens: list[str]) -> str:
    if not tokens:
        return ""
    index = 0
    if tokens[0] == "env":
        index = 1
        while index < len(tokens) and "=" in tokens[index]:
            index += 1
    return pathlib.PurePosixPath(tokens[index]).name if index < len(tokens) else ""


def shell_paths(tokens: list[str]) -> list[str]:
    command = shell_command(tokens)
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


def direct_paths(value: Any, key: str = "") -> list[str]:
    if isinstance(value, dict):
        result: list[str] = []
        for child_key, child in value.items():
            if child_key in DIRECT_PATH_KEYS:
                result.extend(direct_paths(child, child_key))
        return result
    if isinstance(value, list):
        result = []
        for child in value:
            result.extend(direct_paths(child, key))
        return result
    return [value] if isinstance(value, str) and key in DIRECT_PATH_KEYS else []


def extract_paths(tool_name: str, tool_input: dict[str, Any]) -> list[str]:
    lowered = tool_name.lower()
    paths: list[str] = []
    if lowered == "apply_patch" or "patch" in tool_input:
        patch = tool_input.get("patch", tool_input.get("input", ""))
        if isinstance(patch, str):
            paths.extend(PATCH_PATH.findall(patch))
    if lowered in {"bash", "exec_command"}:
        paths.extend(shell_paths(shell_tokens(tool_input)))
    else:
        paths.extend(direct_paths(tool_input))
    return list(dict.fromkeys(path.strip() for path in paths if path.strip()))


def classify(tool_name: str, tool_input: dict[str, Any]) -> str:
    lowered = tool_name.lower()
    if lowered in {"edit", "write", "apply_patch"}:
        return "mutation"
    if lowered.startswith("mcp__filesystem__"):
        operation = lowered.rsplit("__", 1)[-1]
        return (
            "read_only"
            if operation.startswith(("read", "list", "get", "stat", "search"))
            else "mutation"
        )
    if lowered not in {"bash", "exec_command"}:
        return "read_only"
    raw = tool_input.get("command", tool_input.get("cmd", ""))
    if isinstance(raw, str) and any(op in raw for op in ("&&", "||", ";", "|", ">", "<")):
        return "unresolved_mutation"
    tokens = shell_tokens(tool_input)
    command = shell_command(tokens)
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
    return "read_only" if command in READ_ONLY_SHELL_COMMANDS else "unresolved_mutation"


def normalize_path(value: str) -> str:
    raw = pathlib.Path(value).expanduser()
    candidate = raw.resolve() if raw.is_absolute() else (ROOT / raw).resolve()
    if not candidate.is_relative_to(ROOT):
        raise ValueError(f"mutation path escapes project root: {value}")
    return candidate.relative_to(ROOT).as_posix()


def matches(path: str, patterns: Any) -> bool:
    return isinstance(patterns, list) and any(
        isinstance(pattern, str) and fnmatch.fnmatchcase(path, pattern)
        for pattern in patterns
    )


def looks_frontend(path: str) -> bool:
    parts = set(pathlib.PurePosixPath(path).parts)
    suffix = pathlib.PurePosixPath(path).suffix.lower()
    return bool(
        parts & {"web", "frontend", "app", "pages", "components", "mini-program", "miniprogram"}
        and suffix in {".css", ".html", ".js", ".jsx", ".less", ".scss", ".swift", ".ts", ".tsx", ".vue", ".wxml", ".wxss"}
    )


def load_config() -> tuple[dict[str, Any], str | None]:
    path = UI_ROOT / "config.json"
    try:
        value = read_json(path)
    except (OSError, json.JSONDecodeError) as error:
        return {}, f"Invalid UI gate config {path}: {error}"
    if not isinstance(value, dict):
        return {}, f"Invalid UI gate config object: {path}"
    if value.get("schema_version") != 1 or value.get("gate_mode") not in {
        "design_package", "project_global",
    }:
        return value, f"Invalid UI gate configuration: {path}"
    for key in (
        "formal_frontend_paths", "design_artifact_paths", "generated_paths", "test_artifact_paths"
    ):
        patterns = value.get(key, [])
        if not isinstance(patterns, list) or any(not isinstance(item, str) for item in patterns):
            return value, f"Invalid UI gate path configuration: {path}"
    return value, None


def package_digest(task_id: str) -> str:
    package_root = UI_ROOT / "design-packages" / task_id
    manifest = read_json(package_root / "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("task_id") != task_id:
        raise ValueError("invalid design package manifest")
    declared_names = manifest.get("design_files")
    if not isinstance(declared_names, list):
        raise ValueError("invalid declared design files")
    declared = {package_root / pathlib.PurePosixPath(name) for name in declared_names}
    for path in package_root.rglob("*"):
        if path.is_symlink():
            raise ValueError("design package contains a symlink")
        if path.is_file() and path.name != "manifest.json" and path not in declared:
            raise ValueError("design package contains an undeclared file")
    digest = hashlib.sha256()
    normalized = {
        "schema_version": 1,
        "task_id": task_id,
        "title": str(manifest.get("title", "")).strip(),
        "classification": manifest.get("classification"),
        "pages": sorted(set(manifest.get("pages", []))),
        "components": sorted(set(manifest.get("components", []))),
        "allowed_file_patterns": sorted(set(manifest.get("allowed_file_patterns", []))),
        "design_files": sorted(set(declared_names)),
        "status": "pending_approval",
    }
    manifest_data = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    digest.update(len(manifest_data).to_bytes(8, "big"))
    digest.update(manifest_data)
    for path in sorted(declared):
        if not path.is_file() or path.is_symlink():
            raise ValueError("declared design file is missing")
        relative = path.relative_to(package_root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def approvals() -> dict[str, Any]:
    value = read_json(UI_ROOT / "approvals.json")
    if not isinstance(value, dict) or not isinstance(value.get("package_approvals"), dict):
        raise ValueError("invalid UI approvals")
    return value


def project_global_decision(config: dict[str, Any]) -> dict[str, str]:
    if config.get("relocked", True):
        return {"decision": "deny_pending_approval", "reason": "Approve and unlock the project UI baseline first."}
    task_id = config.get("project_global_baseline_task")
    approval = approvals().get("project_global_approval")
    if not isinstance(task_id, str) or not isinstance(approval, dict):
        return {"decision": "deny_pending_approval", "reason": "Approve the project UI baseline first."}
    if approval.get("status") != "approved" or approval.get("task_id") != task_id:
        return {"decision": "deny_pending_approval", "reason": "Approve the project UI baseline first."}
    try:
        current = package_digest(task_id)
    except (OSError, ValueError, json.JSONDecodeError):
        return {"decision": "deny_invalidated_approval", "reason": "Approved project UI baseline is invalid."}
    if current != approval.get("digest"):
        return {"decision": "deny_invalidated_approval", "reason": "Approved project UI baseline changed."}
    return {"decision": "allow_approved_frontend_scope", "reason": "Project UI baseline is approved."}


def package_decision(paths: list[str]) -> dict[str, str]:
    records = [
        item
        for item in approvals()["package_approvals"].values()
        if isinstance(item, dict)
        and item.get("status") == "approved"
        and item.get("gate_mode") == "design_package"
    ]
    if not records:
        return {"decision": "deny_pending_approval", "reason": "Approve a design package before modifying formal frontend code."}
    for path in paths:
        matching = [item for item in records if matches(path, item.get("allowed_file_patterns"))]
        if not matching:
            return {"decision": "deny_scope_mismatch", "reason": "Approve a design package that declares this frontend path."}
        valid = False
        for item in matching:
            try:
                valid = package_digest(item["task_id"]) == item.get("digest")
            except (OSError, ValueError, json.JSONDecodeError):
                valid = False
            if valid:
                break
        if not valid:
            return {"decision": "deny_invalidated_approval", "reason": "Approved design package changed; approve the revised design."}
    return {"decision": "allow_approved_frontend_scope", "reason": "Frontend path is inside an approved design package."}


def decide(tool_name: str, tool_input: dict[str, Any]) -> dict[str, str]:
    mutation = classify(tool_name, tool_input)
    if mutation == "read_only":
        return {"decision": "allow_non_visual", "reason": "Read-only operation."}
    config, error = load_config()
    if config.get("enabled") is False or config.get("hard_gate_enabled") is False:
        return {"decision": "allow_non_visual", "reason": "UI hard gate is disabled."}
    raw_paths = extract_paths(tool_name, tool_input)
    if mutation == "unresolved_mutation" and not raw_paths:
        if config.get("gate_mode") == "project_global" and error is None:
            global_result = project_global_decision(config)
            if global_result["decision"].startswith("allow_"):
                return global_result
        return {
            "decision": "deny_invalid_configuration" if error else "deny_pending_approval",
            "reason": "Approve the UI design first, then use apply_patch or a file-specific write tool.",
        }
    try:
        paths = [normalize_path(path) for path in raw_paths]
    except ValueError as exc:
        return {"decision": "deny_invalid_configuration", "reason": str(exc)}
    formal = [
        path
        for path in paths
        if matches(path, config.get("formal_frontend_paths")) or (error and looks_frontend(path))
    ]
    if not formal:
        if paths and all(matches(path, config.get("design_artifact_paths")) for path in paths):
            return {"decision": "allow_design_artifact", "reason": "Design-artifact path."}
        return {"decision": "allow_non_visual", "reason": "No formal frontend path."}
    if error:
        return {"decision": "deny_invalid_configuration", "reason": error}
    return (
        project_global_decision(config)
        if config.get("gate_mode") == "project_global"
        else package_decision(formal)
    )


def deny_output(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be an object")
        tool_name = payload.get("tool_name", payload.get("toolName", ""))
        tool_input = payload.get("tool_input", payload.get("toolInput", payload.get("input", {})))
        if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
            raise ValueError("hook payload is missing tool_name or tool_input")
        result = decide(tool_name, tool_input)
    except Exception as error:
        result = {
            "decision": "deny_invalid_configuration",
            "reason": f"UI design gate could not evaluate the operation: {error}",
        }
    if result["decision"].startswith("deny_"):
        print(json.dumps(deny_output(result["reason"]), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
