#!/usr/bin/env python3
"""Shared memory review queue and approval helpers.

This module is intentionally dependency-free so Codex and Claude Code hooks can
run it quickly. It parses Markdown proposal files into a JSON review queue,
tracks decisions separately, and applies approved memories only after an
explicit user action from the CLI or local review server.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

import memory_project
from loop_superpowers import atomic_write_text
from ui_design_store import atomic_write_json, exclusive_lock

APP_ROOT = pathlib.Path(__file__).resolve().parents[1]
PROJECT_ROOT = pathlib.Path(
    os.environ.get("MEMORY_REVIEW_PROJECT_ROOT", str(memory_project.current_project(APP_ROOT)))
).expanduser().resolve()
ROOT = PROJECT_ROOT
CODEX_DIR = PROJECT_ROOT / "codex"
PROJECT_PROPOSALS = CODEX_DIR / "memory_proposals.md"
PROJECT_LONG = CODEX_DIR / "codex_long_memory.md"
PROJECT_QUEUE = CODEX_DIR / "memory_review_queue.json"
PROJECT_STATE = CODEX_DIR / "memory_review_state.json"

PERSONAL_DIR = pathlib.Path.home() / ".codex" / "personal_memory"
PERSONAL_PROPOSALS = PERSONAL_DIR / "proposals.md"
PERSONAL_LONG = PERSONAL_DIR / "long.md"
PERSONAL_SHORT = PERSONAL_DIR / "short.md"

REVIEW_HOST = "127.0.0.1"
REVIEW_PORT = 8897
REVIEW_URL = f"http://{REVIEW_HOST}:{REVIEW_PORT}"

SENSITIVE_PATTERNS = {
    "api_key": re.compile(r"\b(api[_-]?key|apikey)\b\s*[:=]", re.I),
    "access_key": re.compile(r"\b(access[_-]?key|AccessKeyId|AccessKeySecret)\b", re.I),
    "authorization": re.compile(r"\b(Authorization|Bearer\s+[A-Za-z0-9._~+/=-]{12,})\b", re.I),
    "password": re.compile(r"\b(password|passwd|pwd|NOEMA_MYSQL_PASSWORD)\b", re.I),
    "secret": re.compile(r"\b(secret|client_secret|ANTHROPIC_AUTH_TOKEN)\b", re.I),
    "token": re.compile(r"\b(token|access_token|refresh_token)\b\s*[:=]", re.I),
    "env_production": re.compile(r"\.env\.production", re.I),
    "sms_code": re.compile(r"\b(verification_code|verify_code|短信验证码)\b", re.I),
}
PERSONAL_NOISE_PATTERNS = [
    re.compile(r"# Overview\s+Generate 0 to 3 hyperpersonalized suggestions", re.I | re.S),
    re.compile(r"Policies to always exclude|You are an expert at upholding safety", re.I | re.S),
    re.compile(r"# In app browser:|Current URL:|Files mentioned by the user|codex-clipboard", re.I),
    re.compile(r"AGENTS\.md instructions|<INSTRUCTIONS>|</INSTRUCTIONS>", re.I | re.S),
    re.compile(r"审核台|候选记忆|记忆候选|项目短期记忆|项目长期记忆|codex_short_memory", re.I),
    re.compile(r"^memory_id:\s*M-\d{8}-\d{6}", re.I | re.M),
]
AGENT_CATEGORIES = {
    "personal": {
        "development_habit": "开发习惯",
        "collaboration_preference": "协作偏好",
        "work_style": "工作方式",
        "thinking_style": "思维方式",
        "user_profile": "用户画像",
        "workflow_preference": "跨项目工作流偏好",
    },
    "project": {
        "project_architecture": "项目架构",
        "deployment_rule": "部署规则",
        "product_direction": "产品方向",
        "technical_constraint": "技术约束",
        "project_workflow": "项目工作流",
    },
}


def now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def read_text(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def write_json(path: pathlib.Path, value: Any) -> None:
    atomic_write_json(path, value)


def read_json(path: pathlib.Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def stable_id(prefix: str, source_path: pathlib.Path, heading: str, body: str) -> str:
    digest = hashlib.sha1(
        f"{source_path}\n{heading}\n{body[:4000]}".encode("utf-8")
    ).hexdigest()[:12]
    return f"{prefix}-{digest}"


def split_heading_sections(text: str) -> list[tuple[str, str]]:
    matches = list(re.finditer(r"^### .+$", text, flags=re.MULTILINE))
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        heading = match.group(0).strip()
        body = text[match.end() : end].strip()
        sections.append((heading, body))
    return sections


def metadata_value(body: str, key: str) -> str:
    match = re.search(rf"^(?:-\s*)?{re.escape(key)}:\s*(.+)$", body, flags=re.MULTILINE)
    return match.group(1).strip().strip("`") if match else ""


def candidate_provenance(body: str) -> tuple[str, list[str], int]:
    source_agent = metadata_value(body, "source_agent") or "unknown"
    if source_agent not in {"codex", "claude-code", "unknown"}:
        source_agent = "unknown"
    source_agents = {
        value.strip()
        for value in metadata_value(body, "source_agents").split(",")
        if value.strip() in {"codex", "claude-code", "unknown"}
    }
    source_agents.add(source_agent)
    raw_policy_version = metadata_value(body, "policy_version") or "1"
    try:
        policy_version = int(raw_policy_version)
    except ValueError:
        policy_version = 1
    if policy_version <= 0:
        policy_version = 1
    return source_agent, sorted(source_agents), policy_version


def without_candidate_metadata(body: str) -> str:
    return re.sub(
        r"^(?:-\s*)?(?:candidate_id|source_event|source_agent|source_agents|policy_version|identity|category):\s*.+$",
        "",
        body,
        flags=re.MULTILINE,
    ).strip()


def write_proposals_atomically(path: pathlib.Path, content: str) -> None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o644
    atomic_write_text(path, content, mode=mode)


def merge_candidate_source_agent(
    text: str, identity: str, source_agent: str
) -> tuple[str, str, list[str]] | None:
    headings = list(re.finditer(r"^### .+$", text, flags=re.MULTILINE))
    for index, heading_match in enumerate(headings):
        body_start = heading_match.end()
        body_end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        body = text[body_start:body_end]
        if metadata_value(body, "identity") != identity:
            continue
        _, source_agents, _ = candidate_provenance(body)
        merged_agents = sorted({*source_agents, source_agent})
        if merged_agents != source_agents:
            prefix = "- " if re.search(r"^- source_agent:", body, flags=re.MULTILINE) else ""
            value = ",".join(merged_agents)
            rendered = f"{prefix}source_agents: " + (f"`{value}`" if prefix else value)
            if re.search(r"^(?:-\s*)?source_agents:\s*.+$", body, flags=re.MULTILINE):
                body = re.sub(
                    r"^(?:-\s*)?source_agents:\s*.+$",
                    rendered,
                    body,
                    count=1,
                    flags=re.MULTILINE,
                )
            else:
                source_line = re.search(
                    r"^(?:-\s*)?source_agent:\s*.+$", body, flags=re.MULTILINE
                )
                if source_line is None:
                    raise ValueError("candidate provenance is missing source_agent")
                body = body[: source_line.end()] + "\n" + rendered + body[source_line.end() :]
        candidate_id = metadata_value(body, "memory_id") or metadata_value(body, "candidate_id")
        if not candidate_id:
            candidate_id = stable_id(
                "P", PROJECT_PROPOSALS, heading_match.group(0).strip(), body.strip()
            )
        updated = text[:body_start] + body + text[body_end:]
        return updated, candidate_id, merged_agents
    return None


def first_fenced_text(body: str) -> str:
    match = re.search(r"```(?:text|markdown|md)?\n(.*?)\n```", body, flags=re.S)
    if match:
        return match.group(1).strip()
    return body.strip()


def short_summary(text: str, max_len: int = 140) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    return clean[: max_len - 3] + "..." if len(clean) > max_len else clean


def normalize_memory(text: str) -> str:
    return re.sub(r"\W+", "", text.lower())


def candidate_identity(
    scope: str, target: str, category: str, title: str, summary: str
) -> str:
    value = json.dumps(
        [scope, target, category, normalize_memory(title), normalize_memory(summary)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def risk_flags(text: str) -> list[str]:
    return sorted(name for name, pattern in SENSITIVE_PATTERNS.items() if pattern.search(text))


def is_noise_personal_candidate(item: dict[str, Any]) -> bool:
    if item.get("scope") != "personal" or item.get("status", "pending") != "pending":
        return False
    content = item.get("content", "") or ""
    if any(pattern.search(content) for pattern in PERSONAL_NOISE_PATTERNS):
        return True
    if re.search(r"(项目|服务|代码|文件|部署|员工|接口|路径)", content, re.I) and not re.search(
        r"(跨项目|个人偏好|用户习惯|以后都|每次都|我的工作方式)", content
    ):
        return True
    if not re.search(
        r"(常用开发习惯|开发习惯|工作习惯|工作方式|协作方式|思维方式|用户画像|"
        r"偏好|习惯|以后|每次|总是|都需要|长期|短期|跨项目|审批机制)",
        content,
    ):
        return True
    if len(content) > 1800 and not re.search(
        r"(偏好|习惯|工作方式|协作方式|思维方式|用户画像|以后|每次|长期|短期)",
        content,
    ):
        return True
    return False


def is_project_checkpoint(heading: str, body: str) -> bool:
    return (
        "Review checkpoint from" in heading
        and (
            "Review whether this thread introduced stable project facts" in body
            or "Review whether this Claude Code thread introduced stable project facts" in body
        )
    )


def state_map() -> dict[str, Any]:
    state = read_json(PROJECT_STATE, {"items": {}, "last_reminder_at": ""})
    if not isinstance(state, dict):
        state = {"items": {}, "last_reminder_at": ""}
    state.setdefault("items", {})
    state.setdefault("last_reminder_at", "")
    if not PROJECT_STATE.exists():
        write_json(PROJECT_STATE, state)
    return state


def parse_project_candidates() -> list[dict[str, Any]]:
    text = read_text(PROJECT_PROPOSALS)
    items: list[dict[str, Any]] = []
    for heading, body in split_heading_sections(text):
        candidate_id = metadata_value(body, "candidate_id") or stable_id(
            "P", PROJECT_PROPOSALS, heading, body
        )
        created = heading.removeprefix("### ").split(" - ", 1)[0].strip()
        source_event = metadata_value(body, "source_event")
        source_agent, source_agents, policy_version = candidate_provenance(body)
        identity = metadata_value(body, "identity")
        category = metadata_value(body, "category")
        content = without_candidate_metadata(body)
        checkpoint = is_project_checkpoint(heading, content)
        items.append(
            {
                "id": candidate_id,
                "scope": "project",
                "target": "project_long",
                "review_kind": "checkpoint" if checkpoint else "memory",
                "actionable": not checkpoint,
                "source": "project_proposals",
                "source_event": source_event,
                "source_agent": source_agent,
                "source_agents": source_agents,
                "policy_version": policy_version,
                "identity": identity,
                "category": category,
                "source_path": str(PROJECT_PROPOSALS),
                "created_at": created,
                "title": heading.removeprefix("### ").strip(),
                "summary": short_summary(content),
                "content": content,
                "risk_flags": risk_flags(content),
            }
        )
    return items


def parse_personal_candidates() -> list[dict[str, Any]]:
    text = read_text(PERSONAL_PROPOSALS)
    items: list[dict[str, Any]] = []
    for heading, body in split_heading_sections(text):
        memory_id = metadata_value(body, "memory_id")
        if not memory_id:
            # Legacy unstructured proposals are preserved for audit but should
            # not be treated as pending approvals.
            continue
        proposal_status = metadata_value(body, "status") or "pending"
        if proposal_status != "pending":
            continue
        target = metadata_value(body, "target") or "unsure"
        created = metadata_value(body, "created") or heading.removeprefix("### ").strip()
        source_event = metadata_value(body, "source_event")
        source_agent, source_agents, policy_version = candidate_provenance(body)
        identity = metadata_value(body, "identity")
        category = metadata_value(body, "category")
        content = first_fenced_text(body)
        items.append(
            {
                "id": memory_id,
                "scope": "personal",
                "target": target,
                "review_kind": "memory",
                "actionable": True,
                "source": "personal_proposals",
                "source_event": source_event,
                "source_agent": source_agent,
                "source_agents": source_agents,
                "policy_version": policy_version,
                "identity": identity,
                "category": category,
                "source_path": str(PERSONAL_PROPOSALS),
                "created_at": created,
                "title": heading.removeprefix("### ").strip(),
                "summary": short_summary(content),
                "content": content,
                "risk_flags": risk_flags(content),
            }
        )
    return items


def build_queue() -> dict[str, Any]:
    queue_lock = PROJECT_QUEUE.with_suffix(PROJECT_QUEUE.suffix + ".lock")
    with exclusive_lock(queue_lock):
        state = state_map()
        decisions = state.get("items", {})
        items = parse_project_candidates() + parse_personal_candidates()
        for item in items:
            decision = decisions.get(item["id"], {})
            item["status"] = decision.get("status", "pending")
            item["decision"] = decision
        queue = {
            "generated_at": now(),
            "review_url": REVIEW_URL,
            "items": items,
            "counts": count_items(items),
        }
        write_json(PROJECT_QUEUE, queue)
        return queue


def create_agent_candidate(
    scope: str,
    target: str,
    category: str,
    title: str,
    summary: str,
    source_event: str = "agent_summary",
    *,
    source_agent: str = "unknown",
    policy_version: int = 1,
) -> dict[str, Any]:
    """Persist a candidate already distilled by the active Codex/Claude model."""
    title = re.sub(r"\s+", " ", title).strip()[:100]
    summary = summary.strip()
    if scope not in {"personal", "project"}:
        raise ValueError("scope must be personal or project")
    if target not in {"long", "short"}:
        raise ValueError("target must be long or short")
    if scope == "project" and target != "long":
        raise ValueError("project candidates must target long memory")
    if category not in AGENT_CATEGORIES[scope]:
        raise ValueError("unsupported candidate category")
    if source_agent not in {"codex", "claude-code", "unknown"}:
        raise ValueError("source_agent must be codex, claude-code, or unknown")
    if (
        not isinstance(policy_version, int)
        or isinstance(policy_version, bool)
        or policy_version <= 0
    ):
        raise ValueError("policy_version must be a positive integer")
    if not title or len(summary) < 12 or len(summary) > 1200:
        raise ValueError("candidate title/summary length is invalid")
    if risk_flags(f"{title}\n{summary}"):
        raise ValueError("candidate contains sensitive material")
    markdown = f"**标题：{title}**\n\n**分类：{AGENT_CATEGORIES[scope][category]}**\n\n{summary}"
    identity = candidate_identity(scope, target, category, title, summary)
    proposals = PERSONAL_PROPOSALS if scope == "personal" else PROJECT_PROPOSALS
    proposal_lock = proposals.with_suffix(proposals.suffix + ".lock")
    result: dict[str, Any]
    with exclusive_lock(proposal_lock):
        existing = read_text(proposals)
        merged = merge_candidate_source_agent(existing, identity, source_agent)
        if merged is not None:
            updated, candidate_id, source_agents = merged
            if updated != existing:
                write_proposals_atomically(proposals, updated)
            result = {
                "created": False,
                "reason": "duplicate",
                "id": candidate_id,
                "source_agents": source_agents,
            }
        else:
            if scope == "personal":
                approved_path = PERSONAL_LONG if target == "long" else PERSONAL_SHORT
            else:
                approved_path = PROJECT_LONG
            approved = read_text(approved_path)
            if normalize_memory(markdown) in normalize_memory(approved):
                result = {"created": False, "reason": "duplicate"}
            else:
                created = now()
                source_agents = sorted({source_agent})
                rendered_agents = ",".join(source_agents)
                if scope == "personal":
                    stamp = _dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
                    candidate_id = f"M-{stamp}"
                    suffix = 2
                    while candidate_id in existing:
                        candidate_id = f"M-{stamp}-{suffix}"
                        suffix += 1
                    body = (
                        f"memory_id: {candidate_id}\nstatus: pending\ntarget: {target}\n"
                        f"created: {created}\nsource_event: {source_event}\n"
                        f"source_agent: {source_agent}\nsource_agents: {rendered_agents}\n"
                        f"policy_version: {policy_version}\nidentity: {identity}\n"
                        f"category: {category}\n\ncandidate:\n\n```text\n{markdown}\n```\n\n"
                        "approval_rule: Promote only after explicit user approval of this exact content."
                    )
                    section = f"\n### {candidate_id}\n\n{body}\n"
                else:
                    heading = f"### {created} - {title}"
                    body_without_id = (
                        f"{markdown}\n\n"
                        f"- source_event: `{source_event}`\n"
                        f"- source_agent: `{source_agent}`\n"
                        f"- source_agents: `{rendered_agents}`\n"
                        f"- policy_version: `{policy_version}`\n"
                        f"- identity: `{identity}`\n"
                        f"- category: `{category}`"
                    )
                    candidate_id = stable_id(
                        "P", PROJECT_PROPOSALS, heading, body_without_id
                    )
                    body = (
                        f"{markdown}\n\n"
                        f"- candidate_id: `{candidate_id}`\n"
                        f"- source_event: `{source_event}`\n"
                        f"- source_agent: `{source_agent}`\n"
                        f"- source_agents: `{rendered_agents}`\n"
                        f"- policy_version: `{policy_version}`\n"
                        f"- identity: `{identity}`\n"
                        f"- category: `{category}`"
                    )
                    section = f"\n{heading}\n\n{body}\n"
                write_proposals_atomically(proposals, existing + section)
                result = {
                    "created": True,
                    "id": candidate_id,
                    "scope": scope,
                    "target": target,
                    "content": markdown,
                    "source_agents": source_agents,
                }
    build_queue()
    return result


def count_items(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "pending": 0,
        "actionable_pending": 0,
        "checkpoint_pending": 0,
        "project_pending": 0,
        "personal_pending": 0,
        "approved": 0,
        "rejected": 0,
        "deferred": 0,
    }
    for item in items:
        status = item.get("status", "pending")
        if status == "pending":
            counts["pending"] += 1
            if item.get("review_kind") == "checkpoint":
                counts["checkpoint_pending"] += 1
            elif item.get("actionable", True):
                counts["actionable_pending"] += 1
            if item.get("scope") == "project":
                counts["project_pending"] += 1
            if item.get("scope") == "personal":
                counts["personal_pending"] += 1
        elif status in counts:
            counts[status] += 1
    return counts


def load_queue(refresh: bool = True) -> dict[str, Any]:
    if refresh or not PROJECT_QUEUE.exists():
        return build_queue()
    queue = read_json(PROJECT_QUEUE, {"items": [], "counts": {}})
    if not isinstance(queue, dict):
        return build_queue()
    return queue


def find_item(candidate_id: str) -> dict[str, Any]:
    queue = load_queue(refresh=True)
    for item in queue.get("items", []):
        if item.get("id") == candidate_id:
            return item
    raise KeyError(f"Unknown memory candidate: {candidate_id}")


def memory_title(item: dict[str, Any], content: str) -> str:
    summary = item.get("summary") or short_summary(content, 80)
    summary = re.sub(r"[`#*_>\[\]]", "", summary).strip()
    return summary[:64] or item.get("id", "Approved memory")


def append_official_memory(path: pathlib.Path, item: dict[str, Any], content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    title = memory_title(item, content)
    date = _dt.datetime.now().astimezone().date().isoformat()
    entry = f"\n### {date} - {title}\n\n{content.strip()}\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry)


def decision_target_path(target: str) -> pathlib.Path:
    if target == "project_long":
        return PROJECT_LONG
    if target == "personal_long":
        return PERSONAL_LONG
    if target == "personal_short":
        return PERSONAL_SHORT
    raise ValueError(f"Unsupported approval target: {target}")


def approve(candidate_id: str, target: str | None = None, content: str | None = None) -> dict[str, Any]:
    item = find_item(candidate_id)
    if target is None:
        if item["scope"] == "project":
            target = "project_long"
        elif item.get("target") == "short":
            target = "personal_short"
        else:
            target = "personal_long"
    approved_content = (content if content is not None else item.get("content", "")).strip()
    if not approved_content:
        raise ValueError("Cannot approve empty memory content")
    if risk_flags(f"{item.get('title', '')}\n{approved_content}"):
        raise ValueError("Cannot approve candidate containing sensitive material")
    destination = decision_target_path(target)
    append_official_memory(destination, item, approved_content)
    record_decision(
        candidate_id,
        {
            "status": "approved",
            "approved_target": target,
            "approved_path": str(destination),
            "decided_at": now(),
            "risk_flags": item.get("risk_flags", []),
            "source_agent": item.get("source_agent", "unknown"),
            "source_agents": item.get("source_agents", [item.get("source_agent", "unknown")]),
            "policy_version": item.get("policy_version", 1),
        },
    )
    return find_item(candidate_id)


def record_decision(candidate_id: str, decision: dict[str, Any]) -> None:
    state = state_map()
    state["items"][candidate_id] = decision
    write_json(PROJECT_STATE, state)
    build_queue()


def reject(candidate_id: str) -> None:
    record_decision(candidate_id, {"status": "rejected", "decided_at": now()})


def defer(candidate_id: str) -> None:
    record_decision(candidate_id, {"status": "deferred", "decided_at": now()})


def reset(candidate_id: str) -> None:
    state = state_map()
    state.get("items", {}).pop(candidate_id, None)
    write_json(PROJECT_STATE, state)
    build_queue()


def reject_noise_personal_candidates(dry_run: bool = True) -> list[str]:
    queue = load_queue(refresh=True)
    items = [item for item in queue.get("items", []) if is_noise_personal_candidate(item)]
    ids = [item["id"] for item in items]
    if not dry_run:
        archive = CODEX_DIR / "memory_review_noise_personal.md"
        archive.parent.mkdir(parents=True, exist_ok=True)
        with archive.open("a", encoding="utf-8") as handle:
            for item in items:
                handle.write(f"\n### {item['id']} - quarantined personal candidate\n\n")
                handle.write(f"- original source: `{item.get('source_path', '')}`\n")
                handle.write(f"- original target: `{item.get('target', '')}`\n")
                handle.write(f"- quarantined_at: `{now()}`\n\n")
                handle.write(item.get("content", "").strip() + "\n")
        for candidate_id in ids:
            record_decision(
                candidate_id,
                {
                    "status": "rejected",
                    "reason": "noise_quarantined",
                    "quarantine_path": str(archive),
                    "decided_at": now(),
                },
            )
    return ids


def review_service_running(timeout: float = 0.6) -> bool:
    try:
        with urllib.request.urlopen(f"{REVIEW_URL}/health", timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def start_review_service_if_needed() -> bool:
    if review_service_running():
        return False
    server_script = APP_ROOT / "scripts" / "memory_review_server.py"
    log_path = CODEX_DIR / "memory_review_server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["MEMORY_REVIEW_PROJECT_ROOT"] = str(PROJECT_ROOT)
    with log_path.open("ab") as log_handle:
        subprocess.Popen(
            [sys.executable, str(server_script)],
            cwd=str(PROJECT_ROOT),
            stdout=log_handle,
            stderr=log_handle,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    for _ in range(10):
        time.sleep(0.2)
        if review_service_running():
            return True
    return False


def should_remind(queue: dict[str, Any], force: bool = False) -> bool:
    counts = queue.get("counts", {})
    if force:
        return True
    if counts.get("personal_pending", 0) > 3 or counts.get("project_pending", 0) > 5:
        return True
    state = state_map()
    last = state.get("last_reminder_at")
    if not last:
        return counts.get("pending", 0) > 0
    try:
        last_dt = _dt.datetime.fromisoformat(last)
    except ValueError:
        return True
    return (_dt.datetime.now().astimezone() - last_dt).total_seconds() >= 24 * 3600


def mark_reminded() -> None:
    state = state_map()
    state["last_reminder_at"] = now()
    write_json(PROJECT_STATE, state)


def review_summary(queue: dict[str, Any]) -> str:
    counts = queue.get("counts", {})
    return (
        f"pending={counts.get('pending', 0)}, "
        f"project={counts.get('project_pending', 0)}, "
        f"personal={counts.get('personal_pending', 0)}, "
        f"project_root={PROJECT_ROOT}, "
        f"url={REVIEW_URL}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and manage memory review queue")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("refresh")
    sub.add_parser("summary")
    sub.add_parser("ensure-server")
    args = parser.parse_args()

    if args.command == "refresh":
        queue = build_queue()
        print(review_summary(queue))
        return 0
    if args.command == "summary":
        queue = load_queue(refresh=True)
        print(review_summary(queue))
        return 0
    if args.command == "ensure-server":
        queue = build_queue()
        started = start_review_service_if_needed()
        print(f"started={started} {review_summary(queue)}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
