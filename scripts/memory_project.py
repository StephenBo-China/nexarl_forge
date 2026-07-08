#!/usr/bin/env python3
"""Project registry and initialization helpers for the memory review console."""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import pathlib
import re
import subprocess
from typing import Any


APP_ROOT = pathlib.Path(__file__).resolve().parents[1]
REGISTRY_PATH = pathlib.Path(
    os.environ.get(
        "MEMORY_REVIEW_PROJECT_REGISTRY",
        str(pathlib.Path.home() / ".codex" / "memory_review" / "projects.json"),
    )
).expanduser()
CODEX_LOOP_DIR = pathlib.Path.home() / ".codex" / "loop_engineering"
CLAUDE_LOOP_DIR = pathlib.Path.home() / ".claude" / "loop_engineering"
DEFAULT_STAGING_HOST = "root@8.210.155.175"
DEFAULT_BASE_PORT = 8081


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


def registry() -> dict[str, Any]:
    data = read_json(REGISTRY_PATH, {"current_project": "", "projects": []})
    if not isinstance(data, dict):
        data = {"current_project": "", "projects": []}
    data.setdefault("current_project", "")
    data.setdefault("projects", [])
    return data


def project_entry(root: pathlib.Path) -> dict[str, Any]:
    root = root.resolve()
    return {
        "name": repo_name(root),
        "root": str(root),
        "is_git_repo": (root / ".git").exists(),
        "has_memory": (root / "codex" / "codex_long_memory.md").exists()
        and (root / "codex" / "codex_short_memory.md").exists()
        and (root / "codex" / "memory_proposals.md").exists(),
        "has_loop": (root / ".loop" / "config.json").exists(),
        "last_opened_at": now(),
    }


def register_project(root: str | pathlib.Path, make_current: bool = True) -> dict[str, Any]:
    project_root = normalize_project_root(root)
    data = registry()
    entry = project_entry(project_root)
    projects = [p for p in data.get("projects", []) if p.get("root") != str(project_root)]
    projects.append(entry)
    projects.sort(key=lambda item: item.get("name", ""))
    data["projects"] = projects
    if make_current:
        data["current_project"] = str(project_root)
    write_json(REGISTRY_PATH, data)
    return data


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
    data["projects"] = refreshed
    write_json(REGISTRY_PATH, data)
    return data


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
staging deployment, Claude evaluation, or master/production decisions.

## Memory Governance

Project short memory may be appended by hooks. Project long memory should
receive only reviewed durable project facts. Personal long and short memory
require explicit approval of exact content; write personal candidates only to
`~/.codex/personal_memory/proposals.md`.
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
- `/Users/stephenbo/.codex/personal_memory/long.md`
- `/Users/stephenbo/.codex/personal_memory/short.md`

Read project short memory selectively from `codex/codex_short_memory.md`; do
not load the entire file by default.
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
- When `.loop/config.json` exists, also read:
  - `/Users/stephenbo/.codex/loop_engineering`
  - `/Users/stephenbo/.claude/loop_engineering`

## Write Policy

- Project short memory may be appended by hooks.
- Project long memory receives reviewed durable facts only; write candidates to
  `codex/memory_proposals.md` first.
- Personal long and short memory require explicit approval of exact content.
- Personal candidates may be written only to
  `/Users/stephenbo/.codex/personal_memory/proposals.md`, and only when they
  are distilled cross-project habits, preferences, thinking style, workflow
  preferences, or user-profile facts.

## Memory Review Console

The central memory review console is maintained at:

- `{APP_ROOT}`

For this project, pass:

- `MEMORY_REVIEW_PROJECT_ROOT={root}`
"""


def hook_script(root: pathlib.Path, source: str) -> str:
    source_label = "claude_code" if source == "claude" else "codex"
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
        return {{"raw_stdin": raw[:4000]}}


def find_prompt(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\\n".join(filter(None, [find_prompt(item) for item in value])).strip()
    if isinstance(value, dict):
        for key in ["prompt", "user_prompt", "userPrompt", "input", "message", "text", "content"]:
            if key in value:
                found = find_prompt(value[key])
                if found:
                    return found
        for item in value.values():
            found = find_prompt(item)
            if found:
                return found
    return ""


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


def context_text(event: str, counts: dict[str, int]) -> str:
    loop = ""
    if LOOP_CONFIG.exists():
        loop = f"""

## Loop Engineering

- project loop config: `{{LOOP_CONFIG}}` (present)
- Codex loop directory: `/Users/stephenbo/.codex/loop_engineering`
- Claude loop directory: `/Users/stephenbo/.claude/loop_engineering`
- Required behavior: read `.loop/config.json` before loop planning,
  staging work, Claude evaluation, or master/production decisions.
"""
    return f"""# Shared Memory Context Packet

Generated: {{now()}}
Trigger: {{SOURCE}}:{{event}}
Repository: `{{ROOT}}`

## Required Memory

- `README.md`
- `codex/codex_long_memory.md`
- `codex/codex_short_memory.md` (read selectively)
- `codex/memory_proposals.md`
- `/Users/stephenbo/.codex/personal_memory/long.md`
- `/Users/stephenbo/.codex/personal_memory/short.md`

## Pending Memory Review

- pending total: {{counts.get("pending", 0)}}
- project candidates: {{counts.get("project_pending", 0)}}
- personal candidates: {{counts.get("personal_pending", 0)}}
- review URL: {{REVIEW_URL}}
- CLI: `MEMORY_REVIEW_PROJECT_ROOT={{ROOT}} python3 {{APP_ROOT}}/scripts/memory_review.py list`
{{loop}}
"""


def append_short(event: str, payload: Any) -> None:
    prompt = find_prompt(payload)
    entry = f"\\n### {{now()}} - {{SOURCE}}:{{event}}\\n\\n- cwd: `{{os.getcwd()}}`\\n"
    if prompt:
        entry += "\\n```text\\n" + prompt[:3000] + ("\\n...[truncated]" if len(prompt) > 3000 else "") + "\\n```\\n"
    else:
        entry += "- no user prompt payload was available to this hook.\\n"
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


def codex_hooks_json() -> str:
    return """{
  "hooks": {
    "SessionStart": [{"matcher": "startup|resume|clear|compact", "hooks": [{"type": "command", "command": "/usr/bin/python3 \\"$(git rev-parse --show-toplevel)/.codex/hooks/shared_memory_hook.py\\" session_start", "timeout": 10, "statusMessage": "Loading project memory"}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/usr/bin/python3 \\"$(git rev-parse --show-toplevel)/.codex/hooks/shared_memory_hook.py\\" user_prompt_submit", "timeout": 10, "statusMessage": "Recording project memory context"}]}],
    "PreCompact": [{"matcher": "manual|auto", "hooks": [{"type": "command", "command": "/usr/bin/python3 \\"$(git rev-parse --show-toplevel)/.codex/hooks/shared_memory_hook.py\\" pre_compact", "timeout": 10, "statusMessage": "Preserving project memory"}]}],
    "PostCompact": [{"matcher": "manual|auto", "hooks": [{"type": "command", "command": "/usr/bin/python3 \\"$(git rev-parse --show-toplevel)/.codex/hooks/shared_memory_hook.py\\" post_compact", "timeout": 10, "statusMessage": "Refreshing project memory"}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/python3 \\"$(git rev-parse --show-toplevel)/.codex/hooks/shared_memory_hook.py\\" stop", "timeout": 10, "statusMessage": "Finalizing project memory checkpoint"}]}]
  }
}"""


def claude_settings_json() -> str:
    return codex_hooks_json().replace(".codex/hooks/shared_memory_hook.py", ".claude/hooks/shared_memory_hook.py")


def init_project(root: str | pathlib.Path) -> dict[str, Any]:
    project_root = normalize_project_root(root)
    changes: list[dict[str, str]] = []
    name = repo_name(project_root)
    ensure_file(project_root / "codex" / "codex_long_memory.md", f"# {name} Project Long Memory\n\n## Approved Project Memories\n", changes)
    ensure_file(project_root / "codex" / "codex_short_memory.md", f"# {name} Project Short Memory\n\n## Current Working Notes\n\n## Recent Hook Events\n", changes)
    ensure_file(project_root / "codex" / "memory_proposals.md", f"# {name} Memory Proposals\n\n## Pending Project Long-Memory Candidates\n", changes)
    ensure_file(project_root / "codex" / "codex_context_packet.md", f"# {name} Context Packet\n\nGenerated: {now()}\n", changes)
    ensure_file(project_root / "codex" / "shared_memory_context_packet.md", f"# {name} Shared Memory Context Packet\n\nGenerated: {now()}\n", changes)
    append_if_missing(project_root / "AGENTS.md", f"# {name} Codex Instructions", agent_memory_block(project_root), changes)
    append_if_missing(project_root / "CLAUDE.md", "# " + name + " Shared Memory Instructions", claude_md(project_root), changes)
    ensure_file(project_root / ".codex" / "hooks.json", codex_hooks_json(), changes)
    ensure_file(project_root / ".codex" / "hooks" / "shared_memory_hook.py", hook_script(project_root, "codex"), changes)
    ensure_file(project_root / ".claude" / "settings.json", claude_settings_json(), changes)
    ensure_file(project_root / ".claude" / "hooks" / "shared_memory_hook.py", hook_script(project_root, "claude"), changes)
    ensure_file(project_root / ".claude" / "rules" / "shared-memory.md", shared_memory_rule(project_root), changes)
    register_project(project_root, make_current=True)
    return {"ok": True, "project": project_entry(project_root), "changes": changes}


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
        "schema_version": 1,
        "project_repo_name": name,
        "loop_enabled": True,
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
        "staging": {
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
        },
        "sensitive_data_policy": {
            "do_not_record_tokens": True,
            "do_not_record_verification_codes": True,
            "do_not_record_api_keys": True,
            "do_not_record_rds_or_oss_secrets": True,
        },
    }


def init_loop(root: str | pathlib.Path, port: int | None = None) -> dict[str, Any]:
    project_root = normalize_project_root(root)
    result = init_project(project_root)
    changes = result["changes"]
    selected_port = int(port or recommend_port())
    config_path = project_root / ".loop" / "config.json"
    if config_path.exists():
        changes.append({"path": str(config_path), "status": "existing"})
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(config_path, loop_config(project_root, selected_port))
        changes.append({"path": str(config_path), "status": "created"})
    for rel in ["loop/prd", "loop/acceptance", "loop/reports", "loop/state", "loop/claude_tests"]:
        path = project_root / rel
        if path.exists():
            changes.append({"path": str(path), "status": "existing"})
        else:
            path.mkdir(parents=True, exist_ok=True)
            changes.append({"path": str(path), "status": "created"})
    register_project(project_root, make_current=True)
    return {"ok": True, "project": project_entry(project_root), "port": selected_port, "changes": changes}


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
    for name in ["register", "use", "init"]:
        p = sub.add_parser(name)
        p.add_argument("project_root")
    loop_parser = sub.add_parser("init-loop")
    loop_parser.add_argument("project_root")
    loop_parser.add_argument("--port", type=int, default=None)
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
    if args.command == "init-loop":
        print(json.dumps(init_loop(args.project_root, args.port), ensure_ascii=False, indent=2))
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
