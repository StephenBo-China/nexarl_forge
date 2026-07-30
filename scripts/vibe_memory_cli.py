#!/usr/bin/env python3
"""Fail-open command line entry point for shared memory hooks."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections.abc import Sequence

import vibe_memory_router as router


def hook_command(args: argparse.Namespace) -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        result = router.handle_event(
            args.agent,
            args.event,
            payload,
            pathlib.Path.cwd(),
        )
        print(json.dumps(result, ensure_ascii=False))
    except Exception:
        print(
            json.dumps(
                {"status": "degraded", "error": "钩子处理失败"},
                ensure_ascii=False,
            )
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Vibe Memory shared runtime CLI")
    subcommands = parser.add_subparsers(dest="command", required=True)
    hook = subcommands.add_parser("hook", help="Route a Codex or Claude Code hook")
    hook.add_argument("--agent", choices=("codex", "claude-code"), required=True)
    hook.add_argument("--event", required=True)
    hook.set_defaults(command_handler=hook_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.command_handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
