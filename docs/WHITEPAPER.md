# Nexarl Forge Whitepaper

## Abstract

Nexarl Forge is a local-first human–AI coding collaboration workspace. It gives Codex and Claude Code a shared, approval-driven control plane for memory, project context, design governance, UI Skills, and Loop engineering workflows while keeping user data on the developer's Mac.

## The problem

AI coding sessions repeatedly rediscover project decisions, personal working preferences, and release constraints. Unreviewed automatic memory can also capture sensitive or incorrect content. Nexarl Forge addresses both problems with explicit scope routing and human approval.

## Architecture

The installer places an immutable, versioned runtime under the user's Application Support directory and exposes a stable CLI. A loopback-only service provides the management console. Universal user hooks are shared by Codex and Claude Code; they send event metadata to the local control plane, which selects personal context and the deepest registered project context.

Registered working directories may produce project candidates. Unregistered directories produce personal candidates only. Promotion into official long- or short-term memory is approval-gated.

## Collaboration controls

The console manages current memories, projects, design preferences, UI design approval gates, UI Skills, Loop methodology, migration, repair, rollback, and uninstall. These controls make the human's decision the durable source of truth while allowing assistants to work continuously.

## Privacy and safety

The service binds to loopback, avoids third-party runtime dependencies, preserves unrelated hooks, and uses timestamped backups for managed changes. Release checks reject personal paths, credentials, and memory artifacts from the distributable tree.

## Roadmap

Future releases may add more human–AI collaboration capabilities while preserving local-first storage, approval gates, and compatibility for existing installations.

## License

Nexarl Forge is released under the MIT License.
