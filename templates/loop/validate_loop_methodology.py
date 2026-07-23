#!/usr/bin/env python3
"""Validate the repository's Loop + Superpowers workflow contract."""

from __future__ import annotations

MANAGED_BY_VIBE_LOOP_SUPERPOWERS = 1

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any


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

PHASE_ARTIFACTS = {
    "design": ("design", "acceptance"),
    "implementation": ("design", "acceptance", "plan"),
    "completion": (
        "design",
        "acceptance",
        "plan",
        "internal_review",
        "verification",
        "external_evaluation",
    ),
}

REPORT_LABELS = {
    "internal_review": "internal review",
    "verification": "verification",
    "external_evaluation": "external evaluation",
}

EXPECTED_ARTIFACTS = {
    "design": "loop/prd/current_prd.md",
    "acceptance": "loop/acceptance/criteria.md",
    "plan": "loop/plans/current_plan.md",
    "internal_review": "loop/reports/internal_review_latest.json",
    "verification": "loop/reports/verification_latest.json",
    "external_evaluation": "loop/reports/claude_eval_latest.json",
}

EXPECTED_ROUTING = {
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

CHECKLIST_MARKER = r"(?:[-+*]|\d+[.)])"
CHECKLIST_ITEM = rf"^\s*{CHECKLIST_MARKER}\s+\[[ xX]\](?:\s+|$)"
UNCHECKED_ITEM = rf"^\s*{CHECKLIST_MARKER}\s+\[ \](?:\s+|$)"


class ValidationResult:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.checked_artifacts = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "passed" if self.ok else "failed",
            "checked_artifacts": self.checked_artifacts,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def _read_json(path: Path, result: ValidationResult, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        result.errors.append(f"missing {label}: {path}")
    except json.JSONDecodeError as exc:
        result.errors.append(f"invalid {label} JSON at {path}: {exc}")
    return None


def _artifact_path(root: Path, relative: Any, result: ValidationResult, key: str) -> Path | None:
    if not isinstance(relative, str) or not relative.strip():
        result.errors.append(f"artifact path for {key} must be a non-empty string")
        return None
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        result.errors.append(f"artifact path for {key} escapes project root: {relative}")
        return None
    return candidate


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _object_section(
    parent: dict[str, Any], key: str, result: ValidationResult, *, label: str | None = None
) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        result.errors.append(f"{label or key} must be an object")
        return {}
    return value


def _validate_contract(superpowers: Any, result: ValidationResult) -> dict[str, Any]:
    if not isinstance(superpowers, dict):
        result.errors.append("missing methodology.superpowers configuration")
        return {}
    if superpowers.get("enabled") is not True:
        result.errors.append("methodology.superpowers.enabled must be true")

    plugin = _object_section(superpowers, "plugin", result)
    if plugin.get("selector") != "superpowers@openai-api-curated":
        result.errors.append("Superpowers plugin selector must use the curated marketplace")

    declared = superpowers.get("declared_skills", [])
    if not isinstance(declared, list) or not all(
        isinstance(item, str) for item in declared
    ):
        result.errors.append("declared_skills must be a list of strings")
        declared_set = (
            {item for item in declared if isinstance(item, str)}
            if isinstance(declared, list)
            else set()
        )
    else:
        declared_set = set(declared)
    missing = sorted(EXPECTED_SKILLS - declared_set)
    unexpected = sorted(declared_set - EXPECTED_SKILLS)
    if missing:
        result.errors.append(f"missing declared Superpowers skills: {', '.join(missing)}")
    if unexpected:
        result.errors.append(f"unexpected declared Superpowers skills: {', '.join(unexpected)}")

    authority = _object_section(superpowers, "authority", result)
    expected_authority = {
        "orchestrator": "loop",
        "worktree": "loop_worktree_flow_only",
        "branch_finish": "loop_release_workflow_only",
        "production": "loop_user_approval_only",
        "staging": "loop_single_active_branch",
    }
    for key, expected in expected_authority.items():
        if authority.get(key) != expected:
            result.errors.append(f"Superpowers authority.{key} must be {expected}")

    subagents = _object_section(superpowers, "subagents", result)
    if subagents.get("requires_explicit_user_authorization") is not True:
        result.errors.append("subagents must require explicit user authorization")
    if subagents.get("default_enabled") is not False:
        result.errors.append("subagents must be disabled by default")
    if subagents.get("parallel_requires_independent_worktrees") is not True:
        result.errors.append("parallel agents must require independent worktrees")

    evaluator = _object_section(superpowers, "evaluator", result)
    if evaluator.get("may_modify_product_source") is not False:
        result.errors.append("independent evaluator must not modify product source")
    evaluator_plugin = _object_section(
        evaluator, "plugin", result, label="evaluator.plugin"
    )
    if evaluator_plugin.get("selector") != "superpowers@claude-plugins-official":
        result.errors.append("independent evaluator must use the Claude official Superpowers plugin")

    routing = _object_section(superpowers, "routing", result)
    for key, expected in EXPECTED_ROUTING.items():
        if routing.get(key) != expected:
            result.errors.append(f"Superpowers routing.{key} must be {expected}")
    artifacts = _object_section(superpowers, "artifacts", result)
    for key, expected in EXPECTED_ARTIFACTS.items():
        if artifacts.get(key) != expected:
            result.errors.append(f"Superpowers artifacts.{key} must be {expected}")
    return artifacts


def _validate_loop_guardrails(
    root: Path,
    config: dict[str, Any],
    artifacts: dict[str, Any],
    result: ValidationResult,
) -> None:
    expected_true = {
        "production_guardrails": (
            "ask_before_merge_master",
            "ask_before_production_deploy",
            "never_deploy_production_during_loop",
        ),
        "worktree": (
            "enabled",
            "loop_requires_dedicated_worktree",
            "one_task_one_conversation_one_worktree_one_branch",
            "primary_loop_conversation_owns_product_source_edits",
            "avoid_multiple_conversations_mutating_same_loop_branch",
            "staging_single_active_loop_branch_by_default",
        ),
    }
    for section, keys in expected_true.items():
        values = _object_section(config, section, result)
        for key in keys:
            if values.get(key) is not True:
                result.errors.append(f"{section}.{key} must be true")

    verification = _object_section(config, "verification", result)
    if verification.get("commands") != ["pytest -q -p no:cacheprovider tests"]:
        result.errors.append("verification.commands must run the release regression suite")
    worktree = _object_section(config, "worktree", result)
    if worktree.get("finish_validation_commands") != [
        "python3 scripts/validate_loop_methodology.py --phase completion"
    ]:
        result.errors.append(
            "worktree.finish_validation_commands must run the completion gate"
        )

    operative_artifacts = (
        ("prd", "save_latest_to", "design"),
        ("prd", "acceptance_criteria_path", "acceptance"),
        ("claude_eval", "report_json", "external_evaluation"),
    )
    sections: dict[str, dict[str, Any]] = {}
    for section, key, artifact in operative_artifacts:
        if section not in sections:
            sections[section] = _object_section(config, section, result)
        values = sections[section]
        operative_path = _artifact_path(root, values.get(key), result, f"{section}.{key}")
        methodology_path = _artifact_path(
            root,
            artifacts.get(artifact),
            result,
            f"methodology.superpowers.artifacts.{artifact}",
        )
        if (
            operative_path is not None
            and methodology_path is not None
            and operative_path != methodology_path
        ):
            result.errors.append(
                f"{section}.{key} must match "
                f"methodology.superpowers.artifacts.{artifact}"
            )


def _without_fenced_code(text: str) -> str:
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.lstrip(" \t")
        marker = re.match(r"(`{3,}|~{3,})", stripped)
        if not marker:
            output.append(line)
            index += 1
            continue
        fence_character = marker.group(1)[0]
        minimum_length = len(marker.group(1))
        closing_index = None
        for candidate_index in range(index + 1, len(lines)):
            candidate = lines[candidate_index].lstrip(" \t")
            if re.fullmatch(
                rf"{re.escape(fence_character)}{{{minimum_length},}}[ \t]*(?:\r?\n)?",
                candidate,
            ):
                closing_index = candidate_index
                break
        if closing_index is None:
            output.append(line)
            index += 1
            continue
        for fenced_line in lines[index : closing_index + 1]:
            output.append("\n" if fenced_line.endswith("\n") else "")
        index = closing_index + 1
    return "".join(output)


def _validate_markdown(path: Path, key: str, phase: str, result: ValidationResult) -> None:
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        result.errors.append(f"{key} artifact is empty: {path}")
        return
    semantic_text = _without_fenced_code(text)
    if re.search(r"\b(?:TBD|TODO)\b", semantic_text, flags=re.IGNORECASE):
        result.errors.append(f"{key} artifact contains an unresolved placeholder: {path}")
    if phase == "completion" and key in {"acceptance", "plan"}:
        if not re.search(CHECKLIST_ITEM, semantic_text, flags=re.MULTILINE):
            result.errors.append(f"{key} artifact must contain checklist items: {path}")
        if re.search(UNCHECKED_ITEM, semantic_text, flags=re.MULTILINE):
            result.errors.append(f"{key} artifact contains an unchecked checklist item: {path}")


def _validate_report(
    root: Path,
    path: Path,
    key: str,
    branch: str,
    result: ValidationResult,
) -> str | None:
    value = _read_json(path, result, f"{REPORT_LABELS[key]} report")
    if not isinstance(value, dict):
        return None
    if value.get("status") != "passed":
        result.errors.append(f"{REPORT_LABELS[key]} report status must be passed: {path}")
    if value.get("branch") != branch:
        result.errors.append(
            f"{REPORT_LABELS[key]} report branch must match {branch}: {path}"
        )
    tested_commit = value.get("tested_commit")
    if not isinstance(tested_commit, str) or not tested_commit.strip():
        result.errors.append(f"{REPORT_LABELS[key]} report must name tested_commit: {path}")
        return None
    if not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", tested_commit):
        result.errors.append(
            f"{REPORT_LABELS[key]} tested_commit must be an immutable full commit ID: {path}"
        )
        return None
    ancestor = _git(root, "merge-base", "--is-ancestor", tested_commit, "HEAD")
    if ancestor.returncode != 0:
        result.errors.append(
            f"{REPORT_LABELS[key]} tested_commit must be an ancestor of HEAD: {path}"
        )
        return None
    return tested_commit


def _validate_report_freshness(
    root: Path,
    tested_commits: list[str],
    artifacts: dict[str, Any],
    additional_report_paths: set[str],
    result: ValidationResult,
) -> None:
    if len(tested_commits) != len(REPORT_LABELS):
        return
    unique_commits = set(tested_commits)
    if len(unique_commits) != 1:
        result.errors.append("completion reports must validate the same tested_commit")
        return
    tested_commit = tested_commits[0]
    allowed_exact = {
        str(artifacts[key])
        for key in REPORT_LABELS
        if isinstance(artifacts.get(key), str)
    }
    allowed_exact.update(additional_report_paths)
    changed = set(filter(None, _git(root, "diff", "--name-only", tested_commit, "--").stdout.splitlines()))
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all").stdout
    for line in status.splitlines():
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        changed.add(path)
    disallowed = sorted(
        path
        for path in changed
        if path not in allowed_exact and not path.startswith("loop/claude_tests/")
    )
    if disallowed:
        result.errors.append(
            "files changed after tested_commit require fresh reports: " + ", ".join(disallowed)
        )


def validate_project(project_root: str | Path, *, phase: str) -> ValidationResult:
    if phase not in PHASE_ARTIFACTS:
        raise ValueError(f"unsupported phase: {phase}")
    root = Path(project_root).expanduser().resolve()
    result = ValidationResult()
    config = _read_json(root / ".loop" / "config.json", result, "Loop config")
    if not isinstance(config, dict):
        return result
    methodology = _object_section(config, "methodology", result)
    if methodology.get("provider") != "superpowers":
        result.errors.append("methodology.provider must be superpowers")
    artifacts = _validate_contract(methodology.get("superpowers"), result)
    _validate_loop_guardrails(root, config, artifacts, result)

    branch = ""
    tested_commits: list[str] = []
    additional_report_paths: set[str] = set()
    if phase == "completion":
        branch_result = _git(root, "branch", "--show-current")
        branch = branch_result.stdout.strip()
        if branch_result.returncode != 0 or not branch:
            result.errors.append("completion validation requires a named Git branch")
        project_name = config.get("project_repo_name")
        branch_prefix = (
            f"loop/{project_name.replace('_', '-')}-"
            if isinstance(project_name, str) and project_name
            else ""
        )
        branch_pattern = rf"^{re.escape(branch_prefix)}\d{{8}}-[a-z0-9]+(?:-[a-z0-9]+)*$"
        if not branch_prefix or not re.fullmatch(branch_pattern, branch):
            result.errors.append(
                "completion validation requires a dedicated Loop branch matching "
                f"{branch_prefix}<YYYYMMDD>-<slug>"
            )
        claude_eval = _object_section(config, "claude_eval", result)
        markdown_report = _artifact_path(
            root,
            claude_eval.get("report_markdown"),
            result,
            "claude_eval.report_markdown",
        )
        if markdown_report is not None:
            additional_report_paths.add(markdown_report.relative_to(root).as_posix())

    for key in PHASE_ARTIFACTS[phase]:
        path = _artifact_path(root, artifacts.get(key), result, key)
        if path is None:
            continue
        if not path.is_file():
            result.errors.append(f"missing {key} artifact: {path}")
            continue
        result.checked_artifacts += 1
        if key in REPORT_LABELS:
            tested_commit = _validate_report(root, path, key, branch, result)
            if tested_commit:
                tested_commits.append(tested_commit)
        else:
            _validate_markdown(path, key, phase, result)
    if phase == "completion":
        _validate_report_freshness(
            root,
            tested_commits,
            artifacts,
            additional_report_paths,
            result,
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--phase", choices=tuple(PHASE_ARTIFACTS), required=True)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    result = validate_project(args.project_root, phase=args.phase)
    if args.as_json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    else:
        state = "PASS" if result.ok else "FAIL"
        print(f"Loop + Superpowers {args.phase} validation: {state}")
        print(f"Checked artifacts: {result.checked_artifacts}")
        for warning in result.warnings:
            print(f"WARNING: {warning}")
        for error in result.errors:
            print(f"ERROR: {error}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
