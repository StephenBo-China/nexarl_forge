# Windows 原生版

## 环境

安装 Windows Python 3.10 或更高版本（勾选 Add Python to PATH），以及 Git for Windows。运行时不依赖 WSL、PowerShell、Windows 任务计划程序或 Windows 服务，也无需第三方 Python 包。

当前为原生适配开发分支；专项验证已覆盖安装、后台、hook 和记忆审批，完整跨平台回归仍在进行。

## 安装

在 CMD 中执行：

```bat
git clone https://gitee.com/StephenBo_China/nexarl_forge.git
cd nexarl_forge
git switch feat/windows-native
install.cmd
```

已有本地分支时，直接在仓库目录执行 `install.cmd`。Git Bash 可以执行 `bash install.sh`。两种入口均调用 Windows Python，不启动 WSL。需要 Claude Code hook 时追加 `--with-claude-hooks`。

安装副本放在 `%LOCALAPPDATA%\NexarlForge`，管理页面默认是 http://127.0.0.1:8897/ 。安装后可在首次设置中选择 hook、保留时间、端口及登录自启。

## 管理后台

CMD 中使用完整入口路径，不必修改 PATH：

```bat
"%LOCALAPPDATA%\NexarlForge\bin\nexarl-forge.cmd" status
"%LOCALAPPDATA%\NexarlForge\bin\nexarl-forge.cmd" open
"%LOCALAPPDATA%\NexarlForge\bin\nexarl-forge.cmd" stop
"%LOCALAPPDATA%\NexarlForge\bin\nexarl-forge.cmd" start
"%LOCALAPPDATA%\NexarlForge\bin\nexarl-forge.cmd" restart
"%LOCALAPPDATA%\NexarlForge\bin\nexarl-forge.cmd" doctor
```

后台由当前用户的独立 Python 进程运行。关闭安装终端不会停止后台；停止后执行 start 即可恢复。未启用登录自启时，重新登录 Windows 后需要手动 start。

登录自启是可选项：启用后在当前用户 Startup 文件夹写入受管的 `NexarlForge.cmd`，登录时运行 Python。不会创建计划任务或注册系统服务。若同名 Startup 文件不是本工具管理的文件，设置会拒绝覆盖。

Git Bash 可调用安装目录 `bin/nexarl-forge`，例如：

```bash
"$LOCALAPPDATA/NexarlForge/bin/nexarl-forge" status
```

## 记忆与项目

个人记忆保存在 Windows 用户目录 `.codex/personal_memory` 下；项目记忆仍保存在已注册项目的 `codex` 下。安装不会迁移或读取 WSL 中的历史记忆。保留候选审核机制，正式记忆的提升仍需明确审批。

```bat
"%LOCALAPPDATA%\NexarlForge\bin\nexarl-forge.cmd" project register --help
"%LOCALAPPDATA%\NexarlForge\bin\nexarl-forge.cmd" project init --help
```

先注册项目，再按项目命令帮助初始化。Codex、Claude Code 及业务项目自身的工具链仍需按需安装；它们不是后台启动所需的依赖。worktree 验证命令按 Windows 参数规则执行，不隐式启动 PowerShell；跨平台项目建议使用 Python 或 Git 命令。

## 更新与卸载

更新本地源码后重新运行 `install.cmd`。更新保存上一运行副本，`rollback` 切换到上一副本；`repair` 修复托管入口、hook 和登录自启设置，源码文件损坏时应从完整源码重新安装。

```bat
"%LOCALAPPDATA%\NexarlForge\bin\nexarl-forge.cmd" rollback
"%LOCALAPPDATA%\NexarlForge\bin\nexarl-forge.cmd" repair
"%LOCALAPPDATA%\NexarlForge\bin\nexarl-forge.cmd" uninstall
```

卸载停止后台、移除运行副本、托管入口及自启项，保留个人和项目记忆、配置及日志。不会删除自定义 hook。`doctor` 中某项失败时查看对应错误；服务日志位于 `%LOCALAPPDATA%\NexarlForge\logs\service.log`。
