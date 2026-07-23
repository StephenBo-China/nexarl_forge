#!/usr/bin/env python3
"""Managed Loop Engineering × Superpowers contract and rollout helpers."""

from __future__ import annotations

import copy
import datetime as _dt
import json
import os
import pathlib
import shutil
import tempfile
from typing import Any


SCHEMA_VERSION = 3
MANAGED_VERSION = 1
COMPLETION_COMMAND = "python3 scripts/validate_loop_methodology.py --phase completion"
VALIDATOR_RELATIVE_PATH = pathlib.Path("scripts/validate_loop_methodology.py")
VALIDATOR_TEMPLATE = (
    pathlib.Path(__file__).resolve().parents[1]
    / "templates"
    / "loop"
    / "validate_loop_methodology.py"
)
MANAGED_RULE_START = "<!-- vibe-loop-superpowers:start -->"
MANAGED_RULE_END = "<!-- vibe-loop-superpowers:end -->"
MANAGED_VALIDATOR_MARKER = "MANAGED_BY_VIBE_LOOP_SUPERPOWERS ="

EXPECTED_SKILLS = frozenset(
    {
        "brainstorming",
        "dispatching-parallel-agents",
        "executing-plans",
        "finishing-a-development-branch",
        "receiving-code-review",
        "requesting-code-review",
        "subagent-driven-development",
        "systematic-debugging",
        "test-driven-development",
        "using-git-worktrees",
        "using-superpowers",
        "verification-before-completion",
        "writing-plans",
        "writing-skills",
    }
)

ARTIFACTS = {
    "design": "loop/prd/current_prd.md",
    "acceptance": "loop/acceptance/criteria.md",
    "plan": "loop/plans/current_plan.md",
    "internal_review": "loop/reports/internal_review_latest.json",
    "verification": "loop/reports/verification_latest.json",
    "external_evaluation": "loop/reports/claude_eval_latest.json",
}

ROUTING = {
    "intake": ["using-superpowers"],
    "new_feature_or_behavior_change": [
        "brainstorming",
        "writing-plans",
        "using-git-worktrees",
    ],
    "implementation_inline": [
        "executing-plans",
        "test-driven-development",
        "requesting-code-review",
        "receiving-code-review",
    ],
    "implementation_subagent": [
        "subagent-driven-development",
        "test-driven-development",
        "requesting-code-review",
        "receiving-code-review",
    ],
    "parallel_independent_work": ["dispatching-parallel-agents"],
    "bug_or_test_failure": [
        "systematic-debugging",
        "test-driven-development",
        "verification-before-completion",
    ],
    "completion": [
        "verification-before-completion",
        "finishing-a-development-branch",
    ],
    "skill_authoring": ["writing-skills"],
}


def methodology_defaults() -> dict[str, Any]:
    """Return a fresh default method contract for one project."""
    return {
        "provider": "superpowers",
        "superpowers": {
            "enabled": True,
            "plugin": {
                "selector": "superpowers@openai-api-curated",
                "methodology_version": "5.1.3",
            },
            "artifacts": copy.deepcopy(ARTIFACTS),
            "authority": {
                "orchestrator": "loop",
                "worktree": "loop_worktree_flow_only",
                "staging": "loop_single_active_branch",
                "branch_finish": "loop_release_workflow_only",
                "production": "loop_user_approval_only",
            },
            "declared_skills": sorted(EXPECTED_SKILLS),
            "routing": copy.deepcopy(ROUTING),
            "evaluator": {
                "role": "independent_claude_evaluator",
                "plugin": {"selector": "superpowers@claude-plugins-official"},
                "allowed_skills": [
                    "receiving-code-review",
                    "systematic-debugging",
                    "verification-before-completion",
                ],
                "may_modify_product_source": False,
            },
            "subagents": {
                "default_enabled": False,
                "requires_explicit_user_authorization": True,
                "parallel_requires_independent_worktrees": True,
            },
        },
    }


def atomic_write_text(target: pathlib.Path, content: str, mode: int = 0o644) -> None:
    """Atomically replace a text file in its own directory."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: pathlib.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = pathlib.Path(handle.name)
        temporary_path.chmod(mode)
        os.replace(temporary_path, target)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def timestamped_backup(target: pathlib.Path) -> pathlib.Path:
    stamp = _dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    backup = target.with_name(f"{target.name}.bak.{stamp}")
    shutil.copy2(target, backup)
    return backup


def validator_text() -> str:
    return VALIDATOR_TEMPLATE.read_text(encoding="utf-8")


def install_validator(
    project_root: pathlib.Path, changes: list[dict[str, str]]
) -> dict[str, Any]:
    """Install or upgrade the managed validator without overwriting custom code."""
    target = project_root / VALIDATOR_RELATIVE_PATH
    expected = validator_text()
    if not target.exists():
        atomic_write_text(target, expected, mode=0o755)
        changes.append({"path": str(target), "status": "created"})
        return {"status": "managed", "path": str(target)}

    current = target.read_text(encoding="utf-8")
    if current == expected:
        changes.append({"path": str(target), "status": "existing"})
        return {"status": "managed", "path": str(target)}
    if MANAGED_VALIDATOR_MARKER not in current:
        changes.append({"path": str(target), "status": "conflict"})
        return {"status": "custom_conflict", "path": str(target)}

    backup = timestamped_backup(target)
    atomic_write_text(target, expected, mode=0o755)
    changes.extend(
        [
            {"path": str(backup), "status": "backup"},
            {"path": str(target), "status": "upgraded"},
        ]
    )
    return {"status": "managed", "path": str(target)}


def read_loop_config_strict(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Loop config does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid loop config JSON: {path}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Invalid loop config object: {path}")
    return value


def append_unique_command(config: dict[str, Any]) -> None:
    worktree = config.setdefault("worktree", {})
    if not isinstance(worktree, dict):
        raise ValueError("Invalid loop config: worktree must be an object")
    commands = worktree.setdefault("finish_validation_commands", [])
    if not isinstance(commands, list) or not all(
        isinstance(command, str) for command in commands
    ):
        raise ValueError(
            "Invalid loop config: worktree.finish_validation_commands must be a list of strings"
        )
    if COMPLETION_COMMAND not in commands:
        commands.append(COMPLETION_COMMAND)


def validator_status(project_root: pathlib.Path) -> str:
    target = project_root / VALIDATOR_RELATIVE_PATH
    if not target.exists():
        return "missing"
    current = target.read_text(encoding="utf-8")
    if current == validator_text():
        return "managed"
    if MANAGED_VALIDATOR_MARKER in current:
        return "managed_upgrade_available"
    return "custom_conflict"


def inspect_config(config: dict[str, Any], current_validator_status: str) -> dict[str, Any]:
    methodology = config.get("methodology", {})
    if not isinstance(methodology, dict):
        methodology = {}
    method = methodology.get("superpowers", {})
    if not isinstance(method, dict):
        method = {}
    authority = method.get("authority", {})
    if not isinstance(authority, dict):
        authority = {}
    declared = method.get("declared_skills", [])
    contract_ok = (
        int(config.get("schema_version", 0) or 0) >= SCHEMA_VERSION
        and methodology.get("provider") == "superpowers"
        and method.get("enabled") is True
        and isinstance(declared, list)
        and set(declared) == EXPECTED_SKILLS
        and authority.get("orchestrator") == "loop"
        and authority.get("worktree") == "loop_worktree_flow_only"
    )
    worktree = config.get("worktree", {})
    commands = worktree.get("finish_validation_commands", []) if isinstance(worktree, dict) else []
    gate_ok = COMPLETION_COMMAND in commands and current_validator_status == "managed"
    return {
        "contract_ok": contract_ok,
        "completion_gate": "configured" if gate_ok else "needs_attention",
        "validator_status": current_validator_status,
    }


def added_config_paths(current: Any, upgraded: Any, prefix: str = "") -> list[str]:
    if not isinstance(current, dict) or not isinstance(upgraded, dict):
        return []
    paths: list[str] = []
    for key, value in upgraded.items():
        path = f"{prefix}.{key}" if prefix else key
        if key not in current:
            paths.append(path)
        elif isinstance(current[key], dict) and isinstance(value, dict):
            paths.extend(added_config_paths(current[key], value, path))
    return paths
