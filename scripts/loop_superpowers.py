#!/usr/bin/env python3
"""Managed Loop Engineering × Superpowers contract and rollout helpers."""

from __future__ import annotations

import copy
import pathlib
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
