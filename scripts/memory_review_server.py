#!/usr/bin/env python3
"""Local-only web UI for memory approval."""

from __future__ import annotations

import json
import importlib
import os
import pathlib
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import memory_project
import memory_review_queue as review


HOST = review.REVIEW_HOST
PORT = review.REVIEW_PORT


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
    .toolbar, .memory-toolbar, .project-toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding: 10px; border: 1px solid var(--line-soft); border-radius: var(--radius); background: #1b1b1b; }
    .memory-toolbar, .project-toolbar { display: none; }
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
    @media (max-width: 760px) {
      header { padding: 10px 12px; }
      .topbar { align-items: flex-start; flex-direction: column; }
      .top-actions { width: 100%; }
      .project-badge { max-width: 100%; }
      main { padding: 14px 12px 32px; }
      .item-head, .memory-source-head { grid-template-columns: 1fr; display: grid; }
      .actions button, .toolbar button, .project-toolbar button { flex: 1 1 auto; }
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
        <input id="projectPath" placeholder="输入本机项目仓库路径，例如 /Users/.../my_repo">
        <input id="loopPort" placeholder="Loop staging 端口，留空用推荐值">
        <button onclick="registerProjectFromInput()">注册</button>
        <button onclick="initProjectFromInput()">初始化记忆</button>
        <button class="primary" onclick="initLoopFromInput()">初始化 Loop</button>
      </div>
      <div id="counts" class="counts"></div>
    </div>
  </header>
  <main id="items"></main>
<script>
let queue = {items: [], counts: {}};
let activeMemory = {sources: []};
let projectState = {current_project: '', registry: {projects: []}, recommend_port: 8081};
let currentView = 'review';

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

const statusLabel = {pending: '待审批', approved: '已批准', rejected: '已拒绝', deferred: '已暂缓'};
const scopeLabel = {project: '项目记忆', personal: '个人记忆'};
const targetLabel = {project_long: '项目长期记忆', personal_long: '个人长期记忆', personal_short: '个人短期记忆', short: '个人短期候选', unsure: '待判断'};
const sourceLabel = {project_proposals: '项目候选文件', personal_proposals: '个人候选文件'};
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
  document.getElementById('loopDocTab').classList.toggle('active', view === 'loopDocs');
  document.getElementById('strategyTab').classList.toggle('active', view === 'strategy');
  document.querySelector('.toolbar').style.display = view === 'review' ? 'flex' : 'none';
  document.getElementById('memoryToolbar').style.display = view === 'memory' ? 'flex' : 'none';
  document.getElementById('projectToolbar').style.display = view === 'projects' ? 'flex' : 'none';
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
  if (!confirm(`将初始化 loop 项目：\\n${root}\\n\\n建议/选择的 staging 端口：${port}\\n\\n需要你确认这个端口后才会写入 .loop/config.json。继续？`)) return;
  const result = await api('/api/projects/init-loop', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({project_root: root, port})
  });
  projectState = result.projects;
  updateProjectBadge();
  showMessage(`loop 项目初始化完成，端口 ${result.port}`);
  await loadQueue();
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
      <h3>最近一次初始化结果</h3>
      <p>状态：${esc(lastResult.ok ? '成功' : '未知')} ${lastResult.port ? `| loop 端口：${esc(lastResult.port)}` : ''}</p>
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
            <span class="tag">${project.has_memory ? '项目记忆已初始化' : '项目记忆未完整初始化'}</span>
            <span class="tag">${project.has_loop ? 'Loop 已初始化' : 'Loop 未初始化'}</span>
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
          <li><strong>初始化 loop 项目</strong>：在项目记忆初始化基础上创建 <code>.loop/config.json</code> 和 <code>loop/</code> 工作目录；端口由系统推荐，但必须经你在弹窗确认后写入。</li>
          <li>已存在文件不会覆盖，会在结果中显示为 <code>existing</code>。</li>
        </ul>
      </section>
      ${resultBlock}
      ${rows}
    </div>
  `;
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
            <li>代码仓库：<code>/Users/stephenbo/Noema/Projects/vibe_coding_manage_platform</code></li>
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
        <p>审核台本身不主动生成候选，它负责读取、展示、编辑、审批和落盘。候选主要由 Codex/Claude hooks 写入。</p>
        <ul>
          <li>个人候选由全局 Codex hook 写入：<code>~/.codex/hooks/personal_memory_hook.py</code>。</li>
          <li>个人 hook 注册在 <code>~/.codex/hooks.json</code>，触发于 <code>UserPromptSubmit</code>、<code>PreCompact</code>、<code>Stop</code>。</li>
          <li>个人候选写入：<code>~/.codex/personal_memory/proposals.md</code>。</li>
          <li>项目候选由项目 Codex hook 和 Claude Code hook 写入：<code>.codex/hooks/*memory_hook.py</code>、<code>.claude/hooks/*memory_hook.py</code>。</li>
          <li>项目 hook 注册在项目的 <code>.codex/hooks.json</code> 和 <code>.claude/settings.json</code>，主要在 <code>PreCompact</code>、<code>Stop</code> 写项目长期记忆检查点。</li>
          <li>项目候选写入当前项目：<code>&lt;project&gt;/codex/memory_proposals.md</code>。</li>
        </ul>
      </section>
      <section class="doc-section">
        <h3>候选生成策略</h3>
        <ul>
          <li>个人候选必须是提炼后的跨项目事实，例如开发习惯、协作偏好、思维方式、工作流偏好或用户画像。</li>
          <li>个人 hook 会过滤截图描述、当前 URL、附件提示、AGENTS 指令、系统提示、ambient suggestion、原始会话内容和一次性项目任务。</li>
          <li>个人 hook 会拒绝疑似敏感信息：token、验证码、API key、RDS/OSS 密钥、密码、<code>.env.production</code> 等。</li>
          <li>个人 hook 每次最多生成 2 条候选，且会对已存在候选和已批准记忆做去重。</li>
          <li>项目长期候选当前采用“检查点提醒”策略：hook 只写入是否需要沉淀稳定项目事实的提醒，不自动把整段会话当成最终长期记忆。</li>
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
          <li>允许自动写入：项目长期记忆检查点候选，用于提醒用户是否需要整理长期事实。</li>
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
          <li>点击“初始化 loop 项目”，创建 <code>.loop/config.json</code>、<code>loop/prd</code>、<code>loop/acceptance</code>、<code>loop/reports</code>、<code>loop/claude_tests</code>。</li>
          <li>后续在 Codex 中贴 Markdown PRD，并要求先生成验收标准。</li>
          <li>Codex 负责开发和 staging，Claude Code 负责 Playwright/浏览器验收，直到通过或触发暂停条件。</li>
        </ol>
      </section>
      <section class="doc-section">
        <h3>CLI 等价命令</h3>
        <pre>python3 /Users/stephenbo/Noema/Projects/vibe_coding_manage_platform/scripts/memory_project.py register /path/to/repo
python3 /Users/stephenbo/Noema/Projects/vibe_coding_manage_platform/scripts/memory_project.py init /path/to/repo
python3 /Users/stephenbo/Noema/Projects/vibe_coding_manage_platform/scripts/memory_project.py init-loop /path/to/repo --port 8082
python3 /Users/stephenbo/Noema/Projects/vibe_coding_manage_platform/scripts/memory_project.py list
python3 /Users/stephenbo/Noema/Projects/vibe_coding_manage_platform/scripts/memory_project.py use /path/to/repo</pre>
      </section>
    </div>
  `;
}

function renderLoopDocs() {
  document.getElementById('counts').innerHTML = [
    'Codex 开发',
    'Claude Code 验收',
    'Playwright 默认测试',
    'Worktree 隔离',
    '最多 10 轮',
    '合并和正式上线需用户确认'
  ].map(x => `<span class="pill">${esc(x)}</span>`).join('');
  document.getElementById('items').innerHTML = `
    <div class="doc">
      <section class="doc-hero">
        <h2>Loop 开发使用说明</h2>
        <p>Loop engineering 是一套跨项目循环开发流程：你给出 Markdown PRD，Codex 负责开发、分支、提交、staging 部署和修复；Claude Code 负责独立测试与评测报告；Codex 读取报告继续迭代。最终合并主分支和正式上线必须在你完成产品验收后，由你主动确认。</p>
      </section>

      <div class="doc-grid">
        <section class="doc-section">
          <h3>Worktree 优先</h3>
          <ul>
            <li>当用户说 <code>开 worktree</code>，Codex 应先创建或使用专用 git worktree，再开始实质性项目工作。</li>
            <li>loop 开发默认先进入专用 worktree：一个任务或 loop 功能，对应一个对话、一个 worktree、一个分支。</li>
            <li>主开发对话拥有产品源码修改；review、eval、报告和实验对话使用辅助 worktree/分支。</li>
            <li>不要让多个对话在同一个 worktree 写产品代码，也不要并发改同一个 loop 分支；辅助分支通过主 worktree merge 或 cherry-pick。</li>
            <li>staging 默认由一个活跃 loop 分支占用；需要并行 staging 时，必须显式配置不同远程路径和端口。</li>
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
            <li>报告、记忆、测试产物禁止记录 token、验证码、API key、RDS/OSS 密钥等敏感信息。</li>
          </ul>
        </section>
      </div>

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
          <li>Codex 创建或使用专用 worktree，并创建或切换到 <code>loop/&lt;project&gt;-&lt;date&gt;-&lt;slug&gt;</code> 分支。</li>
          <li>Codex 开发、运行本地验证、commit/push。</li>
          <li>Codex 部署远程 staging。</li>
          <li>Codex 通过 <code>claude -p</code> 下发测试任务给 Claude Code。</li>
          <li>Claude Code 默认用 Playwright 测试，可补充内部浏览器评测。</li>
          <li>Claude Code 输出 <code>loop/reports/claude_eval_latest.md</code> 和 <code>loop/reports/claude_eval_latest.json</code>。</li>
          <li>Codex 读取报告继续修复并进入下一轮。</li>
          <li>循环通过、达到停止条件，或需要用户判断时暂停。</li>
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
        <p>从主仓库为一个 loop 功能创建独立 worktree：</p>
        <pre>cd /path/to/project
mkdir -p /Users/stephenbo/Noema/Projects/worktrees
git worktree add -b loop/&lt;project&gt;-&lt;date&gt;-&lt;slug&gt; \
  /Users/stephenbo/Noema/Projects/worktrees/&lt;repo&gt;-&lt;slug&gt; \
  master</pre>
        <p>为评测或 review 创建辅助 worktree：</p>
        <pre>git worktree add -b codex/eval-&lt;slug&gt;-r1 \
  /Users/stephenbo/Noema/Projects/worktrees/&lt;repo&gt;-eval-&lt;slug&gt;-r1 \
  loop/&lt;project&gt;-&lt;date&gt;-&lt;slug&gt;</pre>
        <p>查看和清理 worktree：</p>
        <pre>git worktree list
git worktree remove /Users/stephenbo/Noema/Projects/worktrees/&lt;repo&gt;-&lt;slug&gt;
git worktree prune</pre>
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
          <li>远程 staging：<code>root@8.210.155.175</code></li>
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
        length = int(self.headers.get("Content-Length", "0") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw or "{}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.send_json({"ok": True, "url": review.REVIEW_URL})
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
            payload = page().encode("utf-8")
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
                port = body.get("port")
                result = memory_project.init_loop(body.get("project_root", ""), int(port) if port else None)
                payload = switch_project(result["project"]["root"])
                self.send_json({
                    "ok": True,
                    "port": result.get("port"),
                    "changes": result.get("changes", []),
                    "projects": payload,
                })
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
            self.send_json({"error": str(exc)}, status=500)


def main() -> int:
    review.build_queue()
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
