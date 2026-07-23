# Loop Engineering × Superpowers 跨项目初始化与升级设计

## 背景与目标

中央管理器目前能够初始化项目记忆、生成 Codex/Claude Code 受管钩子，并创建或升级通用 Loop Engineering 配置。全局 `~/.codex/loop_engineering/` 与 `~/.claude/loop_engineering/` 已描述 Superpowers 方法层，试点项目也已有完整的 `methodology.superpowers` 契约和完成验证器，但中央管理器生成的新项目配置仍只有通用 Loop 字段，记忆审核台的 Loop 说明也没有展示新方法。

本次改造让中央管理器成为 Loop × Superpowers 的唯一跨项目分发入口：新 Loop 项目默认获得最新方法契约；旧 Loop 项目通过显式升级补齐；只初始化记忆的项目不被强制启用 Loop。Loop 继续掌管 worktree、分支、staging、独立评测、release、主分支和 production，Superpowers 只提供阶段内工程方法。

## 已确认的产品决策

1. 新项目通过“初始化 Loop”默认启用 Superpowers。
2. 旧项目只在用户显式执行“升级 Loop”后迁移，不静默批量修改。
3. 不新增独立 Superpowers 钩子；扩展现有受管记忆钩子的上下文提醒。
4. “初始化记忆”可以独立使用，不因此创建 `.loop/config.json` 或启用 Superpowers。
5. 升级必须保留项目自定义的数据库、OSS、端口、远程目录、测试命令、审批规则及其他已有字段。
6. 子代理和并行代理默认关闭，只有用户明确授权且满足 Loop 隔离要求时才能使用。
7. 本次只修改中央管理器功能分支；主分支合并和任何部署继续受用户审批控制。

## 方案选择

采用“中央默认 + 显式迁移 + 现有钩子增强”。不采用仅更新文档的方案，因为它不能保证新项目获得配置和完成门禁；也不采用全局强制钩子方案，因为它会影响未接入 Loop 的仓库，并产生插件技能发现与钩子执行两套控制面。

## 系统边界

### 中央管理器负责

- 生成完整的 Loop × Superpowers 默认配置。
- 对现有 `.loop/config.json` 做只补缺失项的升级。
- 分发语言无关的 Loop 方法验证器，并把 completion 验证接入 `worktree_flow.py finish`。
- 生成能够识别 Loop × Superpowers 的 AGENTS.md、CLAUDE.md、共享规则和受管记忆钩子。
- 在记忆审核台展示项目接入状态、操作入口、变更结果和完整使用说明。
- 通过自动化测试保证初始化、迁移、备份和幂等性。

### 项目仓库负责

- 保存项目特有的 `.loop/config.json`、设计、验收、计划和报告。
- 配置项目技术栈自己的单元测试、集成测试、staging 资源和 Claude Code 评测方式。
- 可以扩展中央分发的方法验证规则，但不得关闭 Loop authority、报告身份和完成证据等最低门禁。

### 插件负责

- Codex 与 Claude Code 各自通过官方插件提供和发现 Superpowers 技能。
- 中央管理器不复制插件缓存，不把插件版本固化为跨项目必须一致，也不通过钩子模拟技能调用。

## 配置模型

`memory_project.loop_config()` 生成 schema version 3。现有 Loop 生命周期字段保持不变，新增完整的 `methodology.superpowers` 默认契约：

- `enabled: true` 与 `provider: superpowers`。
- Codex/Claude Code 官方插件选择器。
- Loop authority：worktree、staging、release、主分支、production 均由 Loop 控制。
- 标准产物：设计、验收、计划、内部审查、完成验证和独立评测。
- 14 项技能声明与按 intake、新功能、实现、调试、评审、完成、技能维护分类的路由。
- Claude Code 独立评测者只允许评测相关技能，默认不能修改产品源码。
- 子代理默认关闭，授权与隔离要求显式配置。

中央管理器分发纯 Python、无第三方依赖的 `scripts/validate_loop_methodology.py`，用于校验配置契约、产物路径、清单、分支与不可变提交身份、报告状态和新鲜度。`worktree.finish_validation_commands` 默认包含 `python3 scripts/validate_loop_methodology.py --phase completion`；项目技术栈的测试继续放在 `verification.commands`。只有验证器存在、默认 completion 命令已接入且方法契约完整时，状态才显示为“最新 Loop × Superpowers（完成门禁已配置）”。

## 初始化与升级行为

### 初始化记忆

`init` 继续创建缺失的记忆文件、项目规则、受管钩子和共享规则，不创建 Loop 配置。生成的规则使用条件式措辞：仅当 `.loop/config.json` 存在且启用了 `methodology.superpowers` 时，才要求读取 Loop × Superpowers 说明和执行技能路由。

已有文件继续遵循“不覆盖”原则。需要采用最新受管钩子的项目使用独立的“升级记忆钩子”操作。

### 初始化 Loop

`init-loop` 先执行记忆初始化，再创建 schema version 3 的完整 Loop × Superpowers 配置、标准 Loop 目录和方法验证器，并把 completion 命令接入 finish。结果返回每个文件的 `created`、`existing` 或 `upgraded` 状态，以及方法契约与完成门禁状态。

初始化不自动安装插件、不部署 staging，也不访问外部模型。页面说明会提示插件必须在各自平台安装并从新会话生效。

### 升级 Loop

`upgrade-loop` 只在用户对指定项目发起操作时执行。升级流程：

1. 读取并验证现有 JSON；非法 JSON 直接失败且不写文件。
2. 计算 schema version 3 默认值与现有配置的深度合并结果。
3. 现有值优先，只补缺失键；`schema_version` 与 canonical root 由管理器维护。
4. 安装缺失的方法验证器；如果存在中央管理器可识别的旧版受管验证器，先备份再升级；无法识别所有权的同名文件不覆盖并报告人工处理。
5. 在保留已有 finish 命令顺序的前提下补入唯一的受管 completion 命令。
6. 写入前创建带时间戳的 `.bak.*` 配置备份。
7. 原子写入升级结果，返回新增键、验证器与门禁状态摘要。
8. 再次执行升级时不得继续改动文件或创建无意义备份。

升级不会自动修改 AGENTS.md、CLAUDE.md 或钩子；页面在检测到旧规则或旧受管钩子时，单独提示执行“升级记忆规则/钩子”。这样配置迁移与对话上下文迁移各自可审计、失败时互不影响。

## 受管规则与钩子

不增加新的 hook event 或 hook 文件。现有 Codex/Claude Code `shared_memory_hook.py` 在生成 context packet 时读取 `.loop/config.json` 的安全子集，并在启用 Superpowers 时增加：

- Loop 是唯一生命周期编排器。
- 需要读取两侧 Loop 目录和项目 Loop × Superpowers 说明。
- 新功能、Bug、评审、完成阶段的技能路由摘要。
- 完成声明前必须运行项目配置的完成验证命令。
- 子代理需要用户明确授权。

钩子不执行技能、不修改 Loop 配置、不创建 worktree、不调用外部模型，也不把完整配置或敏感字段复制到短期记忆。解析失败时只输出“Loop 配置不可解析”的提醒，不阻断普通记忆上下文生成。

`upgrade-memory-hooks` 继续只替换中央管理器拥有的两个 hook script，替换前保留时间戳备份。新增“升级记忆规则”能力，以受管标记块更新 AGENTS.md、CLAUDE.md 和 `.claude/rules/shared-memory.md` 中由中央管理器维护的段落；不改动标记块外的用户内容。旧文件没有受管标记时只追加一次最新规则块。

## 项目状态与审核台交互

项目管理页面为每个仓库计算以下独立状态：

- 记忆：未初始化、已初始化、受管规则/钩子可升级。
- Loop：未初始化、旧版 Loop、Loop × Superpowers 已启用。
- 完成门禁：未配置、已配置。
- 配置健康度：有效、JSON 无效、关键契约不完整。

按钮按状态显示：

- 未初始化记忆：`初始化记忆`。
- 未初始化 Loop：`初始化 Loop`。
- 旧版或契约不完整：`预览升级 Loop`，确认后执行。
- 受管规则/钩子过期：`升级记忆规则/钩子`。

升级预览显示将新增的配置路径、会保留的项目定制类别、将创建的备份，以及验证器同名冲突等需要人工处理的事项。页面不展示密钥或完整敏感配置值。

Loop 使用说明新增“Loop × Superpowers”章节，说明角色关系、标准阶段、技能路由、产物位置、初始化/升级方式、完成门禁、子代理限制和主分支/production 审批边界。

## API 与数据流

保持现有本地 API 兼容：

- `/api/projects/init-loop` 继续初始化指定项目，返回新的状态字段。
- 新增 Loop 升级预览接口，只做读取和计算。
- 执行升级接口要求明确的项目根目录和用户确认标记。
- 记忆规则/钩子升级使用独立接口，避免与 Loop 配置升级形成半成功事务。

数据流为：页面选择项目 → 后端规范化并校验项目根目录 → 读取项目状态 → 生成预览 → 用户确认 → 创建备份 → 原子写入 → 重新计算状态 → 页面展示逐文件结果。

所有路径必须经过项目根目录规范化与仓库存在性检查；API 不接受客户端提供的任意目标文件路径。

## 错误处理与恢复

- 非 Git 目录、目录不存在或无写权限：操作前失败，不产生部分文件。
- `.loop/config.json` 非法：保留原文件，返回可读错误，不尝试修复猜测。
- 备份失败：停止升级，不覆盖原文件。
- 写入失败：使用同目录临时文件和原子替换，原配置保持可恢复。
- 规则块无法安全识别：不重写整个文件，只报告需要人工处理。
- 插件未安装：配置仍可初始化，但状态显示“插件待安装”，不声称方法层已可执行。
- 验证器同名文件属于项目自定义实现：不覆盖，显示“门禁待人工接入”，不得显示完整接入。

## 测试策略

所有行为先写失败测试，再做最小实现：

1. 新项目初始化生成 schema version 3、完整 Superpowers 契约、方法验证器和 finish completion 命令。
2. 纯记忆初始化不创建 `.loop/config.json`。
3. 旧配置升级补齐方法字段并保留数据库、OSS、端口、路径、验证命令和未知扩展字段。
4. 首次升级创建配置或受管验证器备份；重复升级幂等且不创建额外备份。
5. 非法 JSON、备份失败和原子写入失败不破坏原配置。
6. hook context 只在 Superpowers 启用时出现方法提醒，不泄漏完整配置或敏感值。
7. 规则块升级保留用户自定义文本，并能从没有标记的历史文件安全追加。
8. 项目状态能够区分未初始化、旧版、方法已启用、验证器冲突、门禁未配置和完整接入。
9. API 预览无写入，执行接口要求确认，并拒绝非法项目根目录。
10. 记忆审核台 Loop 说明包含最新初始化、升级和权限边界。
11. 运行中央管理器完整测试、Python 语法检查和 `git diff --check`。

## 发布与兼容性

本次变更在 `codex/superpowers-finish-validation` 功能分支继续开发。旧 CLI 参数和现有 API 路径保持兼容；新增字段只扩展返回值。完成后先在临时项目目录验证初始化与升级，再由用户验收功能分支。未经用户后续明确批准，不合并中央管理器 master，不批量升级注册项目，也不修改 production 或 staging 环境。

## 完成标准

- 新项目能从审核台或 CLI 初始化记忆，并单独初始化最新 Loop × Superpowers。
- 旧项目能预览并显式升级，项目定制值和用户规则内容得到保留。
- 现有钩子提供准确的条件式方法提醒，没有新增独立 Superpowers 钩子。
- 审核台准确展示接入状态和最新完整使用说明。
- 所有新增测试、中央管理器完整测试、语法检查和差异检查通过。
- 功能只存在于中央功能分支，等待用户验收与后续主分支合并授权。
