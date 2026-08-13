# Vibe Memory 中文使用说明

Vibe Memory 是 Codex 与 Claude Code 共用的本地记忆管理器。它把个人/项目
记忆、候选审批、项目路由、UI 设计治理、UI Skills 与 Loop 工作流集中在一个
仅监听本机回环地址的审核台中。

本文给出从克隆、安装、迁移到卸载的可复制流程。首个公开版本仅支持 macOS
（Apple Silicon 与 Intel），需要 Python 3.10+，不依赖第三方 Python 包。

## 1. 从克隆开始安装

在普通 Terminal 中执行；如使用 fork，请替换仓库 URL：

```bash
git clone https://github.com/noema-ai/vibe_coding_manage_platform.git
cd vibe_coding_manage_platform
./install.sh
```

同时启用 Codex 与 Claude Code 通用用户 hooks：

```bash
./install.sh --with-claude-hooks
```

安装器会检查 macOS 和 Python 3.10+，将版本化运行时复制到
`~/Library/Application Support/VibeMemory/`，创建稳定命令
`~/.local/bin/vibe-memory`，结构化合并用户级 hooks，安装并启动
LaunchAgent `com.noema.vibe-memory`，最后运行 doctor。无关 hooks 不会被覆盖；
受管文件变化前会创建带时间戳备份。安装完成后可移动或删除源码 clone。

若 zsh 新终端找不到 `vibe-memory`，执行：

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.zshrc"
source "$HOME/.zshrc"
```

## 2. 首次运行选项

```bash
vibe-memory open
```

首次运行页可选择：Codex hooks、Claude Code hooks、自动候选检查、个人短期
记忆保留天数、是否登录时启动、服务端口（默认 8897），以及是否注册一个
workspace。三类正式记忆——个人长期、个人短期、项目长期——始终要求用户对
具体内容显式审批，设置页不能关闭这条规则。

安装或修复 hooks 后，应完全退出旧客户端，再启动 fresh Codex or Claude Code
session，让客户端重新加载并信任用户级配置。旧会话继续可用，但不能作为 hook
已加载的验证证据。

若关闭“登录时启动”，首次设置完成后服务会停止。需要使用时显式启动当前登录
会话，且不会改变该持久偏好：

```bash
vibe-memory start && vibe-memory open
```

手动 plist 同时使用 `RunAtLoad=false` 和 `KeepAlive=false`，因此下次登录不会
自动启动，进程退出后也不会自动重启。
若登录启动本来已启用，`vibe-memory start` 会保持两个键均为 true；两种模式都
只启动当前会话，不改写已保存的偏好。

## 3. 注册与初始化项目边界

workspace 可以是代码仓库，也可以是普通目录：

```bash
vibe-memory project register "/path/to/workspace"
vibe-memory project init "/path/to/workspace"
vibe-memory project list
```

- `register` 只把规范化路径写入全局项目注册表并选中项目。
- `init` 只创建缺失的项目记忆文件和受管说明块，不安装 project hooks。
- Codex/Claude universal hooks 是唯一事件入口，避免用户级与项目级重复触发。
- registered cwd 命中最深的已注册父目录，可使用项目记忆和个人记忆，也可由
  当前模型生成项目与个人候选。
- unregistered cwd 只有个人上下文，只能生成 personal candidate；不得创建或
  修改项目文件，也不得生成项目候选。

注销项目只移除注册与受管说明，不删除项目记忆：

```bash
vibe-memory project unregister "/path/to/workspace"
```

## 4. 旧安装迁移：先预览，再批准

迁移目标必须先注册。预览不写文件；应用必须同时提供 `--approved` 和明确的
项目根：

```bash
vibe-memory migrate preview --project-root "/path/to/workspace"
vibe-memory migrate apply --approved --project-root "/path/to/workspace"
vibe-memory doctor
```

预览/迁移检查项目与个人记忆、候选与审核状态、项目注册表、design
preferences、UI design approval、UI Skills、Loop、policy 以及旧 Codex/Claude
项目 hooks。应用阶段只删除可识别的旧受管 hook 条目，保留第三方配置，并输出
root、before/after digest、changed paths、backups 与 result 的审计信息。

`partial` 或 `failed` 会返回非零状态。按输出定位失败的控制面区域或备份，不要
盲目重试；修正后重新 preview，再显式 apply。必须同时检查 preview JSON 和退出码：
任何项目 `error`、无效 preflight 或非 clean 结果都会返回非零，但仍完整打印 JSON
诊断。

## 5. Hook 与模型各自负责什么

共享 hook 只写 event metadata 和策略提醒。它不会复制 raw prompt，不会截断或
总结 prompt，不会调用模型 API，也不会自行制造候选。active Codex or Claude Code model
使用当前对话中已有的上下文，蒸馏 personal long、personal short、
project long 候选或 project short 工作摘要，然后通过统一候选接口提交。

项目短期记忆是当前模型蒸馏、可定期压缩的工作摘要，不是 hook 捕获的提示词。
若当前模型没有提出候选，hook 不会从事件 payload 补造一个。

## 6. 审批与隐私治理

- 个人候选只允许跨项目可复用的开发习惯、协作偏好、工作流偏好、思维方式和
  稳定用户画像。
- 项目候选只允许稳定架构、部署规则、产品方向、技术约束和项目工作流事实。
- 一次 substantial instruction 最多提出两个蒸馏候选；相同候选去重，冲突候选
  不得自动覆盖 active memory。
- raw conversation、一次性任务、PRD 原文、截图、URL、本机路径、不确定猜测、
  token、密码、验证码、API key 和云服务密钥不得进入候选。
- 个人长期/短期和项目长期候选先保持 pending；用户查看确切内容后才可批准为
  active。编辑、驳回、删除和噪声隔离均为显式、可审计操作。
- Codex 与 Claude Code 共用存储、项目注册表、审批状态和 policy，同时在候选
  provenance 中保留来源模型。

## 7. 审核台全部功能

运行 `vibe-memory open` 后可使用以下页面：

1. **pending**：查看、搜索、编辑、批准、驳回、延期、重置候选；噪声候选可
   隔离并标记拒绝，同时保留原始提案以供审计。
2. **active**：浏览、搜索、编辑或删除已生效的项目/个人长短期记忆。
3. **projects**：注册、选择、初始化、查看状态，或升级已有项目。
4. **design preferences**：维护全局默认值和项目 override；支持 inherit、
   replace、append、clear，并展示最终 effective value 与来源。
5. **UI design approval**：配置正式前端、设计产物、生成文件和测试产物路径；
   使用 `design_package` 或 `project_global` 模式；创建/修订设计包，批准、驳回、
   退回修改、失效和重新锁定 hard gate。
6. **UI Skills**：从编辑器、目录、ZIP 或固定 Git revision 导入；检查
   `SKILL.md`、license、scripts、digest 与 diff；validate、request revision、
   approve、publish 到 Codex/Claude、disable、scan 和 rollback。
7. **Loop**：初始化或升级 Loop × Superpowers；阅读 PRD/验收标准、worktree、
   staging、Claude 独立评测、release、master/production 审批边界。
8. **policy**：查看 scope routing、候选类别、优先级、审批规则、隐私排除、审计、
   备份和恢复策略。

审核台仅绑定 `127.0.0.1`，不得直接暴露到公网。

## 8. UI 设计审批要点

可见界面任务在批准前只允许调研、读代码和编写设计稿/原型/交互说明，不能改
正式前端业务代码；纯后端和无界面任务不触发门禁。

- `design_package`：批准绑定 task、version、文件范围和 SHA-256 digest。设计
  文件变化会使批准失效，未声明路径保持禁止。
- `project_global`：批准一个项目基线后解锁正式前端，直到 relock、模式改变或
  基线 digest 改变。
- 修改路径或模式会关闭/重锁 hard gate；只有 Codex 与 Claude hook smoke test
  都通过后才能启用。

所有修改操作使用唯一 idempotency key。摘要冲突必须重新检查内容和新 digest，
不能绕过。

## 9. UI Skills 与恢复

推荐生命周期是 import/bootstrap → validate → approve → atomic publish。
bootstrap 只创建待审核草稿，不批准、不发布。发布使用 staging directory 原子
替换 Codex 与 Claude 两个目标；包脚本只展示，不执行。unmanaged/ignored/drifted
Skill 只报告，不修改。

发布异常时先 disable hard gate，检查 audit 和 deployment report，再 rollback 到
已批准的 immutable version；不要删除 unmanaged Skill。完成后修复 hooks 并启动
fresh Codex or Claude Code session 验证。

## 10. Loop 与发布审批边界

Loop 是生命周期编排器：负责需求验收、一个任务一个 worktree/branch、staging、
独立 Claude evaluation、release、主分支与 production；Superpowers 只提供构思、
计划、TDD、调试、review 和完成前验证。子代理/并行代理仍需用户明确授权。

Loop 期间不得合并 master 或部署 production。主分支合并和正式上线必须由用户
在产品验收后主动确认；force push 也不属于默认授权。

## 11. 日常检查与 doctor

```bash
vibe-memory status
vibe-memory doctor
vibe-memory doctor --json
vibe-memory hooks status
```

doctor 检查 runtime、Python、Codex/Claude hooks、service、data 与完整 control
plane。全部健康时各区域为 `ok/current/healthy/ready`，命令返回 0。否则命令返回
非零并列出 action 或 `non_ok_areas`；按具体区域修复，不要忽略非零状态。

## 12. 更新与回滚

更新源必须是已审核的本地 clone：

```bash
cd "/path/to/local/clone"
git pull --ff-only
vibe-memory update --source-root "/path/to/local/clone"
vibe-memory doctor
```

update 先安装和验证新版本，再原子切换 `current`，同时保留旧版本与全部数据。
如果更新后的 smoke test 或 doctor 失败：

```bash
vibe-memory rollback
vibe-memory doctor
```

rollback 只切换程序和受管配置，不回退更新后产生的记忆。若没有可回退版本，
命令会明确失败而不是猜测目标。

## 13. Repair、Hooks 与 LaunchAgent

```bash
vibe-memory repair
vibe-memory hooks status
vibe-memory hooks repair
vibe-memory doctor
```

`repair` 恢复版本化 runtime、launcher、配置与 LaunchAgent，并重启服务；
`hooks repair` 只修复签名匹配的受管条目，保留无关 hooks，随后运行 smoke test。
两者都会拒绝不安全的 symlink、错误 ownership 或并发变化，而不是覆盖未知文件。
修复完成后启动 fresh Codex or Claude Code session。

## 14. 安全卸载

默认卸载保留数据，是推荐的可恢复方式：

```bash
vibe-memory uninstall
```

它移除 runtime、stable launcher、LaunchAgent 和受管 hooks，但保留个人/项目
memory、proposals、review history、project registry、design preferences、UI
design approvals/audit、UI Skills/deployments、Loop/worktree state、logs 和
backups。

删除数据必须同时给出删除开关、二次批准和每个精确路径：

```bash
vibe-memory uninstall --remove-data --approved-data-deletion \
  --data-path "$HOME/.codex/memory_review/projects.json"
```

每个 `--data-path` 必须是 allowlist 中精确的受管 regular file，cannot be a directory，
也不能是 symlink。工具会在停服务或修改 hooks/runtime 前完整验证所有
目标，不会推断要删除的项目目录。若只想重装，请使用默认卸载，不要删除数据。

## 15. 服务与 LaunchAgent 故障排查

1. `vibe-memory doctor --json` 查看是 runtime、service、hook 还是 control plane。
2. 若关闭登录启动，先运行 `vibe-memory start && vibe-memory open`；否则运行
   `vibe-memory repair` 修复受管安装并重新加载 LaunchAgent。
3. `vibe-memory hooks status` 检查两个客户端；需要时运行 hooks repair。
4. 若端口被占用，停止错误监听者或使用空闲回环端口重新安装/修复；`open` 会
   拒绝连接身份或版本不匹配的服务。
5. 查看 `~/Library/Logs/VibeMemory/`，但不要把含私有内容的日志发布到仓库。
6. 完全退出并重启客户端；首次提示时信任用户级配置。fresh client trust/restart
   是真实 hook 验收的一部分。
7. migration partial 时只按报告恢复对应时间戳备份，重新 preview 后再 apply。

## 16. 开发者源码流程（非安装用户主路径）

终端用户应使用上面的 `vibe-memory` 命令。维护者可从源码运行 Python 测试和
开发服务器，但 direct Python/start script 不是 installed flow，也不得写入用户
hooks。发布前执行：

```bash
python3 -m unittest discover -s tests -v
python3 scripts/verify_release.py --tree .
python3 scripts/public_release_check.py --tree .
```

verify_release 是真实 13-gate：manifest、Python、full unit tests、real Darwin
installed-runtime E2E、public tree、plist、loopback、permissions、Codex hook、
Claude hook、control plane、rollback、uninstall。完整发布流程见
[RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md)。

## 17. 命令帮助

```bash
vibe-memory --help
vibe-memory project --help
vibe-memory migrate --help
vibe-memory hooks --help
```
