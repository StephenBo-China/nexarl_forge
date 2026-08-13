#!/usr/bin/env python3
"""Local-only web UI for memory approval."""

from __future__ import annotations

import argparse
import json
import importlib
import os
import pathlib
import re
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import memory_project
import memory_review_queue as review
import ui_design_cli
import ui_design_gate
import ui_design_preferences
import ui_skill_publisher
import ui_skill_registry
import vibe_memory_paths
import vibe_memory_settings

MAX_JSON_BODY = 64 * 1024


def server_address(environ: dict[str, str] | None = None) -> tuple[str, int]:
    values = os.environ if environ is None else environ
    host = values.get("MEMORY_REVIEW_HOST", review.REVIEW_HOST)
    port_raw = values.get("MEMORY_REVIEW_PORT", str(review.REVIEW_PORT))
    try:
        port = int(port_raw)
    except (TypeError, ValueError) as error:
        raise ValueError("memory review server port must be an integer") from error
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("memory review server must bind to loopback")
    return host, port


HOST, PORT = server_address()
review.REVIEW_HOST = HOST
review.REVIEW_PORT = PORT
review.REVIEW_URL = f"http://[{HOST}]:{PORT}" if HOST == "::1" else f"http://{HOST}:{PORT}"


def health_payload() -> dict[str, object]:
    manifest = vibe_memory_paths.read_release_manifest(review.APP_ROOT / "release.json")
    return {
        "ok": True,
        "service": "vibe-memory",
        "app_version": manifest["app_version"],
        "data_schema_version": manifest["data_schema_version"],
    }


def settings_payload() -> dict[str, object]:
    return vibe_memory_settings.load_settings(vibe_memory_paths.for_home())


def save_first_run_settings(body: dict[str, object]) -> dict[str, object]:
    paths = vibe_memory_paths.for_home()
    return vibe_memory_settings.apply_first_run(
        paths,
        body,
        manager_source_root=pathlib.Path(__file__).resolve().parents[1],
        register_workspace=memory_project.register_project,
    )


def first_run_page() -> str:
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>设置 Vibe Memory</title><style>
:root{color-scheme:dark;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;background:#171717;color:#ececec}body{margin:0;display:grid;min-height:100vh;place-items:center}.wizard{width:min(680px,calc(100vw - 32px));background:#202020;border:1px solid #333;border-radius:12px;padding:28px;box-shadow:0 18px 60px #0006}h1{font-size:24px;margin:0 0 8px}p{color:#aaa;line-height:1.6}.row{display:flex;justify-content:space-between;gap:20px;padding:14px 0;border-top:1px solid #303030}.stack{display:grid;gap:8px}.radios{display:flex;gap:14px;flex-wrap:wrap}input[type=text],input[type=number]{background:#2b2b2b;border:1px solid #444;border-radius:7px;color:#fff;padding:9px;width:min(380px,70vw)}button{margin-top:18px;background:#eee;border:0;border-radius:8px;color:#171717;font-weight:650;padding:11px 16px}.message{min-height:20px;color:#8ab4ff}</style></head>
<body><form class="wizard" id="first-run"><h1>首次运行设置</h1><p>配置本机客户端与保留策略。工作区必须手动输入一个已存在的目录；浏览器无法打开原生目录选择器。</p>
<label class="row"><span>Codex hooks</span><input name="codex_hooks" type="checkbox" checked></label>
<label class="row"><span>Claude Code hooks</span><input name="claude_hooks" type="checkbox"></label>
<label class="row"><span>自动候选检查</span><input name="automatic_candidate_checks" type="checkbox" checked></label>
<div class="row"><span>个人短记忆保留</span><div class="radios"><label><input name="personal_short_retention_days" type="radio" value="0">不保留</label><label><input name="personal_short_retention_days" type="radio" value="14">14 天</label><label><input name="personal_short_retention_days" type="radio" value="30" checked>30 天</label></div></div>
<label class="row"><span>登录时启动</span><input name="start_at_login" type="checkbox" checked></label>
<label class="row"><span>本机端口</span><input name="service_port" type="number" min="1" max="65535" value="8897" required></label>
<label class="row stack"><span>可选工作区路径</span><input name="workspace" type="text" placeholder="/path/to/workspace"></label>
<div class="message" id="message"></div><button type="submit">保存并继续</button></form>
<script>const form=document.getElementById('first-run'),msg=document.getElementById('message');form.addEventListener('submit',async e=>{e.preventDefault();msg.textContent='正在保存…';const f=new FormData(form),body={codex_hooks:f.has('codex_hooks'),claude_hooks:f.has('claude_hooks'),automatic_candidate_checks:f.has('automatic_candidate_checks'),personal_short_retention_days:Number(f.get('personal_short_retention_days')),start_at_login:f.has('start_at_login'),service_port:Number(f.get('service_port')),workspace:String(f.get('workspace')||'').trim()};try{const r=await fetch('/api/settings/first-run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),data=await r.json();if(!r.ok)throw new Error(data.error||'保存失败');location.href='/'}catch(error){msg.textContent=error.message}}</script></body></html>"""


def service_action_path(paths: vibe_memory_paths.RuntimePaths) -> pathlib.Path:
    return pathlib.Path(paths.install_root) / "state" / "service-action.json"


def read_service_action(paths: vibe_memory_paths.RuntimePaths) -> dict[str, object]:
    path = service_action_path(paths)
    if path.is_symlink():
        raise ValueError("service action state must not be a symlink")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError("service action state must be an object")
    return value


def write_service_action(paths: vibe_memory_paths.RuntimePaths, *, desired: bool) -> dict[str, object]:
    value = {
        "generation": uuid.uuid4().hex,
        "desired_start_at_login": desired,
        "status": "active" if desired else "bootout_pending",
    }
    vibe_memory_settings.vibe_memory_install._atomic_write_private_json(service_action_path(paths), value)
    return value


def _service_action_matches(paths: vibe_memory_paths.RuntimePaths, generation: str, desired: bool) -> bool:
    value = read_service_action(paths)
    return value.get("generation") == generation and value.get("desired_start_at_login") is desired


def complete_scheduled_bootout(paths: vibe_memory_paths.RuntimePaths, generation: str) -> None:
    action_path = service_action_path(paths)
    if not _service_action_matches(paths, generation, False):
        return
    try:
        vibe_memory_settings.vibe_memory_install.bootout_launch_agent()
    except Exception as error:  # Persist a retry-visible diagnostic.
        if _service_action_matches(paths, generation, False):
            value = read_service_action(paths)
            value["error"] = str(error)[:500]
            vibe_memory_settings.vibe_memory_install._atomic_write_private_json(action_path, value)
    else:
        if _service_action_matches(paths, generation, False):
            action_path.unlink(missing_ok=True)


def scheduled_bootout_worker(paths: vibe_memory_paths.RuntimePaths, generation: str) -> None:
    try:
        complete_scheduled_bootout(paths, generation)
    except Exception as error:  # Never leak a background traceback.
        try:
            if _service_action_matches(paths, generation, False):
                value = read_service_action(paths)
                value["error"] = str(error)[:500]
                vibe_memory_settings.vibe_memory_install._atomic_write_private_json(service_action_path(paths), value)
        except Exception:
            return


UI_DESIGN_GET_ROUTES = {
    "/api/ui-design/context",
    "/api/ui-design/project-config",
    "/api/ui-design/packages",
    "/api/ui-skills",
}
UI_DESIGN_POST_ROUTES = {
    "/api/ui-design/preferences/global",
    "/api/ui-design/preferences/project",
    "/api/ui-design/project-config/set-mode",
    "/api/ui-design/project-config/set-paths",
    "/api/ui-design/project-config/enable-hard-gate",
    "/api/ui-design/project-config/relock",
    "/api/ui-design/packages/create",
    "/api/ui-design/packages/revise",
    "/api/ui-design/packages/approve",
    "/api/ui-design/packages/reject",
    "/api/ui-design/packages/request-revision",
    "/api/ui-design/packages/invalidate",
    "/api/ui-design/baseline/approve",
    "/api/ui-skills/import",
    "/api/ui-skills/validate",
    "/api/ui-skills/request-revision",
    "/api/ui-skills/approve",
    "/api/ui-skills/publish",
    "/api/ui-skills/rollback",
    "/api/ui-skills/disable",
    "/api/ui-skills/reject",
    "/api/ui-skills/scan",
    "/api/ui-skills/ignore-unmanaged",
}
UI_DESIGN_CONFIRMED_ROUTES = {
    "/api/ui-design/project-config/set-mode",
    "/api/ui-design/project-config/enable-hard-gate",
    "/api/ui-design/project-config/relock",
    "/api/ui-design/packages/approve",
    "/api/ui-design/packages/reject",
    "/api/ui-design/packages/invalidate",
    "/api/ui-design/baseline/approve",
    "/api/ui-skills/approve",
    "/api/ui-skills/publish",
    "/api/ui-skills/rollback",
    "/api/ui-skills/disable",
    "/api/ui-skills/reject",
}


def ui_design_error_status(error: Exception) -> int:
    if isinstance(error, PermissionError):
        return 403
    if isinstance(
        error,
        (
            ui_skill_registry.DigestConflict,
            ui_skill_registry.InvalidTransition,
            ui_skill_publisher.IdempotencyConflict,
            ui_skill_publisher.TargetDigestConflict,
            ui_design_gate.DigestConflict,
            ui_design_gate.IdempotencyConflict,
        ),
    ):
        return 409
    if isinstance(error, ui_skill_registry.RegistryError) and "idempotency" in str(error):
        return 409
    if isinstance(error, ui_design_gate.DesignPackageNotFound):
        return 404
    if isinstance(
        error,
        (
            ValueError,
            ui_design_preferences.PreferenceValidationError,
            ui_design_gate.GateValidationError,
        ),
    ):
        return 400
    return 500


def _require_idempotency(body: dict) -> str:
    key = body.get("idempotency_key")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("idempotency_key is required")
    return key.strip()


def _require_confirmation(path: str, body: dict) -> None:
    if path in UI_DESIGN_CONFIRMED_ROUTES and body.get("confirmed") is not True:
        raise PermissionError("explicit confirmation is required")


def _skill_namespace(command: str, **values: object) -> argparse.Namespace:
    defaults = {
        "command": "ui-skill",
        "ui_skill_command": command,
        "draft_id": None,
        "name": None,
        "digest": None,
        "reason": "",
        "version": None,
        "project": None,
        "github": None,
        "local": None,
        "zip": None,
        "editor_json": None,
        "path": None,
        "revision": None,
        "scope": "global",
        "targets": "codex,claude",
        "version_label": "1.0.0",
        "idempotency_key": None,
    }
    defaults.update(values)
    return argparse.Namespace(**defaults)


def _project_from(value: object) -> pathlib.Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("project is required")
    return pathlib.Path(value).expanduser()


def _design_namespace(command: str, **values: object) -> argparse.Namespace:
    defaults = {
        "command": "ui-design",
        "ui_design_command": command,
        "project_config_command": None,
        "package_command": None,
        "baseline_command": None,
        "project": None,
        "mode": None,
        "json_file": None,
        "manifest": None,
        "task": None,
        "digest": None,
        "reason": "",
        "confirmed": False,
        "idempotency_key": None,
    }
    defaults.update(values)
    return argparse.Namespace(**defaults)


def ui_design_get(path: str, query: dict[str, object]) -> dict:
    if path == "/api/ui-design/context":
        raw_project = query.get("project") or str(review.PROJECT_ROOT)
        if isinstance(raw_project, list):
            raw_project = raw_project[0] if raw_project else ""
        project = pathlib.Path(str(raw_project)).expanduser()
        return {
            "project": str(project),
            "global_preferences": ui_design_preferences.load_global_preferences(),
            "project_preferences": ui_design_preferences.load_project_overrides(project),
            "effective_preferences": ui_design_preferences.effective_preferences(project),
        }
    if path in {"/api/ui-design/project-config", "/api/ui-design/packages"}:
        raw_project = query.get("project") or str(review.PROJECT_ROOT)
        if isinstance(raw_project, list):
            raw_project = raw_project[0] if raw_project else ""
        project = _project_from(raw_project)
        if path == "/api/ui-design/packages":
            return ui_design_cli.dispatch(
                _design_namespace(
                    "package", package_command="list", project=str(project)
                )
            )
        config = ui_design_cli.dispatch(
            _design_namespace(
                "project-config", project_config_command="show", project=str(project)
            )
        )
        return {
            "config": config,
            "gate_status": ui_design_gate.gate_status(project),
            "packages": ui_design_gate.list_design_packages(project),
            "audit": ui_design_gate.audit_history(project),
        }
    if path == "/api/ui-skills":
        raw_project = query.get("project")
        if isinstance(raw_project, list):
            raw_project = raw_project[0] if raw_project else None
        scan = ui_design_cli.dispatch(
            _skill_namespace("scan", project=str(raw_project) if raw_project else None)
        )
        items = []
        for draft in ui_skill_registry.list_drafts():
            item = ui_design_cli.dispatch(
                _skill_namespace("show", draft_id=draft["id"])
            )
            deployment = ui_skill_registry.latest_deployment(item["name"])
            applies = deployment and (
                deployment.get("digest") == item.get("digest")
                or (
                    deployment.get("status") == "disabled"
                    and item.get("status") == "published"
                )
            )
            item["deployment"] = deployment if applies else None
            item["deployment_status"] = deployment.get("status") if applies else None
            items.append(item)
        return {"items": items, "discovered": scan.get("items", [])}
    raise ValueError(f"unknown UI design GET route: {path}")


def ui_design_post(path: str, body: dict) -> dict:
    if path not in UI_DESIGN_POST_ROUTES:
        raise ValueError(f"unknown UI design POST route: {path}")
    key = _require_idempotency(body)
    _require_confirmation(path, body)

    if path.startswith("/api/ui-design/preferences/"):
        value = body.get("value")
        if not isinstance(value, dict):
            raise ValueError("value must be an object")
        with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            command = "set-global" if path.endswith("/global") else "set-project"
            args = argparse.Namespace(
                command="ui-design",
                ui_design_command="preferences",
                preference_command=command,
                json_file=handle.name,
                project=body.get("project"),
                idempotency_key=key,
            )
            if command == "set-project" and not body.get("project"):
                raise ValueError("project is required")
            return ui_design_cli.dispatch(args)

    if path.startswith("/api/ui-design/project-config/"):
        project = _project_from(body.get("project"))
        command = path.rsplit("/", 1)[-1]
        if command == "set-paths":
            paths = body.get("paths")
            if not isinstance(paths, dict):
                raise ValueError("paths must be an object")
            with tempfile.NamedTemporaryFile(
                "w", suffix=".json", encoding="utf-8"
            ) as handle:
                json.dump(paths, handle, ensure_ascii=False)
                handle.flush()
                return ui_design_cli.dispatch(
                    _design_namespace(
                        "project-config",
                        project_config_command=command,
                        project=str(project),
                        json_file=handle.name,
                        idempotency_key=key,
                    )
                )
        return ui_design_cli.dispatch(
            _design_namespace(
                "project-config",
                project_config_command=command,
                project=str(project),
                mode=body.get("mode"),
                confirmed=body.get("confirmed") is True,
                idempotency_key=key,
            )
        )

    if path.startswith("/api/ui-design/packages/"):
        project = _project_from(body.get("project"))
        command = path.rsplit("/", 1)[-1]
        if command in {"create", "revise"}:
            manifest = body.get("manifest")
            if not isinstance(manifest, dict):
                raise ValueError("manifest must be an object")
            with tempfile.NamedTemporaryFile(
                "w", suffix=".json", encoding="utf-8"
            ) as handle:
                json.dump(manifest, handle, ensure_ascii=False)
                handle.flush()
                return ui_design_cli.dispatch(
                    _design_namespace(
                        "package",
                        package_command=command,
                        project=str(project),
                        task=body.get("task_id"),
                        manifest=handle.name,
                        idempotency_key=key,
                    )
                )
        return ui_design_cli.dispatch(
            _design_namespace(
                "package",
                package_command=command,
                project=str(project),
                task=body.get("task_id"),
                digest=body.get("digest"),
                reason=body.get("reason", ""),
                confirmed=body.get("confirmed") is True,
                idempotency_key=key,
            )
        )

    if path == "/api/ui-design/baseline/approve":
        project = _project_from(body.get("project"))
        return ui_design_cli.dispatch(
            _design_namespace(
                "baseline",
                baseline_command="approve",
                project=str(project),
                task=body.get("task_id"),
                digest=body.get("digest"),
                confirmed=body.get("confirmed") is True,
                idempotency_key=key,
            )
        )

    command = path.rsplit("/", 1)[-1].replace("request-revision", "request-revision")
    values = {
        "idempotency_key": key,
        "project": body.get("project"),
        "draft_id": body.get("draft_id"),
        "name": body.get("name"),
        "digest": body.get("digest"),
        "reason": body.get("reason", ""),
        "version": body.get("version"),
    }
    if command == "import":
        source = body.get("source")
        if not isinstance(source, dict):
            raise ValueError("source must be an object")
        source_type = source.get("type")
        values.update(
            {
                "scope": body.get("scope", "global"),
                "targets": ",".join(body.get("targets", ["codex", "claude"])),
                "version_label": body.get("version_label", "1.0.0"),
            }
        )
        if source_type == "local":
            values["local"] = source.get("path")
            return ui_design_cli.dispatch(_skill_namespace(command, **values))
        if source_type == "zip":
            values["zip"] = source.get("path")
            return ui_design_cli.dispatch(_skill_namespace(command, **values))
        if source_type == "github":
            values.update(
                github=source.get("repo"),
                path=source.get("path"),
                revision=source.get("revision"),
            )
            return ui_design_cli.dispatch(_skill_namespace(command, **values))
        if source_type == "editor":
            files = source.get("files")
            if not isinstance(files, dict):
                raise ValueError("editor source files must be an object")
            with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8") as handle:
                json.dump(files, handle, ensure_ascii=False)
                handle.flush()
                values["editor_json"] = handle.name
                return ui_design_cli.dispatch(_skill_namespace(command, **values))
        raise ValueError(f"unsupported source type: {source_type}")
    return ui_design_cli.dispatch(_skill_namespace(command, **values))


def project_operation(operation: str, body: dict) -> dict:
    project_root = body.get("project_root")
    if not isinstance(project_root, str) or not project_root.strip():
        raise ValueError("project_root is required")
    root = pathlib.Path(project_root).expanduser().resolve()
    if not (root / ".git").exists():
        raise ValueError(f"Git repository is required: {root}")
    port = body.get("port")
    selected_port = int(port) if port else None
    config_path = root / ".loop" / "config.json"
    if operation == "init-loop":
        if config_path.exists():
            raise FileExistsError(
                "Loop config already exists; use preview-loop-upgrade before upgrade-loop"
            )
        return memory_project.init_loop(root, selected_port)
    if operation == "preview-loop-upgrade":
        return memory_project.preview_loop_upgrade(root, selected_port)
    if operation == "upgrade-loop":
        if body.get("confirmed") is not True:
            raise PermissionError("explicit upgrade confirmation is required")
        return memory_project.upgrade_loop(root, selected_port)
    if operation == "upgrade-memory":
        if body.get("confirmed") is not True:
            raise PermissionError("explicit memory upgrade confirmation is required")
        return memory_project.upgrade_memory(root)
    raise ValueError(f"Unknown project operation: {operation}")


def project_error_status(error: Exception) -> int:
    if isinstance(error, FileExistsError):
        return 409
    if isinstance(error, PermissionError):
        return 403
    if isinstance(error, ValueError):
        return 400
    return 500


def switch_project(project_root: str) -> dict:
    global review
    root = str(pathlib.Path(project_root).expanduser().resolve())
    memory_project.set_current_project(root)
    os.environ["MEMORY_REVIEW_PROJECT_ROOT"] = root
    review = importlib.reload(review)
    review.build_queue()
    return project_payload()


def project_payload() -> dict:
    data = memory_project.list_projects()
    current = str(review.PROJECT_ROOT)
    if current and all(item.get("root") != current for item in data.get("projects", [])):
        data = memory_project.register_project(current, make_current=True)
    return {
        "current_project": str(review.PROJECT_ROOT),
        "registry": data,
        "recommend_port": memory_project.recommend_port(),
        "project": memory_project.project_entry(review.PROJECT_ROOT),
    }


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def read_text(path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def short_summary(text: str, max_len: int = 160) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    return clean[: max_len - 3] + "..." if len(clean) > max_len else clean


def split_memory_sections(text: str) -> list[dict]:
    pattern = r"^#{2,3} .+$"
    matches = list(re.finditer(pattern, text, flags=re.MULTILINE))
    if not matches:
        clean = text.strip()
        return [{"title": "全文", "content": clean, "summary": short_summary(clean)}] if clean else []
    sections = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        title = match.group(0).strip()
        body = text[match.end() : end].strip()
        if not body:
            continue
        content = f"{title}\n\n{body}".strip()
        sections.append(
            {
                "original_index": index,
                "start": match.start(),
                "end": end,
                "title": re.sub(r"^#{2,3}\s+", "", title).strip(),
                "content": content,
                "summary": short_summary(body or title),
            }
        )
    return sections


def active_memory_sources() -> list[dict]:
    return [
        {
            "id": "project_long",
            "label": "项目长期记忆",
            "path": review.PROJECT_LONG,
            "description": "已批准沉淀的项目稳定事实、架构决策、产品方向和部署规则。",
            "latest_first": False,
            "limit": None,
        },
        {
            "id": "project_short",
            "label": "项目短期记忆",
            "path": review.CODEX_DIR / "codex_short_memory.md",
            "description": "项目近期会话、hook 事件和临时工作状态。默认展示最近 120 条。",
            "latest_first": True,
            "limit": 120,
        },
        {
            "id": "personal_long",
            "label": "个人长期记忆",
            "path": review.PERSONAL_LONG,
            "description": "已批准的跨项目长期偏好、协作规则和稳定个人上下文。",
            "latest_first": False,
            "limit": None,
        },
        {
            "id": "personal_short",
            "label": "个人短期记忆",
            "path": review.PERSONAL_SHORT,
            "description": "已批准的跨项目短期个人上下文。",
            "latest_first": False,
            "limit": None,
        },
    ]


def active_memory_source(source_id: str) -> dict:
    for source in active_memory_sources():
        if source["id"] == source_id:
            return source
    raise ValueError(f"Unknown active memory source: {source_id}")


def active_memory_payload() -> dict:
    sources = active_memory_sources()
    result = []
    for source in sources:
        text = read_text(source["path"])
        sections = split_memory_sections(text)
        total = len(sections)
        if source["latest_first"]:
            sections = list(reversed(sections))
        limit = source["limit"]
        truncated = bool(limit and len(sections) > limit)
        if limit:
            sections = sections[:limit]
        for index, section in enumerate(sections):
            section["id"] = f"{source['id']}-{section.get('original_index', index)}"
            section.pop("start", None)
            section.pop("end", None)
        result.append(
            {
                "id": source["id"],
                "label": source["label"],
                "description": source["description"],
                "path": str(source["path"]),
                "exists": source["path"].exists(),
                "total": total,
                "shown": len(sections),
                "truncated": truncated,
                "items": sections,
            }
        )
    return {"generated_at": review.now(), "sources": result}


def update_active_memory(source_id: str, item_id: str, content: str | None, delete: bool = False) -> dict:
    source = active_memory_source(source_id)
    path = source["path"]
    text = read_text(path)
    sections = split_memory_sections(text)
    expected_prefix = f"{source_id}-"
    if not item_id.startswith(expected_prefix):
        raise ValueError("Memory item does not belong to the selected source")
    try:
        original_index = int(item_id.removeprefix(expected_prefix))
    except ValueError as exc:
        raise ValueError("Invalid memory item id") from exc
    target = next((section for section in sections if section.get("original_index") == original_index), None)
    if not target:
        raise ValueError("Active memory item not found")
    replacement = ""
    if not delete:
        replacement = (content or "").strip()
        if not replacement:
            raise ValueError("Cannot save empty active memory")
        replacement = replacement + "\n\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    new_text = text[: target["start"]] + replacement + text[target["end"] :]
    path.write_text(new_text.rstrip() + "\n", encoding="utf-8")
    return active_memory_payload()


def page() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>记忆审批台</title>
  <style>
    :root {
      color-scheme: dark;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", sans-serif;
      --bg: #171717;
      --bg-elevated: #1f1f1f;
      --panel: #202020;
      --panel-soft: #242424;
      --panel-hover: #2a2a2a;
      --field: #2b2b2b;
      --line: #333333;
      --line-soft: #2a2a2a;
      --text: #ececec;
      --text-subtle: #c8c8c8;
      --muted: #9f9f9f;
      --muted-2: #777777;
      --accent: #8ab4ff;
      --accent-solid: #3b82f6;
      --danger: #ff8a80;
      --danger-bg: #3a2020;
      --warning-bg: #332a1d;
      --shadow: 0 14px 42px rgba(0, 0, 0, .28);
      --radius: 8px;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); font-size: 14px; letter-spacing: 0; }
    header { position: sticky; top: 0; z-index: 3; background: rgba(23, 23, 23, .96); border-bottom: 1px solid var(--line-soft); padding: 12px 18px 10px; backdrop-filter: blur(16px); }
    .topbar { display: flex; align-items: center; justify-content: space-between; gap: 16px; max-width: 1240px; margin: 0 auto; }
    .brand { display: flex; align-items: center; gap: 10px; min-width: 0; }
    .brand-mark { width: 28px; height: 28px; display: grid; place-items: center; border-radius: 7px; background: var(--panel-soft); border: 1px solid var(--line); color: var(--text); font-weight: 700; }
    .brand-copy { min-width: 0; }
    h1 { margin: 0; font-size: 16px; line-height: 1.35; font-weight: 650; }
    .subtitle { margin-top: 1px; color: var(--muted); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: min(680px, 64vw); }
    .top-actions { display: flex; align-items: center; gap: 8px; min-width: 0; }
    .project-badge { max-width: 360px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; border: 1px solid var(--line); background: var(--panel); color: var(--text-subtle); border-radius: 999px; padding: 6px 10px; font-size: 12px; }
    button, select, input { font: inherit; }
    button, select { border: 1px solid var(--line); background: var(--panel); color: var(--text); border-radius: 7px; padding: 7px 10px; cursor: pointer; min-height: 32px; }
    input { border: 1px solid var(--line); background: var(--field); color: var(--text); border-radius: 7px; padding: 8px 10px; min-height: 34px; outline: none; }
    input:focus, textarea:focus, select:focus { border-color: #5f6368; box-shadow: 0 0 0 2px rgba(138, 180, 255, .12); }
    button:hover, select:hover { border-color: #4a4a4a; background: var(--panel-hover); }
    button.primary { background: #e8e8e8; color: #161616; border-color: #e8e8e8; font-weight: 650; }
    button.primary:hover { background: #ffffff; border-color: #ffffff; }
    button.active { background: #303030; border-color: #5a5a5a; color: #fff; }
    button.danger { color: var(--danger); }
    button:disabled { cursor: default; color: var(--muted); background: #242424; border-color: var(--line-soft); opacity: .9; }
    button:disabled:hover { background: #242424; border-color: var(--line-soft); }
    main { max-width: 1240px; margin: 0 auto; padding: 18px 18px 40px; }
    .workspace-bar { max-width: 1240px; margin: 10px auto 0; display: grid; gap: 10px; }
    .view-tabs { display: flex; gap: 6px; flex-wrap: wrap; padding: 3px; border: 1px solid var(--line-soft); background: #1b1b1b; border-radius: 9px; width: fit-content; }
    .view-tabs button { border: 0; background: transparent; color: var(--text-subtle); min-height: 30px; padding: 6px 10px; }
    .view-tabs button.active { background: var(--panel-hover); color: var(--text); box-shadow: inset 0 0 0 1px var(--line); }
    .toolbar, .memory-toolbar, .project-toolbar, .design-toolbar, .ui-design-approval-toolbar, .ui-skill-toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding: 10px; border: 1px solid var(--line-soft); border-radius: var(--radius); background: #1b1b1b; }
    .memory-toolbar, .project-toolbar, .design-toolbar, .ui-design-approval-toolbar, .ui-skill-toolbar { display: none; }
    .toolbar select { min-width: 134px; }
    .memory-toolbar input { min-width: min(360px, 72vw); }
    .project-toolbar input:first-child { min-width: min(560px, 80vw); flex: 1 1 420px; }
    .project-toolbar input:nth-child(2) { min-width: 210px; }
    #message { min-height: 18px; color: var(--accent); }
    .counts { display: flex; gap: 8px; flex-wrap: wrap; margin: 0; }
    .pill { background: #1f1f1f; border: 1px solid var(--line-soft); border-radius: 999px; padding: 5px 9px; font-size: 12px; color: var(--muted); }
    .item { background: var(--panel); border: 1px solid var(--line-soft); border-radius: var(--radius); margin: 12px 0; overflow: hidden; box-shadow: none; }
    .item:hover { border-color: #3a3a3a; }
    .item-head { padding: 14px 16px; display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 14px; border-bottom: 1px solid var(--line-soft); }
    .meta { color: var(--muted); font-size: 12px; line-height: 1.6; }
    .summary { font-weight: 650; margin-bottom: 6px; color: var(--text); line-height: 1.45; }
    .label-row { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 9px; }
    .tag { background: #2a2a2a; border: 1px solid #363636; border-radius: 999px; color: var(--text-subtle); padding: 3px 8px; font-size: 12px; max-width: 100%; overflow: hidden; text-overflow: ellipsis; }
    .tag.warn { background: var(--warning-bg); border-color: #5a4527; color: #ffd08a; }
    .risk { background: var(--danger-bg); color: var(--danger); border: 1px solid #684040; border-radius: 7px; padding: 5px 8px; font-size: 12px; display: inline-block; margin-top: 10px; }
    .content-title { padding: 10px 16px 8px; border-top: 1px solid var(--line-soft); color: var(--muted); font-size: 12px; font-weight: 650; }
    textarea { width: 100%; min-height: 190px; color: var(--text); background: #191919; border: 0; border-top: 1px solid var(--line-soft); padding: 13px 16px; font: 13px/1.65 ui-monospace, SFMono-Regular, Menlo, monospace; resize: vertical; outline: none; }
    .actions, .memory-actions { display: flex; flex-wrap: wrap; gap: 8px; padding: 12px 16px 14px; }
    .memory-source { margin-bottom: 14px; }
    .memory-source-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; padding: 14px 16px; border-bottom: 1px solid var(--line-soft); }
    .memory-grid { display: grid; gap: 10px; padding: 12px 14px 14px; }
    .memory-entry { border: 1px solid var(--line-soft); border-radius: var(--radius); background: #191919; overflow: hidden; }
    .memory-entry summary { cursor: pointer; padding: 11px 12px; font-weight: 650; }
    .memory-entry .meta { padding: 0 12px 10px; }
    .memory-entry pre { margin: 0; padding: 12px; border-top: 1px solid var(--line-soft); white-space: pre-wrap; word-break: break-word; color: var(--text-subtle); font: 13px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace; }
    .memory-entry textarea { min-height: 180px; border-top: 1px solid var(--line-soft); }
    .memory-actions { border-top: 1px solid var(--line-soft); padding: 10px 12px 12px; }
    .empty { color: var(--muted); padding: 32px; text-align: center; background: var(--panel); border: 1px solid var(--line-soft); border-radius: var(--radius); }
    .empty h2 { margin: 0 0 8px; color: var(--text); font-size: 17px; }
    .empty p { margin: 0 auto 14px; max-width: 620px; color: var(--text-subtle); line-height: 1.7; }
    .empty .actions { justify-content: center; padding: 0; }
    .doc { display: grid; gap: 14px; }
    .doc-hero, .doc-section { background: var(--panel); border: 1px solid var(--line-soft); border-radius: var(--radius); }
    .doc-hero { padding: 18px 20px; }
    .doc-hero h2 { margin: 0 0 8px; font-size: 20px; line-height: 1.35; }
    .doc-hero p { margin: 0; color: var(--text-subtle); line-height: 1.75; }
    .doc-section { padding: 16px; }
    .doc-section h3 { margin: 0 0 10px; font-size: 16px; line-height: 1.45; }
    .doc-section p, .doc-section li { color: var(--text-subtle); line-height: 1.75; }
    .doc-section ul, .doc-section ol { margin: 8px 0 0; padding-left: 22px; }
    .doc-section code { background: #2b2b2b; border: 1px solid #383838; border-radius: 5px; padding: 1px 5px; color: var(--text); }
    .doc-section pre { margin: 10px 0 0; padding: 12px; overflow: auto; white-space: pre-wrap; word-break: break-word; color: var(--text-subtle); background: #191919; border: 1px solid var(--line-soft); border-radius: var(--radius); font: 13px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace; }
    .doc-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; align-items: start; }
    .section-title { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin: 18px 0 10px; color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
    .skill-wizard { max-width: 720px; margin: 0 auto 18px; padding: 18px; background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); box-shadow: var(--shadow); }
    .skill-wizard[hidden] { display: none; }
    .skill-wizard-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
    .skill-wizard-head h2 { margin: 0 0 5px; font-size: 18px; }
    .skill-wizard-head p { margin: 0; color: var(--muted); line-height: 1.55; }
    .skill-wizard-steps { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; padding: 0; margin: 0 0 18px; list-style: none; }
    .skill-wizard-step { min-height: 44px; display: flex; align-items: center; gap: 8px; padding: 8px 10px; border: 1px solid var(--line-soft); border-radius: 7px; color: var(--muted); background: #1b1b1b; }
    .skill-wizard-step[aria-current="step"] { color: var(--text); border-color: #696969; background: #303030; }
    .skill-step-number { width: 24px; height: 24px; flex: 0 0 24px; display: grid; place-items: center; border-radius: 999px; border: 1px solid currentColor; font-size: 12px; font-weight: 700; }
    .skill-source-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin-top: 14px; }
    .skill-source-card { min-height: 92px; padding: 14px; text-align: left; display: grid; align-content: start; gap: 5px; }
    .skill-source-card[aria-checked="true"] { border-color: #d0d0d0; background: #303030; box-shadow: inset 0 0 0 1px #d0d0d0; }
    .skill-source-card strong { color: var(--text); }
    .skill-source-card span { color: var(--muted); line-height: 1.45; }
    .skill-fields { display: grid; gap: 13px; }
    .skill-field { display: grid; gap: 6px; }
    .skill-field label, .skill-field legend { color: var(--text-subtle); font-size: 12px; font-weight: 650; }
    .skill-field input, .skill-field select, .skill-field textarea { width: 100%; border: 1px solid var(--line); background: var(--field); color: var(--text); border-radius: 7px; padding: 9px 10px; outline: none; }
    .skill-field textarea { min-height: 210px; font: 13px/1.65 ui-monospace, SFMono-Regular, Menlo, monospace; resize: vertical; }
    .skill-field-help { color: var(--muted); font-size: 12px; line-height: 1.5; }
    .skill-field-error, .skill-submit-error { color: var(--danger); font-size: 12px; line-height: 1.5; }
    .skill-error-summary { margin-bottom: 12px; padding: 9px 11px; color: var(--danger); background: var(--danger-bg); border: 1px solid #684040; border-radius: 7px; line-height: 1.5; }
    .skill-targets { display: flex; flex-wrap: wrap; gap: 14px; padding: 10px 0 0; border: 0; }
    .skill-targets label { display: inline-flex; align-items: center; gap: 7px; min-height: 44px; cursor: pointer; }
    .skill-review { display: grid; border: 1px solid var(--line-soft); border-radius: 7px; overflow: hidden; }
    .skill-review-row { display: grid; grid-template-columns: minmax(120px, .35fr) minmax(0, 1fr); gap: 14px; padding: 10px 12px; border-bottom: 1px solid var(--line-soft); }
    .skill-review-row:last-child { border-bottom: 0; }
    .skill-review-label { color: var(--muted); }
    .skill-review-value { color: var(--text); white-space: pre-wrap; overflow-wrap: anywhere; }
    .skill-safety-note { margin-top: 14px; padding: 11px 12px; color: #ffd08a; background: var(--warning-bg); border-left: 3px solid #b78a45; border-radius: 5px; line-height: 1.6; }
    .skill-wizard-actions { display: flex; justify-content: space-between; gap: 10px; margin-top: 18px; }
    .skill-wizard-actions-end { display: flex; gap: 8px; margin-left: auto; }
    button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
    @media (max-width: 760px) {
      header { padding: 10px 12px; }
      .topbar { align-items: flex-start; flex-direction: column; }
      .top-actions { width: 100%; }
      .project-badge { max-width: 100%; }
      main { padding: 14px 12px 32px; }
      .item-head, .memory-source-head { grid-template-columns: 1fr; display: grid; }
      .actions button, .toolbar button, .project-toolbar button { flex: 1 1 auto; }
      .skill-wizard { padding: 14px; }
      .skill-wizard-head { align-items: stretch; }
      .skill-wizard-steps { gap: 5px; }
      .skill-wizard-step { display: grid; justify-items: center; align-content: center; padding: 7px 4px; text-align: center; font-size: 11px; }
      .skill-source-grid { grid-template-columns: 1fr; }
      .skill-review-row { grid-template-columns: 1fr; gap: 4px; }
      .skill-wizard-actions { flex-direction: column-reverse; }
      .skill-wizard-actions-end { width: 100%; flex-direction: column-reverse; }
      .skill-wizard-actions button, .skill-wizard-actions-end button { width: 100%; min-height: 44px; }
    }
    @media (prefers-reduced-motion: no-preference) {
      .skill-source-card, .skill-wizard-step { transition: border-color 160ms ease, background-color 160ms ease, color 160ms ease; }
    }
  </style>
</head>
<body>
  <header>
    <div class="topbar">
      <div class="brand">
        <div class="brand-mark">忆</div>
        <div class="brand-copy">
          <h1>记忆审批台</h1>
          <div class="subtitle">跨项目记忆、审批和 Loop 初始化工作台</div>
        </div>
      </div>
      <div class="top-actions">
        <span id="currentProjectBadge" class="project-badge">当前项目：加载中</span>
        <button onclick="loadQueue(); loadProjects();">刷新</button>
      </div>
    </div>
    <div class="workspace-bar">
      <div class="view-tabs">
        <button id="reviewTab" class="active" onclick="setView('review')">待审批</button>
        <button id="memoryTab" onclick="setView('memory')">已生效记忆</button>
        <button id="projectTab" onclick="setView('projects')">项目管理</button>
        <button id="designPreferencesTab" onclick="setView('designPreferences')">设计偏好</button>
        <button id="uiDesignApprovalTab" onclick="setView('uiDesignApproval')">UI 设计审批</button>
        <button id="uiSkillsTab" onclick="setView('uiSkills')">UI Skills</button>
        <button id="loopDocTab" onclick="setView('loopDocs')">Loop 说明</button>
        <button id="strategyTab" onclick="setView('strategy')">记忆策略</button>
      </div>
      <div class="toolbar">
        <select id="status" aria-label="候选状态">
          <option value="actionable_pending">可审批候选</option>
          <option value="pending">全部待处理</option>
          <option value="checkpoint_pending">检查点</option>
          <option value="approved">已批准</option>
          <option value="rejected">已拒绝</option>
          <option value="deferred">已暂缓</option>
          <option value="all">全部</option>
        </select>
        <select id="scope" aria-label="记忆范围">
          <option value="all">全部范围</option>
          <option value="project">项目</option>
          <option value="personal">个人</option>
        </select>
        <button onclick="refreshQueue()">刷新队列</button>
        <button onclick="rejectNoisePersonal(false)">预览噪声</button>
        <button class="danger" onclick="rejectNoisePersonal(true)">拒绝噪声</button>
        <span id="message" class="meta"></span>
      </div>
      <div id="memoryToolbar" class="memory-toolbar">
        <select id="memoryScope" aria-label="已生效记忆范围">
          <option value="all">全部已生效记忆</option>
          <option value="project_long">项目长期记忆</option>
          <option value="project_short">项目短期记忆</option>
          <option value="personal_long">个人长期记忆</option>
          <option value="personal_short">个人短期记忆</option>
        </select>
        <input id="memorySearch" placeholder="搜索已生效记忆" oninput="renderMemory()">
        <button onclick="loadMemory()">刷新已生效记忆</button>
      </div>
      <div id="projectToolbar" class="project-toolbar">
        <input id="projectPath" placeholder="输入本机项目仓库路径，例如 ~/projects/my_repo">
        <input id="loopPort" placeholder="Loop staging 端口，留空用推荐值">
        <button onclick="registerProjectFromInput()">注册</button>
        <button onclick="initProjectFromInput()">初始化记忆</button>
        <button class="primary" onclick="initLoopFromInput()">初始化 Loop × Superpowers</button>
        <button onclick="previewLoopUpgradeFromInput()">预览升级 Loop</button>
        <button onclick="upgradeMemoryFromInput()">升级记忆规则/钩子</button>
      </div>
      <div id="designToolbar" class="design-toolbar">
        <button onclick="loadDesignPreferences()">刷新设计偏好</button>
        <button class="primary" onclick="saveGlobalPreferences()">保存全局偏好</button>
        <button onclick="saveProjectPreferences()">保存项目覆盖</button>
      </div>
      <div id="uiDesignApprovalToolbar" class="ui-design-approval-toolbar">
        <button onclick="loadUIDesignApproval()">刷新审批状态</button>
        <button onclick="saveUIDesignPaths()">保存路径配置</button>
        <button class="primary" onclick="enableUIDesignGate()">Smoke test 并启用硬门禁</button>
        <button class="danger" onclick="relockUIDesignGate()">立即重新锁定</button>
      </div>
      <div id="uiSkillToolbar" class="ui-skill-toolbar">
        <button onclick="loadUISkills()">扫描并刷新</button>
        <button class="primary" onclick="openUISkillImportWizard()">导入 UI Skill</button>
      </div>
      <div id="counts" class="counts"></div>
    </div>
  </header>
  <main>
    <section id="uiSkillImportWizard" class="skill-wizard" role="dialog" aria-labelledby="uiSkillWizardTitle" hidden>
      <div id="uiSkillWizardLive" class="meta" aria-live="polite"></div>
      <div id="uiSkillWizardContent"></div>
    </section>
    <div id="items"></div>
  </main>
<script>
let queue = {items: [], counts: {}};
let activeMemory = {sources: []};
let projectState = {current_project: '', registry: {projects: []}, recommend_port: 8081};
let uiDesignContext = null;
let uiDesignApprovalState = {config: {}, gate_status: {}, packages: [], audit: []};
let uiSkillState = {items: [], discovered: []};
let uiSkillWizard = defaultUISkillWizardState();
let currentView = 'review';

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

const statusLabel = {pending: '待审批', approved: '已批准', rejected: '已拒绝', deferred: '已暂缓'};
const scopeLabel = {project: '项目记忆', personal: '个人记忆'};
const targetLabel = {project_long: '项目长期记忆', personal_long: '个人长期记忆', personal_short: '个人短期记忆', short: '个人短期候选', unsure: '待判断'};
const sourceLabel = {project_proposals: '项目候选文件', personal_proposals: '个人候选文件'};
const loopStatusLabel = {
  not_initialized: 'Loop 未初始化',
  legacy: '旧版 Loop',
  superpowers_incomplete: 'Loop × Superpowers 待完善',
  superpowers_ready: 'Loop × Superpowers 已就绪',
  invalid: 'Loop 配置无效'
};
const memoryStatusLabel = {
  not_initialized: '项目记忆未初始化',
  initialized: '项目记忆已就绪',
  upgrade_available: '记忆规则/钩子可升级'
};
const pluginStatusLabel = {
  installed: '双端 Superpowers 已安装',
  partial: 'Superpowers 部分安装',
  missing: 'Superpowers 未安装'
};
const riskLabel = {
  api_key: '疑似 API Key',
  access_key: '疑似 AccessKey',
  authorization: '疑似授权头',
  password: '疑似密码',
  secret: '疑似密钥',
  token: '疑似 Token',
  env_production: '涉及 .env.production',
  sms_code: '疑似验证码'
};

function isCheckpointItem(item) {
  const text = `${item.title || ''}\n${item.summary || ''}\n${item.content || ''}`;
  return item.review_kind === 'checkpoint'
    || item.actionable === false
    || (item.source === 'project_proposals'
      && text.includes('Review whether')
      && text.includes('introduced stable project facts'));
}

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) throw new Error(await res.text());
  return await res.json();
}

function showMessage(text) {
  document.getElementById('message').textContent = text;
  setTimeout(() => { document.getElementById('message').textContent = ''; }, 4000);
}

function updateProjectBadge() {
  const badge = document.getElementById('currentProjectBadge');
  if (!badge) return;
  const root = projectState.current_project || '';
  const name = root ? root.split('/').filter(Boolean).pop() : '未选择项目';
  badge.textContent = root ? `当前项目：${name}` : '当前项目：未选择';
  badge.title = root;
}

async function loadQueue() {
  queue = await api('/api/queue');
  render();
}

async function loadMemory() {
  activeMemory = await api('/api/active-memory');
  renderMemory();
}

function setView(view) {
  currentView = view;
  document.getElementById('reviewTab').classList.toggle('active', view === 'review');
  document.getElementById('memoryTab').classList.toggle('active', view === 'memory');
  document.getElementById('projectTab').classList.toggle('active', view === 'projects');
  document.getElementById('designPreferencesTab').classList.toggle('active', view === 'designPreferences');
  document.getElementById('uiDesignApprovalTab').classList.toggle('active', view === 'uiDesignApproval');
  document.getElementById('uiSkillsTab').classList.toggle('active', view === 'uiSkills');
  document.getElementById('loopDocTab').classList.toggle('active', view === 'loopDocs');
  document.getElementById('strategyTab').classList.toggle('active', view === 'strategy');
  document.querySelector('.toolbar').style.display = view === 'review' ? 'flex' : 'none';
  document.getElementById('memoryToolbar').style.display = view === 'memory' ? 'flex' : 'none';
  document.getElementById('projectToolbar').style.display = view === 'projects' ? 'flex' : 'none';
  document.getElementById('designToolbar').style.display = view === 'designPreferences' ? 'flex' : 'none';
  document.getElementById('uiDesignApprovalToolbar').style.display = view === 'uiDesignApproval' ? 'flex' : 'none';
  document.getElementById('uiSkillToolbar').style.display = view === 'uiSkills' ? 'flex' : 'none';
  if (view === 'designPreferences') {
    loadDesignPreferences().catch(err => showMessage(err.message));
    return;
  }
  if (view === 'uiSkills') {
    loadUISkills().catch(err => showMessage(err.message));
    return;
  }
  if (view === 'uiDesignApproval') {
    loadUIDesignApproval().catch(err => showMessage(err.message));
    return;
  }
  if (view === 'strategy') {
    renderMemoryStrategy();
    return;
  }
  if (view === 'projects') {
    loadProjects().catch(err => showMessage(err.message));
    return;
  }
  if (view === 'loopDocs') {
    renderLoopDocs();
    return;
  }
  if (view === 'memory' && !activeMemory.sources.length) {
    loadMemory().catch(err => showMessage(err.message));
    return;
  }
  view === 'review' ? render() : renderMemory();
}

async function loadProjects() {
  projectState = await api('/api/projects');
  updateProjectBadge();
  renderProjects();
}

async function registerProjectFromInput() {
  const root = document.getElementById('projectPath').value.trim();
  if (!root) return showMessage('请输入项目路径');
  projectState = await api('/api/projects/register', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({project_root: root})
  });
  updateProjectBadge();
  showMessage('已注册并切换项目');
  await loadQueue();
  renderProjects();
}

async function useProject(root, name) {
  projectState = await api('/api/projects/use', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({project_root: root})
  });
  updateProjectBadge();
  showMessage(`已切换到 ${name || root} 项目`);
  activeMemory = {sources: []};
  await loadQueue();
  renderProjects();
}

async function initProjectFromInput() {
  const root = document.getElementById('projectPath').value.trim();
  if (!root) return showMessage('请输入项目路径');
  if (!confirm(`将在本机路径初始化项目记忆和 Codex/Claude hooks：\\n${root}\\n\\n已存在文件不会覆盖。继续？`)) return;
  const result = await api('/api/projects/init', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({project_root: root})
  });
  projectState = result.projects;
  updateProjectBadge();
  showMessage(`项目记忆初始化完成：${result.changes.length} 项`);
  await loadQueue();
  renderProjects(result);
}

async function initLoopFromInput() {
  const root = document.getElementById('projectPath').value.trim();
  if (!root) return showMessage('请输入项目路径');
  const rawPort = document.getElementById('loopPort').value.trim();
  const port = rawPort ? Number(rawPort) : projectState.recommend_port;
  if (!Number.isInteger(port) || port < 1 || port > 65535) return showMessage('请输入有效端口');
  if (!confirm(`将为新项目初始化 Loop × Superpowers：\\n${root}\\n\\n建议/选择的 staging 端口：${port}\\n\\n如果项目已有 Loop 配置，请改用“预览升级 Loop”。继续？`)) return;
  const result = await api('/api/projects/init-loop', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({project_root: root, port})
  });
  projectState = result.projects;
  updateProjectBadge();
  showMessage(`Loop × Superpowers 初始化完成，端口 ${result.port}`);
  await loadQueue();
  renderProjects(result);
}

async function previewLoopUpgradeFromInput() {
  const root = document.getElementById('projectPath').value.trim();
  if (!root) return showMessage('请输入项目路径');
  const rawPort = document.getElementById('loopPort').value.trim();
  const body = {project_root: root};
  if (rawPort) {
    const port = Number(rawPort);
    if (!Number.isInteger(port) || port < 1 || port > 65535) return showMessage('请输入有效端口');
    body.port = port;
  }
  const preview = await api('/api/projects/preview-loop-upgrade', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  renderProjects(preview);
  const additions = (preview.added_paths || []).join('\\n') || '无配置字段变化';
  const conflict = preview.validator_action === 'custom_conflict'
    ? '\\n\\n检测到自定义同名验证器，将保留原文件并标记人工处理。'
    : '';
  if (!confirm(`升级预览：\\n\\n将新增的配置路径：\\n${additions}\\n\\n现有项目资源配置和未知扩展字段会保留；变更前创建备份。${conflict}\\n\\n确认执行升级？`)) return;
  const result = await api('/api/projects/upgrade-loop', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({...body, confirmed: true})
  });
  projectState = result.projects;
  updateProjectBadge();
  showMessage('Loop × Superpowers 升级完成');
  renderProjects(result);
}

async function upgradeMemoryFromInput() {
  const root = document.getElementById('projectPath').value.trim();
  if (!root) return showMessage('请输入项目路径');
  if (!confirm(`将升级中央管理器拥有的记忆规则块和两个现有 hook：\\n${root}\\n\\n修改前保留时间戳备份，不新增 Superpowers hook，不改规则块外的用户内容。继续？`)) return;
  const result = await api('/api/projects/upgrade-memory', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({project_root: root, confirmed: true})
  });
  projectState = result.projects;
  updateProjectBadge();
  showMessage('记忆规则/钩子升级完成');
  renderProjects(result);
}

async function refreshQueue() {
  queue = await api('/api/refresh', {method: 'POST'});
  showMessage('已刷新');
  render();
}

async function rejectNoisePersonal(apply) {
  if (apply && !confirm('确定将明显噪声的个人记忆候选标记为已拒绝吗？这不会删除原始 proposals 文件。')) return;
  const result = await api('/api/reject-noise-personal', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({apply})
  });
  showMessage(`${apply ? '已拒绝' : '预览'} ${result.ids.length} 条噪声个人候选`);
  if (apply) {
    queue = result.queue;
    render();
  } else {
    alert(result.ids.length ? result.ids.join('\\n') : '没有检测到明显噪声个人候选');
  }
}

function filteredItems() {
  const status = document.getElementById('status').value;
  const scope = document.getElementById('scope').value;
  return queue.items.filter(item => {
    const isCheckpoint = isCheckpointItem(item);
    if (status === 'actionable_pending' && !(item.status === 'pending' && !isCheckpoint)) return false;
    else if (status === 'checkpoint_pending' && !(item.status === 'pending' && isCheckpoint)) return false;
    else if (!['all', 'actionable_pending', 'checkpoint_pending'].includes(status) && item.status !== status) return false;
    if (scope !== 'all' && item.scope !== scope) return false;
    return true;
  });
}

function render() {
  if (currentView !== 'review') return;
  const counts = queue.counts || {};
  document.getElementById('counts').innerHTML = [
    `可审批 ${counts.actionable_pending || 0}`,
    `检查点 ${counts.checkpoint_pending || 0}`,
    `全部待处理 ${counts.pending || 0}`,
    `项目 ${counts.project_pending || 0}`,
    `个人 ${counts.personal_pending || 0}`,
    `已批准 ${counts.approved || 0}`,
    `已拒绝 ${counts.rejected || 0}`,
    `已暂缓 ${counts.deferred || 0}`,
  ].map(x => `<span class="pill">${esc(x)}</span>`).join('');

  const items = filteredItems();
  if (!items.length) {
    const status = document.getElementById('status').value;
    const hint = status === 'actionable_pending'
      ? `<h2>当前没有可直接审批的候选</h2>
         <p>这通常是好事：说明暂时没有需要你立刻批准进入长期/个人记忆的内容。仍有 ${esc(counts.checkpoint_pending || 0)} 个检查点，它们更像“是否需要整理成长期记忆”的提醒。</p>
         <div class="actions"><button onclick="document.getElementById('status').value='checkpoint_pending'; render();">查看检查点</button><button onclick="document.getElementById('status').value='all'; render();">查看全部记录</button></div>`
      : `<h2>没有符合条件的候选</h2>
         <p>可以放宽筛选条件，或刷新队列查看最新候选。</p>
         <div class="actions"><button onclick="document.getElementById('status').value='all'; document.getElementById('scope').value='all'; render();">清除筛选</button><button onclick="refreshQueue()">刷新队列</button></div>`;
    document.getElementById('items').innerHTML = `<div class="empty">${hint}</div>`;
    return;
  }
  document.getElementById('items').innerHTML = items.map(item => {
    const target = item.scope === 'project' ? 'project_long' : (item.target === 'short' ? 'personal_short' : 'personal_long');
    const risks = (item.risk_flags || []).length ? `<span class="risk">风险：${esc(item.risk_flags.map(x => riskLabel[x] || x).join('、'))}</span>` : '';
    const isCheckpoint = isCheckpointItem(item);
    const source = sourceLabel[item.source] || item.source || '未知来源';
    const displayTarget = targetLabel[target] || target;
    const sourceTarget = targetLabel[item.target] || item.target || '待判断';
    const summary = isCheckpoint || (item.source === 'project_proposals' && String(item.summary || '').startsWith('- Review whether'))
      ? '项目长期记忆检查点：请判断这个会话是否有稳定事实需要沉淀'
      : (item.summary || item.title);
    const actions = isCheckpoint ? `
        <span class="tag warn">检查点不是可直接批准的记忆。需要先人工整理成明确记忆，再批准。</span>
        <button data-action="defer" data-id="${esc(item.id)}">暂缓</button>
        <button class="danger" data-action="reject" data-id="${esc(item.id)}">拒绝</button>
        <button data-action="reset" data-id="${esc(item.id)}">重置</button>
      ` : `
        <button class="primary" data-action="approve" data-id="${esc(item.id)}" data-target="${esc(target)}">批准为 ${esc(displayTarget)}</button>
        <button data-action="approve" data-id="${esc(item.id)}" data-target="project_long">项目长期</button>
        <button data-action="approve" data-id="${esc(item.id)}" data-target="personal_long">个人长期</button>
        <button data-action="approve" data-id="${esc(item.id)}" data-target="personal_short">个人短期</button>
        <button data-action="defer" data-id="${esc(item.id)}">暂缓</button>
        <button class="danger" data-action="reject" data-id="${esc(item.id)}">拒绝</button>
        <button data-action="reset" data-id="${esc(item.id)}">重置</button>
      `;
    return `<section class="item" data-id="${esc(item.id)}">
      <div class="item-head">
        <div>
          <div class="summary">${esc(summary)}</div>
          <div class="meta">编号：${esc(item.id)} | 范围：${esc(scopeLabel[item.scope] || item.scope)} | 建议：${esc(sourceTarget)} | 状态：${esc(statusLabel[item.status] || item.status)} | 创建：${esc(item.created_at)}</div>
          <div class="label-row">
            <span class="tag">候选来源：${esc(source)}</span>
            <span class="tag">默认批准目标：${esc(displayTarget)}</span>
            <span class="tag">原始文件：${esc(item.source_path || '')}</span>
            ${isCheckpoint ? '<span class="tag warn">这是检查点，不是最终记忆</span>' : ''}
          </div>
          ${risks}
        </div>
        <div class="meta">${esc(source)}</div>
      </div>
      <div class="content-title">原始候选内容，可编辑后再批准</div>
      <textarea id="content-${esc(item.id)}">${esc(item.content)}</textarea>
      <div class="actions">
        ${actions}
      </div>
    </section>`;
  }).join('');
}

function renderMemory() {
  if (currentView !== 'memory') return;
  const scope = document.getElementById('memoryScope').value;
  const keyword = document.getElementById('memorySearch').value.trim().toLowerCase();
  const sources = (activeMemory.sources || []).filter(source => scope === 'all' || source.id === scope);
  document.getElementById('counts').innerHTML = sources.map(source => {
    const suffix = source.truncated ? ` / 共 ${source.total} 条，仅显示最近 ${source.shown} 条` : ` / 共 ${source.total} 条`;
    return `<span class="pill">${esc(source.label)}${esc(suffix)}</span>`;
  }).join('');
  const blocks = sources.map(source => {
    const items = source.items.filter(item => {
      if (!keyword) return true;
      return `${item.title}\n${item.summary}\n${item.content}`.toLowerCase().includes(keyword);
    });
    const entries = items.length ? items.map(item => `
      <details class="memory-entry">
        <summary>${esc(item.title || '未命名记忆')}</summary>
        <div class="meta">${esc(item.summary || '')}</div>
        <textarea id="memory-content-${esc(item.id)}">${esc(item.content || '')}</textarea>
        <div class="memory-actions">
          <button class="primary" data-memory-action="save" data-source="${esc(source.id)}" data-id="${esc(item.id)}">保存修改</button>
          <button class="danger" data-memory-action="delete" data-source="${esc(source.id)}" data-id="${esc(item.id)}">删除这条记忆</button>
        </div>
      </details>
    `).join('') : '<div class="empty">这一类没有符合搜索条件的已生效记忆。</div>';
    return `<section class="item memory-source">
      <div class="memory-source-head">
        <div>
          <div class="summary">${esc(source.label)}</div>
          <div class="meta">${esc(source.description)}</div>
          <div class="label-row">
            <span class="tag">原始文件：${esc(source.path)}</span>
            <span class="tag">当前显示：${esc(source.shown)} 条</span>
            <span class="tag">总计：${esc(source.total)} 条</span>
            ${source.truncated ? '<span class="tag warn">短期记忆较长，默认只显示最近记录</span>' : ''}
          </div>
        </div>
        <div class="meta">${source.exists ? '已存在' : '文件不存在'}</div>
      </div>
      <div class="memory-grid">${entries}</div>
    </section>`;
  });
  document.getElementById('items').innerHTML = blocks.join('') || '<div class="empty">没有可展示的已生效记忆。</div>';
}

function renderProjects(lastResult) {
  if (currentView !== 'projects') return;
  const projects = projectState.registry?.projects || [];
  const current = projectState.current_project || '';
  document.getElementById('counts').innerHTML = [
    `当前项目 ${current || '未设置'}`,
    `已注册 ${projects.length}`,
    `推荐 loop 端口 ${projectState.recommend_port || 8081}`
  ].map(x => `<span class="pill">${esc(x)}</span>`).join('');
  const resultBlock = lastResult ? `
    <section class="doc-section">
      <h3>最近一次项目操作结果</h3>
      <p>状态：${esc(lastResult.ok ? '成功' : '未知')} ${lastResult.port ? `| loop 端口：${esc(lastResult.port)}` : ''}</p>
      ${(lastResult.added_paths || []).length ? `<p>预览将新增的配置路径：</p><pre>${esc(lastResult.added_paths.join('\\n'))}</pre>` : ''}
      ${lastResult.validator_action ? `<p>验证器状态：${esc(lastResult.validator_action)}</p>` : ''}
      <pre>${esc((lastResult.changes || []).map(item => `${item.status.padEnd(8)} ${item.path}`).join('\\n'))}</pre>
    </section>
  ` : '';
  const rows = projects.length ? projects.map(project => {
    const selected = project.root === current;
    const switchButton = selected
      ? '<button disabled aria-current="true">已选择项目</button>'
      : `<button class="primary" onclick="useProject('${esc(project.root)}', '${esc(project.name)}')">切换到此项目</button>`;
    return `
    <section class="item">
      <div class="memory-source-head">
        <div>
          <div class="summary">${esc(project.name)} ${selected ? '（当前）' : ''}</div>
          <div class="meta">${esc(project.root)}</div>
          <div class="label-row">
            <span class="tag">${project.is_git_repo ? 'Git 仓库' : '非 Git 或未检测到 .git'}</span>
            <span class="tag">${esc(memoryStatusLabel[project.memory_status] || project.memory_status)}</span>
            <span class="tag">${esc(loopStatusLabel[project.loop_status] || project.loop_status)}</span>
            <span class="tag">完成门禁：${esc(project.completion_gate)}</span>
            <span class="tag">${esc(pluginStatusLabel[project.plugin_status] || project.plugin_status)}</span>
          </div>
        </div>
        <div class="actions">
          ${switchButton}
          <button onclick="document.getElementById('projectPath').value='${esc(project.root)}'">填入路径</button>
        </div>
      </div>
    </section>`;
  }).join('') : '<div class="empty">还没有注册项目。输入本机仓库路径后，可以注册或初始化。</div>';
  document.getElementById('items').innerHTML = `
    <div class="doc">
      <section class="doc-hero">
        <h2>项目管理</h2>
        <p>记忆审核台代码是跨项目通用的，但每个仓库的项目长短期记忆独立存放在该仓库的 <code>codex/</code> 目录。切换当前项目后，后端会写入注册表，后续 API 直接读取所选项目，无需重启 8897。</p>
      </section>
      <section class="doc-section">
        <h3>初始化说明</h3>
        <ul>
          <li><strong>注册项目</strong>：只把路径加入 <code>~/.codex/memory_review/projects.json</code> 并切换当前项目。</li>
          <li><strong>初始化项目记忆</strong>：创建缺失的项目记忆文件、Codex hook、Claude Code hook 和共享规则。</li>
          <li><strong>初始化 Loop × Superpowers</strong>：只用于尚无 Loop 配置的新项目，创建 schema 3 配置、标准目录和 completion 验证器。</li>
          <li><strong>预览升级 Loop</strong>：对旧项目先只读展示新增路径、备份与冲突，再由用户确认升级；数据库、OSS、端口、远程路径和未知扩展字段保持原值。</li>
          <li><strong>升级记忆规则/钩子</strong>：只更新中央托管规则块和两个既有 hook，保留备份，不新增 Superpowers hook。</li>
          <li>已存在文件不会覆盖，会在结果中显示为 <code>existing</code>。</li>
        </ul>
      </section>
      ${resultBlock}
      ${rows}
    </div>
  `;
}

function idempotencyKey(prefix) {
  return `${prefix}-${crypto.randomUUID()}`;
}

async function loadDesignPreferences() {
  const project = projectState.current_project || '';
  uiDesignContext = await api(`/api/ui-design/context?project=${encodeURIComponent(project)}`);
  renderDesignPreferences();
}

function renderDesignPreferences() {
  if (currentView !== 'designPreferences' || !uiDesignContext) return;
  const effective = uiDesignContext.effective_preferences || {value: {}, sources: {}};
  document.getElementById('counts').innerHTML = [
    `项目 ${uiDesignContext.project}`,
    `覆盖字段 ${Object.keys(uiDesignContext.project_preferences || {}).length}`,
    `有效字段来源 ${Object.keys(effective.sources || {}).length}`
  ].map(x => `<span class="pill">${esc(x)}</span>`).join('');
  document.getElementById('items').innerHTML = `
    <div class="doc-grid">
      <section class="item">
        <div class="item-head"><div><div class="summary">全局设计偏好</div><div class="meta">所有项目继承；保存会创建可审计备份。</div></div><span class="tag">global</span></div>
        <textarea id="globalPreferences">${esc(JSON.stringify(uiDesignContext.global_preferences || {}, null, 2))}</textarea>
      </section>
      <section class="item">
        <div class="item-head"><div><div class="summary">当前项目覆盖</div><div class="meta">支持 inherit / replace / append / clear。</div></div><span class="tag">project</span></div>
        <textarea id="projectPreferences">${esc(JSON.stringify(uiDesignContext.project_preferences || {}, null, 2))}</textarea>
      </section>
    </div>
    <section class="item">
      <div class="item-head"><div><div class="summary">最终生效偏好</div><div class="meta">合并结果及每个字段的 global / project 来源。</div></div><span class="tag">effective</span></div>
      <textarea readonly>${esc(JSON.stringify(effective, null, 2))}</textarea>
    </section>`;
}

async function saveGlobalPreferences() {
  const value = JSON.parse(document.getElementById('globalPreferences').value);
  await api('/api/ui-design/preferences/global', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({value, idempotency_key: idempotencyKey('pref-global')})
  });
  showMessage('全局设计偏好已保存');
  await loadDesignPreferences();
}

async function saveProjectPreferences() {
  const value = JSON.parse(document.getElementById('projectPreferences').value);
  await api('/api/ui-design/preferences/project', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({project: projectState.current_project, value, idempotency_key: idempotencyKey('pref-project')})
  });
  showMessage('项目设计偏好已保存');
  await loadDesignPreferences();
}

async function loadUIDesignApproval() {
  const project = projectState.current_project || '';
  uiDesignApprovalState = await api(`/api/ui-design/project-config?project=${encodeURIComponent(project)}`);
  renderUIDesignApproval();
}

function pathLines(value) {
  return [...new Set(String(value || '').split(/\\r?\\n/).map(x => x.trim()).filter(Boolean))];
}

function designPackageActions(item, mode) {
  const task = esc(item.task_id);
  const digest = esc(item.digest);
  const actions = [];
  if (item.status !== 'approved') {
    if (mode === 'project_global') {
      actions.push(`<button class="primary" data-ui-design-action="approve-baseline" data-task="${task}" data-digest="${digest}">批准为项目基线</button>`);
    } else {
      actions.push(`<button class="primary" data-ui-design-action="approve" data-task="${task}" data-digest="${digest}">批准此设计包</button>`);
    }
  }
  actions.push(`<button data-ui-design-action="request-revision" data-task="${task}">要求修改</button>`);
  actions.push(`<button class="danger" data-ui-design-action="reject" data-task="${task}">拒绝</button>`);
  if (item.status === 'approved') actions.push(`<button class="danger" data-ui-design-action="invalidate" data-task="${task}">显式失效</button>`);
  return actions.join('');
}

function renderUIDesignApproval() {
  if (currentView !== 'uiDesignApproval') return;
  const config = uiDesignApprovalState.config || {};
  const gateStatus = uiDesignApprovalState.gate_status || {};
  const packages = uiDesignApprovalState.packages || [];
  const audit = uiDesignApprovalState.audit || [];
  const smoke = config.hook_smoke_test || {};
  document.getElementById('counts').innerHTML = [
    `模式 ${config.gate_mode || 'design_package'}`,
    `硬门禁 ${config.hard_gate_enabled ? '已启用' : '未启用'}`,
    `当前决策 ${gateStatus.decision || '未配置'}`,
    `待审批 ${packages.filter(x => x.status !== 'approved').length}`
  ].map(x => `<span class="pill">${esc(x)}</span>`).join('');
  const packageCards = packages.map(item => {
    const manifest = item.manifest || {};
    const documents = Object.entries(item.design_documents || {}).map(([name, content]) => `
      <details class="memory-entry"><summary>${esc(name)}</summary><pre>${esc(content || '（尚未填写）')}</pre></details>
    `).join('');
    return `<section class="item">
      <div class="item-head"><div><div class="summary">${esc(manifest.title || item.task_id)}</div>
        <div class="meta">${esc(item.task_id)} · ${esc(item.status)} · ${esc(manifest.classification || '')}</div>
        <div class="label-row"><span class="tag">摘要 ${esc(item.digest)}</span><span class="tag">页面 ${esc((manifest.pages || []).join(', ') || '未声明')}</span><span class="tag">组件 ${esc((manifest.components || []).join(', ') || '未声明')}</span></div>
      </div><span class="tag">${esc(config.gate_mode || 'design_package')}</span></div>
      <div class="content-title">允许修改的正式前端范围</div><pre class="doc-section">${esc((manifest.allowed_file_patterns || []).join('\\n'))}</pre>
      <div class="content-title">设计、交互与响应式说明（安全文本预览）</div>${documents || '<div class="empty">没有设计文档。</div>'}
      <details class="memory-entry"><summary>审批与摘要差异</summary><pre>${esc(JSON.stringify({approval: item.approval || null, current_digest: item.digest, superseded_digest: (item.approval || {}).superseded_digest || null}, null, 2))}</pre></details>
      <div class="actions">${designPackageActions(item, config.gate_mode)}</div>
    </section>`;
  }).join('');
  const auditRows = audit.map(item => `<div class="meta">${esc(item.at)} · ${esc(item.event)} · ${esc(item.task_id || '')} · ${esc(item.status || '')}</div>`).join('');
  document.getElementById('items').innerHTML = `
    <div class="doc">
      <section class="doc-hero"><h2>UI 设计审批</h2><p>可见界面任务在批准前只允许调研、读取代码以及编写设计稿、原型和交互说明；批准后才按项目模式解锁正式前端文件。纯后端或无界面任务不触发。</p></section>
      <div class="doc-grid">
        <section class="item"><div class="item-head"><div><div class="summary">审批模式</div><div class="meta"><code>design_package</code> 按任务、版本和文件范围批准；<code>project_global</code> 批准一次项目基线后解锁全部正式前端，直到重锁或基线变化。</div></div></div>
          <div class="actions"><select id="uiGateMode"><option value="design_package" ${config.gate_mode === 'design_package' ? 'selected' : ''}>design_package（按设计包）</option><option value="project_global" ${config.gate_mode === 'project_global' ? 'selected' : ''}>project_global（全项目）</option></select><button onclick="changeUIDesignMode()">确认切换并重锁</button></div>
        </section>
        <section class="item"><div class="item-head"><div><div class="summary">硬门禁与双端 Hook</div><div class="meta">Codex：${esc((smoke.codex || {}).status || 'not_run')} · Claude：${esc((smoke.claude || {}).status || 'not_run')} · 当前：${config.hard_gate_enabled ? '已启用' : '未启用'}</div></div><span class="tag">${esc(gateStatus.status || 'missing')}</span></div><pre class="doc-section">${esc(JSON.stringify(gateStatus, null, 2))}</pre></section>
      </div>
      <section class="item"><div class="item-head"><div><div class="summary">项目路径分类</div><div class="meta">每行一个项目相对 glob；保存路径会关闭硬门禁并要求重新 smoke test。</div></div></div>
        <div class="doc-grid">
          <label class="doc-section"><strong>正式前端路径</strong><textarea id="formalFrontendPaths">${esc((config.formal_frontend_paths || []).join('\\n'))}</textarea></label>
          <label class="doc-section"><strong>设计产物路径</strong><textarea id="designArtifactPaths">${esc((config.design_artifact_paths || []).join('\\n'))}</textarea></label>
          <label class="doc-section"><strong>生成代码路径</strong><textarea id="generatedPaths">${esc((config.generated_paths || []).join('\\n'))}</textarea></label>
          <label class="doc-section"><strong>测试产物路径</strong><textarea id="testArtifactPaths">${esc((config.test_artifact_paths || []).join('\\n'))}</textarea></label>
        </div>
      </section>
      <div class="section-title">待审批设计包</div>${packageCards || '<div class="empty">暂无设计包。代理可先生成设计包与设计文档，再回到这里审批。</div>'}
      <section class="doc-section"><h3>审计历史</h3>${auditRows || '<div class="meta">暂无 UI 设计审批事件。</div>'}</section>
    </div>`;
}

async function uiDesignApprovalMutation(route, payload, dangerous, message) {
  if (dangerous && !confirm(message || '确认执行此 UI 设计审批操作？')) return null;
  const result = await api(`/api/ui-design/${route}`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({...payload, project: projectState.current_project, confirmed: dangerous, idempotency_key: idempotencyKey(`ui-design-${route.replaceAll('/', '-')}`)})});
  await loadUIDesignApproval();
  return result;
}

async function changeUIDesignMode() {
  const mode = document.getElementById('uiGateMode').value;
  await uiDesignApprovalMutation('project-config/set-mode', {mode}, true, mode === 'project_global' ? '确认切换为全项目一次批准模式？切换会立即重锁，之后需批准项目 UI 基线。' : '确认切换为按设计包审批模式？切换会立即重锁。');
}

async function saveUIDesignPaths() {
  await uiDesignApprovalMutation('project-config/set-paths', {paths: {
    formal_frontend_paths: pathLines(document.getElementById('formalFrontendPaths').value),
    design_artifact_paths: pathLines(document.getElementById('designArtifactPaths').value),
    generated_paths: pathLines(document.getElementById('generatedPaths').value),
    test_artifact_paths: pathLines(document.getElementById('testArtifactPaths').value)
  }}, false);
  showMessage('UI 路径已保存；硬门禁需重新 smoke test 后启用');
}

async function enableUIDesignGate() {
  await uiDesignApprovalMutation('project-config/enable-hard-gate', {}, true, '确认在临时夹具中验证 Codex 与 Claude Hook，并在双端均通过后启用正式前端硬门禁？');
}

async function relockUIDesignGate() {
  await uiDesignApprovalMutation('project-config/relock', {}, true, '确认立即重新锁定正式前端开发？');
}

async function handleUIDesignApprovalAction(button) {
  const action = button.dataset.uiDesignAction;
  const task_id = button.dataset.task;
  const digest = button.dataset.digest;
  if (action === 'approve') await uiDesignApprovalMutation('packages/approve', {task_id, digest}, true, '确认批准此摘要与声明文件范围？');
  if (action === 'approve-baseline') await uiDesignApprovalMutation('baseline/approve', {task_id, digest}, true, '确认批准此设计包为全项目 UI 基线并解锁正式前端？');
  if (action === 'request-revision') await uiDesignApprovalMutation('packages/request-revision', {task_id, reason: prompt('请输入修改要求') || '需要补充设计说明'}, false);
  if (action === 'reject') await uiDesignApprovalMutation('packages/reject', {task_id, reason: prompt('请输入拒绝原因') || ''}, true, '确认拒绝此设计包？');
  if (action === 'invalidate') await uiDesignApprovalMutation('packages/invalidate', {task_id, reason: prompt('请输入失效原因') || '范围已变化'}, true, '确认让此设计包审批立即失效？');
}

function defaultUISkillWizardState() {
  return {
    open: false,
    step: 1,
    sourceType: '',
    fields: {
      skillMD: '',
      localPath: '',
      zipPath: '',
      githubRepo: '',
      githubPath: '',
      revision: '',
      scope: 'global',
      versionLabel: '1.0.0',
      codex: true,
      claude: true
    },
    errors: {},
    submitting: false,
    dirty: false,
    idempotencyKey: idempotencyKey('skill-import')
  };
}

function clearUISkillWizardLive() {
  const live = document.getElementById('uiSkillWizardLive');
  if (live) live.textContent = '';
}

function wizardError(name) {
  const message = uiSkillWizard.errors[name];
  return message ? `<div id="${name}Error" class="skill-field-error">${esc(message)}</div>` : '';
}

function wizardErrorLink(name) {
  return uiSkillWizard.errors[name] ? ` aria-invalid="true" aria-describedby="${name}Error"` : '';
}

function wizardSteps() {
  const labels = ['1 选择来源', '2 配置导入', '3 确认并校验'];
  return `<ol class="skill-wizard-steps" aria-label="导入进度">${labels.map((label, index) => {
    const number = index + 1;
    const current = number === uiSkillWizard.step ? ' aria-current="step"' : '';
    return `<li class="skill-wizard-step"${current}><span class="skill-step-number">${number}</span><span>${label}</span></li>`;
  }).join('')}</ol>`;
}

function openUISkillImportWizard() {
  uiSkillWizard = defaultUISkillWizardState();
  clearUISkillWizardLive();
  uiSkillWizard.open = true;
  renderUISkillImportWizard();
  const wizard = document.getElementById('uiSkillImportWizard');
  wizard.scrollIntoView({behavior: 'smooth', block: 'start'});
  document.getElementById('uiSkillWizardTitle').focus();
}

function closeUISkillImportWizard(force = false) {
  if (uiSkillWizard.submitting && !force) return;
  if (!force && uiSkillWizard.dirty && !confirm('确认放弃尚未导入的 UI Skill 信息？')) return;
  uiSkillWizard = defaultUISkillWizardState();
  clearUISkillWizardLive();
  renderUISkillImportWizard();
  const trigger = document.querySelector('#uiSkillToolbar button.primary');
  if (trigger) trigger.focus();
}

function selectUISkillSource(sourceType) {
  const allowed = ['editor', 'local', 'zip', 'github'];
  if (!allowed.includes(sourceType) || uiSkillWizard.submitting) return;
  if (uiSkillWizard.sourceType && uiSkillWizard.sourceType !== sourceType) {
    for (const name of ['skillMD', 'localPath', 'zipPath', 'githubRepo', 'githubPath', 'revision']) {
      uiSkillWizard.fields[name] = '';
    }
  }
  uiSkillWizard.sourceType = sourceType;
  uiSkillWizard.errors = {};
  uiSkillWizard.dirty = true;
  renderUISkillImportWizard();
}

function setUISkillWizardField(name, value) {
  if (!(name in uiSkillWizard.fields) || uiSkillWizard.submitting) return;
  uiSkillWizard.fields[name] = value;
  if (name === 'codex' || name === 'claude') {
    if (!uiSkillWizard.fields.codex && !uiSkillWizard.fields.claude) {
      uiSkillWizard.errors.targets = '至少选择 Codex 或 Claude Code 其中一个发布目标。';
    } else {
      delete uiSkillWizard.errors.targets;
    }
    renderUISkillImportWizard();
  } else {
    delete uiSkillWizard.errors[name];
  }
  uiSkillWizard.dirty = true;
}

function wizardErrorSummary() {
  const count = Object.keys(uiSkillWizard.errors).filter(name => name !== 'submit').length;
  return count ? `<div class="skill-error-summary" role="alert">请修正以下字段后继续（${count} 项）。</div>` : '';
}

function sourceSelectionMarkup() {
  const options = [
    ['editor', '编辑器', '粘贴完整 SKILL.md'],
    ['local', '本地目录', '输入包含 SKILL.md 的本机绝对路径'],
    ['zip', 'ZIP', '输入本机 ZIP 文件绝对路径'],
    ['github', 'GitHub', '填写仓库、Skill 路径和固定 revision']
  ];
  return `<div class="skill-source-grid" role="radiogroup" aria-label="Skill 来源">${options.map(([value, title, detail]) => `
    <button type="button" class="skill-source-card" role="radio" aria-checked="${uiSkillWizard.sourceType === value}" data-skill-source="${value}" onclick="selectUISkillSource('${value}')">
      <strong>${title}</strong><span>${detail}</span>
    </button>`).join('')}</div>${wizardError('sourceType')}`;
}

function sourceFieldsMarkup() {
  const fields = uiSkillWizard.fields;
  if (uiSkillWizard.sourceType === 'editor') {
    return `<div class="skill-field">
      <label for="uiSkillEditor">完整 SKILL.md *</label>
      <textarea id="uiSkillEditor" oninput="setUISkillWizardField('skillMD', this.value)"${wizardErrorLink('skillMD')} placeholder="---&#10;name: my-ui-style&#10;description: ...&#10;---&#10;# Instructions">${esc(fields.skillMD)}</textarea>
      <div class="skill-field-help">frontmatter 至少包含 name 与 description。</div>${wizardError('skillMD')}
    </div>`;
  }
  if (uiSkillWizard.sourceType === 'local') {
    return `<div class="skill-field">
      <label for="uiSkillLocalPath">本地 Skill 目录绝对路径 *</label>
      <input id="uiSkillLocalPath" value="${esc(fields.localPath)}" oninput="setUISkillWizardField('localPath', this.value)"${wizardErrorLink('localPath')} placeholder="~/skills/my-ui-skill">
      <div class="skill-field-help">目录内必须包含 SKILL.md；文件不会上传。</div>${wizardError('localPath')}
    </div>`;
  }
  if (uiSkillWizard.sourceType === 'zip') {
    return `<div class="skill-field">
      <label for="uiSkillZipPath">本地 ZIP 绝对路径 *</label>
      <input id="uiSkillZipPath" value="${esc(fields.zipPath)}" oninput="setUISkillWizardField('zipPath', this.value)"${wizardErrorLink('zipPath')} placeholder="~/skills/my-ui-skill.zip">
      <div class="skill-field-help">审核台读取本机 ZIP 并执行路径安全与体积校验；文件不会上传。</div>${wizardError('zipPath')}
    </div>`;
  }
  return `<div class="skill-field">
      <label for="uiSkillGithubRepo">GitHub 仓库 *</label>
      <input id="uiSkillGithubRepo" value="${esc(fields.githubRepo)}" oninput="setUISkillWizardField('githubRepo', this.value)"${wizardErrorLink('githubRepo')} placeholder="owner/repository">
      ${wizardError('githubRepo')}
    </div>
    <div class="skill-field">
      <label for="uiSkillGithubPath">仓库内 Skill 路径 *</label>
      <input id="uiSkillGithubPath" value="${esc(fields.githubPath)}" oninput="setUISkillWizardField('githubPath', this.value)"${wizardErrorLink('githubPath')} placeholder="skills/my-ui-skill">
      ${wizardError('githubPath')}
    </div>
    <div class="skill-field">
      <label for="uiSkillGithubRevision">完整 revision *</label>
      <input id="uiSkillGithubRevision" value="${esc(fields.revision)}" oninput="setUISkillWizardField('revision', this.value)"${wizardErrorLink('revision')} placeholder="40 位 Git 提交哈希">
      <div class="skill-field-help">必须固定到完整提交哈希，不能使用 main、分支名或浮动标签。</div>${wizardError('revision')}
    </div>`;
}

function configurationMarkup() {
  const fields = uiSkillWizard.fields;
  return `<div class="skill-fields">
    ${sourceFieldsMarkup()}
    <div class="skill-field">
      <label for="uiSkillScope">作用域 *</label>
      <select id="uiSkillScope" onchange="setUISkillWizardField('scope', this.value)"${wizardErrorLink('scope')}>
        <option value="global"${fields.scope === 'global' ? ' selected' : ''}>全局</option>
        <option value="project"${fields.scope === 'project' ? ' selected' : ''}>当前项目：${esc(projectState.current_project || '未选择')}</option>
      </select>${wizardError('scope')}
    </div>
    <div class="skill-field">
      <label for="uiSkillVersionLabel">版本标签 *</label>
      <input id="uiSkillVersionLabel" value="${esc(fields.versionLabel)}" oninput="setUISkillWizardField('versionLabel', this.value)"${wizardErrorLink('versionLabel')} placeholder="1.0.0">
      ${wizardError('versionLabel')}
    </div>
    <fieldset class="skill-field skill-targets"${uiSkillWizard.errors.targets ? ' aria-describedby="targetsError"' : ''}>
      <legend>发布目标 *</legend>
      <label><input id="uiSkillTargetCodex" type="checkbox"${fields.codex ? ' checked' : ''} onchange="setUISkillWizardField('codex', this.checked)"> Codex</label>
      <label><input id="uiSkillTargetClaude" type="checkbox"${fields.claude ? ' checked' : ''} onchange="setUISkillWizardField('claude', this.checked)"> Claude Code</label>
    </fieldset>${wizardError('targets')}
  </div>`;
}

function skillFrontmatterHas(text, key) {
  const lines = text.split('\\n').map(line => line.trim());
  if (lines[0] !== '---') return false;
  const closing = lines.indexOf('---', 1);
  if (closing < 2) return false;
  return lines.slice(1, closing).some(line => line.startsWith(`${key}:`) && line.slice(key.length + 1).trim());
}

function validateUISkillWizardStep(step) {
  const fields = uiSkillWizard.fields;
  const errors = {};
  if (step === 1 && !uiSkillWizard.sourceType) {
    errors.sourceType = '请选择一种 Skill 来源后继续。';
  }
  if (step === 2) {
    if (uiSkillWizard.sourceType === 'editor') {
      if (!fields.skillMD.trim()) errors.skillMD = '请粘贴完整 SKILL.md。';
      else if (!skillFrontmatterHas(fields.skillMD, 'name') || !skillFrontmatterHas(fields.skillMD, 'description')) errors.skillMD = 'SKILL.md frontmatter 必须包含非空的 name 与 description。';
    }
    if (uiSkillWizard.sourceType === 'local' && !fields.localPath.trim().startsWith('/')) errors.localPath = '请输入以 / 开头的本机绝对目录路径。';
    if (uiSkillWizard.sourceType === 'zip' && !fields.zipPath.trim().startsWith('/')) errors.zipPath = '请输入以 / 开头的本机 ZIP 绝对路径。';
    if (uiSkillWizard.sourceType === 'github') {
      if (!fields.githubRepo.trim() || !fields.githubRepo.includes('/')) errors.githubRepo = '请输入 owner/repository 格式的 GitHub 仓库。';
      if (!fields.githubPath.trim()) errors.githubPath = '请输入仓库内 Skill 路径。';
      if (!/^[0-9a-fA-F]{40}$/.test(fields.revision.trim())) errors.revision = '请输入 40 位完整 Git 提交哈希。';
    }
    if (fields.scope === 'project' && !projectState.current_project) errors.scope = '当前未选择项目，请先在项目管理中选择项目。';
    if (!fields.versionLabel.trim()) errors.versionLabel = '请输入版本标签，例如 1.0.0。';
    if (!fields.codex && !fields.claude) errors.targets = '至少选择 Codex 或 Claude Code 其中一个发布目标。';
  }
  uiSkillWizard.errors = errors;
  if (Object.keys(errors).length) {
    renderUISkillImportWizard();
    const field = Object.keys(errors)[0];
    const ids = {sourceType: null, skillMD: 'uiSkillEditor', localPath: 'uiSkillLocalPath', zipPath: 'uiSkillZipPath', githubRepo: 'uiSkillGithubRepo', githubPath: 'uiSkillGithubPath', revision: 'uiSkillGithubRevision', scope: 'uiSkillScope', versionLabel: 'uiSkillVersionLabel', targets: 'uiSkillTargetCodex'};
    requestAnimationFrame(() => {
      const target = ids[field] ? document.getElementById(ids[field]) : document.querySelector('[data-skill-source]');
      if (target) target.focus();
    });
    return false;
  }
  return true;
}

function nextUISkillWizardStep() {
  if (uiSkillWizard.submitting || !validateUISkillWizardStep(uiSkillWizard.step)) return;
  uiSkillWizard.step = Math.min(3, uiSkillWizard.step + 1);
  renderUISkillImportWizard();
}

function previousUISkillWizardStep() {
  if (uiSkillWizard.submitting) return;
  uiSkillWizard.errors = {};
  uiSkillWizard.step = Math.max(1, uiSkillWizard.step - 1);
  renderUISkillImportWizard();
}

function uiSkillSourceLabel() {
  return {editor: '编辑器', local: '本地目录', zip: 'ZIP', github: 'GitHub'}[uiSkillWizard.sourceType] || '';
}

function uiSkillSourceSummary() {
  const fields = uiSkillWizard.fields;
  if (uiSkillWizard.sourceType === 'editor') {
    const firstLine = fields.skillMD.split('\\n').find(line => line.trim()) || '';
    return `${fields.skillMD.length} 个字符；首行：${firstLine}`;
  }
  if (uiSkillWizard.sourceType === 'local') return fields.localPath;
  if (uiSkillWizard.sourceType === 'zip') return fields.zipPath;
  return `${fields.githubRepo}/${fields.githubPath}\nrevision ${fields.revision}`;
}

function reviewMarkup() {
  const fields = uiSkillWizard.fields;
  const targets = [fields.codex ? 'Codex' : '', fields.claude ? 'Claude Code' : ''].filter(Boolean).join(' + ');
  const scope = fields.scope === 'project' ? `当前项目：${projectState.current_project}` : '全局';
  const rows = [
    ['来源', uiSkillSourceLabel()],
    ['来源定位', uiSkillSourceSummary()],
    ['作用域', scope],
    ['版本标签', fields.versionLabel],
    ['发布目标', targets]
  ];
  return `<div class="skill-review">${rows.map(([label, value]) => `<div class="skill-review-row"><div class="skill-review-label">${esc(label)}</div><div class="skill-review-value">${esc(value)}</div></div>`).join('')}</div>
    <div class="skill-safety-note">本操作只创建待审核草稿并执行静态校验，不会自动批准、发布或执行包内脚本。</div>
    ${uiSkillWizard.errors.submit ? `<div class="skill-submit-error" role="alert">${esc(uiSkillWizard.errors.submit)}</div>` : ''}`;
}

function uiSkillImportPayload() {
  const fields = uiSkillWizard.fields;
  let source;
  if (uiSkillWizard.sourceType === 'editor') source = {type: 'editor', files: {'SKILL.md': fields.skillMD}};
  if (uiSkillWizard.sourceType === 'local') source = {type: 'local', path: fields.localPath.trim()};
  if (uiSkillWizard.sourceType === 'zip') source = {type: 'zip', path: fields.zipPath.trim()};
  if (uiSkillWizard.sourceType === 'github') source = {type: 'github', repo: fields.githubRepo.trim(), path: fields.githubPath.trim(), revision: fields.revision.trim()};
  return {
    source,
    scope: fields.scope,
    project: fields.scope === 'project' ? projectState.current_project : null,
    targets: [fields.codex ? 'codex' : '', fields.claude ? 'claude' : ''].filter(Boolean),
    version_label: fields.versionLabel.trim(),
    idempotency_key: uiSkillWizard.idempotencyKey
  };
}

async function submitUISkillImport() {
  if (uiSkillWizard.submitting || !validateUISkillWizardStep(2)) return;
  uiSkillWizard.submitting = true;
  uiSkillWizard.errors = {};
  document.getElementById('uiSkillWizardLive').textContent = '正在导入并校验…';
  renderUISkillImportWizard();
  try {
    const result = await api('/api/ui-skills/import', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(uiSkillImportPayload())});
    const focusId = result.id ? `ui-skill-${result.id}` : '';
    closeUISkillImportWizard(true);
    await loadUISkills();
    showMessage(`已导入 ${result.name || 'UI Skill'}；草稿 ${result.id || ''}；摘要 ${result.digest || ''}；请查看校验结果后再批准与发布`);
    requestAnimationFrame(() => {
      const target = focusId ? document.getElementById(focusId) : null;
      if (target) target.focus();
    });
  } catch (error) {
    uiSkillWizard.submitting = false;
    uiSkillWizard.errors = {submit: `导入失败：${error.message} 请检查输入后重试。`};
    document.getElementById('uiSkillWizardLive').textContent = uiSkillWizard.errors.submit;
    renderUISkillImportWizard();
  }
}

function renderUISkillImportWizard() {
  const host = document.getElementById('uiSkillImportWizard');
  if (!host) return;
  host.hidden = !uiSkillWizard.open;
  if (!uiSkillWizard.open) {
    document.getElementById('uiSkillWizardContent').innerHTML = '';
    return;
  }
  const descriptions = {
    1: '选择 Skill 来源；所有来源都会先进入静态校验。',
    2: '填写来源信息、作用域、版本标签和发布目标。',
    3: '核对只读摘要后创建待审核草稿。'
  };
  let body = sourceSelectionMarkup();
  if (uiSkillWizard.step === 2) body = configurationMarkup();
  if (uiSkillWizard.step === 3) body = reviewMarkup();
  const previous = uiSkillWizard.step > 1 ? `<button type="button" onclick="previousUISkillWizardStep()"${uiSkillWizard.submitting ? ' disabled' : ''}>返回修改</button>` : '';
  const selectionDisabled = uiSkillWizard.sourceType ? '' : ' disabled';
  const fields = uiSkillWizard.fields;
  const targetsDisabled = !fields.codex && !fields.claude ? ' disabled' : '';
  const nextDisabled = uiSkillWizard.submitting ? ' disabled' : (uiSkillWizard.step === 1 ? selectionDisabled : targetsDisabled);
  const next = uiSkillWizard.step < 3
    ? `<button type="button" class="primary" onclick="nextUISkillWizardStep()"${nextDisabled}>继续</button>`
    : `<button type="button" class="primary" onclick="submitUISkillImport()"${uiSkillWizard.submitting ? ' disabled' : ''}>${uiSkillWizard.submitting ? '正在导入并校验…' : '导入并静态校验'}</button>`;
  document.getElementById('uiSkillWizardContent').innerHTML = `
    <div class="skill-wizard-head"><div><h2 id="uiSkillWizardTitle" tabindex="-1">导入 UI Skill</h2><p>${descriptions[uiSkillWizard.step]}</p></div><button type="button" aria-label="关闭导入向导" onclick="closeUISkillImportWizard()"${uiSkillWizard.submitting ? ' disabled' : ''}>关闭</button></div>
    ${wizardSteps()}
    ${wizardErrorSummary()}
    ${body}
    <div class="skill-wizard-actions">${previous}<div class="skill-wizard-actions-end"><button type="button" onclick="closeUISkillImportWizard()"${uiSkillWizard.submitting ? ' disabled' : ''}>取消</button>${next}</div></div>`;
}

async function loadUISkills() {
  const project = projectState.current_project || '';
  uiSkillState = await api(`/api/ui-skills?project=${encodeURIComponent(project)}`);
  renderUISkills();
}

function skillActions(skill) {
  const id = esc(skill.id);
  const digest = esc(skill.digest);
  const name = esc(skill.name);
  const status = skill.deployment_status || skill.status;
  const actions = [];
  if (['draft', 'validated'].includes(status)) actions.push(`<button data-ui-action="validate" data-id="${id}">重新校验</button>`);
  if (status === 'validated') actions.push(`<button class="primary" data-ui-action="approve-publish" data-id="${id}" data-digest="${digest}">批准并发布</button>`);
  if (status === 'validated') actions.push(`<button data-ui-action="request-revision" data-id="${id}">要求修改</button>`);
  if (['draft', 'validated', 'approved', 'publish_failed'].includes(skill.status)) actions.push(`<button class="danger" data-ui-action="reject" data-id="${id}">拒绝</button>`);
  if (status === 'approved') actions.push(`<button class="primary" data-ui-action="publish" data-id="${id}" data-digest="${digest}">发布到双端</button>`);
  if (['published', 'disabled'].includes(status)) actions.push(`<button data-ui-action="rollback" data-name="${name}" data-version="${esc(skill.version_id)}">回滚此版本</button>`);
  if (status === 'published') actions.push(`<button class="danger" data-ui-action="disable" data-name="${name}">停用双端 Skill</button>`);
  return actions.join('');
}

function renderUISkills() {
  if (currentView !== 'uiSkills') return;
  const skills = uiSkillState.items || [];
  const discovered = uiSkillState.discovered || [];
  document.getElementById('counts').innerHTML = [
    `受管 Skill ${skills.length}`,
    `发现目录 ${discovered.length}`,
    `待审批 ${skills.filter(x => x.status === 'validated').length}`
  ].map(x => `<span class="pill">${esc(x)}</span>`).join('');
  const managed = skills.map(skill => `<section id="ui-skill-${esc(skill.id)}" class="item" tabindex="-1">
    <div class="item-head"><div><div class="summary">${esc(skill.name)}</div>
      <div class="meta">${esc(skill.id)} · ${esc(skill.deployment_status || skill.status)} · ${esc(skill.scope && skill.scope.type)} · ${esc((skill.targets || []).join(' + '))}</div>
      <div class="label-row"><span class="tag">摘要 ${esc(skill.digest)}</span><span class="tag">来源 ${esc(JSON.stringify(skill.source || {}))}</span><span class="tag">许可证 ${esc(JSON.stringify((skill.validation_report || {}).license || '未声明'))}</span><span class="tag">脚本 ${esc(JSON.stringify((skill.validation_report || {}).scripts || []))}</span></div>
    </div><span class="tag">${esc(skill.version_label)}</span></div>
    <div class="content-title">SKILL.md（只读审核内容）</div>
    <pre class="doc-section">${esc(skill.skill_md || '')}</pre>
    <details class="memory-entry"><summary>校验报告、差异与发布详情</summary><pre>${esc(JSON.stringify({validation: skill.validation_report || {}, diff: skill.diff || null, details: skill.status_details || {}}, null, 2))}</pre></details>
    <div class="actions">${skillActions(skill)}</div>
  </section>`).join('');
  const unmanaged = discovered.map(item => `<section class="item"><div class="item-head"><div><div class="summary">${esc(item.name || item.path)}</div><div class="meta">${esc(item.status)} · ${esc(item.agent)} · ${esc(item.path)}</div><div class="label-row"><span class="tag">${esc(item.digest)}</span>${item.name_conflict ? '<span class="tag warn">同名摘要冲突</span>' : ''}</div></div></div></section>`).join('');
  document.getElementById('items').innerHTML = (managed || '<div class="empty"><h2>暂无受管 UI Skill</h2><p>使用“导入 UI Skill”从编辑器、本地目录、ZIP 或 GitHub 创建待审核草稿。</p></div>') + (unmanaged ? `<div class="section-title">未管理 Skill（只读发现）</div>${unmanaged}` : '');
}

async function uiSkillMutation(route, payload, dangerous, promptText) {
  if (dangerous && !confirm(promptText || '确认执行此双端 UI Skill 操作？')) return null;
  const result = await api(`/api/ui-skills/${route}`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({...payload, confirmed: dangerous, idempotency_key: idempotencyKey(`skill-${route}`)})});
  await loadUISkills();
  return result;
}

async function handleUISkillAction(button) {
  const action = button.dataset.uiAction;
  const draft_id = button.dataset.id;
  const digest = button.dataset.digest;
  if (action === 'validate') await uiSkillMutation('validate', {draft_id}, false);
  if (action === 'request-revision') await uiSkillMutation('request-revision', {draft_id, reason: prompt('请输入修改要求') || '需要修改'}, false);
  if (action === 'reject') await uiSkillMutation('reject', {draft_id, reason: prompt('请输入拒绝原因') || ''}, true, '确认拒绝此 UI Skill 草稿？');
  if (action === 'publish') await uiSkillMutation('publish', {draft_id, digest, project: projectState.current_project}, true, '确认原子发布到 Codex 与 Claude Code？');
  if (action === 'approve-publish') {
    if (!confirm('确认批准此摘要，并原子发布到 Codex 与 Claude Code？')) return;
    await api('/api/ui-skills/approve', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({draft_id, digest, confirmed: true, idempotency_key: idempotencyKey('skill-approve')})});
    await api('/api/ui-skills/publish', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({draft_id, digest, project: projectState.current_project, confirmed: true, idempotency_key: idempotencyKey('skill-publish')})});
    await loadUISkills();
  }
  if (action === 'rollback') await uiSkillMutation('rollback', {name: button.dataset.name, version: button.dataset.version, project: projectState.current_project}, true, '确认将 Codex 与 Claude Code 同时回滚到此版本？');
  if (action === 'disable') await uiSkillMutation('disable', {name: button.dataset.name, project: projectState.current_project}, true, '确认同时停用 Codex 与 Claude Code 中的此 Skill？');
}

function renderMemoryStrategy() {
  document.getElementById('counts').innerHTML = [
    '中心审核台',
    '项目记忆按仓库隔离',
    '个人记忆全局审批',
    '新项目可一键初始化',
    'Loop 可选初始化'
  ].map(x => `<span class="pill">${esc(x)}</span>`).join('');
  document.getElementById('items').innerHTML = `
    <div class="doc">
      <section class="doc-hero">
        <h2>记忆策略完整使用说明</h2>
        <p>这套策略把“审核台代码”和“记忆数据”分开：审核台作为本机跨项目管理平台统一维护；项目长短期记忆属于具体仓库；个人长短期记忆属于用户本人，并且必须审批后才正式生效。</p>
      </section>
      <div class="doc-grid">
        <section class="doc-section">
          <h3>中心审核台</h3>
          <ul>
            <li>代码仓库：<code>&lt;repo-root&gt;</code></li>
            <li>本机地址：<code>http://127.0.0.1:8897/</code></li>
            <li>项目注册表：<code>~/.codex/memory_review/projects.json</code></li>
            <li>审核台只允许本机访问，负责查看候选、审批、编辑、删除、项目切换和初始化。</li>
          </ul>
        </section>
        <section class="doc-section">
          <h3>项目记忆</h3>
          <ul>
            <li>每个仓库独立维护 <code>codex/codex_long_memory.md</code> 和 <code>codex/codex_short_memory.md</code>。</li>
            <li>项目长期记忆记录稳定架构、产品方向、部署规则、数据库/OSS/服务约定和 loop guardrails。</li>
            <li>项目短期记忆记录近期指令、当前状态、hook 事件、loop 轮次、Claude 评测摘要和未解决问题。</li>
            <li>项目长期记忆默认先进入 <code>codex/memory_proposals.md</code>，审核后再生效。</li>
          </ul>
        </section>
        <section class="doc-section">
          <h3>个人记忆</h3>
          <ul>
            <li>个人长期记忆：<code>~/.codex/personal_memory/long.md</code></li>
            <li>个人短期记忆：<code>~/.codex/personal_memory/short.md</code></li>
            <li>个人候选：<code>~/.codex/personal_memory/proposals.md</code></li>
            <li>个人记忆只记录跨项目的开发习惯、协作偏好、思维方式、工作流偏好或用户画像。</li>
            <li>禁止把原始会话、PRD、截图描述、hook payload、系统提示或一次性项目任务写成个人记忆。</li>
          </ul>
        </section>
        <section class="doc-section">
          <h3>审批规则</h3>
          <ul>
            <li>个人长短期记忆必须由用户审批具体内容后才能正式写入。</li>
            <li>项目长期记忆应审批后写入，避免把临时判断沉淀成长期事实。</li>
            <li>项目短期记忆可由 hook 自动追加，但展示时只读最近或摘要。</li>
            <li>敏感信息会标红风险，但最终是否批准由用户判断。</li>
            <li>token、验证码、API key、RDS/OSS 密钥、密码不应写入任何记忆。</li>
          </ul>
        </section>
      </div>
      <section class="doc-section">
        <h3>候选记忆写入来源</h3>
        <p>审核台本身不主动总结候选。当前 Codex 或 Claude Code 对话模型负责提炼，hook 只生成本地提醒和上下文检查点。</p>
        <ul>
          <li>Codex 使用当前 Codex 模型总结；Claude Code 使用当前 Claude 模型总结，不额外调用第三方模型 API。</li>
          <li>全局和项目 hook 注册在对应配置中，负责在 <code>UserPromptSubmit</code>、<code>PreCompact</code>、<code>Stop</code> 等节点更新提醒。</li>
          <li>个人候选写入：<code>~/.codex/personal_memory/proposals.md</code>。</li>
          <li>项目候选写入当前项目：<code>&lt;project&gt;/codex/memory_proposals.md</code>。</li>
        </ul>
      </section>
      <section class="doc-section">
        <h3>候选生成策略</h3>
        <ul>
          <li>个人候选必须是提炼后的跨项目事实，例如开发习惯、协作偏好、思维方式、工作流偏好或用户画像。</li>
          <li>当前对话模型必须把候选改写为标题、分类和 1-3 句独立总结，禁止复制原始 prompt。</li>
          <li>模型排除截图、URL、路径、系统提示、一次性任务和敏感信息；每轮最多生成 2 条并去重。</li>
          <li>项目长期候选只允许稳定架构、部署、产品、技术约束或项目工作流事实。</li>
          <li>项目短期记忆可由 hook 自动追加，记录近期指令、hook 事件、当前状态、loop 轮次和临时上下文。</li>
        </ul>
      </section>
      <section class="doc-section">
        <h3>正式记忆生效方式</h3>
        <ul>
          <li>个人长期记忆只有在用户审批具体候选后，才写入 <code>~/.codex/personal_memory/long.md</code>。</li>
          <li>个人短期记忆只有在用户审批具体候选后，才写入 <code>~/.codex/personal_memory/short.md</code>。</li>
          <li>项目长期记忆只有在用户审批项目候选后，才写入 <code>&lt;project&gt;/codex/codex_long_memory.md</code>。</li>
          <li>项目短期记忆自动写入 <code>&lt;project&gt;/codex/codex_short_memory.md</code>，不作为长期事实直接生效。</li>
          <li>审核台批准候选时，会记录审批状态到 <code>&lt;project&gt;/codex/memory_review_state.json</code>，并刷新 <code>&lt;project&gt;/codex/memory_review_queue.json</code>。</li>
          <li>Codex/Claude 后续通过项目 context packet、个人 context packet、项目/个人长短期记忆文件感知已生效记忆。</li>
        </ul>
      </section>
      <section class="doc-section">
        <h3>自动化写入边界</h3>
        <ul>
          <li>允许自动写入：项目短期记忆、context packet、review queue、review state、个人候选 proposals。</li>
          <li>允许当前 Codex/Claude 模型写入总结后的个人或项目候选；hook 本身只写提醒，不复制原始 prompt。</li>
          <li>禁止自动写入：正式个人长期记忆、正式个人短期记忆。</li>
          <li>默认禁止自动写入：正式项目长期记忆，除非用户明确批准候选或明确要求写入具体内容。</li>
          <li>审核台可以编辑、删除已生效记忆；这属于用户在本地页面上的显式操作。</li>
          <li>任何包含密钥、token、验证码、密码或生产环境敏感配置的内容都不应写入候选或正式记忆。</li>
        </ul>
      </section>
      <section class="doc-section">
        <h3>新项目接入步骤</h3>
        <ol>
          <li>在“项目管理”输入项目仓库路径。</li>
          <li>点击“初始化项目记忆”，创建项目记忆文件和 Codex/Claude hooks。</li>
          <li>在 Codex 中运行 <code>/hooks</code> 并信任新 hook；Claude Code 新会话会读取 <code>CLAUDE.md</code> 和 <code>.claude/settings.json</code>。</li>
          <li>之后 Codex/Claude 每次工作都会读取相关项目记忆和个人记忆指针。</li>
        </ol>
      </section>
      <section class="doc-section">
        <h3>Loop 项目接入步骤</h3>
        <ol>
          <li>在“项目管理”输入项目路径。</li>
          <li>确认推荐 staging 端口，或手动输入端口。</li>
          <li>新项目点击“初始化 Loop × Superpowers”；已有 Loop 项目点击“预览升级 Loop”，确认预览后显式升级。</li>
          <li>如页面提示规则或钩子过期，单独点击“升级记忆规则/钩子”；不会新增独立 Superpowers hook。</li>
          <li>后续在 Codex 中贴 Markdown PRD，并要求先生成验收标准。</li>
          <li>Codex 负责开发和 staging，Claude Code 负责 Playwright/浏览器验收，直到通过或触发暂停条件。</li>
        </ol>
      </section>
      <section class="doc-section">
        <h3>CLI 等价命令</h3>
        <pre>python3 &lt;path-to-vibe_coding_manage_platform&gt;/scripts/memory_project.py register /path/to/repo
python3 &lt;path-to-vibe_coding_manage_platform&gt;/scripts/memory_project.py init /path/to/repo
python3 &lt;path-to-vibe_coding_manage_platform&gt;/scripts/memory_project.py init-loop /path/to/repo --port 8082
python3 &lt;path-to-vibe_coding_manage_platform&gt;/scripts/memory_project.py upgrade-loop /path/to/repo
python3 &lt;path-to-vibe_coding_manage_platform&gt;/scripts/memory_project.py list
python3 &lt;path-to-vibe_coding_manage_platform&gt;/scripts/memory_project.py use /path/to/repo</pre>
      </section>
    </div>
  `;
}

function renderLoopDocs() {
  document.getElementById('counts').innerHTML = [
    'Codex 开发',
    'Claude Code 验收',
    'Playwright 默认测试',
    '多对话 Worktree 隔离',
    'Release / Staging 串行锁',
    '原仓库自动安全同步',
    '最多 10 轮',
    '合并和正式上线需用户确认'
  ].map(x => `<span class="pill">${esc(x)}</span>`).join('');
  document.getElementById('items').innerHTML = `
    <div class="doc">
      <section class="doc-hero">
        <h2>Loop 开发使用说明</h2>
        <p>Loop engineering 是一套支持多对话隔离开发、串行安全发布的跨项目流程。Loop 是唯一生命周期编排器，负责需求验收、worktree、分支、staging、独立评测、release、主分支和 production；Superpowers 是阶段内工程方法，负责构思、计划、TDD、系统调试、代码审查和完成前验证。</p>
      </section>

      <section class="doc-section">
        <h3>Loop × Superpowers 标准阶段</h3>
        <ol>
          <li><code>using-superpowers</code> 判断任务路径；新行为先通过 <code>brainstorming</code> 形成用户批准的设计。</li>
          <li><code>writing-plans</code> 写出可执行计划，再由 Loop 创建和登记独立 worktree。</li>
          <li>实施使用 TDD：先看到失败测试，再做最小实现；Bug 和评测 finding 先用系统调试定位根因。</li>
          <li>内部规格与质量审查通过后，Claude Code 执行独立评测。</li>
          <li>完成前验证器检查设计、验收、计划、报告状态、分支和不可变 tested commit；之后才允许 <code>finish</code> 等待用户验收。</li>
        </ol>
        <p>子代理和并行代理必须获得用户明确授权，并使用互不冲突的 Loop worktree。Superpowers 不得绕过 Loop 合并主分支、占用共享 staging 或部署 production。</p>
      </section>

      <div class="doc-grid">
        <section class="doc-section">
          <h3>Worktree 优先</h3>
          <ul>
            <li>原始仓库是 canonical workspace，默认保持在主分支并作为 <code>origin/master</code> 的本地镜像；日常开发不在原始仓库进行。</li>
            <li>当用户说 <code>开 worktree</code>，Codex 应在原始仓库之外创建专用 git worktree，再开始实质性项目工作。</li>
            <li>一个任务或 loop 功能对应一个对话、一个外部 worktree、一个分支；多个对话可以并行开发，但不能共享 worktree 或分支。</li>
            <li>主开发对话拥有产品源码修改；review、eval、报告和实验对话使用辅助 worktree/分支。</li>
            <li>功能开发可并行；主分支合并、主分支 push、原始仓库同步和共享 staging 部署必须通过仓库级锁串行执行。</li>
            <li>staging 默认由一个活跃分支占用；并行 staging 必须显式隔离远程路径、端口、测试数据和 OSS 前缀。</li>
          </ul>
        </section>

        <section class="doc-section">
          <h3>角色分工</h3>
          <ul>
            <li><strong>用户</strong>：提供 PRD，确认验收标准，做最终产品验收，批准合并主分支和正式上线。</li>
            <li><strong>Codex</strong>：读取 loop 配置，创建 loop 分支，开发代码，commit/push，部署 staging，读取 Claude 报告并修复。</li>
            <li><strong>Claude Code</strong>：通过 <code>claude</code> 命令接收测试任务，默认用 Playwright 测试，也可使用 Claude Code 内部浏览器能力，输出结构化报告。</li>
          </ul>
        </section>

        <section class="doc-section">
          <h3>核心边界</h3>
          <ul>
            <li>loop 过程只在 <code>loop/&lt;project&gt;-&lt;date&gt;-&lt;slug&gt;</code> 开发分支进行。</li>
            <li>每轮允许自动 commit/push，但不能自动合并 <code>master</code>。</li>
            <li>正式上线前必须再次询问用户。</li>
            <li>主分支整合在基于最新远端主分支的临时 release worktree 进行，不能在脏的原始仓库里解决冲突。</li>
            <li>禁止 force push、自动 stash、reset、checkout 覆盖或删除用户文件。</li>
            <li>最终完成必须验证功能提交已进入主分支，且远端主分支、原始仓库和部署 commit 一致。</li>
            <li>报告、记忆、测试产物禁止记录 token、验证码、API key、RDS/OSS 密钥等敏感信息。</li>
          </ul>
        </section>
      </div>

      <section class="doc-section">
        <h3>多对话、Release 队列与原仓库同步</h3>
        <ol>
          <li>每个对话通过 <code>worktree_flow.py start</code> 注册唯一任务所有权，并从最新远端主分支创建外部 worktree。</li>
          <li>各对话独立开发、提交、push 和评测，达到验收状态后运行 <code>finish</code>，但不能自动合并主分支。</li>
          <li>用户批准合并后，任务进入仓库级 release 队列；同一时间只有一个 release lock 持有者。</li>
          <li>release worktree 合并最新远端主分支与功能分支，解决冲突并执行完整测试；远端主分支并发更新会让普通 push 安全失败。</li>
          <li>push 成功后以 <code>ff-only</code> 同步原始仓库。原始仓库分支不正确、历史分叉或脏文件路径重叠时停止并报告，不覆盖用户修改。</li>
          <li>共享 staging 部署使用 staging lock，并部署确定的远端主分支 commit。</li>
          <li>只有远端主分支、原始仓库、部署 commit 一致，且功能 commit 是主分支祖先时，才可报告最终完成。</li>
        </ol>
      </section>

      <section class="doc-section">
        <h3>在已经接入 loop 的仓库中启动</h3>
        <p>如果当前仓库已经存在 <code>.loop/config.json</code>，直接在 Codex 对话中输入：</p>
        <pre>按当前仓库 .loop/config.json 启动 loop 开发。

这是 PRD：
&lt;你的 Markdown PRD&gt;

先开 worktree，再生成 loop/acceptance/criteria.md，让我确认验收标准。</pre>
        <p>Codex 会先读取项目 loop 配置、个人级 Codex/Claude loop 目录，并按 worktree-first 规则创建或使用专用 worktree，然后生成验收标准。你确认后才进入开发循环。</p>
      </section>

      <section class="doc-section">
        <h3>在新仓库首次接入 loop</h3>
        <p>如果当前仓库还没有 <code>.loop/config.json</code>，先输入：</p>
        <pre>读取 ~/.codex/loop_engineering 和 ~/.claude/loop_engineering，为当前仓库接入 loop engineering。
我会以 markdown 提供 PRD；请先创建/检查 .loop/config.json，生成验收标准让我确认，不要合并 master，不要部署正式服务。</pre>
        <p>项目内应创建这些文件和目录：</p>
        <pre>.loop/config.json
loop/prd/
loop/acceptance/
loop/reports/
loop/state/
loop/claude_tests/
scripts/loop_controller.py
scripts/run_claude_eval.sh
scripts/deploy_staging.sh</pre>
      </section>

      <section class="doc-section">
        <h3>项目配置命名规则</h3>
        <p>每个项目都应有自己的 <code>.loop/config.json</code>。跨项目默认命名规则：</p>
        <pre>{
  "project_repo_name": "&lt;project_repo_name&gt;",
  "branch.name_format": "loop/&lt;project&gt;-&lt;date&gt;-&lt;slug&gt;",
  "staging.database": "&lt;project_repo_name&gt;_loop_staging",
  "staging.oss_bucket": "&lt;project_repo_name&gt;-loop-staging",
  "staging.remote_path": "/root/&lt;project_repo_name&gt;_loop_staging"
}</pre>
        <p>staging 端口按项目递增，例如当前项目使用 <code>8081</code>，其他项目可用 <code>8082</code>、<code>8083</code>。远程服务器系统防火墙和阿里云安全组都需要放行对应端口。</p>
      </section>

      <section class="doc-section">
        <h3>完整循环流程</h3>
        <ol>
          <li>用户在对话中贴 Markdown PRD。</li>
          <li>Codex 保存或同步 PRD 到 <code>loop/prd/current_prd.md</code>。</li>
          <li>Codex 生成 <code>loop/acceptance/criteria.md</code>。</li>
          <li>用户确认验收标准。</li>
          <li>Codex 通过中央 Worktree CLI 创建外部专用 worktree，并创建唯一 <code>loop/&lt;project&gt;-&lt;date&gt;-&lt;slug&gt;</code> 分支。</li>
          <li>Codex 开发、运行本地验证、commit/push。</li>
          <li>Codex 部署远程 staging。</li>
          <li>Codex 通过 <code>claude -p</code> 下发测试任务给 Claude Code。</li>
          <li>Claude Code 默认用 Playwright 测试，可补充内部浏览器评测。</li>
          <li>Claude Code 输出 <code>loop/reports/claude_eval_latest.md</code> 和 <code>loop/reports/claude_eval_latest.json</code>。</li>
          <li>Codex 读取报告继续修复并进入下一轮。</li>
          <li>循环通过、达到停止条件，或需要用户判断时暂停。</li>
          <li>用户批准合并后进入串行 release 队列，在临时 release worktree 合并最新主分支并完整测试。</li>
          <li>推送主分支后安全同步原始仓库，部署主分支测试服务，并验证提交一致性。</li>
        </ol>
      </section>

      <div class="doc-grid">
        <section class="doc-section">
          <h3>停止条件</h3>
          <ul>
            <li>Claude Code 一轮完整验收通过。</li>
            <li>达到最大自动轮数，默认 <code>10</code> 轮。</li>
            <li>连续两轮出现同一个 P0/P1 问题。</li>
            <li>需要用户做产品、权限、付费额度、生产资源或安全判断。</li>
            <li>涉及合并主分支或正式上线。</li>
          </ul>
        </section>

        <section class="doc-section">
          <h3>报告要求</h3>
          <ul>
            <li>Markdown 报告：<code>loop/reports/claude_eval_latest.md</code></li>
            <li>JSON 报告：<code>loop/reports/claude_eval_latest.json</code></li>
            <li>每个问题应包含严重级别、标题、复现步骤、预期结果、实际结果。</li>
            <li>敏感信息必须脱敏，不得写入验证码、token、密钥、数据库密码、OSS AccessKey。</li>
          </ul>
        </section>
      </div>

      <section class="doc-section">
        <h3>Worktree 常用命令</h3>
        <pre>python3 &lt;path-to-vibe_coding_manage_platform&gt;/scripts/worktree_flow.py start /path/to/repo \
  --task &lt;slug&gt; --conversation &lt;conversation-id&gt;

python3 &lt;path-to-vibe_coding_manage_platform&gt;/scripts/worktree_flow.py status /path/to/repo
python3 &lt;path-to-vibe_coding_manage_platform&gt;/scripts/worktree_flow.py finish /path/to/repo --task &lt;slug&gt;

# 仅在用户明确批准合并主分支后：
python3 &lt;path-to-vibe_coding_manage_platform&gt;/scripts/worktree_flow.py release /path/to/repo \
  --task &lt;slug&gt; --approved --test-command "python3 -m pytest -q tests"

python3 &lt;path-to-vibe_coding_manage_platform&gt;/scripts/worktree_flow.py sync-canonical /path/to/repo
python3 &lt;path-to-vibe_coding_manage_platform&gt;/scripts/worktree_flow.py deploy-staging /path/to/repo \
  --task &lt;slug&gt; --approved --command "./scripts/deploy_staging.sh" \
  --deployed-commit-command "./scripts/deployed_commit.sh"
python3 &lt;path-to-vibe_coding_manage_platform&gt;/scripts/worktree_flow.py verify /path/to/repo --task &lt;slug&gt;
python3 &lt;path-to-vibe_coding_manage_platform&gt;/scripts/worktree_flow.py cleanup /path/to/repo --task &lt;slug&gt; --approved</pre>
        <p>运行状态和锁保存在 <code>~/.codex/worktree_manager/</code>，不提交到项目仓库。清理只允许删除已经进入远端主分支且工作目录干净的本地 worktree；删除远端功能分支仍需单独授权。</p>
      </section>

      <section class="doc-section">
        <h3>常用命令</h3>
        <p><code>python3 scripts/loop_controller.py status</code></p>
        <ul>
          <li>使用场景：查看当前仓库是否已接入 loop、当前分支、staging 配置、Claude 测试配置和 loop 状态。</li>
          <li>使用样例：开始 loop 前先执行，确认端口、远程路径、数据库、OSS bucket 是否符合预期。</li>
        </ul>
        <pre>python3 scripts/loop_controller.py status</pre>

        <p><code>python3 scripts/loop_controller.py init --slug &lt;需求简称&gt;</code></p>
        <ul>
          <li>使用场景：在用户确认验收标准后，创建或切换到 loop 开发分支，并初始化 <code>loop/state/loop_state.json</code>。</li>
          <li>使用样例：PRD 是“新增团队邀请功能”，可以用 <code>team-invite</code> 作为 slug。</li>
        </ul>
        <pre>python3 scripts/loop_controller.py init --slug team-invite</pre>

        <p><code>python3 scripts/loop_controller.py save-prd &lt;markdown文件&gt;</code></p>
        <ul>
          <li>使用场景：用户把 PRD 放在某个 Markdown 文件里时，将它复制为标准入口 <code>loop/prd/current_prd.md</code>。</li>
          <li>使用样例：把 <code>docs/team_invite_prd.md</code> 作为本轮 loop 的 PRD。</li>
        </ul>
        <pre>python3 scripts/loop_controller.py save-prd docs/team_invite_prd.md</pre>

        <p><code>python3 scripts/loop_controller.py next-round</code></p>
        <ul>
          <li>使用场景：进入下一轮 Codex 开发、staging 部署、Claude 测试前，递增当前 loop 轮次。</li>
          <li>使用样例：Claude 报告有 P1/P2 问题，Codex 修复前先把状态推进到下一轮。</li>
        </ul>
        <pre>python3 scripts/loop_controller.py next-round</pre>

        <p><code>python3 scripts/loop_controller.py guard-loop-branch</code></p>
        <ul>
          <li>使用场景：执行自动 commit、push、deploy 前确认当前分支是 <code>loop/...</code>，避免误在 <code>master</code> 上跑 loop 自动化。</li>
          <li>使用样例：部署 staging 前脚本会自动调用，也可以手动检查。</li>
        </ul>
        <pre>python3 scripts/loop_controller.py guard-loop-branch</pre>

        <p><code>./scripts/deploy_staging.sh</code></p>
        <ul>
          <li>使用场景：Codex 完成本轮开发并 commit/push 后，将当前 loop 分支部署到远程 staging。</li>
          <li>使用样例：在 <code>loop/noema-ai-box-20260709-team-invite</code> 分支上部署到 <code>http://8.210.155.175:8081</code>。</li>
          <li>注意：脚本会尝试打开服务器系统防火墙端口；阿里云安全组仍可能需要额外放行对应端口。</li>
        </ul>
        <pre>./scripts/deploy_staging.sh</pre>

        <p><code>./scripts/run_claude_eval.sh</code></p>
        <ul>
          <li>使用场景：staging 部署完成后，让 Claude Code 读取项目、PRD、验收标准和 staging URL，默认用 Playwright 执行独立测试。</li>
          <li>使用样例：默认读取 <code>loop/prd/current_prd.md</code>，并要求 Claude 写入 <code>loop/reports/claude_eval_latest.md</code> 和 <code>loop/reports/claude_eval_latest.json</code>。</li>
        </ul>
        <pre>./scripts/run_claude_eval.sh</pre>

        <p><code>./scripts/run_claude_eval.sh &lt;prompt或PRD文件&gt;</code></p>
        <ul>
          <li>使用场景：本轮只想让 Claude 针对某个临时测试说明或指定 PRD 文件评测。</li>
          <li>使用样例：让 Claude 按 <code>loop/prd/regression_round_2.md</code> 执行第二轮回归。</li>
        </ul>
        <pre>./scripts/run_claude_eval.sh loop/prd/regression_round_2.md</pre>
      </section>

      <section class="doc-section">
        <h3>noema_ai_box 当前配置</h3>
        <ul>
          <li>项目配置：<code>.loop/config.json</code></li>
          <li>远程 staging：<code>root@&lt;staging-host&gt;</code></li>
          <li>staging 端口：<code>8081</code></li>
          <li>staging 地址：<code>http://8.210.155.175:8081</code></li>
          <li>staging 数据库：<code>noema_ai_box_loop_staging</code></li>
          <li>staging OSS bucket：<code>noema-ai-box-loop-staging</code></li>
          <li>远程路径：<code>/root/noema_ai_box_loop_staging</code></li>
        </ul>
      </section>

      <section class="doc-section">
        <h3>最短启动口令</h3>
        <pre>按当前仓库 .loop/config.json 启动 loop 开发，并先开 worktree。

这是 PRD：
&lt;你的 Markdown PRD&gt;

先生成验收标准让我确认。不要合并 master，不要部署正式服务。</pre>
      </section>

      <section class="doc-section">
        <h3>个人记忆自动候选规则</h3>
        <p>自动生成的个人长期/短期记忆候选只应记录你这个人如何工作、如何思考、如何希望 AI 协作。候选必须是提炼后的跨项目事实，而不是原始会话内容。</p>
        <ul>
          <li>可以生成：常用开发习惯、思维方式、协作偏好、工作流偏好、稳定用户画像。</li>
          <li>不要生成：原始对话、PRD、截图描述、hook payload、系统提示、ambient suggestion、一次性项目任务。</li>
          <li>个人长期记忆：长期稳定、跨项目复用的习惯或偏好。</li>
          <li>个人短期记忆：近期阶段性、跨项目有用的个人上下文。</li>
          <li>正式个人长短期记忆仍必须由用户审批后才能生效。</li>
        </ul>
      </section>
    </div>
  `;
}

async function approveItem(id, target) {
  const content = document.getElementById(`content-${id}`).value;
  await api('/api/approve', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id, target, content})});
  showMessage(`已批准 ${id}`);
  await loadQueue();
}
async function rejectItem(id) {
  await api('/api/reject', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id})});
  showMessage(`已拒绝 ${id}`);
  await loadQueue();
}
async function deferItem(id) {
  await api('/api/defer', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id})});
  showMessage(`已暂缓 ${id}`);
  await loadQueue();
}
async function resetItem(id) {
  await api('/api/reset', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id})});
  showMessage(`已重置 ${id}`);
  await loadQueue();
}

async function saveActiveMemory(sourceId, itemId) {
  const content = document.getElementById(`memory-content-${itemId}`).value;
  activeMemory = await api('/api/active-memory/update', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({source_id: sourceId, item_id: itemId, content})
  });
  showMessage(`已保存 ${itemId}`);
  renderMemory();
}

async function deleteActiveMemory(sourceId, itemId) {
  if (!confirm('确定删除这条已生效记忆吗？此操作会直接修改对应记忆文件。')) return;
  activeMemory = await api('/api/active-memory/delete', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({source_id: sourceId, item_id: itemId})
  });
  showMessage(`已删除 ${itemId}`);
  renderMemory();
}

document.getElementById('items').addEventListener('click', event => {
  const uiDesignButton = event.target.closest('button[data-ui-design-action]');
  if (uiDesignButton) {
    handleUIDesignApprovalAction(uiDesignButton).catch(err => showMessage(err.message));
    return;
  }
  const uiSkillButton = event.target.closest('button[data-ui-action]');
  if (uiSkillButton) {
    handleUISkillAction(uiSkillButton).catch(err => showMessage(err.message));
    return;
  }
  const memoryButton = event.target.closest('button[data-memory-action]');
  if (memoryButton) {
    const sourceId = memoryButton.dataset.source;
    const itemId = memoryButton.dataset.id;
    if (memoryButton.dataset.memoryAction === 'save') saveActiveMemory(sourceId, itemId);
    if (memoryButton.dataset.memoryAction === 'delete') deleteActiveMemory(sourceId, itemId);
    return;
  }
  const button = event.target.closest('button[data-action]');
  if (button) {
    const id = button.dataset.id;
    const action = button.dataset.action;
    if (!id || !action) return;
    if (action === 'approve') approveItem(id, button.dataset.target);
    if (action === 'reject') rejectItem(id);
    if (action === 'defer') deferItem(id);
    if (action === 'reset') resetItem(id);
  }
});
document.getElementById('status').addEventListener('change', render);
document.getElementById('scope').addEventListener('change', render);
document.getElementById('memoryScope').addEventListener('change', renderMemory);
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && uiSkillWizard.open) closeUISkillImportWizard();
});
loadQueue().catch(err => showMessage(err.message));
loadProjects().catch(err => showMessage(err.message));
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def send_json(self, value: object, status: int = 200) -> None:
        payload = json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def read_json(self) -> dict:
        host = self.headers.get("Host", "")
        allowed_hosts = {"127.0.0.1", "localhost", "::1"}
        try:
            parsed_host = urlparse(f"//{host}")
            host_name = parsed_host.hostname
            host_port = parsed_host.port or PORT
        except ValueError as error:
            raise ValueError("Host is invalid") from error
        if host_name not in allowed_hosts or host_port != PORT:
            raise ValueError("Host must be loopback")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json")
        origin = self.headers.get("Origin")
        if origin:
            parsed_origin = urlparse(origin)
            try:
                origin_port = parsed_origin.port or 80
            except ValueError as error:
                raise ValueError("Origin is invalid") from error
            if (
                parsed_origin.scheme != "http"
                or parsed_origin.hostname != host_name
                or origin_port != host_port
            ):
                raise ValueError("Origin must exactly match Host")
        raw_length = self.headers.get("Content-Length", "0") or "0"
        try:
            length = int(raw_length)
        except (TypeError, ValueError) as error:
            raise ValueError("Content-Length must be an integer") from error
        if not 0 <= length <= MAX_JSON_BODY:
            raise ValueError("JSON body exceeds 64 KiB")
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw or "{}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in UI_DESIGN_GET_ROUTES:
            try:
                query = parse_qs(parsed.query, keep_blank_values=True)
                self.send_json(ui_design_get(parsed.path, query))
            except Exception as exc:  # noqa: BLE001 - local admin tool surfaces errors.
                self.send_json({"error": str(exc)}, status=ui_design_error_status(exc))
            return
        if parsed.path == "/health":
            self.send_json(health_payload())
            return
        if parsed.path == "/api/settings":
            self.send_json(settings_payload())
            return
        if parsed.path == "/api/queue":
            self.send_json(review.load_queue(refresh=True))
            return
        if parsed.path == "/api/active-memory":
            self.send_json(active_memory_payload())
            return
        if parsed.path == "/api/projects":
            self.send_json(project_payload())
            return
        if parsed.path == "/":
            force_first_run = parse_qs(parsed.query).get("first-run") == ["1"]
            show_first_run = force_first_run or not bool(settings_payload()["first_run_complete"])
            payload = (first_run_page() if show_first_run else page()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path in UI_DESIGN_POST_ROUTES:
                self.send_json(ui_design_post(parsed.path, self.read_json()))
                return
            if parsed.path == "/api/settings/first-run":
                result = save_first_run_settings(self.read_json())
                if result.get("bootout_after_response"):
                    result["service_action"] = "bootout_scheduled"
                    action_paths = vibe_memory_paths.for_home()
                    generation = str(result["service_action_generation"])
                self.send_json(result)
                if result.get("bootout_after_response"):
                    threading.Thread(
                        target=scheduled_bootout_worker,
                        args=(action_paths, generation),
                        daemon=False,
                    ).start()
                return
            if parsed.path == "/api/projects/register":
                body = self.read_json()
                payload = switch_project(body.get("project_root", ""))
                self.send_json(payload)
                return
            if parsed.path == "/api/projects/use":
                body = self.read_json()
                payload = switch_project(body.get("project_root", ""))
                self.send_json(payload)
                return
            if parsed.path == "/api/projects/init":
                body = self.read_json()
                result = memory_project.init_project(body.get("project_root", ""))
                payload = switch_project(result["project"]["root"])
                self.send_json({"ok": True, "changes": result.get("changes", []), "projects": payload})
                return
            if parsed.path == "/api/projects/init-loop":
                body = self.read_json()
                result = project_operation("init-loop", body)
                payload = switch_project(result["project"]["root"])
                self.send_json({
                    "ok": True,
                    "port": result.get("port"),
                    "changes": result.get("changes", []),
                    "projects": payload,
                })
                return
            if parsed.path == "/api/projects/preview-loop-upgrade":
                self.send_json(project_operation("preview-loop-upgrade", self.read_json()))
                return
            if parsed.path == "/api/projects/upgrade-loop":
                result = project_operation("upgrade-loop", self.read_json())
                payload = switch_project(result["project"]["root"])
                self.send_json({**result, "projects": payload})
                return
            if parsed.path == "/api/projects/upgrade-memory":
                result = project_operation("upgrade-memory", self.read_json())
                payload = switch_project(result["project"]["root"])
                self.send_json({**result, "projects": payload})
                return
            if parsed.path == "/api/active-memory/update":
                body = self.read_json()
                payload = update_active_memory(
                    body.get("source_id", ""),
                    body.get("item_id", ""),
                    body.get("content", ""),
                    delete=False,
                )
                self.send_json(payload)
                return
            if parsed.path == "/api/active-memory/delete":
                body = self.read_json()
                payload = update_active_memory(
                    body.get("source_id", ""),
                    body.get("item_id", ""),
                    None,
                    delete=True,
                )
                self.send_json(payload)
                return
            if parsed.path == "/api/refresh":
                self.send_json(review.build_queue())
                return
            if parsed.path == "/api/reject-noise-personal":
                body = self.read_json()
                apply = bool(body.get("apply", False))
                ids = review.reject_noise_personal_candidates(dry_run=not apply)
                payload = {"ok": True, "ids": ids}
                if apply:
                    payload["queue"] = review.load_queue(refresh=True)
                self.send_json(payload)
                return
            body = self.read_json()
            candidate_id = body.get("id")
            if not candidate_id:
                self.send_json({"error": "missing id"}, status=400)
                return
            if parsed.path == "/api/approve":
                item = review.approve(candidate_id, target=body.get("target"), content=body.get("content"))
                self.send_json({"ok": True, "item": item})
                return
            if parsed.path == "/api/reject":
                review.reject(candidate_id)
                self.send_json({"ok": True})
                return
            if parsed.path == "/api/defer":
                review.defer(candidate_id)
                self.send_json({"ok": True})
                return
            if parsed.path == "/api/reset":
                review.reset(candidate_id)
                self.send_json({"ok": True})
                return
            self.send_json({"error": "not found"}, status=404)
        except Exception as exc:  # noqa: BLE001 - local admin tool should surface errors.
            status = (
                ui_design_error_status(exc)
                if parsed.path in UI_DESIGN_POST_ROUTES
                else project_error_status(exc)
            )
            self.send_json({"error": str(exc)}, status=status)


def main() -> int:
    review.build_queue()
    try:
        ui_design_get("/api/ui-skills", {})
    except Exception:
        pass
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Memory review server running at {review.REVIEW_URL}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
