# Loop × Superpowers Universal Rollout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make new Loop projects initialize with the complete Superpowers method contract and completion gate, while existing projects upgrade explicitly without losing project-specific configuration or custom instructions.

**Architecture:** Add a focused `loop_superpowers.py` module for the managed contract, status inspection, migration preview, validator installation, and rule-block ownership. Keep `memory_project.py` as the project/CLI orchestrator and `memory_review_server.py` as the local API/UI adapter. Distribute the already-audited, dependency-free project validator as a versioned template and let the existing immutable-feature `finish` flow execute its managed completion command.

**Tech Stack:** Python 3.10+ standard library, `unittest`, embedded HTML/CSS/JavaScript, Git.

---

## File map

- Create `scripts/loop_superpowers.py`: contract constants, schema defaults, inspection, migration preview/apply, atomic backups, managed validator and managed instruction blocks.
- Create `templates/loop/validate_loop_methodology.py`: versioned, dependency-free validator installed into Loop projects.
- Modify `scripts/memory_project.py`: delegate Superpowers defaults and upgrades, generate conditional agent/hook context, expose CLI operations.
- Modify `scripts/memory_review_server.py`: expose preview/apply endpoints, show project readiness states, and update Loop documentation.
- Modify `tests/test_loop_superpowers_rollout.py`: focused unit tests for the new module and generated hook/rule behavior.
- Modify `tests/test_worktree_flow.py`: update schema expectations and prove generated completion validation is executed by `finish`.
- Modify `tests/test_memory_review.py`: verify managed rule/hook backup, preservation, and conditional context.
- Modify `README.md`: document latest initialization, explicit upgrade, preview, and hook/rule upgrade commands.

### Task 1: Define the managed contract and project readiness model

**Files:**
- Create: `scripts/loop_superpowers.py`
- Create: `tests/test_loop_superpowers_rollout.py`
- Modify: `scripts/memory_project.py`
- Modify: `tests/test_worktree_flow.py`

- [ ] **Step 1: Write failing contract tests**

Add tests that require schema 3, the exact 14 skills, Loop authority, artifact paths, evaluator restrictions, explicit subagent authorization, and the managed finish command:

```python
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import loop_superpowers
import memory_project


class LoopSuperpowersRolloutTest(unittest.TestCase):
    def test_new_loop_contract_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            project = pathlib.Path(value) / "sample"
            project.mkdir()
            config = memory_project.loop_config(project, 8123)

        method = config["methodology"]["superpowers"]
        self.assertEqual(config["schema_version"], 3)
        self.assertEqual(config["methodology"]["provider"], "superpowers")
        self.assertTrue(method["enabled"])
        self.assertEqual(set(method["declared_skills"]), loop_superpowers.EXPECTED_SKILLS)
        self.assertEqual(method["authority"]["orchestrator"], "loop")
        self.assertEqual(method["authority"]["worktree"], "loop_worktree_flow_only")
        self.assertFalse(method["evaluator"]["may_modify_product_source"])
        self.assertFalse(method["subagents"]["default_enabled"])
        self.assertTrue(method["subagents"]["requires_explicit_user_authorization"])
        self.assertEqual(
            config["worktree"]["finish_validation_commands"],
            [loop_superpowers.COMPLETION_COMMAND],
        )
```

Update `test_generated_loop_config_has_safe_multi_conversation_defaults` to expect schema 3 and the same completion command instead of an empty list.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
python3 -m unittest -v tests.test_loop_superpowers_rollout tests.test_worktree_flow.WorktreeFlowTest.test_generated_loop_config_has_safe_multi_conversation_defaults
```

Expected: FAIL because `loop_superpowers` does not exist and the current generator returns schema 2 with an empty finish command.

- [ ] **Step 3: Implement the exact contract constants**

Create `scripts/loop_superpowers.py` with these public constants and factory boundary:

```python
from __future__ import annotations

import copy
import hashlib
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
VALIDATOR_TEMPLATE = pathlib.Path(__file__).resolve().parents[1] / "templates" / "loop" / "validate_loop_methodology.py"
MANAGED_RULE_START = "<!-- vibe-loop-superpowers:start -->"
MANAGED_RULE_END = "<!-- vibe-loop-superpowers:end -->"

EXPECTED_SKILLS = frozenset({
    "brainstorming", "dispatching-parallel-agents", "executing-plans",
    "finishing-a-development-branch", "receiving-code-review",
    "requesting-code-review", "subagent-driven-development",
    "systematic-debugging", "test-driven-development", "using-git-worktrees",
    "using-superpowers", "verification-before-completion", "writing-plans",
    "writing-skills",
})

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
    "new_feature_or_behavior_change": ["brainstorming", "writing-plans", "using-git-worktrees"],
    "implementation_inline": ["executing-plans", "test-driven-development", "requesting-code-review", "receiving-code-review"],
    "implementation_subagent": ["subagent-driven-development", "test-driven-development", "requesting-code-review", "receiving-code-review"],
    "parallel_independent_work": ["dispatching-parallel-agents"],
    "bug_or_test_failure": ["systematic-debugging", "test-driven-development", "verification-before-completion"],
    "completion": ["verification-before-completion", "finishing-a-development-branch"],
    "skill_authoring": ["writing-skills"],
}


def methodology_defaults() -> dict[str, Any]:
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
                "allowed_skills": ["receiving-code-review", "systematic-debugging", "verification-before-completion"],
                "may_modify_product_source": False,
            },
            "subagents": {
                "default_enabled": False,
                "requires_explicit_user_authorization": True,
                "parallel_requires_independent_worktrees": True,
            },
        },
    }
```

Modify `memory_project.loop_config()` to set `schema_version` to `SCHEMA_VERSION`, use `[COMPLETION_COMMAND]`, and add `"methodology": methodology_defaults()` without changing existing lifecycle/resource defaults.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the Step 2 command.

Expected: PASS for both contract tests.

- [ ] **Step 5: Commit the contract**

```bash
git add scripts/loop_superpowers.py scripts/memory_project.py tests/test_loop_superpowers_rollout.py tests/test_worktree_flow.py
git commit -m "feat: define default Loop Superpowers contract"
```

### Task 2: Distribute the audited validator and make initialization complete

**Files:**
- Create: `templates/loop/validate_loop_methodology.py`
- Modify: `scripts/loop_superpowers.py`
- Modify: `scripts/memory_project.py`
- Modify: `tests/test_loop_superpowers_rollout.py`

- [ ] **Step 1: Write failing installation and idempotency tests**

Add:

```python
def test_init_loop_installs_managed_validator_and_is_idempotent(self) -> None:
    with tempfile.TemporaryDirectory() as value:
        project = pathlib.Path(value) / "sample"
        project.mkdir()
        (project / ".git").mkdir()
        first = memory_project.init_loop(project, 8123)
        validator = project / "scripts" / "validate_loop_methodology.py"
        first_text = validator.read_text(encoding="utf-8")
        second = memory_project.init_loop(project, 8123)

        self.assertIn("Validate the repository's Loop + Superpowers workflow contract", first_text)
        self.assertIn(loop_superpowers.MANAGED_VALIDATOR_MARKER, first_text)
        self.assertEqual(first_text, validator.read_text(encoding="utf-8"))
        self.assertTrue(any(item["status"] == "created" and item["path"] == str(validator) for item in first["changes"]))
        self.assertTrue(any(item["status"] == "existing" and item["path"] == str(validator) for item in second["changes"]))
```

- [ ] **Step 2: Run the test and verify RED**

```bash
python3 -m unittest -v tests.test_loop_superpowers_rollout.LoopSuperpowersRolloutTest.test_init_loop_installs_managed_validator_and_is_idempotent
```

Expected: FAIL because initialization does not install the validator.

- [ ] **Step 3: Add the versioned validator template**

Create `templates/loop/validate_loop_methodology.py` from the complete audited validator at project commit `973f7ba9a175ec379f2008e3c5cceba852fecb56`, path `scripts/validate_loop_methodology.py`. Preserve all 506 lines and add this ownership line immediately after the module docstring:

```python
MANAGED_BY_VIBE_LOOP_SUPERPOWERS = 1
```

This source is the already tested validator that enforces exact skills/routing, Loop authority, contained operative artifact paths, branch/checklist completion, fenced examples, immutable full commit IDs, shared tested commit freshness, and configured Markdown evaluation exemptions.

- [ ] **Step 4: Implement safe installation**

Add:

```python
MANAGED_VALIDATOR_MARKER = "MANAGED_BY_VIBE_LOOP_SUPERPOWERS ="


def validator_text() -> str:
    return VALIDATOR_TEMPLATE.read_text(encoding="utf-8")


def install_validator(project_root: pathlib.Path, changes: list[dict[str, str]]) -> dict[str, Any]:
    target = project_root / VALIDATOR_RELATIVE_PATH
    expected = validator_text()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text(expected, encoding="utf-8")
        target.chmod(0o755)
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
    changes.extend([
        {"path": str(backup), "status": "backup"},
        {"path": str(target), "status": "upgraded"},
    ])
    return {"status": "managed", "path": str(target)}
```

Implement `timestamped_backup()` with `shutil.copy2` and `atomic_write_text()` with `tempfile.NamedTemporaryFile(dir=target.parent, delete=False)`, `flush`, `os.fsync`, `chmod`, then `os.replace`; unlink the temporary path on error.

Call `install_validator()` from `init_loop()` after directories exist. Return `methodology_status` in the result.

- [ ] **Step 5: Run focused and full current tests**

```bash
python3 -m unittest -v tests.test_loop_superpowers_rollout
python3 -m unittest -v tests.test_memory_review tests.test_worktree_flow
```

Expected: all tests PASS.

- [ ] **Step 6: Commit validator distribution**

```bash
git add templates/loop/validate_loop_methodology.py scripts/loop_superpowers.py scripts/memory_project.py tests/test_loop_superpowers_rollout.py
git commit -m "feat: install managed Loop methodology validator"
```

### Task 3: Implement explicit preview and lossless Loop upgrades

**Files:**
- Modify: `scripts/loop_superpowers.py`
- Modify: `scripts/memory_project.py`
- Modify: `tests/test_loop_superpowers_rollout.py`
- Modify: `tests/test_worktree_flow.py`

- [ ] **Step 1: Write failing migration tests**

Cover preserved project resources/unknown fields, backup creation, preview without writes, invalid JSON, custom validator conflict, managed validator replacement, completion-command deduplication, and second-run idempotency:

```python
def test_preview_and_upgrade_preserve_custom_values_and_are_idempotent(self) -> None:
    with tempfile.TemporaryDirectory() as value:
        project = pathlib.Path(value) / "sample"
        project.mkdir()
        (project / ".git").mkdir()
        config_path = project / ".loop" / "config.json"
        config_path.parent.mkdir()
        original = {
            "schema_version": 2,
            "project_repo_name": "sample",
            "repository": {"canonical_root": "/old", "main_branch": "main", "remote": "upstream"},
            "worktree": {"finish_validation_commands": ["make preflight"]},
            "staging": {"port": 9191, "database": "offline", "oss_bucket": "owned", "remote_path": "/srv/sample"},
            "verification": {"commands": ["make test"]},
            "custom_extension": {"keep": True},
        }
        config_path.write_text(json.dumps(original), encoding="utf-8")

        preview = memory_project.preview_loop_upgrade(project, 8123)
        self.assertEqual(json.loads(config_path.read_text(encoding="utf-8")), original)
        self.assertIn("methodology", preview["added_paths"])

        first = memory_project.upgrade_loop(project, 8123)
        upgraded = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(upgraded["staging"], original["staging"])
        self.assertEqual(upgraded["verification"]["commands"], ["make test"])
        self.assertEqual(upgraded["custom_extension"], {"keep": True})
        self.assertEqual(upgraded["repository"]["canonical_root"], str(project.resolve()))
        self.assertEqual(upgraded["worktree"]["finish_validation_commands"], ["make preflight", loop_superpowers.COMPLETION_COMMAND])
        self.assertEqual(len(list(config_path.parent.glob("config.json.bak.*"))), 1)

        second = memory_project.upgrade_loop(project, 8123)
        self.assertEqual(second["config_status"], "existing")
        self.assertEqual(len(list(config_path.parent.glob("config.json.bak.*"))), 1)
```

- [ ] **Step 2: Run migration tests and verify RED**

```bash
python3 -m unittest -v tests.test_loop_superpowers_rollout
```

Expected: FAIL because preview/apply APIs and backup semantics do not exist.

- [ ] **Step 3: Implement strict reads, preview, and apply**

Add these boundaries:

```python
def read_loop_config_strict(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid loop config JSON: {path}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Invalid loop config object: {path}")
    return value


def append_unique_command(config: dict[str, Any]) -> None:
    worktree = config.setdefault("worktree", {})
    commands = worktree.setdefault("finish_validation_commands", [])
    if COMPLETION_COMMAND not in commands:
        commands.append(COMPLETION_COMMAND)


def inspect_config(config: dict[str, Any], validator_status: str) -> dict[str, Any]:
    method = config.get("methodology", {}).get("superpowers", {})
    contract_ok = (
        config.get("schema_version", 0) >= SCHEMA_VERSION
        and config.get("methodology", {}).get("provider") == "superpowers"
        and method.get("enabled") is True
        and set(method.get("declared_skills", [])) == EXPECTED_SKILLS
        and method.get("authority", {}).get("orchestrator") == "loop"
    )
    gate_ok = COMPLETION_COMMAND in config.get("worktree", {}).get("finish_validation_commands", []) and validator_status == "managed"
    return {"contract_ok": contract_ok, "completion_gate": "configured" if gate_ok else "needs_attention"}
```

Use the existing recursive merge behavior to preserve all current values, force only `schema_version = 3` and the canonical root, then append the one managed completion command. `preview_loop_upgrade()` must compute `added_paths`, `preserved_categories`, validator action, and readiness from in-memory values only. `upgrade_loop()` must install/upgrade the validator, create a config backup only when bytes will change, and atomically replace the JSON.

Change CLI `upgrade-loop` to call `upgrade_loop()` rather than `init_loop()`. Add `preview-loop-upgrade` with the same root/port arguments.

- [ ] **Step 4: Run tests and verify GREEN**

```bash
python3 -m unittest -v tests.test_loop_superpowers_rollout tests.test_worktree_flow
```

Expected: all tests PASS.

- [ ] **Step 5: Commit explicit migration**

```bash
git add scripts/loop_superpowers.py scripts/memory_project.py tests/test_loop_superpowers_rollout.py tests/test_worktree_flow.py
git commit -m "feat: add explicit lossless Loop upgrades"
```

### Task 4: Upgrade managed rules and enrich existing hooks without adding hooks

**Files:**
- Modify: `scripts/loop_superpowers.py`
- Modify: `scripts/memory_project.py`
- Modify: `tests/test_memory_review.py`
- Modify: `tests/test_loop_superpowers_rollout.py`

- [ ] **Step 1: Write failing managed-rule and hook tests**

Add assertions that pure memory projects receive conditional text, enabled projects receive a safe method summary, invalid JSON produces a warning, secrets are absent, managed blocks update in place, user text is preserved, and backups are created only on change:

```python
def test_hook_context_is_conditional_and_safe(self) -> None:
    hook = memory_project.hook_script(pathlib.Path("/tmp/project"), "codex")
    self.assertIn("def loop_context()", hook)
    self.assertIn("Loop × Superpowers", hook)
    self.assertIn("requires explicit user authorization", hook)
    self.assertNotIn("oss_access_key", hook)
    self.assertNotIn("database_password", hook)


def test_upgrade_managed_rules_preserves_user_text(self) -> None:
    with tempfile.TemporaryDirectory() as value:
        project = pathlib.Path(value)
        agents = project / "AGENTS.md"
        agents.write_text("# User rules\n\nKeep this exact text.\n", encoding="utf-8")
        first = memory_project.upgrade_memory_rules(project)
        updated = agents.read_text(encoding="utf-8")
        second = memory_project.upgrade_memory_rules(project)
        self.assertIn("Keep this exact text.", updated)
        self.assertIn(loop_superpowers.MANAGED_RULE_START, updated)
        self.assertEqual(updated, agents.read_text(encoding="utf-8"))
        self.assertTrue(any(item["status"] == "backup" for item in first["changes"]))
        self.assertFalse(any(item["status"] == "backup" for item in second["changes"]))
```

- [ ] **Step 2: Run tests and verify RED**

```bash
python3 -m unittest -v tests.test_memory_review tests.test_loop_superpowers_rollout
```

Expected: FAIL because conditional method context and managed rule upgrades do not exist.

- [ ] **Step 3: Add the exact managed instruction block**

Expose `managed_rule_block()` returning a marker-wrapped block with these rules:

```markdown
<!-- vibe-loop-superpowers:start -->
## Loop Engineering With Superpowers

When `.loop/config.json` enables `methodology.superpowers`, read the project
Loop configuration, both personal Loop directories, and the configured workflow
document before substantial development.

- Loop is the lifecycle authority for worktrees, branches, staging, evaluation,
  release, main merge, production resources, and deployment.
- Use Superpowers for brainstorming, written plans, TDD, systematic debugging,
  code review, and verification before completion.
- Use only artifact paths declared in `.loop/config.json`.
- Run configured finish validation before claiming the Loop feature is ready.
- Subagents and parallel agents require explicit user authorization and Loop-safe
  isolated worktrees.
<!-- vibe-loop-superpowers:end -->
```

Implement `replace_managed_block()` so one well-formed block is replaced, no block is appended once, and unmatched start/end markers raise a safe conflict without rewriting. `upgrade_memory_rules()` handles AGENTS.md, CLAUDE.md and `.claude/rules/shared-memory.md`, backing up each changed existing file before atomic write.

- [ ] **Step 4: Enhance the existing hook generator**

Inside generated hook code, add `loop_context()` that:

1. Returns an empty string when `.loop/config.json` is absent.
2. Parses JSON and returns a short warning on parse/type failure.
3. Checks only `methodology.provider`, `methodology.superpowers.enabled`, subagent authorization, and `worktree.finish_validation_commands`.
4. Returns the lifecycle/method/validation reminder without copying database, OSS, token, password, or other configuration values.

Keep the current hook events and filenames unchanged. Add `upgrade-rules` and `upgrade-memory` CLI commands; `upgrade-memory` sequentially calls the rules and hook upgrades and returns both change lists.

- [ ] **Step 5: Run tests and verify GREEN**

```bash
python3 -m unittest -v tests.test_memory_review tests.test_loop_superpowers_rollout
```

Expected: all tests PASS.

- [ ] **Step 6: Commit rule and hook upgrades**

```bash
git add scripts/loop_superpowers.py scripts/memory_project.py tests/test_memory_review.py tests/test_loop_superpowers_rollout.py
git commit -m "feat: add managed Loop Superpowers context upgrades"
```

### Task 5: Add project readiness, preview, and explicit upgrade APIs

**Files:**
- Modify: `scripts/memory_project.py`
- Modify: `scripts/memory_review_server.py`
- Modify: `tests/test_loop_superpowers_rollout.py`

- [ ] **Step 1: Write failing status and API-boundary tests**

Extract POST dispatch helpers where needed so tests do not start a real server. Test these exact states:

```python
def test_project_entry_reports_loop_readiness(self) -> None:
    with tempfile.TemporaryDirectory() as value:
        project = pathlib.Path(value) / "sample"
        project.mkdir()
        (project / ".git").mkdir()
        empty = memory_project.project_entry(project)
        self.assertEqual(empty["loop_status"], "not_initialized")

        memory_project.init_loop(project, 8123)
        ready = memory_project.project_entry(project)
        self.assertEqual(ready["loop_status"], "superpowers_ready")
        self.assertEqual(ready["completion_gate"], "configured")
        self.assertIn(ready["plugin_status"], {"installed", "partial", "missing"})
```

Also test that preview leaves file mtimes/content unchanged, apply requires `confirmed is True`, a non-Git root is rejected, and invalid JSON returns an error without a backup or write.

- [ ] **Step 2: Run tests and verify RED**

```bash
python3 -m unittest -v tests.test_loop_superpowers_rollout
```

Expected: FAIL because readiness fields and confirmed upgrade endpoints do not exist.

- [ ] **Step 3: Implement project status**

Extend `project_entry()` with:

```python
{
    "memory_status": "not_initialized" | "initialized" | "upgrade_available",
    "loop_status": "not_initialized" | "legacy" | "superpowers_incomplete" | "superpowers_ready" | "invalid",
    "completion_gate": "not_applicable" | "needs_attention" | "configured",
    "managed_rules_status": "missing" | "upgrade_available" | "current" | "conflict",
    "managed_hooks_status": "missing" | "upgrade_available" | "current",
    "plugin_status": "installed" | "partial" | "missing",
}
```

Derive these values by read-only inspection; never initialize or upgrade while listing projects. Detect Codex by the presence of `~/.codex/plugins/cache/openai-api-curated/superpowers/*/skills/using-superpowers/SKILL.md`; detect Claude Code from the `superpowers@claude-plugins-official` entry in `~/.claude/plugins/installed_plugins.json`. Report `installed` only when both are present, `partial` when one is present, and `missing` when neither is present. A malformed Claude registry counts as unavailable and must not break project listing.

- [ ] **Step 4: Add local API routes**

Add:

- `POST /api/projects/preview-loop-upgrade` → calls `preview_loop_upgrade` and performs no writes.
- `POST /api/projects/upgrade-loop` → rejects unless JSON body has `confirmed: true`, then calls `upgrade_loop`.
- `POST /api/projects/upgrade-memory` → rejects unless `confirmed: true`, then calls the combined managed rules/hooks upgrade.

Keep `/api/projects/init-loop` for only new/not-initialized projects; return HTTP 409 with an instruction to preview/upgrade when a config already exists. Normalize roots and require a Git repository before mutation.

- [ ] **Step 5: Run tests and verify GREEN**

```bash
python3 -m unittest -v tests.test_loop_superpowers_rollout tests.test_memory_review
```

Expected: all tests PASS.

- [ ] **Step 6: Commit API and status support**

```bash
git add scripts/memory_project.py scripts/memory_review_server.py tests/test_loop_superpowers_rollout.py
git commit -m "feat: expose Loop readiness and upgrade APIs"
```

### Task 6: Update the memory review console project manager and Loop documentation

**Files:**
- Modify: `scripts/memory_review_server.py`
- Modify: `tests/test_loop_superpowers_rollout.py`

- [ ] **Step 1: Write failing rendered-page tests**

Add tests against `memory_review_server.page()` requiring:

```python
html = memory_review_server.page()
self.assertIn("初始化 Loop × Superpowers", html)
self.assertIn("预览升级 Loop", html)
self.assertIn("升级记忆规则/钩子", html)
self.assertIn("Superpowers 是阶段内工程方法", html)
self.assertIn("Loop 是唯一生命周期编排器", html)
self.assertIn("子代理和并行代理必须获得用户明确授权", html)
self.assertIn("preview-loop-upgrade", html)
self.assertIn("confirmed: true", html)
```

- [ ] **Step 2: Run the page test and verify RED**

```bash
python3 -m unittest -v tests.test_loop_superpowers_rollout
```

Expected: FAIL because the current UI has one combined “初始化 / 升级 Loop” action and old documentation.

- [ ] **Step 3: Implement state-driven actions**

Replace the combined toolbar action with:

- `初始化记忆`
- `初始化 Loop × Superpowers`
- `预览升级 Loop`
- `升级记忆规则/钩子`

Render per-project tags from the status fields rather than `has_loop` alone. The preview action first calls `/api/projects/preview-loop-upgrade`, renders added paths, preserved categories, backup actions and conflicts, and only then offers a confirmation that sends `confirmed: true` to `/api/projects/upgrade-loop`.

Use existing `esc()` for every path, status and server-returned string. Do not render configuration values, database names, OSS buckets, remote paths, or command contents in preview.

- [ ] **Step 4: Replace Loop documentation with the approved model**

Update `renderLoopDocs()` and project initialization help to cover:

1. Loop lifecycle authority versus Superpowers method responsibility.
2. New-project `init` then `init-loop` flow.
3. Old-project preview then explicit `upgrade-loop` flow.
4. Existing managed hook/rule upgrade without a new hook.
5. Brainstorming → approved design → written plan → Loop worktree → TDD → internal review → Claude evaluation → completion verification.
6. Validator/report artifacts and immutable tested commit identity.
7. Subagent authorization and isolated worktree restriction.
8. No main merge or production deployment without user approval.

- [ ] **Step 5: Run rendered-page and full tests**

```bash
python3 -m unittest -v tests.test_loop_superpowers_rollout
python3 -m unittest -v tests.test_memory_review tests.test_worktree_flow
```

Expected: all tests PASS.

- [ ] **Step 6: Commit the console experience**

```bash
git add scripts/memory_review_server.py tests/test_loop_superpowers_rollout.py
git commit -m "feat: update Loop Superpowers project management UI"
```

### Task 7: Document commands and verify an end-to-end temporary project

**Files:**
- Modify: `README.md`
- Modify: `docs/worktree_loop_workflow.md`
- Modify: `tests/test_loop_superpowers_rollout.py`

- [ ] **Step 1: Add a failing documentation assertion**

Require README and workflow documentation to contain all supported commands and boundaries:

```python
def test_documentation_lists_latest_initialization_and_upgrade_commands(self) -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    workflow = (ROOT / "docs" / "worktree_loop_workflow.md").read_text(encoding="utf-8")
    for command in ("init-loop", "preview-loop-upgrade", "upgrade-loop", "upgrade-memory"):
        self.assertIn(command, readme)
    self.assertIn("Superpowers", workflow)
    self.assertIn("Loop remains", workflow)
```

- [ ] **Step 2: Run the documentation test and verify RED**

```bash
python3 -m unittest -v tests.test_loop_superpowers_rollout.LoopSuperpowersRolloutTest.test_documentation_lists_latest_initialization_and_upgrade_commands
```

Expected: FAIL because preview and combined managed-memory upgrade commands are undocumented.

- [ ] **Step 3: Update README and workflow documentation**

Document these exact commands:

```bash
python3 scripts/memory_project.py init /path/to/repo
python3 scripts/memory_project.py init-loop /path/to/repo --port 8082
python3 scripts/memory_project.py preview-loop-upgrade /path/to/repo
python3 scripts/memory_project.py upgrade-loop /path/to/repo
python3 scripts/memory_project.py upgrade-memory /path/to/repo
```

State that initialization does not install plugins or use external models; old projects upgrade explicitly; backups are created only for changed managed files; project resources remain authoritative; Loop controls release/production; and completion validation is installed automatically.

- [ ] **Step 4: Run an isolated CLI smoke test**

Use `mktemp -d`, initialize a Git repository inside that exact temporary directory, then run the five commands above against it. Verify:

```bash
test -f "$TEMP_PROJECT/.loop/config.json"
test -f "$TEMP_PROJECT/scripts/validate_loop_methodology.py"
python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); assert c["schema_version"] == 3; assert c["methodology"]["superpowers"]["enabled"] is True' "$TEMP_PROJECT/.loop/config.json"
```

Expected: initialization succeeds; preview produces JSON without changing the config hash; both upgrades are idempotent; schema is 3; validator exists.

- [ ] **Step 5: Run complete verification**

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/memory_project.py scripts/loop_superpowers.py scripts/memory_review_server.py scripts/worktree_flow.py templates/loop/validate_loop_methodology.py
git diff --check
git status --short
```

Expected: all tests PASS; syntax checks and `git diff --check` succeed; status contains only intended tracked changes.

- [ ] **Step 6: Review the complete diff**

Review for configuration loss, unsafe path acceptance, non-atomic writes, secret rendering, duplicate commands/blocks, accidental hook additions, UI escaping errors, and any main/production behavior. Fix every issue through a failing regression test before changing implementation.

- [ ] **Step 7: Commit documentation and final regression changes**

```bash
git add README.md docs/worktree_loop_workflow.md tests/test_loop_superpowers_rollout.py
git commit -m "docs: document universal Loop Superpowers rollout"
```

- [ ] **Step 8: Push only the feature branch**

```bash
git push origin codex/superpowers-finish-validation
```

Expected: normal non-force push succeeds. Do not merge `master`, upgrade registered user projects, deploy staging, or access production.
