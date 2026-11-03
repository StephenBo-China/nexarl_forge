# Linux / WSL 源码运行与首次配置

本入口用于有 systemd 用户会话的 Linux 或 WSL2 环境，要求 Python 3.10+。
它补充源码运行和网页首次设置，不改变官方 macOS 安装器的支持范围。

## 从源码启动

在 Linux / WSL 终端进入仓库根目录。先确认 `systemctl --user` 可以连接用户服务管理器：

```bash
systemctl --user is-system-running
python3 scripts/nexarl_linux_service.py
```

浏览器打开 `http://127.0.0.1:8897/`，按需要选择 Codex / Claude Code hooks、
短期记忆保留时间和自动启动。工作区可留空；填写时必须是 Linux 可访问的已存在目录。
WSL 下的 Windows 磁盘路径通常形如 `/mnt/d/projects/example`，不能直接填写 `D:\projects\example`。

保存会补齐运行配置、生成 WSL/Linux 的 CLI launcher、保留并合并已有第三方 hooks，
并写入 `~/.config/systemd/user/nexarl-forge.service`。正式记忆仍要求用户审批。
首次配置不会创建项目记忆，注册和初始化仍是两个独立操作。

首次手动启动默认使用 8897。建议第一次保存时保留此端口，然后在终端用 Ctrl+C
结束前台服务，再启动 systemd 服务：

```bash
systemctl --user start nexarl-forge.service
systemctl --user status nexarl-forge.service
```

后台入口保持 `http://127.0.0.1:8897/`。运行时依赖源码目录，不能在运行中移动或删除仓库。

## 后续使用

```bash
systemctl --user restart nexarl-forge.service
systemctl --user stop nexarl-forge.service
```

访问 `http://127.0.0.1:8897/?first-run=1` 可重新打开配置页。

- 开启自动启动：使用 `systemctl --user enable`，在后续用户服务会话启动时运行。
- 关闭自动启动：只 disable，不中止当前后台。
- 修改端口：systemd 管理的服务会在保存响应发送后重新启动，页面跳转到新端口。
- 自动启动不表示 Windows 开机后主动启动 WSL，也不会配置 linger 或 Windows 计划任务。
- 若 systemd 用户会话不可用，保存会报错并回滚本次写入；请先解决服务管理器配置。
- hooks 安装到运行该服务的 Linux 用户目录，只接入同环境的 Codex / Claude Code。
  Windows Codex 需要另行配置 Windows hooks 或桥接；本改动不修改 Windows 客户端配置。
- 官方 macOS `install`、`update`、`rollback`、`start`、`open` 和完整 `doctor` 流程没有在此入口中整体移植。
  Linux 请使用上述 systemctl 命令管理后台。

## 验证范围

新增测试覆盖完整运行配置生成、保留其他 hooks、自动启动开关、失败回滚、
端口冲突、第三方 launcher / unit 保护和网页脚本语法。

WSL/Linux 运行原有测试时，可将临时目录放在用户 home 中，避免旧 macOS 安装器
把 `/tmp` 按 macOS 别名解析为 `/private/tmp`：

```bash
mkdir -p "$HOME/.cache/nexarl-test-tmp"
TMPDIR="$HOME/.cache/nexarl-test-tmp" python3 -m unittest discover -s tests -q
```

macOS 安装端到端验证仍需在 macOS 上执行，不应用 Linux 的网页检查替代。