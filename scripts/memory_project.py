#!/usr/bin/env python3
"""Project registry and initialization helpers for the memory review console."""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import fcntl
import json
import os
import pathlib
import re
import shlex
import shutil
import stat
import subprocess
from typing import Any
import uuid

import loop_superpowers
import ui_design_preferences
import ui_design_store
import vibe_memory_install
import vibe_memory_paths


APP_ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNTIME_PATHS = vibe_memory_paths.for_home()
DEFAULT_WORKTREE_ROOT = pathlib.Path(
    os.environ.get("CODEX_WORKTREE_ROOT", str(RUNTIME_PATHS.worktree_root))
).expanduser()
REGISTRY_PATH = pathlib.Path(
    os.environ.get(
        "MEMORY_REVIEW_PROJECT_REGISTRY",
        str(RUNTIME_PATHS.project_registry),
    )
).expanduser()
CODEX_LOOP_DIR = RUNTIME_PATHS.personal_memory.parent / "loop_engineering"
CLAUDE_LOOP_DIR = RUNTIME_PATHS.personal_memory.parents[1] / ".claude" / "loop_engineering"
DEFAULT_STAGING_HOST = "root@8.210.155.175"
DEFAULT_BASE_PORT = 8081
UI_DESIGN_GATE_HOOK_TEMPLATE = (
    APP_ROOT / "templates" / "ui_design" / "ui_design_gate_hook.py"
)


def now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def repo_name(path: pathlib.Path) -> str:
    return path.resolve().name


def normalize_project_root(path: str | pathlib.Path) -> pathlib.Path:
    return pathlib.Path(path).expanduser().resolve()


def read_json(path: pathlib.Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _empty_registry() -> dict[str, Any]:
    return {"current_project": "", "projects": []}


@contextlib.contextmanager
def _registry_lock(exclusive: bool):
    parent = REGISTRY_PATH.absolute().parent
    vibe_memory_install._validate_install_ancestor_chain(parent)
    canonical_parent = vibe_memory_install._canonical_install_path(parent)
    if not exclusive and not canonical_parent.exists():
        yield None
        return
    if exclusive:
        parent_fd = vibe_memory_install._open_or_create_directory_chain(canonical_parent)
    else:
        parent_fd = os.open(
            canonical_parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    try:
        fcntl.flock(parent_fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield parent_fd
    finally:
        fcntl.flock(parent_fd, fcntl.LOCK_UN)
        os.close(parent_fd)


def _read_registry_at(parent_fd: int) -> dict[str, Any]:
    name = REGISTRY_PATH.name
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return _empty_registry()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("project registry must be a regular file")
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("project registry changed while opening")
        chunks = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    try:
        data = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("project registry is malformed") from error
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("current_project", ""), str)
        or not isinstance(data.get("projects", []), list)
    ):
        raise ValueError("project registry has an invalid structure")
    data.setdefault("current_project", "")
    data.setdefault("projects", [])
    return data


def _write_registry_at(parent_fd: int, value: dict[str, Any]) -> None:
    content = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary_name = f".{REGISTRY_PATH.name}.tmp-{uuid.uuid4().hex}"
    descriptor = os.open(
        temporary_name,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=parent_fd,
    )
    try:
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OSError("short project registry write")
            offset += written
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        try:
            active = os.stat(REGISTRY_PATH.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            active = None
        if active is not None and not stat.S_ISREG(active.st_mode):
            raise ValueError("project registry must be a regular file")
        os.replace(
            temporary_name,
            REGISTRY_PATH.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
    finally:
        try:
            os.unlink(temporary_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass


def registry() -> dict[str, Any]:
    with _registry_lock(exclusive=False) as parent_fd:
        if parent_fd is None:
            return _empty_registry()
        return _read_registry_at(parent_fd)


def _mutate_registry(mutator: Any) -> dict[str, Any]:
    with _registry_lock(exclusive=True) as parent_fd:
        data = _read_registry_at(parent_fd)
        mutator(data)
        _write_registry_at(parent_fd, data)
        return data


def ui_design_config(_root: pathlib.Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "enabled": True,
        "hard_gate_enabled": False,
        "gate_mode": "design_package",
        "relocked": True,
        "formal_frontend_paths": [],
        "design_artifact_paths": ["codex/ui_design/design-packages/**"],
        "generated_paths": [],
        "test_artifact_paths": [],
        "hook_smoke_test": {
            "codex": {"status": "not_run"},
            "claude": {"status": "not_run"},
        },
    }


def ui_design_status(root: pathlib.Path) -> str:
    ui_root = root / "codex" / "ui_design"
    config_path = ui_root / "config.json"
    if not config_path.exists():
        return "not_initialized"
    required = (
        ui_root / "preferences.json",
        ui_root / "active-skills.json",
        ui_root / "approvals.json",
    )
    if not all(path.exists() for path in required):
        return "configuration_required"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "configuration_required"
    if not isinstance(config, dict):
        return "configuration_required"
    if config.get("schema_version") != 1:
        return "configuration_required"
    if config.get("gate_mode") not in {"design_package", "project_global"}:
        return "configuration_required"
    if (
        config.get("enabled") is not True
        or config.get("hard_gate_enabled") is not True
    ):
        return "configuration_required"
    paths = config.get("formal_frontend_paths")
    if not isinstance(paths, list) or not paths:
        return "configuration_required"
    return "locked" if config.get("relocked", True) else "ready"


def _read_ui_json(path: pathlib.Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid UI design state: {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"invalid UI design state object: {path}")
    return value


def publish_effective_ui_context(root: str | pathlib.Path) -> dict[str, Any]:
    """Atomically publish the shared, project-effective UI design snapshot."""
    project_root = normalize_project_root(root)
    ui_root = project_root / "codex" / "ui_design"
    config = _read_ui_json(ui_root / "config.json", ui_design_config(project_root))
    active_skills = _read_ui_json(
        ui_root / "active-skills.json",
        {"schema_version": 1, "execution_order": [], "skills": []},
    )
    approvals = _read_ui_json(
        ui_root / "approvals.json",
        {
            "schema_version": 1,
            "package_approvals": {},
            "project_global_approval": None,
        },
    )
    global_preferences = ui_design_preferences.load_global_preferences()
    project_preferences = ui_design_preferences.load_project_overrides(project_root)
    effective_preferences = ui_design_preferences.merge_preferences(
        global_preferences, project_preferences
    )
    snapshot = {
        "schema_version": 1,
        "preferences": {
            "global": global_preferences,
            "project": {"schema_version": 1, "overrides": project_preferences},
            "effective": effective_preferences,
        },
        "active_skills": active_skills,
        "gate": {
            "status": ui_design_status(project_root),
            "enabled": config.get("enabled") is True,
            "hard_gate_enabled": config.get("hard_gate_enabled") is True,
            "mode": config.get("gate_mode", "design_package"),
            "relocked": config.get("relocked", True),
            "config": config,
            "approvals": approvals,
        },
    }
    target = ui_root / "effective-context.json"
    if read_json(target, None) != snapshot:
        ui_design_store.atomic_write_json(target, snapshot)
    return snapshot


def project_entry(root: pathlib.Path) -> dict[str, Any]:
    root = root.resolve()
    has_memory = (root / "codex" / "codex_long_memory.md").exists() and (
        root / "codex" / "codex_short_memory.md"
    ).exists() and (root / "codex" / "memory_proposals.md").exists()
    config_path = root / ".loop" / "config.json"
    loop_status = "not_initialized"
    completion_gate = "not_applicable"
    if config_path.exists():
        try:
            config = loop_superpowers.read_loop_config_strict(config_path)
            validator_state = loop_superpowers.validator_status(root)
            readiness = loop_superpowers.inspect_config(config, validator_state)
            completion_gate = readiness["completion_gate"]
            methodology = config.get("methodology", {})
            provider = methodology.get("provider") if isinstance(methodology, dict) else None
            if readiness["contract_ok"] and completion_gate == "configured":
                loop_status = "superpowers_ready"
            elif provider == "superpowers":
                loop_status = "superpowers_incomplete"
            else:
                loop_status = "legacy"
        except (OSError, TypeError, ValueError):
            loop_status = "invalid"
            completion_gate = "needs_attention"

    rule_states = [
        loop_superpowers.managed_rule_status(root / "AGENTS.md"),
        loop_superpowers.managed_rule_status(root / "CLAUDE.md"),
        loop_superpowers.managed_rule_status(
            root / ".claude" / "rules" / "shared-memory.md"
        ),
    ]
    if "conflict" in rule_states:
        managed_rules_status = "conflict"
    elif all(state == "current" for state in rule_states):
        managed_rules_status = "current"
    elif all(state == "missing" for state in rule_states):
        managed_rules_status = "missing"
    else:
        managed_rules_status = "upgrade_available"

    hook_targets = (
        (root / ".codex" / "hooks" / "shared_memory_hook.py", "codex"),
        (root / ".claude" / "hooks" / "shared_memory_hook.py", "claude"),
    )
    hook_states = []
    for path, source in hook_targets:
        if not path.exists():
            hook_states.append("missing")
        elif path.read_text(encoding="utf-8") == hook_script(root, source):
            hook_states.append("current")
        else:
            hook_states.append("upgrade_available")
    for path in (
        root / ".codex" / "hooks" / "ui_design_gate_hook.py",
        root / ".claude" / "hooks" / "ui_design_gate_hook.py",
    ):
        if not path.exists():
            hook_states.append("missing")
        elif path.read_text(encoding="utf-8") == ui_design_gate_hook_text():
            hook_states.append("current")
        else:
            hook_states.append("upgrade_available")
    managed_hooks_status = (
        "current"
        if all(state == "current" for state in hook_states)
        else "not_applicable"
        if all(state == "missing" for state in hook_states)
        else "upgrade_available"
    )
    memory_status = (
        "not_initialized"
        if not has_memory
        else "initialized"
        if managed_rules_status == "current"
        else "upgrade_available"
    )

    return {
        "name": repo_name(root),
        "root": str(root),
        "is_git_repo": (root / ".git").exists(),
        "has_memory": has_memory,
        "has_loop": config_path.exists(),
        "memory_status": memory_status,
        "loop_status": loop_status,
        "completion_gate": completion_gate,
        "managed_rules_status": managed_rules_status,
        "managed_hooks_status": managed_hooks_status,
        "ui_design_status": ui_design_status(root),
        "plugin_status": loop_superpowers.plugin_status(),
        "last_opened_at": now(),
    }


def register_project(root: str | pathlib.Path, make_current: bool = True) -> dict[str, Any]:
    project_root = normalize_project_root(root)
    if not project_root.is_dir():
        raise ValueError(f"project root must be an existing directory: {project_root}")
    entry = project_entry(project_root)
    def mutate(data: dict[str, Any]) -> None:
        projects = [p for p in data.get("projects", []) if p.get("root") != str(project_root)]
        projects.append(entry)
        projects.sort(key=lambda item: item.get("name", ""))
        data["projects"] = projects
        if make_current:
            data["current_project"] = str(project_root)
    return _mutate_registry(mutate)


def unregister_project(root: str | pathlib.Path) -> dict[str, Any]:
    project_path = normalize_project_root(root)
    project_root = str(project_path)
    registered = set()
    for item in registry().get("projects", []):
        raw_root = item.get("root") if isinstance(item, dict) else None
        if not isinstance(raw_root, str) or not raw_root:
            continue
        try:
            registered.add(str(normalize_project_root(raw_root)))
        except (OSError, RuntimeError, ValueError):
            continue
    if project_root not in registered:
        raise ValueError(f"project is not registered: {project_root}")

    import vibe_memory_migration

    cleanup = vibe_memory_migration.remove_managed_legacy_hooks(project_path)

    def mutate(data: dict[str, Any]) -> None:
        data["projects"] = [
            item for item in data.get("projects", []) if item.get("root") != project_root
        ]
        if data.get("current_project") == project_root:
            data["current_project"] = ""
    data = _mutate_registry(mutate)
    return {
        **data,
        "status": "unregistered",
        "project": project_root,
        "removed_legacy_hooks": cleanup["changed_paths"],
        "legacy_hook_backups": cleanup["backups"],
        "registry": data,
    }


def set_current_project(root: str | pathlib.Path) -> dict[str, Any]:
    return register_project(root, make_current=True)


def list_projects() -> dict[str, Any]:
    data = registry()
    refreshed = []
    for item in data.get("projects", []):
        root = item.get("root", "")
        if root:
            entry = project_entry(pathlib.Path(root))
            entry["last_opened_at"] = item.get("last_opened_at", "")
            refreshed.append(entry)
    return {**data, "projects": refreshed}


def current_project(default: pathlib.Path | None = None) -> pathlib.Path:
    data = registry()
    current = data.get("current_project") or ""
    if current:
        return normalize_project_root(current)
    if default is not None:
        return default.resolve()
    return APP_ROOT


def ensure_file(path: pathlib.Path, content: str, changes: list[dict[str, str]]) -> None:
    if path.exists():
        changes.append({"path": str(path), "status": "existing"})
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")
    changes.append({"path": str(path), "status": "created"})


def append_if_missing(path: pathlib.Path, marker: str, content: str, changes: list[dict[str, str]]) -> None:
    if not path.exists():
        ensure_file(path, content, changes)
        return
    text = path.read_text(encoding="utf-8")
    if marker in text:
        changes.append({"path": str(path), "status": "existing"})
        return
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n\n" + content.rstrip() + "\n")
    changes.append({"path": str(path), "status": "appended"})


def project_title(root: pathlib.Path) -> str:
    return repo_name(root).replace("_", " ")


def agent_candidate_protocol(root: pathlib.Path) -> str:
    stable_cli = RUNTIME_PATHS.install_root / "current/scripts/vibe_memory_cli.py"
    project_environment = shlex.quote(str(root))
    cli_command = shlex.quote(str(stable_cli))
    return f"""## Agent-Generated Memory Candidates

The active conversation model performs candidate summarization itself. Hooks
must not copy raw prompts into candidate files or call another model API.

Before the final response to a substantial instruction, and at compaction
boundaries, review the conversation for at most two durable candidate memories:

- Personal candidates: only cross-project development habits, collaboration
  preferences, work/thinking style, workflow preferences, or user-profile
  facts. Target `long` for stable preferences and `short` for genuinely
  temporary cross-project context.
- Project candidates: only durable architecture, deployment, product,
  technical-constraint, or project-workflow facts. Target `long` only.

Allowed personal categories: `development_habit`,
`collaboration_preference`, `work_style`, `thinking_style`, `user_profile`,
`workflow_preference`. Allowed project categories: `project_architecture`,
`deployment_rule`, `product_direction`, `technical_constraint`,
`project_workflow`.

Never use a raw prompt as candidate content. Write a short title plus a
standalone 1-3 sentence summary. Exclude one-off tasks, screenshots, URLs,
paths, system instructions, uncertain assumptions, credentials, tokens,
verification codes, passwords, and infrastructure secrets. Deduplicate against
pending and approved memory before writing.

Write a distilled candidate with:

```bash
MEMORY_REVIEW_PROJECT_ROOT={project_environment} python3 {cli_command} memory propose \\
  --scope personal|project --target long|short --category CATEGORY \\
  --title "TITLE" --summary "SUMMARY" --source-event agent_summary
```

Do not write directly to official long/short memory. Official promotion remains
approval-gated. If candidate creation fails, skip it and report the concrete
reason in the current Codex or Claude Code conversation.
"""


def agent_memory_block(root: pathlib.Path) -> str:
    name = repo_name(root)
    return f"""# {name} Codex Instructions

## Required Memory Context

For every new Codex thread, substantial user instruction, and compaction
boundary in this repository, load relevant memory from:

- `README.md`
- `codex/codex_long_memory.md`
- `codex/codex_context_packet.md`
- `codex/shared_memory_context_packet.md`
- `~/.codex/personal_memory/long.md`
- `~/.codex/personal_memory/short.md`

Project short memory is large by design. Read recent or relevant sections only:

- `codex/codex_short_memory.md`

If `.loop/config.json` exists, read it before PRD planning, loop branch work,
staging deployment, Claude evaluation, or master/production decisions. When the
user says `开 worktree`, create or use a dedicated git worktree before
substantial project work. For loop development, start in a dedicated worktree by
default before implementation begins. Treat the original repository as the
canonical workspace. Parallel conversations must use different external
worktrees and branches; main integration, canonical synchronization, and shared
staging deployment must be serialized with repository locks. After an approved
main merge, verify remote main, canonical main, and deployed commits match.

## Memory Governance

Project short memory may be appended by hooks. Project long memory should
receive only reviewed durable project facts. Personal long and short memory
require explicit approval of exact content; write personal candidates only to
`~/.codex/personal_memory/proposals.md`.

{agent_candidate_protocol(root)}

{loop_superpowers.managed_rule_block()}
"""


def claude_md(root: pathlib.Path) -> str:
    name = repo_name(root)
    return f"""# {name} Shared Memory Instructions

This repository shares memory between Codex and Claude Code through project
memory files under `codex/` and personal memory under `~/.codex/`.

@.claude/rules/shared-memory.md
@codex/codex_context_packet.md
@codex/shared_memory_context_packet.md
@codex/codex_long_memory.md

If `.loop/config.json` exists, also load:

@.loop/config.json

## Required Startup Context

- `README.md`
- `codex/codex_long_memory.md`
- `codex/codex_context_packet.md`
- `codex/shared_memory_context_packet.md`
- `{RUNTIME_PATHS.personal_memory / "long.md"}`
- `{RUNTIME_PATHS.personal_memory / "short.md"}`

Read project short memory selectively from `codex/codex_short_memory.md`; do
not load the entire file by default.

{agent_candidate_protocol(root)}

{loop_superpowers.managed_rule_block()}
"""


def shared_memory_rule(root: pathlib.Path) -> str:
    return f"""# Shared Codex/Claude Memory Rule

Codex and Claude Code should use the same project memory files in this
repository and the same approval-gated personal memory files.

## Read Policy

- Read project and personal long memory before broad implementation,
  deployment, product, architecture, or workflow decisions.
- Read project short memory selectively: recent entries, targeted searches, or
  status summaries.
- If `.loop/config.json` exists, read it before loop planning, staging
  evaluation, Playwright/browser testing, report writing, or pass/block
  decisions.
- When the user says `开 worktree`, create or use a dedicated git worktree
  before substantial project work. Loop implementation should start in a
  dedicated worktree by default.
- Keep task worktrees outside the canonical repository. One conversation owns
  one task worktree and branch. Main integration, canonical synchronization,
  and shared staging deployment are repository-locked serialized operations.
- After a user-approved main merge, update the canonical repository with
  `ff-only` only when safe. Never auto-stash, reset, force-push, or overwrite
  dirty canonical paths. Verify remote main, canonical main, and deployment
  commit equality before reporting final completion.
- When `.loop/config.json` exists, also read:
  - `{CODEX_LOOP_DIR}`
  - `{CLAUDE_LOOP_DIR}`

## Write Policy

- Project short memory may be appended by hooks.
- Project long memory receives reviewed durable facts only; write candidates to
  `codex/memory_proposals.md` first.
- Personal long and short memory require explicit approval of exact content.
- Personal candidates may be written only to
  `{RUNTIME_PATHS.personal_memory / "proposals.md"}`, and only when they
  are distilled cross-project habits, preferences, thinking style, workflow
  preferences, or user-profile facts.

## Memory Review Console

The central memory review console is maintained at:

- `{APP_ROOT}`

For this project, pass:

- `MEMORY_REVIEW_PROJECT_ROOT={root}`

{agent_candidate_protocol(root)}

{loop_superpowers.managed_rule_block()}
"""


def hook_script(root: pathlib.Path, source: str) -> str:
    source_label = "claude-code" if source == "claude" else "codex"
    return f'''#!/usr/bin/env python3
"""Shared memory hook installed by vibe_coding_manage_platform."""

from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import subprocess
import sys
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[2]
APP_ROOT = pathlib.Path("{APP_ROOT}")
CODEX_DIR = ROOT / "codex"
SHORT_MEMORY = CODEX_DIR / "codex_short_memory.md"
PROJECT_PROPOSALS = CODEX_DIR / "memory_proposals.md"
CONTEXT_PACKET = CODEX_DIR / "codex_context_packet.md"
SHARED_CONTEXT_PACKET = CODEX_DIR / "shared_memory_context_packet.md"
LONG_MEMORY = CODEX_DIR / "codex_long_memory.md"
REVIEW_SCRIPT = APP_ROOT / "scripts" / "memory_review_queue.py"
REVIEW_URL = "http://127.0.0.1:8897"
LOOP_CONFIG = ROOT / ".loop" / "config.json"
SOURCE = "{source_label}"


def now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def read_stdin_json() -> Any:
    raw = sys.stdin.read()
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def ensure_files() -> None:
    CODEX_DIR.mkdir(parents=True, exist_ok=True)
    if not SHORT_MEMORY.exists():
        SHORT_MEMORY.write_text("# Project Short Memory\\n\\n## Recent Hook Events\\n", encoding="utf-8")
    if not PROJECT_PROPOSALS.exists():
        PROJECT_PROPOSALS.write_text("# Project Memory Proposals\\n\\n## Pending Project Long-Memory Candidates\\n", encoding="utf-8")


def refresh_queue() -> dict[str, int]:
    if not REVIEW_SCRIPT.exists():
        return {{"pending": 0, "project_pending": 0, "personal_pending": 0}}
    env = os.environ.copy()
    env["MEMORY_REVIEW_PROJECT_ROOT"] = str(ROOT)
    subprocess.run([sys.executable, str(REVIEW_SCRIPT), "refresh"], cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8, check=False)
    try:
        data = json.loads((CODEX_DIR / "memory_review_queue.json").read_text(encoding="utf-8"))
        return data.get("counts", {{}})
    except Exception:
        return {{"pending": 0, "project_pending": 0, "personal_pending": 0}}


def loop_context() -> str:
    if not LOOP_CONFIG.exists():
        return ""
    try:
        config = json.loads(LOOP_CONFIG.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("root must be an object")
    except (OSError, ValueError, json.JSONDecodeError):
        return f"""

## Loop Engineering

- Loop configuration is invalid: `{{LOOP_CONFIG}}`.
- Fix the configuration before Loop planning, staging, evaluation, or release.
"""

    methodology = config.get("methodology", {{}})
    superpowers = (
        methodology.get("superpowers", {{}})
        if isinstance(methodology, dict)
        else {{}}
    )
    enabled = (
        isinstance(superpowers, dict)
        and methodology.get("provider") == "superpowers"
        and superpowers.get("enabled") is True
    )
    if enabled:
        worktree = config.get("worktree", {{}})
        commands = (
            worktree.get("finish_validation_commands", [])
            if isinstance(worktree, dict)
            else []
        )
        gate = "configured" if commands else "missing"
        return f"""

## Loop × Superpowers

- project loop config: `{{LOOP_CONFIG}}` (present)
- Loop is the only lifecycle orchestrator for worktrees, branches, staging,
  evaluation, release, main merge, and production.
- Use Superpowers for brainstorming, written plans, TDD, systematic debugging,
  code review, and verification before completion.
- finish validation gate: {{gate}}; run configured validation before success claims.
- Subagents and parallel agents require explicit user authorization and isolated
  Loop-safe worktrees.
- Read `{CODEX_LOOP_DIR}` and
  `{CLAUDE_LOOP_DIR}` before substantial Loop work.
"""

    return f"""

## Loop Engineering

- project loop config: `{{LOOP_CONFIG}}` (present)
- Required behavior: read `.loop/config.json` before loop planning, staging,
  evaluation, or master/production decisions.
"""


def context_text(event: str, counts: dict[str, int]) -> str:
    loop = loop_context()
    return f"""# Shared Memory Context Packet

Generated: {{now()}}
Trigger: {{SOURCE}}:{{event}}
Repository: `{{ROOT}}`

## Required Memory

- `README.md`
- `codex/codex_long_memory.md`
- `codex/codex_short_memory.md` (read selectively)
- `codex/memory_proposals.md`
- `{RUNTIME_PATHS.personal_memory / "long.md"}`
- `{RUNTIME_PATHS.personal_memory / "short.md"}`

## Pending Memory Review

- pending total: {{counts.get("pending", 0)}}
- project candidates: {{counts.get("project_pending", 0)}}
- personal candidates: {{counts.get("personal_pending", 0)}}
- review URL: {{REVIEW_URL}}
- CLI: `MEMORY_REVIEW_PROJECT_ROOT={{ROOT}} python3 {{APP_ROOT}}/scripts/memory_review.py list`

## Candidate Generation Reminder

- The active {{SOURCE}} conversation model reviews memory candidates before its
  final response and at compaction boundaries.
- Hooks must not copy raw prompts into candidate files or call another model.
- Create at most two distilled candidates through `memory_review.py propose`.
- If creation fails, report the concrete reason in the current conversation.
{{loop}}
"""


def append_short(event: str, payload: Any) -> None:
    session_id = None
    if isinstance(payload, dict):
        value = payload.get("session_id", payload.get("sessionId"))
        if isinstance(value, str) and value:
            session_id = value
    entry = (
        f"\\n### {{now()}} - {{SOURCE}}:{{event}}\\n\\n"
        f"- source_agent: {{SOURCE}}\\n"
        f"- event: {{event}}\\n"
        f"- cwd: `{{os.getcwd()}}`\\n"
    )
    if session_id is not None:
        entry += f"- session_id: {{session_id}}\\n"
    with SHORT_MEMORY.open("a", encoding="utf-8") as handle:
        handle.write(entry)


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    payload = read_stdin_json()
    ensure_files()
    counts = refresh_queue()
    text = context_text(event, counts).rstrip() + "\\n"
    CONTEXT_PACKET.write_text(text, encoding="utf-8")
    SHARED_CONTEXT_PACKET.write_text(text, encoding="utf-8")
    append_short(event, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _gate_hook_entry(agent: str) -> dict[str, Any]:
    return {
        "matcher": "apply_patch|Edit|Write|Bash|exec_command|mcp__filesystem__.*",
        "hooks": [
            {
                "type": "command",
                "command": (
                    "/usr/bin/python3 \"$(git rev-parse --show-toplevel)/."
                    f"{agent}/hooks/ui_design_gate_hook.py\""
                ),
                "timeout": 10,
                "statusMessage": "Checking UI design approval",
            }
        ],
    }


def _hooks_document(agent: str) -> dict[str, Any]:
    hook_path = f".{agent}/hooks/shared_memory_hook.py"
    command = f'/usr/bin/python3 "$(git rev-parse --show-toplevel)/{hook_path}"'
    return {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume|clear|compact",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{command} session_start",
                            "timeout": 10,
                            "statusMessage": "Loading project memory",
                        }
                    ],
                }
            ],
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{command} user_prompt_submit",
                            "timeout": 10,
                            "statusMessage": "Recording project memory context",
                        }
                    ]
                }
            ],
            "PreCompact": [
                {
                    "matcher": "manual|auto",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{command} pre_compact",
                            "timeout": 10,
                            "statusMessage": "Preserving project memory",
                        }
                    ],
                }
            ],
            "PostCompact": [
                {
                    "matcher": "manual|auto",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{command} post_compact",
                            "timeout": 10,
                            "statusMessage": "Refreshing project memory",
                        }
                    ],
                }
            ],
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{command} stop",
                            "timeout": 10,
                            "statusMessage": "Finalizing project memory checkpoint",
                        }
                    ]
                }
            ],
            "PreToolUse": [_gate_hook_entry(agent)],
        }
    }


def codex_hooks_json() -> str:
    return json.dumps(_hooks_document("codex"), ensure_ascii=False, indent=2) + "\n"


def claude_settings_json() -> str:
    return json.dumps(_hooks_document("claude"), ensure_ascii=False, indent=2) + "\n"


def ui_design_gate_hook_text() -> str:
    return UI_DESIGN_GATE_HOOK_TEMPLATE.read_text(encoding="utf-8")


def _is_managed_gate_entry(entry: Any) -> bool:
    if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
        return False
    return any(
        isinstance(hook, dict)
        and "ui_design_gate_hook.py" in str(hook.get("command", ""))
        for hook in entry["hooks"]
    )


def _merge_gate_hook_config(
    path: pathlib.Path, agent: str, changes: list[dict[str, str]]
) -> None:
    if not path.exists():
        content = codex_hooks_json() if agent == "codex" else claude_settings_json()
        loop_superpowers.atomic_write_text(path, content)
        changes.append({"path": str(path), "status": "created"})
        return
    original = path.read_text(encoding="utf-8")
    try:
        value = json.loads(original)
    except json.JSONDecodeError:
        changes.append({"path": str(path), "status": "conflict"})
        return
    if not isinstance(value, dict):
        changes.append({"path": str(path), "status": "conflict"})
        return
    hooks = value.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        changes.append({"path": str(path), "status": "conflict"})
        return
    entries = hooks.setdefault("PreToolUse", [])
    if not isinstance(entries, list):
        changes.append({"path": str(path), "status": "conflict"})
        return
    expected = _gate_hook_entry(agent)
    updated_entries = [entry for entry in entries if not _is_managed_gate_entry(entry)]
    updated_entries.append(expected)
    if entries == updated_entries:
        changes.append({"path": str(path), "status": "existing"})
        return
    hooks["PreToolUse"] = updated_entries
    backup = loop_superpowers.timestamped_backup(path)
    loop_superpowers.atomic_write_text(
        path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    changes.extend(
        [
            {"path": str(backup), "status": "backup"},
            {"path": str(path), "status": "upgraded"},
        ]
    )


def init_project(root: str | pathlib.Path) -> dict[str, Any]:
    project_root = normalize_project_root(root)
    changes: list[dict[str, str]] = []
    name = repo_name(project_root)
    ensure_file(project_root / "codex" / "codex_long_memory.md", f"# {name} Project Long Memory\n\n## Approved Project Memories\n", changes)
    ensure_file(project_root / "codex" / "codex_short_memory.md", f"# {name} Project Short Memory\n\n## Current Working Notes\n\n## Recent Hook Events\n", changes)
    ensure_file(project_root / "codex" / "memory_proposals.md", f"# {name} Memory Proposals\n\n## Pending Project Long-Memory Candidates\n", changes)
    ensure_file(project_root / "codex" / "codex_context_packet.md", f"# {name} Context Packet\n\nGenerated: {now()}\n", changes)
    ensure_file(project_root / "codex" / "shared_memory_context_packet.md", f"# {name} Shared Memory Context Packet\n\nGenerated: {now()}\n", changes)
    ui_root = project_root / "codex" / "ui_design"
    ensure_file(
        ui_root / "config.json",
        json.dumps(ui_design_config(project_root), ensure_ascii=False, indent=2, sort_keys=True),
        changes,
    )
    ensure_file(
        ui_root / "preferences.json",
        json.dumps({"schema_version": 1, "overrides": {}}, ensure_ascii=False, indent=2, sort_keys=True),
        changes,
    )
    ensure_file(
        ui_root / "active-skills.json",
        json.dumps({"schema_version": 1, "execution_order": [], "skills": []}, ensure_ascii=False, indent=2, sort_keys=True),
        changes,
    )
    ensure_file(
        ui_root / "approvals.json",
        json.dumps(
            {"schema_version": 1, "package_approvals": {}, "project_global_approval": None},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        changes,
    )
    context_path = ui_root / "effective-context.json"
    context_existed = context_path.exists()
    publish_effective_ui_context(project_root)
    changes.append({
        "path": str(context_path),
        "status": "existing" if context_existed else "created",
    })
    append_if_missing(project_root / "AGENTS.md", f"# {name} Codex Instructions", agent_memory_block(project_root), changes)
    append_if_missing(project_root / "CLAUDE.md", "# " + name + " Shared Memory Instructions", claude_md(project_root), changes)
    append_if_missing(project_root / "AGENTS.md", "## Agent-Generated Memory Candidates", agent_candidate_protocol(project_root), changes)
    append_if_missing(project_root / "CLAUDE.md", "## Agent-Generated Memory Candidates", agent_candidate_protocol(project_root), changes)
    # These are inert compatibility assets: without project hook documents they
    # are not hook entry points. Universal user hooks remain the sole entries.
    ensure_file(
        project_root / ".codex" / "hooks" / "ui_design_gate_hook.py",
        ui_design_gate_hook_text(),
        changes,
    )
    ensure_file(
        project_root / ".claude" / "hooks" / "ui_design_gate_hook.py",
        ui_design_gate_hook_text(),
        changes,
    )
    ensure_file(project_root / ".claude" / "rules" / "shared-memory.md", shared_memory_rule(project_root), changes)
    append_if_missing(
        project_root / ".claude" / "rules" / "shared-memory.md",
        "## Agent-Generated Memory Candidates",
        agent_candidate_protocol(project_root),
        changes,
    )
    register_project(project_root, make_current=True)
    return {"ok": True, "project": project_entry(project_root), "changes": changes}


def upgrade_memory_hooks(root: str | pathlib.Path) -> dict[str, Any]:
    """Upgrade managed hook code while preserving a timestamped audit backup."""
    project_root = normalize_project_root(root)
    changes: list[dict[str, str]] = []
    stamp = _dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    for path, content in (
        (project_root / ".codex" / "hooks" / "shared_memory_hook.py", hook_script(project_root, "codex")),
        (project_root / ".claude" / "hooks" / "shared_memory_hook.py", hook_script(project_root, "claude")),
        (project_root / ".codex" / "hooks" / "ui_design_gate_hook.py", ui_design_gate_hook_text()),
        (project_root / ".claude" / "hooks" / "ui_design_gate_hook.py", ui_design_gate_hook_text()),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current == content:
            changes.append({"path": str(path), "status": "existing"})
            continue
        if path.exists():
            backup = path.with_name(path.name + f".bak.{stamp}")
            shutil.copy2(path, backup)
            changes.append({"path": str(backup), "status": "backup"})
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
        changes.append({"path": str(path), "status": "upgraded"})
    _merge_gate_hook_config(
        project_root / ".codex" / "hooks.json", "codex", changes
    )
    _merge_gate_hook_config(
        project_root / ".claude" / "settings.json", "claude", changes
    )
    return {"ok": True, "project": project_entry(project_root), "changes": changes}


def upgrade_memory_rules(root: str | pathlib.Path) -> dict[str, Any]:
    """Install or replace only the central manager's marked instruction block."""
    project_root = normalize_project_root(root)
    changes: list[dict[str, str]] = []
    managed = loop_superpowers.managed_rule_block()
    targets = (
        (project_root / "AGENTS.md", agent_memory_block(project_root)),
        (project_root / "CLAUDE.md", claude_md(project_root)),
        (
            project_root / ".claude" / "rules" / "shared-memory.md",
            shared_memory_rule(project_root),
        ),
    )
    for path, default_content in targets:
        if not path.exists():
            loop_superpowers.atomic_write_text(path, default_content.rstrip() + "\n")
            changes.append({"path": str(path), "status": "created"})
            continue
        current = path.read_text(encoding="utf-8")
        updated, status = loop_superpowers.replace_managed_block(current, managed)
        if status == "conflict":
            changes.append({"path": str(path), "status": "conflict"})
            continue
        if status == "existing":
            changes.append({"path": str(path), "status": "existing"})
            continue
        backup = loop_superpowers.timestamped_backup(path)
        loop_superpowers.atomic_write_text(path, updated)
        changes.extend(
            [
                {"path": str(backup), "status": "backup"},
                {"path": str(path), "status": status},
            ]
        )
    if (project_root / "codex" / "ui_design" / "config.json").exists():
        context_path = project_root / "codex" / "ui_design" / "effective-context.json"
        before = read_json(context_path, {})
        snapshot = publish_effective_ui_context(project_root)
        changes.append(
            {
                "path": str(context_path),
                "status": "existing" if before == snapshot else "upgraded",
            }
        )
    return {"ok": True, "project": project_entry(project_root), "changes": changes}


def upgrade_memory(root: str | pathlib.Path) -> dict[str, Any]:
    project_root = normalize_project_root(root)
    rules = upgrade_memory_rules(project_root)
    hooks = upgrade_memory_hooks(project_root)
    return {
        "ok": True,
        "project": project_entry(project_root),
        "changes": rules["changes"] + hooks["changes"],
    }


def used_loop_ports() -> set[int]:
    ports: set[int] = set()
    for item in registry().get("projects", []):
        root = item.get("root")
        if not root:
            continue
        config_path = pathlib.Path(root) / ".loop" / "config.json"
        data = read_json(config_path, {})
        try:
            port = int(data.get("staging", {}).get("port"))
            ports.add(port)
        except (TypeError, ValueError):
            continue
    return ports


def recommend_port() -> int:
    used = used_loop_ports()
    port = DEFAULT_BASE_PORT
    while port in used:
        port += 1
    return port


def loop_config(root: pathlib.Path, port: int) -> dict[str, Any]:
    name = repo_name(root)
    slug_bucket = re.sub(r"[^a-z0-9-]+", "-", name.lower().replace("_", "-")).strip("-")
    return {
        "schema_version": loop_superpowers.SCHEMA_VERSION,
        "project_repo_name": name,
        "loop_enabled": True,
        "repository": {
            "canonical_root": str(root.resolve()),
            "main_branch": "master",
            "remote": "origin",
        },
        "worktree": {
            "enabled": True,
            "trigger_phrase": "开 worktree",
            "root": str(DEFAULT_WORKTREE_ROOT),
            "default_root": str(DEFAULT_WORKTREE_ROOT),
            "finish_validation_commands": [loop_superpowers.COMPLETION_COMMAND],
            "allow_inside_canonical_root": False,
            "loop_requires_dedicated_worktree": True,
            "one_task_one_conversation_one_worktree_one_branch": True,
            "primary_loop_conversation_owns_product_source_edits": True,
            "auxiliary_conversations_use_auxiliary_branches": True,
            "avoid_multiple_conversations_mutating_same_loop_branch": True,
            "merge_auxiliary_work_through_primary_loop_worktree": True,
            "staging_single_active_loop_branch_by_default": True,
            "registry_path": "~/.codex/worktree_manager/tasks.json",
        },
        "branch": {
            "name_format": "loop/<project>-<date>-<slug>",
            "auto_commit": True,
            "auto_push": True,
            "commit_messages": {
                "implement": "loop: implement <feature>",
                "fix_round": "loop: fix claude findings round <round>",
            },
            "merge_to_main_requires_user_command": True,
        },
        "release": {
            "serialized": True,
            "lock_root": "~/.codex/worktree_manager/locks",
            "temporary_release_branch": True,
            "require_latest_remote_main": True,
            "require_full_tests": True,
            "allow_force_push": False,
        },
        "canonical_sync": {
            "enabled": True,
            "mode": "ff-only",
            "require_main_branch": True,
            "allow_dirty_non_overlapping": True,
            "block_dirty_overlapping": True,
            "allow_auto_stash": False,
            "allow_reset": False,
            "verify_dirty_content_preserved": True,
        },
        "verification": {
            "commands": [],
            "require_feature_ancestor_of_main": True,
            "require_remote_canonical_deploy_commit_match": True,
        },
        "staging": {
            "mode": "shared_locked",
            "lock_root": "~/.codex/worktree_manager/locks",
            "deploy_source_after_merge": "origin/master",
            "host": DEFAULT_STAGING_HOST,
            "port": port,
            "public_base_url": f"http://8.210.155.175:{port}",
            "remote_path": f"/root/{name}_loop_staging",
            "database": f"{name}_loop_staging",
            "oss_bucket": f"{slug_bucket}-loop-staging",
            "allow_real_rds_oss": True,
            "cleanup_test_data_after_eval": True,
            "open_server_firewall_port": True,
            "aliyun_security_group_may_require_manual_or_cli_rule": True,
        },
        "production_guardrails": {
            "never_deploy_production_during_loop": True,
            "ask_before_merge_master": True,
            "ask_before_production_deploy": True,
            "create_production_database_before_master_merge": True,
            "switch_to_production_bucket_before_production_enablement": True,
        },
        "claude_eval": {
            "default_method": "playwright",
            "allow_claude_internal_browser": True,
            "allow_interactive_login": True,
            "allow_test_script_changes": True,
            "test_scripts_dir": "loop/claude_tests",
            "max_auto_rounds": 10,
            "pause_on_same_p0_p1_for_consecutive_rounds": 2,
            "report_markdown": "loop/reports/claude_eval_latest.md",
            "report_json": "loop/reports/claude_eval_latest.json",
        },
        "prd": {
            "input_mode": "chat_markdown",
            "save_latest_to": "loop/prd/current_prd.md",
            "codex_generates_acceptance_criteria": True,
            "acceptance_criteria_path": "loop/acceptance/criteria.md",
            "user_confirms_acceptance_criteria": True,
        },
        "memory": {
            "codex_loop_dir": "~/.codex/loop_engineering",
            "claude_loop_dir": "~/.claude/loop_engineering",
            "both_agents_read_both_dirs": True,
            "project_loop_config_to_personal_long_candidate": True,
            "worktree_flow_document": str(APP_ROOT / "docs" / "worktree_loop_workflow.md"),
        },
        "methodology": loop_superpowers.methodology_defaults(),
        "sensitive_data_policy": {
            "do_not_record_tokens": True,
            "do_not_record_verification_codes": True,
            "do_not_record_api_keys": True,
            "do_not_record_rds_or_oss_secrets": True,
        },
    }


def merge_missing(current: Any, defaults: Any) -> Any:
    """Add new schema defaults without overwriting project-specific values."""
    if not isinstance(current, dict) or not isinstance(defaults, dict):
        return current
    merged = dict(current)
    for key, default in defaults.items():
        if key not in merged:
            merged[key] = default
        elif isinstance(merged[key], dict) and isinstance(default, dict):
            merged[key] = merge_missing(merged[key], default)
    return merged


def upgrade_loop_config(root: pathlib.Path, port: int) -> tuple[dict[str, Any], str]:
    config_path = root / ".loop" / "config.json"
    defaults = loop_config(root, port)
    if not config_path.exists():
        write_json(config_path, defaults)
        return defaults, "created"
    current = read_json(config_path, {})
    if not isinstance(current, dict):
        raise ValueError(f"Invalid loop config JSON: {config_path}")
    upgraded = merge_missing(current, defaults)
    upgraded["schema_version"] = max(
        int(current.get("schema_version", 1)), loop_superpowers.SCHEMA_VERSION
    )
    upgraded.setdefault("repository", {})["canonical_root"] = str(root.resolve())
    if upgraded != current:
        write_json(config_path, upgraded)
        return upgraded, "upgraded"
    return current, "existing"


def _upgraded_loop_value(
    project_root: pathlib.Path, current: dict[str, Any], port: int
) -> dict[str, Any]:
    upgraded = merge_missing(current, loop_config(project_root, port))
    upgraded["schema_version"] = max(
        int(current.get("schema_version", 1)), loop_superpowers.SCHEMA_VERSION
    )
    repository = upgraded.setdefault("repository", {})
    if not isinstance(repository, dict):
        raise ValueError("Invalid loop config: repository must be an object")
    repository["canonical_root"] = str(project_root.resolve())
    loop_superpowers.append_unique_command(upgraded)
    return upgraded


def preview_loop_upgrade(
    root: str | pathlib.Path, port: int | None = None
) -> dict[str, Any]:
    project_root = normalize_project_root(root)
    config_path = project_root / ".loop" / "config.json"
    current = loop_superpowers.read_loop_config_strict(config_path)
    selected_port = int(port or current.get("staging", {}).get("port") or recommend_port())
    upgraded = _upgraded_loop_value(project_root, current, selected_port)
    validator_state = loop_superpowers.validator_status(project_root)
    projected_validator_state = (
        "custom_conflict" if validator_state == "custom_conflict" else "managed"
    )
    return {
        "ok": True,
        "project_root": str(project_root),
        "port": selected_port,
        "added_paths": loop_superpowers.added_config_paths(current, upgraded),
        "preserved_categories": [
            "repository main/remote",
            "staging resources",
            "verification commands",
            "production guardrails",
            "unknown extension fields",
        ],
        "config_will_change": upgraded != current,
        "validator_action": validator_state,
        "readiness": loop_superpowers.inspect_config(
            upgraded, projected_validator_state
        ),
    }


def upgrade_loop(
    root: str | pathlib.Path, port: int | None = None
) -> dict[str, Any]:
    project_root = normalize_project_root(root)
    config_path = project_root / ".loop" / "config.json"
    current = loop_superpowers.read_loop_config_strict(config_path)
    selected_port = int(port or current.get("staging", {}).get("port") or recommend_port())
    upgraded = _upgraded_loop_value(project_root, current, selected_port)
    changes: list[dict[str, str]] = []

    config_status = "existing"
    if upgraded != current:
        backup = loop_superpowers.timestamped_backup(config_path)
        changes.append({"path": str(backup), "status": "backup"})

    methodology_status = loop_superpowers.install_validator(project_root, changes)
    if upgraded != current:
        loop_superpowers.atomic_write_text(
            config_path,
            json.dumps(upgraded, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        changes.append({"path": str(config_path), "status": "upgraded"})
        config_status = "upgraded"

    readiness = loop_superpowers.inspect_config(
        upgraded, methodology_status["status"]
    )
    return {
        "ok": True,
        "project": project_entry(project_root),
        "port": selected_port,
        "changes": changes,
        "config_status": config_status,
        "methodology_status": methodology_status,
        "readiness": readiness,
    }


def init_loop(root: str | pathlib.Path, port: int | None = None) -> dict[str, Any]:
    project_root = normalize_project_root(root)
    config_path = project_root / ".loop" / "config.json"
    if config_path.exists():
        raise FileExistsError(
            "Loop config already exists; use preview-loop-upgrade before upgrade-loop"
        )
    result = init_project(project_root)
    changes = result["changes"]
    selected_port = int(port or recommend_port())
    config_path.parent.mkdir(parents=True, exist_ok=True)
    _, config_status = upgrade_loop_config(project_root, selected_port)
    changes.append({"path": str(config_path), "status": config_status})
    for rel in ["loop/prd", "loop/acceptance", "loop/reports", "loop/state", "loop/claude_tests"]:
        path = project_root / rel
        if path.exists():
            changes.append({"path": str(path), "status": "existing"})
        else:
            path.mkdir(parents=True, exist_ok=True)
            changes.append({"path": str(path), "status": "created"})
    methodology_status = loop_superpowers.install_validator(project_root, changes)
    register_project(project_root, make_current=True)
    return {
        "ok": True,
        "project": project_entry(project_root),
        "port": selected_port,
        "changes": changes,
        "methodology_status": methodology_status,
    }


def git_root(path: pathlib.Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage memory review projects")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in [
        "register",
        "use",
        "init",
        "upgrade-memory-hooks",
        "upgrade-rules",
        "upgrade-memory",
    ]:
        p = sub.add_parser(name)
        p.add_argument("project_root")
    loop_parser = sub.add_parser("init-loop")
    loop_parser.add_argument("project_root")
    loop_parser.add_argument("--port", type=int, default=None)
    upgrade_parser = sub.add_parser("upgrade-loop")
    upgrade_parser.add_argument("project_root")
    upgrade_parser.add_argument("--port", type=int, default=None)
    preview_parser = sub.add_parser("preview-loop-upgrade")
    preview_parser.add_argument("project_root")
    preview_parser.add_argument("--port", type=int, default=None)
    sub.add_parser("list")
    sub.add_parser("current")
    sub.add_parser("recommend-port")
    args = parser.parse_args()

    if args.command == "register":
        print(json.dumps(register_project(args.project_root), ensure_ascii=False, indent=2))
        return 0
    if args.command == "use":
        print(json.dumps(set_current_project(args.project_root), ensure_ascii=False, indent=2))
        return 0
    if args.command == "init":
        print(json.dumps(init_project(args.project_root), ensure_ascii=False, indent=2))
        return 0
    if args.command == "upgrade-memory-hooks":
        print(json.dumps(upgrade_memory_hooks(args.project_root), ensure_ascii=False, indent=2))
        return 0
    if args.command == "upgrade-rules":
        print(json.dumps(upgrade_memory_rules(args.project_root), ensure_ascii=False, indent=2))
        return 0
    if args.command == "upgrade-memory":
        print(json.dumps(upgrade_memory(args.project_root), ensure_ascii=False, indent=2))
        return 0
    if args.command == "init-loop":
        print(json.dumps(init_loop(args.project_root, args.port), ensure_ascii=False, indent=2))
        return 0
    if args.command == "upgrade-loop":
        print(json.dumps(upgrade_loop(args.project_root, args.port), ensure_ascii=False, indent=2))
        return 0
    if args.command == "preview-loop-upgrade":
        print(
            json.dumps(
                preview_loop_upgrade(args.project_root, args.port),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "list":
        print(json.dumps(list_projects(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "current":
        print(current_project())
        return 0
    if args.command == "recommend-port":
        print(recommend_port())
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
