#!/usr/bin/env python3
"""CLI for reviewing and approving memory candidates."""

from __future__ import annotations

import argparse
import json
import sys

import memory_review_queue as review
import ui_design_cli


def print_items(items: list[dict], status: str | None = None) -> None:
    for item in items:
        if status and item.get("status") != status:
            continue
        risks = ",".join(item.get("risk_flags", [])) or "-"
        print(
            f"{item['id']}\t{item.get('status')}\t{item.get('scope')}\t"
            f"{item.get('target')}\trisks={risks}\t{item.get('summary')}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Review shared memory candidates")
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list")
    list_parser.add_argument("--status", default="pending")

    show_parser = sub.add_parser("show")
    show_parser.add_argument("candidate_id")

    approve_parser = sub.add_parser("approve")
    approve_parser.add_argument("candidate_id")
    approve_parser.add_argument(
        "--target",
        choices=["project_long", "personal_long", "personal_short"],
        default=None,
    )
    approve_parser.add_argument("--content-file", default=None)

    reject_parser = sub.add_parser("reject")
    reject_parser.add_argument("candidate_id")

    defer_parser = sub.add_parser("defer")
    defer_parser.add_argument("candidate_id")

    reset_parser = sub.add_parser("reset")
    reset_parser.add_argument("candidate_id")

    propose_parser = sub.add_parser("propose", help="Write a candidate distilled by the active agent model")
    propose_parser.add_argument("--scope", choices=["personal", "project"], required=True)
    propose_parser.add_argument("--target", choices=["long", "short"], required=True)
    propose_parser.add_argument("--category", required=True)
    propose_parser.add_argument("--title", required=True)
    propose_parser.add_argument("--summary", required=True)
    propose_parser.add_argument("--source-event", default="agent_summary")
    propose_parser.add_argument(
        "--source-agent",
        choices=["codex", "claude-code", "unknown"],
        default="unknown",
    )
    propose_parser.add_argument("--policy-version", type=int, default=1)

    noise_parser = sub.add_parser(
        "reject-noise-personal",
        help="Quarantine and mark detected personal noise candidates as rejected",
    )
    noise_parser.add_argument("--apply", action="store_true", help="Actually quarantine and reject detected noise candidates")

    sub.add_parser("refresh")
    sub.add_parser("serve")

    ui_design_cli.register_parsers(sub)
    return parser


def main() -> int:
    parser = build_parser()

    args = parser.parse_args()

    if args.command in ui_design_cli.COMMANDS:
        print(json.dumps(ui_design_cli.dispatch(args), ensure_ascii=False, indent=2))
        return 0

    if args.command == "list":
        queue = review.load_queue(refresh=True)
        print_items(queue.get("items", []), args.status)
        return 0

    if args.command == "show":
        item = review.find_item(args.candidate_id)
        print(f"ID: {item['id']}")
        print(f"Status: {item.get('status')}")
        print(f"Scope: {item.get('scope')}")
        print(f"Target: {item.get('target')}")
        print(f"Created: {item.get('created_at')}")
        print(f"Risks: {', '.join(item.get('risk_flags', [])) or '-'}")
        print()
        print(item.get("content", ""))
        return 0

    if args.command == "approve":
        content = None
        if args.content_file:
            with open(args.content_file, "r", encoding="utf-8") as handle:
                content = handle.read()
        item = review.approve(args.candidate_id, target=args.target, content=content)
        print(f"approved {item['id']}")
        return 0

    if args.command == "reject":
        review.reject(args.candidate_id)
        print(f"rejected {args.candidate_id}")
        return 0

    if args.command == "defer":
        review.defer(args.candidate_id)
        print(f"deferred {args.candidate_id}")
        return 0

    if args.command == "reset":
        review.reset(args.candidate_id)
        print(f"reset {args.candidate_id}")
        return 0

    if args.command == "propose":
        value = review.create_agent_candidate(
            args.scope,
            args.target,
            args.category,
            args.title,
            args.summary,
            args.source_event,
            source_agent=args.source_agent,
            policy_version=args.policy_version,
        )
        print(json.dumps(value, ensure_ascii=False))
        return 0

    if args.command == "reject-noise-personal":
        ids = review.reject_noise_personal_candidates(dry_run=not args.apply)
        action = "would reject" if not args.apply else "rejected"
        print(f"{action} {len(ids)} personal noise candidates")
        for candidate_id in ids:
            print(candidate_id)
        return 0

    if args.command == "refresh":
        queue = review.build_queue()
        print(review.review_summary(queue))
        return 0

    if args.command == "serve":
        from memory_review_server import main as server_main

        return server_main()

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
