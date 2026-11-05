# Windows 原生兼容实施计划

**目标：** 在 `feat/windows-native` 分支实现 Windows Python 3.10+ 与 Git 即可运行的完整版本。
**架构：** 业务层复用现有实现；平台文件操作、安装和后台生命周期采用明确分派，不全局修改 os 或伪造 fcntl 模块。
**技术栈：** Python 标准库、Windows 原生 API、Windows Git、CMD / Git Bash 入口。
**设计：** ../specs/2026-11-05-windows-native-design.md

## 全局约束

- 不依赖 WSL、PowerShell、计划任务、Windows 服务或第三方 Python 包。
- 正式记忆保持审批机制；测试不修改真实用户配置、记忆或自启项。
- 文档默认中文，主要中文文档检查 UTF-8 和乱码。

## 审阅重点

中文与空格路径；进程崩溃释放锁；junction 与符号链接；端口占用及 PID 重用；安装副本脱离源码运行。

## 任务 1：平台文件基础

文件：新增 scripts/runtime_platform.py、tests/test_runtime_platform.py。
接口：validate_path(path)、file_lock(path, timeout=10)、atomic_write(path, content)、sync_directory(path)、set_file_mode(fd, mode)。
- [ ] 先测试中文路径原子替换、多进程互斥与超时、进程退出释放锁、拒绝重解析点。
- [ ] 运行失败测试；实现标准库平台分派；重新运行至通过。
- [ ] 接入已有通用存储，不改变 POSIX 行为。

## 任务 2：项目、队列与 hook

文件：memory_project.py、memory_review_queue.py、vibe_memory_router.py、vibe_memory_hooks.py、vibe_memory_paths.py。
- [ ] Windows 注册表采用路径和文件锁接口，保留 POSIX dir_fd 实现。
- [ ] 队列和上下文包写入、幂等锁适配 Windows；刷新进程不传 pass_fds。
- [ ] Windows 全局与项目 hook 直接调用 Python，保留第三方 hook，生成平台正确候选命令。
- [ ] 测试注册项目、未注册项目不写入、候选审批、两种客户端事件以及中文路径。

## 任务 3：后台与安装

文件：新增 Windows 生命周期模块、install.cmd；修改 install.sh、CLI、settings、server。
- [ ] 生命周期覆盖 install、update、rollback、repair、start、stop、restart、status、doctor、open、uninstall。
- [ ] 用户目录独立副本，原子配置，日志，后台就绪检测，身份验证后停止，不以裸 PID 杀进程。
- [ ] 可选启动文件夹自启，首次设置与管理页面平台分派。
- [ ] 临时用户目录端到端验证安装、重复启动、端口冲突、更新回滚、保留记忆卸载。

## 任务 4：其余业务与回归

文件：迁移、设计、worktree、安装文档及平台测试。
- [ ] 适配业务调用链中的剩余 POSIX 操作，验证主要管理 API 与 Git worktree。
- [ ] 完整 unittest discovery，逐项记录原有平台测试不适用项，不掩盖失败。
- [ ] 检查中文文档编码、git diff --check、全分支审阅。

## 执行记录

- 2026-11-05：从 master 创建 feat/windows-native；设计已经用户确认，并追加无需 PowerShell 与计划任务约束。
- 环境切换为受限写入；仓库改动通过已授权的提权命令进行。
- 必需项目与个人记忆文件在公开仓库 / 当前 Windows 用户目录不存在，WSL 记忆路径不可访问；未创建候选或正式记忆。

### 本轮已验证进展

- 新增 runtime_platform.py：Windows msvcrt 进程锁、POSIX flock 分派、原子写入、重解析点检查；未接入业务层。
- vibe_memory_paths.for_home 新增显式平台与 AppData 参数，Windows 默认指向 LocalAppData/NexarlForge；显式 home 不读取真实 AppData。
- 测试命令：python -B -m unittest discover -s tests -p test_runtime_platform.py -v。
- 结果：7 项中 6 项通过，符号链接测试因无创建权限跳过；另有真实 junction 测试通过。
- 尚未完成：平台基础接入、Windows ACL、项目注册、队列、hook、后台、安装器及完整回归；不应宣称当前已能原生运行。
- 注意：原 test_vibe_memory_paths.py 的默认路径断言固定为 macOS，后续需要改为显式 darwin 以保留原平台契约。

### 2026-11-05 Windows 业务基础接入

- 已接入项目注册表事务、队列原生锁、hook 幂等锁、上下文包 journal 事务。
- Windows 队列刷新使用独立 worker 与注册状态复核，不传 POSIX pass_fds；真实子进程刷新测试通过。
- Windows 进程存活查询使用 SYNCHRONIZE 只读句柄，不调用 os.kill(pid, 0)。
- tests/test_windows_registry.py：7 项通过；平台基础测试此前 6 项通过、1 项因 symlink 权限跳过。
- 尚需：Windows ACL 隔离；hook 命令生成与合并；安装和独立副本；后台启停与自启；首次设置平台分派；迁移/设计/worktree 及完整回归。
- 中间两次提权测试收到 KeyboardInterrupt；未因此重启任何持久服务。相同测试后续在普通沙箱执行完整通过。

### Windows hook 与后台初步接通

- hook 命令直接执行 Python 和稳定脚本；修复备份、幂等、卸载保留自定义配置测试通过。
- Windows hook 写入采用工具锁与用户态内容 CAS；目标打开句柄会阻止替换，修复与卸载在比较后关闭源句柄。并非 macOS 内核交换级保证。
- 新增 windows_service.py，复用现有管理页面 Handler；后台不依赖 shell，绑定 loopback，以独立实例和秘密令牌验证停止，不按裸 PID 杀进程。
- test_windows_service.py 的真实后台启动、健康、重复启动与停止测试通过。启动与停止未接入正式 CLI、安装或首次设置。
- 尚需验证权限 ACL、端口冲突、令牌错误、状态损坏；服务端控制入口只有本机令牌认证。不能在 ACL 完成前宣称发布就绪。
- 自动审批服务曾返回一次 422 且未执行命令；确认文件不存在后原操作重试成功。

### Windows 安装与正式 CLI 接入

- private_directory 使用当前用户 Token SID 与受保护 DACL，允许当前用户和 SYSTEM；后台写令牌前保护安装/state 目录。真实 ACL 测试通过。
- 新增 windows_runtime.py：用户目录独立副本、缺失记忆初始化、配置、hook 修复、后台启动和卸载保留记忆。
- CLI Windows 生命周期已分派 install/update/start/stop/restart/status/doctor/open/uninstall/hooks；repair 与 rollback 仍需实现，不能按现状宣称所有命令完成。
- Windows 安装端到端测试通过：隔离用户目录，安装后执行副本 CLI status，后台健康，卸载保留记忆。
- 新增 install.cmd；install.sh 在 Git Bash 使用 Windows Python 原生入口；入口脚本端到端、失败回滚、登录自启、首次设置、完整 doctor 仍需补测。
- 当前 install 保留 previous 副本，但不具备完整事务回滚。卸载只删除托管运行副本和入口，保留配置日志及记忆。

### 首次设置、自启与完整回归初审

- Windows 自启改为启动文件夹中的 NexarlForge.cmd，不依赖 VBS、PowerShell 或计划任务。受控内容标记用于避免覆盖外部文件。
- Windows 首次设置接入独立事务，保存配置、hook、保留策略、项目注册、自启与服务状态，响应后调用原生停机/端口重启。
- 管理页面首次设置 HTTP 测试通过；独立安装测试确认生成自启项，卸载移除自启并保留记忆。
- 后台明确优先运行安装副本中的 windows_service.py；Windows Python 3.10 缺失 fchmod 的调用已在 hooks/settings 分派。
- CRLF 旧格式短记忆保留问题通过失败测试复现并修复，不对字节长度摘要封装进行文本换行转换。
- Windows 专项测试 14 项全部通过。
- 完整 unittest discovery 在隔离 HOME/USERPROFILE/AppData 中执行：593 项，77 failures、295 errors、19 skipped，耗时 158 秒。日志位于系统临时目录 forge-windows-suite-bz6y_me6/tests.log；尚非全绿。错误类型包括 POSIX API/dir_fd、权限或符号链接前提、硬编码 macOS 路径及真正的兼容缺陷，需逐项审计，不能整体跳过。
- 剩余主要交付：安装事务回滚与更新/rollback/repair；安全 doctor；候选命令平台语法；迁移与设计存储/worktree；入口执行测试；中文用户文档、最终完整验证与分支提交。

### 更新恢复、主动回滚与修复

- 更新注入 hook 修复失败测试通过：恢复原副本、配置与原运行后台。安装更新保留前一副本，不提前销毁原 previous。
- 新增 rollback/repair 并接入正式 CLI；真实两轮安装后回滚、健康检查与修复保留个人记忆测试通过。安装相关 3 项通过。
- 尚需加固：安装失败回滚的并发配置冲突保护、回滚交换中间步骤失败恢复、doctor 完整校验、Git Bash 入口及候选命令平台语法、迁移/worktree、完整回归与最终提交。

### Windows doctor 与候选 CLI

- doctor 覆盖运行配置/版本/解释器/入口、Codex hooks、Claude hooks、服务与记忆注册数据五个领域；安装集成测试全部健康。
- 新增全局 --project-root 参数，Windows 上下文候选命令直接调用解释器与脚本，不使用 env 或 Unix 单引号。参数使用独立 dest，避免与 project/migrate 子命令冲突。
- Windows registry/hook 上下文专项 8 项通过，安装端到端 3 项通过。
- 仍需完成：候选实际提交审批与未注册场景测试、文档命令执行、回滚并发安全审计、迁移/设计/worktree 与完整回归。

### 真实记忆 CLI 与中文 CMD 入口

- 临时用户目录真实子进程验证：项目注册/初始化、候选提交、审批前正式记忆不变、明确审批后写入、未注册项目拒绝且不创建 codex 目录。测试通过。
- 安装集成测试改用中文与空格路径，执行真实 CMD 启动器 status；CLI 输出编码统一 UTF-8，测试使用显式 UTF-8 读取。安装 3 项通过。
- Windows 停机在健康端点关闭后继续只读查询已认证实例 PID 是否退出，避免日志句柄仍在占用；不会按 PID 发终止信号。
- 待继续：doctor 故障用例、安装/回滚并发冲突恢复加固、Git Bash/安装入口端到端、原有迁移设计/worktree 与完整回归分类、中文使用文档和分支提交。

### Git Bash 启动器与旧 hook 迁移

- 中文/空格安装路径真实 Git Bash 启动器 status 通过；安装 3 项端到端通过。安装脚本本身 Git Bash/CMD 调用仍待测试。
- 新增 windows_migration.py，旧 hook 快照预检、已知脚本所有权、配置备份和脚本归档、失败冲突保护；项目注销接通。保留自定义 hook 的测试通过。
- Windows 安全包摘要实现与原 canonical 协议一致，保留文件/总大小/数量限制；专项测试通过。
- 完整兼容仍未完成，尤其 inspect_ui_skill_digests 目录 fd、控制面迁移库存、worktree 命令参数、并发回滚及完整测试分类需要继续。


### 入口、worktree、控制面与独立审查（继续实施）

- Windows 技能摘要库存切换到有界原生安全遍历，校验版本路径组件；迁移专项4项通过，覆盖摘要、越界、旧hook移除及升级入口。
- worktree 使用 CommandLineToArgvW 解析用户命令，保留反斜杠，输出显式UTF-8；7项真实Git工作流通过。跨平台测试将Unix test命令改为Python断言。
- 真实CMD与Git Bash安装入口通过；移走源码后，安装副本status通过。首次设置真实HTTP切换端口与关闭自启后自身停机通过。
- Windows文件过滤器可能短暂占用运行目录，目录切换增加有界PermissionError重试，不覆盖已存在目标。
- repair同步恢复Git Bash入口。回滚目录交换中途失败注入已验证恢复原版本及后台。
- 独立代码审查发现两项P1并已修复：hook事务在自身锁内比对起始内容，只有实际变更才登记精确提交字节，失败恢复保留并报告外部编辑；Windows项目hook升级清理旧入口，不再生成POSIX命令。生成兼容脚本的Windows路径转义也已修复。
- 新增中文Windows用户指南，已在README链接。还需最终UTF-8检查。
- 设计相关94项测试复跑：87项通过，剩余7项为5项macOS首次设置/LaunchAgent mock前提和2项当前账户缺少符号链接特权。曾发现的4处真实临时JSON重读PermissionError已修复；没有整体跳过旧suite。
- 当前尚未提交/推送，完整跨平台旧suite仍未全绿。剩余重点：审查复核、事务冲突注入、Windows路径特殊字符及junction边界、发布元数据和最终验证；不可宣称完整release验收完成。


### 阶段验证与检查点

- 最新完整Windows专项26项全部通过，包含并发外部hook在失败更新后保留的复现回归。
- 原生平台8项：7通过，1项因账户无符号链接特权跳过；junction与ACL真实测试通过。
- 独立复核确认原先两条P1复现已消除；进一步将回滚check+restore放入与hook/settings相同的exclusive_lock，Windows专项再次26项全绿。
- 中文主要文档严格UTF-8读取，无replacement字符或连续问号；git diff --check通过。
- 本次仅保存开发分支阶段检查点，不代表完整发布验收；完整旧suite平台分类、特殊路径与发布元数据仍需继续。


### 收尾验收（2026-11-05）

- 当前实现的 Windows 发布入口 `python scripts/verify_release.py` 已通过：201项，184通过、17明确跳过，耗时120秒。原生测试、共享UI设计/技能管理、worktree、Loop、事件、路径和发布扫描全部纳入；没有将任意失败自动转换为skip。
- 17项skip由5个macOS LaunchAgent专属fixture、1个macOS目录fd安装快照比较、11个本账户缺少symlink权限的用例组成；真实junction、ACL、进程锁及原生等价生命周期测试仍执行。
- 完整旧discovery审计为606项，80 failures/292 errors，已用于识别真实缺陷并修复，不宣称旧全集在Windows全绿。主要POSIX目录fd/flock/launchctl/syscall注入类保留在原macOS CI；Windows使用显式平台测试集，新加Python3.10/3.13 CI。
- 已补齐：发布平台与协议验证、Windows完整doctor控制面、安装完成后doctor检查、重装保留已有端口、目录交换早期失败恢复previous和原后台、首次设置新端口跳转/停机提示。
- 短记忆写入禁用隐式换行转换，备份以O_BINARY写入，真实审批封装摘要及备份字节回归通过。
- 迁移保持只读预览、校验根身份/已知脚本所有权、保留自定义hook、生成前后摘要审计；审计失败恢复、并发源冲突及重绑定拒绝均有Windows实际测试。
- 设计路径已覆盖Windows设备名、ADS、盘符、尾点与junction，合法范围通配符保留；包摘要排序跨平台一致。发布扫描亦拒绝junction，不遍历外部目录。
- Codex及Claude Code真实hook子进程上下文注入通过，未注册目录不写项目数据、正式记忆不自动变更。
- 独立只读审查的P1并发问题已修复且复核确认；未发现其他明确P1/P2。剩余验证限制为本机未执行macOS安装器及Python3.10解释器实际运行，CI矩阵已配置，不能声称其远程结果已通过。
- 提交为本地开发分支，不包含推送、合并或发布。没有修改正式个人记忆或创建个人记忆候选。

- 最后页面核验：发现并修复首次设置事件监听器缺少闭合括号；实际渲染JS使用Node语法检查通过，模拟DOM执行的保存/新端口跳转/停机提示3分支通过。产品运行不依赖Node。修复后页面契约与真实HTTP端口重启/自停2项回归通过。
