# Changelog

All notable changes to Nexarl Forge are documented here.

## [Unreleased]

### Fixed

- 修复首次运行表单的 JavaScript 语法错误，恢复 JSON 保存和错误提示，并在重新配置时回填已保存选项。

### Added

- Linux / WSL 源码运行入口和 systemd 用户服务配置，补齐首次配置所需的运行时字段；关闭自动启动不停止当前后台，端口变化在响应后重启。
- 保存失败时保护并发修改的 hooks、项目注册表及记忆文件，并补充 Linux 回归测试与 CI。

## [1.0.0] - 2026-08-31

### Added

- macOS global installer with versioned runtime, LaunchAgent lifecycle, repair, rollback, migration, and uninstall safeguards.
- Shared Codex and Claude Code user hooks with registered-project routing and personal-only fallback for unregistered directories.
- Approval-gated personal and project long/short memory management.
- Memory review console covering current memories, project management, design preferences, UI design approval, UI Skills, Loop workflow, and memory policy.
- Local release verification, public-release hygiene checks, and bilingual user documentation.
- Primary `nexarl-forge` command.

### Compatibility

- The historical `vibe-memory` launcher remains available as a compatibility alias.
- Existing `VibeMemory` storage paths and `com.noema.vibe-memory` LaunchAgent identity are retained for safe upgrades.

### Known limits

- v1.0.0 supports macOS only and keeps the management service loopback-only.
- Personal long/short memory and project long memory remain approval-gated by design.
