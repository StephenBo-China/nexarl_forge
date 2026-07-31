"""Read-only inventory for the current control-plane migration surface."""

from __future__ import annotations

import json
import pathlib
import re
from collections.abc import Mapping
from typing import Any

import memory_project
from memory_review_queue import count_items
import ui_design_preferences
from vibe_memory_paths import RuntimePaths


_MARKDOWN_SECTION_RE = re.compile(r"(?m)^#{2,6}\s+")


def _issue(path: pathlib.Path, error: Exception) -> dict[str, str]:
    return {"path": str(path), "error": str(error)}


def _read_text(path: pathlib.Path) -> tuple[str | None, dict[str, str] | None]:
    try:
        return path.read_text(encoding="utf-8"), None
    except FileNotFoundError:
        return None, None
    except (OSError, UnicodeDecodeError) as error:
        return None, _issue(path, error)


def _read_json(path: pathlib.Path) -> tuple[Any | None, dict[str, str] | None]:
    text, text_issue = _read_text(path)
    if text_issue is not None or text is None:
        return None, text_issue
    try:
        return json.loads(text), None
    except json.JSONDecodeError as error:
        return None, _issue(path, error)


def _markdown_summary(path: pathlib.Path) -> dict[str, Any]:
    text, issue = _read_text(path)
    if issue is not None:
        return {"path": str(path), "status": "error", "sections": 0, "error": issue["error"]}
    if text is None:
        return {"path": str(path), "status": "missing", "sections": 0}
    return {
        "path": str(path),
        "status": "ok",
        "sections": len(_MARKDOWN_SECTION_RE.findall(text)),
    }


def _json_summary(path: pathlib.Path) -> dict[str, Any]:
    value, issue = _read_json(path)
    if issue is not None:
        return {"path": str(path), "status": "error", "error": issue["error"]}
    if value is None:
        return {"path": str(path), "status": "missing"}
    if isinstance(value, dict):
        return {"path": str(path), "status": "ok", "schema_version": value.get("schema_version")}
    return {"path": str(path), "status": "invalid", "error": "JSON root must be an object"}


def _registry_projects(registry: Mapping[str, object]) -> list[dict[str, Any]]:
    projects = registry.get("projects", [])
    if not isinstance(projects, list):
        return []
    result: list[dict[str, Any]] = []
    for entry in projects:
        if isinstance(entry, dict):
            result.append(entry)
    return result


def valid_project_roots(registry: Mapping[str, object]) -> list[pathlib.Path]:
    roots: list[pathlib.Path] = []
    seen: set[str] = set()
    for entry in _registry_projects(registry):
        root = entry.get("root")
        if not isinstance(root, str) or not root.strip():
            continue
        resolved = pathlib.Path(root).expanduser().resolve()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        roots.append(resolved)
    return roots


def inspect_personal(personal_root: pathlib.Path) -> dict[str, Any]:
    files = {}
    errors: list[dict[str, str]] = []
    total_sections = 0
    for name in ("long.md", "short.md", "proposals.md"):
        summary = _markdown_summary(personal_root / name)
        files[name] = summary
        total_sections += int(summary.get("sections", 0))
        if summary.get("status") == "error":
            errors.append({"path": summary["path"], "error": summary["error"]})
    return {
        "root": str(personal_root),
        "files": files,
        "sections": total_sections,
        "errors": errors,
    }


def inspect_projects(
    project_roots: list[pathlib.Path], registry: Mapping[str, object]
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for root in project_roots:
        config_path = root / ".loop" / "config.json"
        loop_summary = _json_summary(config_path)
        if loop_summary.get("status") == "error":
            errors.append({"path": loop_summary["path"], "error": loop_summary["error"]})
        if loop_summary.get("status") == "ok" and not isinstance(
            loop_summary.get("schema_version"), int
        ):
            errors.append(
                {
                    "path": str(config_path),
                    "error": "loop config schema_version must be an integer",
                }
            )
            loop_summary = {**loop_summary, "status": "invalid"}
        items.append(
            {
                "name": memory_project.repo_name(root),
                "root": str(root),
                "is_git_repo": (root / ".git").exists(),
                "has_memory": all(
                    path.exists()
                    for path in (
                        root / "codex" / "codex_long_memory.md",
                        root / "codex" / "codex_short_memory.md",
                        root / "codex" / "memory_proposals.md",
                    )
                ),
                "has_loop": config_path.exists(),
                "loop": loop_summary,
                "ui_design_status": memory_project.ui_design_status(root),
                "ui_design_config": _json_summary(root / "codex" / "ui_design" / "config.json"),
            }
        )
    return {
        "schema_version": registry.get("schema_version", 1),
        "current_project": registry.get("current_project", ""),
        "registered": len(project_roots),
        "projects": items,
        "errors": errors,
    }


def inspect_review_state(project_roots: list[pathlib.Path]) -> dict[str, Any]:
    projects: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    total_counts = {
        "pending": 0,
        "actionable_pending": 0,
        "checkpoint_pending": 0,
        "project_pending": 0,
        "personal_pending": 0,
        "approved": 0,
        "rejected": 0,
        "deferred": 0,
    }
    total_queue_items = 0
    total_state_items = 0
    total_proposal_sections = 0
    for root in project_roots:
        proposal_summary = _markdown_summary(root / "codex" / "memory_proposals.md")
        if proposal_summary.get("status") == "error":
            errors.append(
                {"path": proposal_summary["path"], "error": proposal_summary["error"]}
            )
        queue_path = root / "codex" / "memory_review_queue.json"
        queue_value, queue_issue = _read_json(queue_path)
        if queue_issue is not None:
            errors.append(queue_issue)
            queue_counts = {key: 0 for key in total_counts}
            queue_items = 0
        else:
            queue_items = 0
            if isinstance(queue_value, dict):
                items = queue_value.get("items", [])
                if isinstance(items, list):
                    queue_items = len(items)
                    queue_counts = count_items(
                        [item for item in items if isinstance(item, dict)]
                    )
                else:
                    queue_counts = {key: 0 for key in total_counts}
            else:
                queue_counts = {key: 0 for key in total_counts}
        state_path = root / "codex" / "memory_review_state.json"
        state_value, state_issue = _read_json(state_path)
        if state_issue is not None:
            errors.append(state_issue)
            state_items = 0
        elif isinstance(state_value, dict):
            items = state_value.get("items", {})
            state_items = len(items) if isinstance(items, dict) else 0
        else:
            state_items = 0
        total_queue_items += queue_items
        total_state_items += state_items
        total_proposal_sections += int(proposal_summary.get("sections", 0))
        for key in total_counts:
            total_counts[key] += int(queue_counts.get(key, 0))
        projects.append(
            {
                "root": str(root),
                "proposals": proposal_summary,
                "queue": {
                    "path": str(queue_path),
                    "items": queue_items,
                    "counts": queue_counts,
                },
                "state": {"path": str(state_path), "items": state_items},
            }
        )
    return {
        "projects": projects,
        "proposal_sections": total_proposal_sections,
        "queue_items": total_queue_items,
        "state_items": total_state_items,
        "counts": total_counts,
        "errors": errors,
    }


def inspect_design_preferences(
    paths: RuntimePaths, project_roots: list[pathlib.Path]
) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    global_path = paths.ui_design_home / "preferences.json"
    global_value, global_issue = _read_json(global_path)
    if global_issue is not None:
        errors.append(global_issue)
        global_summary = {"path": str(global_path), "status": "error", "schema_version": None}
    elif global_value is None:
        global_summary = {"path": str(global_path), "status": "missing", "schema_version": None}
    else:
        try:
            validated = ui_design_preferences.validate_global_preferences(global_value)
        except Exception as error:
            errors.append(_issue(global_path, error))
            global_summary = {"path": str(global_path), "status": "invalid", "schema_version": None}
        else:
            global_summary = {
                "path": str(global_path),
                "status": "ok",
                "schema_version": 1,
                "groups": len(validated),
            }

    project_items: list[dict[str, Any]] = []
    with_overrides = 0
    schema_versions: dict[str, int] = {}
    for root in project_roots:
        path = root / "codex" / "ui_design" / "preferences.json"
        value, issue = _read_json(path)
        if issue is not None:
            errors.append(issue)
            summary = {"path": str(path), "status": "error", "schema_version": None}
        elif value is None:
            summary = {"path": str(path), "status": "missing", "schema_version": None}
        else:
            overrides: Any
            schema_version = None
            if isinstance(value, dict) and "overrides" in value:
                schema_version = value.get("schema_version")
                overrides = value["overrides"]
            else:
                overrides = value
                schema_version = 1 if isinstance(value, dict) else None
            try:
                validated = ui_design_preferences.validate_project_overrides(overrides)
            except Exception as error:
                errors.append(_issue(path, error))
                summary = {"path": str(path), "status": "invalid", "schema_version": schema_version}
            else:
                if schema_version is not None:
                    schema_versions[str(schema_version)] = schema_versions.get(str(schema_version), 0) + 1
                if validated:
                    with_overrides += 1
                summary = {
                    "path": str(path),
                    "status": "ok",
                    "schema_version": schema_version,
                    "overrides": len(validated),
                }
        project_items.append({"root": str(root), "preferences": summary})

    return {
        "global": global_summary,
        "projects": {
            "items": project_items,
            "count": len(project_roots),
            "with_overrides": with_overrides,
            "schema_versions": schema_versions,
        },
        "errors": errors,
    }


def inspect_ui_design_approvals(project_roots: list[pathlib.Path]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    package_approvals = 0
    project_global_approvals = 0
    for root in project_roots:
        path = root / "codex" / "ui_design" / "approvals.json"
        value, issue = _read_json(path)
        if issue is not None:
            errors.append(issue)
            summary = {"path": str(path), "status": "error"}
        elif value is None:
            summary = {"path": str(path), "status": "missing"}
        elif not isinstance(value, dict):
            summary = {"path": str(path), "status": "invalid", "error": "JSON root must be an object"}
            errors.append(_issue(path, ValueError("JSON root must be an object")))
        else:
            packages = value.get("package_approvals", {})
            if not isinstance(packages, dict):
                errors.append(_issue(path, ValueError("package_approvals must be an object")))
                summary = {"path": str(path), "status": "invalid"}
            else:
                package_approvals += len(packages)
                project_global = value.get("project_global_approval")
                if project_global is not None:
                    project_global_approvals += 1
                summary = {
                    "path": str(path),
                    "status": "ok",
                    "schema_version": value.get("schema_version"),
                    "package_approvals": len(packages),
                    "project_global_approval": project_global is not None,
                }
        items.append({"root": str(root), "approvals": summary})
    return {
        "schema_version": 1,
        "projects": items,
        "package_approvals": package_approvals,
        "project_global_approvals": project_global_approvals,
        "errors": errors,
    }


def inspect_ui_skills(ui_design_home: pathlib.Path) -> dict[str, Any]:
    path = ui_design_home / "registry.json"
    value, issue = _read_json(path)
    if issue is not None:
        return {
            "path": str(path),
            "status": "error",
            "schema_version": None,
            "drafts": 0,
            "published": 0,
            "deployments": 0,
            "idempotency": 0,
            "errors": [issue],
        }
    if value is None:
        return {
            "path": str(path),
            "status": "missing",
            "schema_version": None,
            "drafts": 0,
            "published": 0,
            "deployments": 0,
            "idempotency": 0,
            "errors": [],
        }
    if not isinstance(value, dict):
        return {
            "path": str(path),
            "status": "invalid",
            "schema_version": None,
            "drafts": 0,
            "published": 0,
            "deployments": 0,
            "idempotency": 0,
            "errors": [_issue(path, ValueError("JSON root must be an object"))],
        }
    errors: list[dict[str, str]] = []
    drafts = value.get("drafts", {})
    packages = value.get("packages", {})
    deployments = value.get("deployments", {})
    idempotency = value.get("idempotency", {})
    for key, item in (
        ("drafts", drafts),
        ("packages", packages),
        ("deployments", deployments),
        ("idempotency", idempotency),
    ):
        if not isinstance(item, dict):
            errors.append(_issue(path, ValueError(f"{key} must be an object")))
    if errors:
        return {
            "path": str(path),
            "status": "invalid",
            "schema_version": value.get("schema_version"),
            "drafts": 0,
            "published": 0,
            "deployments": 0,
            "idempotency": 0,
            "errors": errors,
        }
    published = 0
    for versions in packages.values():
        if isinstance(versions, list):
            published += len(versions)
    return {
        "path": str(path),
        "status": "ok",
        "schema_version": value.get("schema_version"),
        "drafts": len(drafts),
        "published": published,
        "deployments": len(deployments),
        "idempotency": len(idempotency),
        "errors": [],
    }


def inspect_loop(project_roots: list[pathlib.Path]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    schema_versions: dict[str, int] = {}
    configured = 0
    for root in project_roots:
        path = root / ".loop" / "config.json"
        value, issue = _read_json(path)
        if issue is not None:
            errors.append(issue)
            summary = {"path": str(path), "status": "error", "schema_version": None}
        elif value is None:
            summary = {"path": str(path), "status": "missing", "schema_version": None}
        elif not isinstance(value, dict):
            summary = {"path": str(path), "status": "invalid", "schema_version": None}
            errors.append(_issue(path, ValueError("JSON root must be an object")))
        else:
            schema_version = value.get("schema_version")
            if isinstance(schema_version, int):
                configured += 1
                schema_versions[str(schema_version)] = schema_versions.get(str(schema_version), 0) + 1
                summary = {
                    "path": str(path),
                    "status": "ok",
                    "schema_version": schema_version,
                    "loop_enabled": bool(value.get("loop_enabled")),
                }
            else:
                summary = {"path": str(path), "status": "invalid", "schema_version": schema_version}
                errors.append(
                    _issue(path, ValueError("loop config schema_version must be an integer"))
                )
        items.append({"root": str(root), "config": summary})
    return {
        "projects": items,
        "configured": configured,
        "schema_versions": schema_versions,
        "errors": errors,
    }


def inspect_worktrees(worktree_manager: pathlib.Path) -> dict[str, Any]:
    path = worktree_manager / "tasks.json"
    value, issue = _read_json(path)
    if issue is not None:
        return {
            "path": str(path),
            "status": "error",
            "schema_version": None,
            "tasks": 0,
            "repositories": 0,
            "statuses": {},
            "errors": [issue],
        }
    if value is None:
        return {
            "path": str(path),
            "status": "missing",
            "schema_version": None,
            "tasks": 0,
            "repositories": 0,
            "statuses": {},
            "errors": [],
        }
    if not isinstance(value, dict):
        return {
            "path": str(path),
            "status": "invalid",
            "schema_version": None,
            "tasks": 0,
            "repositories": 0,
            "statuses": {},
            "errors": [_issue(path, ValueError("JSON root must be an object"))],
        }
    tasks = value.get("tasks", {})
    errors: list[dict[str, str]] = []
    if not isinstance(tasks, dict):
        errors.append(_issue(path, ValueError("tasks must be an object")))
        tasks = {}
    status_counts: dict[str, int] = {}
    repositories: set[str] = set()
    for item in tasks.values():
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "unknown"))
        status_counts[status] = status_counts.get(status, 0) + 1
        repository = item.get("repository")
        if isinstance(repository, str) and repository.strip():
            repositories.add(repository)
    return {
        "path": str(path),
        "status": "ok" if not errors else "invalid",
        "schema_version": value.get("schema_version"),
        "tasks": len(tasks),
        "repositories": len(repositories),
        "statuses": status_counts,
        "errors": errors,
    }


def inspect_legacy_hooks(project_roots: list[pathlib.Path]) -> dict[str, Any]:
    projects: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    script_files = 0
    config_documents = 0
    references = 0
    for root in project_roots:
        files = {
            ".codex/hooks/shared_memory_hook.py": root / ".codex" / "hooks" / "shared_memory_hook.py",
            ".claude/hooks/shared_memory_hook.py": root / ".claude" / "hooks" / "shared_memory_hook.py",
            ".codex/hooks/ui_design_gate_hook.py": root / ".codex" / "hooks" / "ui_design_gate_hook.py",
            ".claude/hooks/ui_design_gate_hook.py": root / ".claude" / "hooks" / "ui_design_gate_hook.py",
        }
        documents = {
            ".codex/hooks.json": root / ".codex" / "hooks.json",
            ".claude/settings.json": root / ".claude" / "settings.json",
        }
        file_statuses = {}
        document_statuses = {}
        project_script_files = 0
        project_references = 0
        for label, path in files.items():
            exists = path.exists()
            file_statuses[label] = {"path": str(path), "exists": exists}
            if exists:
                project_script_files += 1
        for label, path in documents.items():
            value, issue = _read_json(path)
            if issue is not None:
                errors.append(issue)
                document_statuses[label] = {"path": str(path), "status": "error"}
                continue
            if value is None:
                document_statuses[label] = {"path": str(path), "status": "missing"}
                continue
            if not isinstance(value, dict):
                document_statuses[label] = {"path": str(path), "status": "invalid"}
                errors.append(_issue(path, ValueError("JSON root must be an object")))
                continue
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
            reference_count = int("shared_memory_hook.py" in encoded) + int(
                "ui_design_gate_hook.py" in encoded
            )
            project_references += reference_count
            document_statuses[label] = {
                "path": str(path),
                "status": "ok",
                "references": reference_count,
            }
        script_files += project_script_files
        config_documents += sum(
            1 for item in document_statuses.values() if item.get("status") == "ok"
        )
        references += project_references
        projects.append(
            {
                "root": str(root),
                "script_files": file_statuses,
                "documents": document_statuses,
                "script_file_count": project_script_files,
                "reference_count": project_references,
            }
        )
    return {
        "projects": projects,
        "script_files": script_files,
        "documents": config_documents,
        "references": references,
        "errors": errors,
    }


def inventory(paths: RuntimePaths, registry: Mapping[str, object]) -> dict[str, Any]:
    project_roots = valid_project_roots(registry)
    return {
        "personal_memory": inspect_personal(paths.personal_memory),
        "projects": inspect_projects(project_roots, registry),
        "memory_review": inspect_review_state(project_roots),
        "design_preferences": inspect_design_preferences(paths, project_roots),
        "ui_design_approvals": inspect_ui_design_approvals(project_roots),
        "ui_skills": inspect_ui_skills(paths.ui_design_home),
        "loop": inspect_loop(project_roots),
        "worktrees": inspect_worktrees(paths.worktree_manager),
        "legacy_hooks": inspect_legacy_hooks(project_roots),
    }
