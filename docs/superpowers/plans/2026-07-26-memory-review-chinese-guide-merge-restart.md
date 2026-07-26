# 记忆审核台中文说明书、合并与重启实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付完整中文版记忆审核台说明书，将已验证功能安全合并到最新本地 `master`，并从原始仓库启动 8897 服务。

**Architecture:** 中文说明书以单一 Markdown 文件随仓库维护，README 只增加入口。合并过程保留原始仓库运行态文件，先同步远端引用并检查路径重叠，再在原始仓库 `master` 合并和复测；服务只从原始仓库脚本启动。

**Tech Stack:** Markdown、Python 3.10+ 标准库、`unittest`、Git worktree、`http.server`、curl。

---

### Task 1: 编写完整中文版说明书

**Files:**
- Create: `docs/MEMORY_REVIEW_USER_GUIDE.zh-CN.md`
- Modify: `README.md`

- [ ] **Step 1: 依据实现编写说明书**

按已批准设计规范写入以下章节：产品定位、数据模型、启动、项目管理、记忆审核、Web 页面、CLI、Loop Engineering、设计偏好、UI Skills、设计包、两种门禁模式、Hooks、备份恢复、排障、安全边界和日常检查清单。命令只使用当前仓库已有入口，所有可变值使用 `<目标项目路径>`、`<草稿ID>`、`<摘要>` 等明确占位符。

- [ ] **Step 2: 在 README 增加中文入口**

在项目介绍之后增加：

```markdown
## 中文使用说明

完整的中文安装、操作、UI 设计审批、UI Skill 管理和故障排查说明，请参阅 [记忆审核台中文版使用说明书](docs/MEMORY_REVIEW_USER_GUIDE.zh-CN.md)。
```

- [ ] **Step 3: 执行文档自审**

Run:

```bash
rg -n "TODO|TBD|待补充|真实密钥|真实验证码" docs/MEMORY_REVIEW_USER_GUIDE.zh-CN.md
git diff --check
```

Expected: 第一条除解释这些禁用词的安全提示外无匹配；第二条退出码为 0。

- [ ] **Step 4: 运行完整测试**

Run: `python3 -m unittest discover -s tests`

Expected: 99 tests，0 failures，退出码 0。

- [ ] **Step 5: 提交说明书**

```bash
git add README.md docs/MEMORY_REVIEW_USER_GUIDE.zh-CN.md docs/superpowers/plans/2026-07-26-memory-review-chinese-guide-merge-restart.md
git commit -m "docs: add Chinese memory review user guide"
```

### Task 2: 安全更新并合并原始仓库 master

**Files:**
- Preserve: `/Users/stephenbo/Noema/Projects/vibe_coding_manage_platform/codex/codex_short_memory.md`
- Preserve: `/Users/stephenbo/Noema/Projects/vibe_coding_manage_platform/codex/memory_proposals.md`

- [ ] **Step 1: 获取远端状态并检查分支关系**

Run:

```bash
git -C /Users/stephenbo/Noema/Projects/vibe_coding_manage_platform fetch origin master
git -C /Users/stephenbo/Noema/Projects/vibe_coding_manage_platform status --short --branch
git -C /Users/stephenbo/Noema/Projects/vibe_coding_manage_platform rev-list --left-right --count master...origin/master
```

Expected: 原始仓库仍在 `master`；两份运行态修改仍存在；输出明确本地与远端领先/落后数量。

- [ ] **Step 2: 检查脏文件与更新路径重叠**

Run:

```bash
git -C /Users/stephenbo/Noema/Projects/vibe_coding_manage_platform diff --name-only
git -C /Users/stephenbo/Noema/Projects/vibe_coding_manage_platform diff --name-only master..origin/master
git -C /Users/stephenbo/Noema/Projects/vibe_coding_manage_platform diff --name-only master..design/ui-design-control-plane-20260725
```

Expected: `codex/codex_short_memory.md` 和 `codex/memory_proposals.md` 不出现在远端或功能分支更新路径中；若重叠则停止，不执行 stash、reset、checkout 覆盖或合并。

- [ ] **Step 3: 快进到最新远端 master**

Run: `git -C /Users/stephenbo/Noema/Projects/vibe_coding_manage_platform merge --ff-only origin/master`

Expected: 成功快进或报告 Already up to date；运行态修改保持不变。

- [ ] **Step 4: 合并功能分支**

Run: `git -C /Users/stephenbo/Noema/Projects/vibe_coding_manage_platform merge --no-ff design/ui-design-control-plane-20260725`

Expected: 创建本地合并提交，无冲突，不推送远端。

- [ ] **Step 5: 在原始仓库复测**

Run: `python3 -m unittest discover -s tests`

Working directory: `/Users/stephenbo/Noema/Projects/vibe_coding_manage_platform`

Expected: 99 tests，0 failures，退出码 0。

- [ ] **Step 6: 验证代码和运行态文件**

Run:

```bash
git -C /Users/stephenbo/Noema/Projects/vibe_coding_manage_platform status --short --branch
git -C /Users/stephenbo/Noema/Projects/vibe_coding_manage_platform merge-base --is-ancestor design/ui-design-control-plane-20260725 master
```

Expected: 功能分支是 `master` 祖先；只保留合并前已存在的两份运行态修改。

### Task 3: 从原始仓库 master 重启本地服务

**Files:**
- Execute: `/Users/stephenbo/Noema/Projects/vibe_coding_manage_platform/scripts/start_memory_review.sh`
- Runtime log: `<目标项目路径>/codex/memory_review_server.log`

- [ ] **Step 1: 检查 8897 当前状态**

Run: `curl -sS http://127.0.0.1:8897/health`

Expected: 若服务离线则记录连接失败；若在线，先确认进程来自哪个仓库，再只终止该精确服务进程。

- [ ] **Step 2: 从原始仓库启动服务**

Run:

```bash
/Users/stephenbo/Noema/Projects/vibe_coding_manage_platform/scripts/start_memory_review.sh /Users/stephenbo/Noema/Projects/vibe_coding_manage_platform
```

Expected: `ensure-server` 返回 8897 地址，后台服务脚本路径属于原始仓库 `master`。

- [ ] **Step 3: 验证健康、主页和进程来源**

Run:

```bash
curl -sS http://127.0.0.1:8897/health
curl -sS -I http://127.0.0.1:8897/
```

Expected: 健康接口返回成功状态；主页返回 HTTP 200；日志和进程命令指向原始仓库。

- [ ] **Step 4: 最终交付核对**

核对本地 `master` commit、功能分支祖先关系、测试计数、服务 URL、说明书路径，以及两份原始运行态修改仍被保留。报告未执行远端推送和生产部署。
