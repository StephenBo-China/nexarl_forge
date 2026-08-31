# Changelog

All notable changes to Nexarl Forge are documented here.

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
