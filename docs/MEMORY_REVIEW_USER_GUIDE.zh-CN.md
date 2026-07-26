# 记忆审核台中文版使用说明书

本说明书适用于 `vibe_coding_manage_platform` 当前版本，面向首次安装者、日常使用者和本地维护者。它覆盖项目与个人记忆审批、跨项目管理、Loop Engineering、UI 设计审批、UI Skills 管理、Codex/Claude Code Hooks、备份恢复和故障排查。

## 1. 产品定位

记忆审核台是一个仅监听本机地址的跨项目管理平台。中心代码维护在本仓库，各项目的数据仍保存在各自仓库中。

它解决五类问题：

- 审核 Codex 或 Claude Code 产生的项目记忆、个人记忆候选。
- 在多个项目之间注册、切换和初始化统一的记忆规则与 Hooks。
- 管理 Loop Engineering 的初始化、升级和完成条件。
- 管理 UI 设计偏好、UI Skills 和前端开发审批门禁。
- 保留审批、发布、回滚和配置变更的本地审计记录。

记忆审核台不是生产部署平台，也不会因为一次记忆或设计审批自动获得合并主分支、推送远端或部署生产的权限。

## 2. 核心概念

### 2.1 项目记忆

项目记忆只属于一个仓库，适合保存稳定架构、产品方向、部署规则、技术约束和项目工作流。

- `codex/codex_long_memory.md`：经审核的长期项目记忆。
- `codex/codex_short_memory.md`：近期工作上下文，由 Hooks 有界更新。
- `codex/memory_proposals.md`：待审核项目记忆候选。

### 2.2 个人记忆

个人记忆在所有项目之间共享，只适合保存可跨项目复用的习惯和偏好。

- `~/.codex/personal_memory/long.md`：稳定的长期偏好。
- `~/.codex/personal_memory/short.md`：真正临时的跨项目背景。
- `~/.codex/personal_memory/proposals.md`：待审核个人候选。

个人候选不得记录普通任务原文、PRD、截图描述、系统提示、URL、路径、凭据、验证码、Token、数据库密码或云存储密钥。

### 2.3 候选、批准与生效

候选只是待审核内容。只有用户审核精确内容并批准后，候选才会写入正式记忆。驳回、延期和隔离不会把内容写入正式记忆。

### 2.4 UI Skill

UI Skill 是 Codex 或 Claude Code 可加载的设计工作流或知识包。审核台把“导入”“校验”“批准”“发布”拆成独立步骤：导入成功不等于批准，批准成功也不等于发布。

### 2.5 设计包

设计包是一次前端任务的审批边界，包括页面、组件、设计文档、交互规则、响应式规则和允许修改的正式前端文件范围。批准与设计包摘要绑定；设计内容变化后，旧批准自动失效。

## 3. 系统要求与目录

### 3.1 运行要求

- macOS 或其他可运行 Python 的本地系统。
- Python 3.10 或更高版本。
- Git；使用 Loop Engineering 时还需要项目是 Git 仓库。
- 不需要安装第三方 Python 包。

### 3.2 中心仓库

默认中心仓库路径：

```text
/Users/stephenbo/Noema/Projects/vibe_coding_manage_platform
```

### 3.3 全局数据

```text
~/.codex/memory_review/projects.json
~/.codex/personal_memory/
~/.codex/ui_design/
~/.codex/skills/
~/.claude/skills/
```

`projects.json` 保存已注册项目和当前项目。`~/.codex/ui_design` 保存 UI Skill 草稿、不可变版本、部署状态、设计偏好、审计记录和幂等结果。

### 3.4 项目数据

```text
<目标项目路径>/codex/
<目标项目路径>/codex/ui_design/
<目标项目路径>/.codex/
<目标项目路径>/.claude/
```

项目初始化不会覆盖已有文件。升级受管文件时，发生变化的旧版本会保存为带时间戳的 `.bak.*` 文件。

## 4. 快速开始

### 4.1 启动服务

从中心仓库启动，并指定当前要审核的项目：

```bash
/Users/stephenbo/Noema/Projects/vibe_coding_manage_platform/scripts/start_memory_review.sh <目标项目路径>
```

默认地址：

```text
http://127.0.0.1:8897/
```

健康检查：

```bash
curl -sS http://127.0.0.1:8897/health
```

启动脚本会先检查服务是否已运行。服务在线时不会重复启动；服务离线时会从中心仓库后台启动，并把日志写入当前目标项目的 `codex/memory_review_server.log`。

### 4.2 注册并初始化项目

```bash
cd /Users/stephenbo/Noema/Projects/vibe_coding_manage_platform
python3 scripts/memory_project.py register <目标项目路径>
python3 scripts/memory_project.py init <目标项目路径>
```

初始化会创建缺失的记忆文件、受管规则、Codex/Claude Hooks 和 UI 设计默认状态，但 UI hard gate 默认关闭。

### 4.3 打开控制台

浏览器访问 `http://127.0.0.1:8897/`。首次使用建议依次完成：

1. 确认右上角或项目区域显示正确项目。
2. 查看待审核记忆。
3. 检查项目初始化和 Hook 状态。
4. 按需配置 UI 设计偏好和前端路径。
5. 只有在两端 Hook smoke test 通过后才启用 UI hard gate。

## 5. 项目管理

### 5.1 查看项目

```bash
python3 scripts/memory_project.py list
python3 scripts/memory_project.py current
```

### 5.2 切换当前项目

```bash
python3 scripts/memory_project.py use <目标项目路径>
```

切换会更新全局项目注册表。服务后续请求会直接读取新项目，不需要重启 8897。

### 5.3 初始化规则和 Hooks

```bash
python3 scripts/memory_project.py init <目标项目路径>
```

重复执行是安全的：已有文件返回 `existing`，缺失文件才会创建。

### 5.4 升级已有项目

只升级记忆规则与 Hooks：

```bash
python3 scripts/memory_project.py upgrade-memory <目标项目路径>
```

也可以分别执行：

```bash
python3 scripts/memory_project.py upgrade-rules <目标项目路径>
python3 scripts/memory_project.py upgrade-memory-hooks <目标项目路径>
```

遇到格式错误或无法识别的受管标记时，升级会报告冲突，不会覆盖原文件。

## 6. 记忆审核

### 6.1 Web 操作

待审核列表显示候选 ID、作用域、目标、摘要、来源和风险提示。审核时：

1. 打开候选详情。
2. 判断内容是否稳定、准确、独立可理解。
3. 必要时编辑为精确版本。
4. 选择批准、驳回或延期。

批准个人记忆前尤其要检查它是否真的可以跨项目复用。

### 6.2 CLI 列表与详情

```bash
MEMORY_REVIEW_PROJECT_ROOT=<目标项目路径> python3 scripts/memory_review.py list
python3 scripts/memory_review.py list --status rejected
python3 scripts/memory_review.py show <候选ID>
```

### 6.3 批准

批准项目长期记忆：

```bash
python3 scripts/memory_review.py approve <候选ID> --target project_long
```

批准个人长期或短期记忆：

```bash
python3 scripts/memory_review.py approve <候选ID> --target personal_long
python3 scripts/memory_review.py approve <候选ID> --target personal_short
```

如果要用审核后的文件内容替代候选原文：

```bash
python3 scripts/memory_review.py approve <候选ID> \
  --target project_long --content-file <审核后内容文件>
```

### 6.4 驳回、延期和重置

```bash
python3 scripts/memory_review.py reject <候选ID>
python3 scripts/memory_review.py defer <候选ID>
python3 scripts/memory_review.py reset <候选ID>
```

`reset` 用于把已作决定的候选恢复到可审核状态，不会自动批准。

### 6.5 主动创建候选

当前对话模型可以提交已提炼候选：

```bash
python3 scripts/memory_review.py propose \
  --scope personal \
  --target long \
  --category workflow_preference \
  --title "简短标题" \
  --summary "独立、准确、可跨项目复用的 1 至 3 句摘要" \
  --source-event agent_summary
```

项目候选使用 `--scope project --target long`。个人允许的分类包括 `development_habit`、`collaboration_preference`、`work_style`、`thinking_style`、`user_profile` 和 `workflow_preference`。

### 6.6 隔离噪声候选

先预览：

```bash
python3 scripts/memory_review.py reject-noise-personal
```

确认后应用：

```bash
python3 scripts/memory_review.py reject-noise-personal --apply
```

应用后原始来源仍保留用于审计，候选被隔离并标记为拒绝，而不是直接删除。

## 7. Web 控制台页面

控制台集中提供以下能力：

- 记忆待审核：查看并处理项目与个人候选。
- 已生效记忆：查看正式项目记忆和个人记忆。
- 项目管理：注册、切换、初始化和查看项目状态。
- Loop 说明：查看 Loop Engineering 方法、边界和常用命令。
- UI 设计审批：配置门禁模式、查看设计包并执行审批。
- 设计偏好：维护全局设计偏好和项目级覆盖。
- UI Skills：查看草稿、校验结果、脚本清单、许可证、摘要和部署状态。

Web 与 CLI 调用相同的领域操作；所有需要幂等键、摘要或显式确认的安全约束不会因为使用 Web 而绕过。

## 8. Loop Engineering

### 8.1 新项目初始化

```bash
python3 scripts/memory_project.py init-loop <目标项目路径> --port <staging端口>
```

未指定端口时可先查询推荐值：

```bash
python3 scripts/memory_project.py recommend-port
```

初始化会创建 `.loop/config.json`、Loop 产物目录和受管完成验证器。

### 8.2 升级已有 Loop 项目

必须先预览：

```bash
python3 scripts/memory_project.py preview-loop-upgrade <目标项目路径>
```

审核预览后再升级：

```bash
python3 scripts/memory_project.py upgrade-loop <目标项目路径>
```

升级保留项目已有的 staging、数据库、OSS、端口、远程路径、验证命令和未知扩展字段。

### 8.3 工作流边界

- 一个任务对应一个对话、一个 worktree 和一个功能分支。
- Codex 负责开发；Claude Code 负责独立评测时，按项目 Loop 配置执行。
- 功能分支内的常规 commit/push 不代表可以合并主分支。
- 合并主分支、正式上线和生产部署必须单独获得用户明确批准。
- 禁止 force push；发布前必须验证功能提交成为主分支祖先。

## 9. UI 设计审批工作流

对于 Web、App、小程序、桌面界面、组件库、视觉样式、交互、响应式或动效任务：

1. 未批准前允许调研、读取代码、生成设计稿、原型和交互说明。
2. 未批准前禁止修改正式前端业务代码。
3. 用户批准设计包或项目全局基线后，才解锁相应正式前端范围。
4. 纯后端和无界面任务不触发该门禁。

项目支持两种模式：

- `design_package`：按任务、摘要、版本和文件范围审批，推荐用于大多数项目。
- `project_global`：批准一次项目基线后解锁全部配置的正式前端路径，直到重新锁定、切换模式或基线变化。

## 10. 设计偏好

### 10.1 查看有效值

```bash
python3 scripts/memory_review.py ui-design preferences show \
  --project <目标项目路径>
```

结果同时显示有效值和来源，便于区分全局继承与项目覆盖。

### 10.2 设置全局偏好

```bash
python3 scripts/memory_review.py ui-design preferences set-global \
  --json-file <全局偏好JSON> \
  --idempotency-key <唯一幂等键>
```

### 10.3 设置项目覆盖

```bash
python3 scripts/memory_review.py ui-design preferences set-project \
  --project <目标项目路径> \
  --json-file <项目覆盖JSON> \
  --idempotency-key <唯一幂等键>
```

项目字段支持：

- `inherit`：继续使用全局值。
- `replace`：替换该字段。
- `append`：向列表追加内容。
- `clear`：清空该字段。

示例：

```json
{
  "visual.preferred_styles": {
    "mode": "append",
    "value": ["editorial"]
  },
  "visual.radius": {
    "mode": "replace",
    "value": "4px"
  },
  "anti_preferences": {
    "mode": "clear"
  }
}
```

## 11. UI Skills 管理

### 11.1 生命周期

标准流程：

```text
导入或 bootstrap → 校验 → 审核 → 批准 → 原子发布 → 扫描验证
```

审批或发布时必须使用当前摘要。草稿内容变化后，应重新校验并审核新摘要，不能绕过冲突。

### 11.2 内置 UI Skills

```bash
python3 scripts/memory_review.py ui-skill bootstrap ui-design-workflow \
  --idempotency-key <唯一幂等键>

python3 scripts/memory_review.py ui-skill bootstrap frontend-design \
  --revision b29e7cf65e5cb78a5ac33d582270551bc74a14eb \
  --idempotency-key <唯一幂等键>

python3 scripts/memory_review.py ui-skill bootstrap ui-ux-pro-max \
  --release v2.11.0 \
  --revision 6142b073958df645d0fb27e682428e69599386dc \
  --cli-version 2.11.0 \
  --expected-npm-shasum 2ff4d811cf1dded593b9d1f37bad65ffa80cb87c \
  --idempotency-key <唯一幂等键>
```

Bootstrap 只创建待审核草稿，不自动批准或发布。UI UX Pro Max 在临时目录运行固定版本生成器；发布已批准版本时不会再次运行 `npx`。

### 11.3 导入自定义 Skill

从固定 Git revision 导入：

```bash
python3 scripts/memory_review.py ui-skill import \
  --github <所有者/仓库> \
  --path <仓库内Skill路径> \
  --revision <完整revision> \
  --scope global \
  --targets codex,claude \
  --version-label <版本标签> \
  --idempotency-key <唯一幂等键>
```

也可以用 `--local <目录>`、`--zip <压缩包>` 或 `--editor-json <文件映射JSON>`。项目级 Skill 使用 `--scope project --project <目标项目路径>`。

### 11.4 查看、校验与退回修改

```bash
python3 scripts/memory_review.py ui-skill list
python3 scripts/memory_review.py ui-skill show <草稿ID>
python3 scripts/memory_review.py ui-skill validate <草稿ID> \
  --idempotency-key <唯一幂等键>
python3 scripts/memory_review.py ui-skill request-revision <草稿ID> \
  --reason "需要修改的具体原因" \
  --idempotency-key <唯一幂等键>
```

校验会检查元数据、引用、路径安全、文件数量、体积、许可证、网络引用和脚本风险。包内脚本只做静态清点，不会在校验、审批或发布阶段执行。

### 11.5 批准与发布

```bash
python3 scripts/memory_review.py ui-skill approve <草稿ID> \
  --digest <审核过的摘要> \
  --idempotency-key <唯一幂等键>

python3 scripts/memory_review.py ui-skill publish <草稿ID> \
  --digest <审核过的摘要> \
  --idempotency-key <唯一幂等键>
```

全局发布目标默认是 `~/.codex/skills` 和 `~/.claude/skills`。双目标发布是一个事务：第二个目标失败时会恢复两个目标的原状态。

### 11.6 扫描、回滚和停用

```bash
python3 scripts/memory_review.py ui-skill scan \
  --idempotency-key <唯一幂等键>
python3 scripts/memory_review.py ui-skill rollback <Skill名称> \
  --version <不可变版本ID> \
  --idempotency-key <唯一幂等键>
python3 scripts/memory_review.py ui-skill disable <Skill名称> \
  --idempotency-key <唯一幂等键>
```

扫描会把目录分类为 managed、unmanaged、ignored、conflicting 或 drifted，但不会删除非受管 Skill。只有确认指纹后，才能用 `ignore-unmanaged <摘要>` 隐藏某个未受管发现项。

## 12. 配置 UI hard gate

### 12.1 配置路径

创建路径 JSON：

```json
{
  "formal_frontend_paths": ["web/src/**"],
  "design_artifact_paths": ["codex/ui_design/design-packages/**"],
  "generated_paths": ["web/generated/**"],
  "test_artifact_paths": ["tests/ui/**"]
}
```

应用配置：

```bash
python3 scripts/memory_review.py ui-design project-config set-paths \
  --project <目标项目路径> \
  --json-file <路径配置JSON> \
  --idempotency-key <唯一幂等键>
```

改变路径会自动关闭 hard gate、重新锁定项目并重置 Codex/Claude smoke test 状态。

### 12.2 选择模式

```bash
python3 scripts/memory_review.py ui-design project-config set-mode \
  --project <目标项目路径> \
  --mode design_package \
  --confirmed \
  --idempotency-key <唯一幂等键>
```

模式切换始终重新锁定，防止旧批准跨模式生效。

### 12.3 启用门禁

```bash
python3 scripts/memory_review.py ui-design project-config enable-hard-gate \
  --project <目标项目路径> \
  --confirmed \
  --idempotency-key <唯一幂等键>
```

启用前必须满足：

- 正式前端路径非空。
- 设计产物路径非空。
- 项目安装了 Codex 和 Claude Code UI gate Hook。
- 两端 Hook 的“正式前端拒绝”和“设计产物允许”smoke test 都通过。

如果任一端失败，hard gate 保持关闭。

### 12.4 重新锁定

```bash
python3 scripts/memory_review.py ui-design project-config relock \
  --project <目标项目路径> \
  --confirmed \
  --idempotency-key <唯一幂等键>
```

需要暂停前端开发、重新设计或处理异常时，优先重新锁定，再检查配置和审计记录。

## 13. 设计包模式

### 13.1 Manifest

```json
{
  "schema_version": 1,
  "task_id": "checkout-redesign",
  "title": "结算页改版",
  "classification": "visual_change",
  "pages": ["checkout"],
  "components": ["CheckoutForm"],
  "allowed_file_patterns": ["web/src/checkout/**"],
  "design_files": [
    "design-brief.md",
    "interaction-spec.md",
    "responsive-spec.md"
  ],
  "status": "pending_approval"
}
```

路径必须相对项目根目录，禁止绝对路径、`..` 遍历和与任务无关的宽泛范围。

### 13.2 创建和查看

```bash
python3 scripts/memory_review.py ui-design package create \
  --project <目标项目路径> \
  --manifest <设计包Manifest> \
  --idempotency-key <唯一幂等键>
python3 scripts/memory_review.py ui-design package list --project <目标项目路径>
python3 scripts/memory_review.py ui-design package show \
  --project <目标项目路径> --task checkout-redesign
```

创建后补全以下设计文件：

- `design-brief.md`：目标、用户、信息架构、视觉方向、Token、参考和理由。
- `interaction-spec.md`：流程、动作、反馈、表单、导航和各类状态。
- `responsive-spec.md`：视口、密度、输入方式、方向和平台规则。

### 13.3 批准

```bash
python3 scripts/memory_review.py ui-design package approve \
  --project <目标项目路径> \
  --task checkout-redesign \
  --digest <当前设计包摘要> \
  --confirmed \
  --idempotency-key <唯一幂等键>
```

批准后只允许修改 `allowed_file_patterns` 声明的路径。其他正式前端路径继续拒绝。

### 13.4 退回、修订、驳回和失效

```bash
python3 scripts/memory_review.py ui-design package request-revision \
  --project <目标项目路径> --task checkout-redesign \
  --reason "补充移动端错误状态" \
  --idempotency-key <唯一幂等键>

python3 scripts/memory_review.py ui-design package revise \
  --project <目标项目路径> --task checkout-redesign \
  --manifest <修订后Manifest> \
  --idempotency-key <唯一幂等键>

python3 scripts/memory_review.py ui-design package reject \
  --project <目标项目路径> --task checkout-redesign \
  --reason "设计方向不符合要求" --confirmed \
  --idempotency-key <唯一幂等键>

python3 scripts/memory_review.py ui-design package invalidate \
  --project <目标项目路径> --task checkout-redesign \
  --reason "实现范围已变化" --confirmed \
  --idempotency-key <唯一幂等键>
```

批准后修改 Manifest 或任何已声明设计文件都会改变摘要，旧批准自动失效。

## 14. 项目全局模式

`project_global` 适合设计系统和整体方向已经稳定、用户希望一次批准后解锁全部正式前端代码的项目。

操作顺序：

1. 切换到 `project_global`，项目自动重新锁定。
2. 在项目配置中审核并设置 `project_global_baseline_task`。
3. 创建并审核对应基线设计包。
4. 使用基线摘要批准。

```bash
python3 scripts/memory_review.py ui-design baseline approve \
  --project <目标项目路径> \
  --task <基线任务ID> \
  --digest <当前基线摘要> \
  --confirmed \
  --idempotency-key <唯一幂等键>
```

以下情况会停止全局解锁：显式 relock、切换模式、基线任务变化、基线内容变化或批准失效。

## 15. Codex 与 Claude Code Hooks

初始化会安装：

```text
<目标项目路径>/.codex/hooks/ui_design_gate_hook.py
<目标项目路径>/.claude/hooks/ui_design_gate_hook.py
```

并把受管 `PreToolUse` 条目合并进两端配置，不替换无关用户 Hook。

Hook 的决策原则：

- 读取、检索和纯后端写入允许。
- 设计包目录写入允许。
- hard gate 关闭时不阻止正式前端。
- hard gate 开启且未批准时，正式前端写入拒绝。
- 批准后，声明范围允许，越界范围拒绝。
- 配置损坏时，对明显前端写入失败关闭，对纯后端仍保持允许。

升级 Hook 或 Skill 后必须启动全新的 Codex 和 Claude Code 会话。若客户端提示 Hook 信任，需要核对文件来源后按客户端流程确认；不要使用危险的全局绕过参数替代信任审核。

## 16. 幂等键与摘要

所有重要写操作要求唯一 `--idempotency-key`。

- 同一个键和完全相同的操作可以安全重试，返回已记录结果。
- 同一个键用于不同参数会返回幂等冲突。
- 每次新操作使用新键，例如 `<操作>-<项目>-<日期>-001`。

摘要用于绑定审核内容。出现摘要冲突时，应重新打开当前内容、检查差异并批准新摘要；不得手工修改注册表或绕过摘要检查。

## 17. 备份、审计与恢复

### 17.1 审计位置

项目 UI 审计通常位于：

```text
<目标项目路径>/codex/ui_design/audit.jsonl
```

全局 UI Skill 审计和部署报告位于：

```text
~/.codex/ui_design/
```

### 17.2 安全恢复顺序

1. 重新锁定项目，必要时保持 `hard_gate_enabled=false`。
2. 查看项目 `audit.jsonl`、全局部署报告和当前摘要。
3. 检查 `.bak.*` 文件，确认来源和时间。
4. UI Skill 使用审核台的 rollback 恢复不可变版本。
5. Hook 配置只恢复已确认的备份，不覆盖无关用户配置。
6. 重新运行两端 smoke test。
7. 两端都通过后才重新启用 hard gate。

不要通过删除未受管 Skill、强制覆盖配置、清空注册表或跳过摘要检查来“修复”问题。

## 18. 常见问题

### 18.1 8897 无法访问

检查：

```bash
curl -sS http://127.0.0.1:8897/health
tail -n 100 <目标项目路径>/codex/memory_review_server.log
```

如果没有服务，从中心仓库重新执行 `scripts/start_memory_review.sh <目标项目路径>`。如果端口被其他进程占用，先确认进程来源，不要盲目终止未知进程。

### 18.2 页面显示了错误项目

```bash
python3 scripts/memory_project.py current
python3 scripts/memory_project.py use <正确项目路径>
```

切换后刷新页面，无需重启服务。

### 18.3 候选没有出现

```bash
MEMORY_REVIEW_PROJECT_ROOT=<目标项目路径> \
  python3 scripts/memory_review.py refresh
```

然后检查 `codex/memory_proposals.md` 或个人 proposals 文件格式，以及候选是否已被批准、驳回或延期。

### 18.4 Skill 已发布但客户端看不到

1. 运行 `ui-skill scan`，确认状态为 managed 且摘要匹配。
2. 确认目录在 `~/.codex/skills/<名称>` 和 `~/.claude/skills/<名称>`。
3. 完全关闭旧会话并启动新会话。
4. Codex 查看启动时的可用 Skills；Claude Code 普通会话可用 `/skills` 查看用户 Skills。

### 18.5 hard gate 无法启用

查看项目配置：

```bash
python3 scripts/memory_review.py ui-design project-config show \
  --project <目标项目路径>
```

确认正式前端路径、设计产物路径、两端 Hook 文件和两个 smoke test 均有效。不要直接编辑配置把 `hard_gate_enabled` 改成 true。

### 18.6 已批准路径仍被拒绝

常见原因：

- 设计文件变化导致摘要失效。
- 实际文件不匹配 `allowed_file_patterns`。
- 项目被 relock。
- 已切换门禁模式。
- 当前任务 ID 与批准记录不一致。

使用 `package show` 和 `project-config show` 检查当前状态，修订后重新走审批。

### 18.7 原子发布失败

查看部署报告和两个目标目录。发布器会尝试恢复两端之前版本；不要在失败后手工只复制其中一个客户端，否则会造成 Codex/Claude 状态不一致。

## 19. 安全边界

以下批准相互独立：

- 批准记忆：只允许内容进入正式记忆。
- 批准 UI Skill：只创建不可变已批准版本。
- 发布 UI Skill：只写入指定 Skill 目录。
- 批准设计包：只解锁声明的前端任务范围。
- 批准项目全局基线：只按当前门禁配置解锁正式前端路径。
- 合并主分支、推送远端、部署 staging、访问生产和正式上线：必须分别获得明确授权。

任何报告、记忆、设计包、测试产物和审计记录都不得保存敏感凭据。

## 20. 日常检查清单

### 每次开始工作

- 确认当前项目正确。
- 加载项目和个人受控记忆。
- 若存在 `.loop/config.json`，读取项目 Loop 配置。
- UI 任务先检查门禁模式、设计偏好和有效 Skills。

### 审核记忆

- 内容是否稳定、准确、独立可理解？
- 个人候选是否真正可跨项目复用？
- 是否包含任务原文、路径、URL 或敏感信息？
- 批准目标是否正确？

### 审核 UI Skill

- 来源和固定 revision 是否可信？
- 名称、描述、许可证和引用是否正确？
- 脚本清单是否可接受？
- 当前摘要是否与批准命令一致？
- Codex/Claude 发布后摘要是否都匹配？

### 审核设计包

- 页面、组件和交互是否完整？
- 响应式、加载、空、错误和成功状态是否明确？
- 允许文件范围是否最小且准确？
- 当前摘要是否仍有效？

### 完成开发

- 运行完整自动化测试。
- 复核 diff、边界、兼容性和未处理异常。
- 验证功能分支、主分支和部署状态。
- 未获得明确批准时，不合并主分支、不部署生产。

## 21. 获取命令帮助

CLI 是最终参数依据。任何命令不确定时先运行：

```bash
python3 scripts/memory_review.py --help
python3 scripts/memory_review.py ui-skill --help
python3 scripts/memory_review.py ui-design --help
python3 scripts/memory_project.py --help
```

升级仓库后应以当前版本的 `--help`、README、测试和本说明书为准。
