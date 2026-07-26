"""CLI parser and shared command dispatch for UI design control operations."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import tempfile
import uuid
from collections.abc import Callable
from typing import Any

import ui_design_preferences as preferences
import ui_design_store as store
import ui_skill_discovery as discovery
import ui_skill_publisher as publisher
import ui_skill_registry as registry
import ui_skill_sources as sources
import ui_skill_validator as validator


COMMANDS = {"ui-skill", "ui-design"}
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _idempotency_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--idempotency-key", required=True)


def register_parsers(sub: argparse._SubParsersAction) -> None:
    skill = sub.add_parser("ui-skill", help="Manage UI skill drafts and deployments")
    skill_sub = skill.add_subparsers(dest="ui_skill_command", required=True)
    skill_sub.add_parser("list")
    show = skill_sub.add_parser("show")
    show.add_argument("draft_id")

    import_parser = skill_sub.add_parser("import")
    source_group = import_parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--github")
    source_group.add_argument("--local")
    source_group.add_argument("--zip")
    source_group.add_argument("--editor-json")
    import_parser.add_argument("--path")
    import_parser.add_argument("--revision")
    import_parser.add_argument("--scope", choices=["global", "project"], required=True)
    import_parser.add_argument("--project")
    import_parser.add_argument("--targets", default="codex,claude")
    import_parser.add_argument("--version-label", default="1.0.0")
    _idempotency_argument(import_parser)

    bootstrap = skill_sub.add_parser("bootstrap")
    bootstrap.add_argument(
        "bootstrap_name",
        choices=["frontend-design", "ui-ux-pro-max", "ui-design-workflow"],
    )
    bootstrap.add_argument("--revision")
    bootstrap.add_argument("--release")
    bootstrap.add_argument("--cli-version")
    bootstrap.add_argument("--expected-npm-shasum")
    bootstrap.add_argument("--scope", choices=["global", "project"], default="global")
    bootstrap.add_argument("--project")
    bootstrap.add_argument("--targets", default="codex,claude")
    _idempotency_argument(bootstrap)

    validate = skill_sub.add_parser("validate")
    validate.add_argument("draft_id")
    _idempotency_argument(validate)
    revise = skill_sub.add_parser("request-revision")
    revise.add_argument("draft_id")
    revise.add_argument("--reason", required=True)
    _idempotency_argument(revise)
    approve = skill_sub.add_parser("approve")
    approve.add_argument("draft_id")
    approve.add_argument("--digest", required=True)
    _idempotency_argument(approve)
    reject = skill_sub.add_parser("reject")
    reject.add_argument("draft_id")
    reject.add_argument("--reason", default="")
    _idempotency_argument(reject)
    publish_parser = skill_sub.add_parser("publish")
    publish_parser.add_argument("draft_id")
    publish_parser.add_argument("--digest", required=True)
    publish_parser.add_argument("--project")
    _idempotency_argument(publish_parser)
    rollback = skill_sub.add_parser("rollback")
    rollback.add_argument("name")
    rollback.add_argument("--version", required=True)
    rollback.add_argument("--project")
    _idempotency_argument(rollback)
    disable = skill_sub.add_parser("disable")
    disable.add_argument("name")
    disable.add_argument("--project")
    _idempotency_argument(disable)
    scan = skill_sub.add_parser("scan")
    scan.add_argument("--project")
    scan.add_argument("--idempotency-key")
    ignore = skill_sub.add_parser("ignore-unmanaged")
    ignore.add_argument("digest")
    _idempotency_argument(ignore)

    design = sub.add_parser("ui-design", help="Manage UI design preferences and gates")
    design_sub = design.add_subparsers(dest="ui_design_command", required=True)
    preference_parser = design_sub.add_parser("preferences")
    preference_sub = preference_parser.add_subparsers(
        dest="preference_command", required=True
    )
    show_preferences = preference_sub.add_parser("show")
    show_preferences.add_argument("--project", required=True)
    set_global = preference_sub.add_parser("set-global")
    set_global.add_argument("--json-file", required=True)
    _idempotency_argument(set_global)
    set_project = preference_sub.add_parser("set-project")
    set_project.add_argument("--project", required=True)
    set_project.add_argument("--json-file", required=True)
    _idempotency_argument(set_project)


def _fingerprint(operation: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        {"operation": operation, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _idempotent(
    key: str,
    operation: str,
    payload: dict[str, Any],
    callback: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    fingerprint = _fingerprint(operation, payload)
    prior = registry.idempotency_result(key, fingerprint)
    if prior is not None:
        return prior
    result = callback()
    registry.record_idempotent_result(
        result, idempotency_key=key, fingerprint=fingerprint
    )
    return result


def _read_json(path: str) -> Any:
    return store.read_json_strict(pathlib.Path(path))


def _targets(value: str) -> list[str]:
    targets = sorted({item.strip() for item in value.split(",") if item.strip()})
    if not targets or set(targets) - {"codex", "claude"}:
        raise ValueError(f"invalid targets: {value}")
    return targets


def _scope(scope_type: str, project: str | None) -> dict[str, Any]:
    if scope_type == "project":
        if not project:
            raise ValueError("project scope requires --project")
        return {"type": "project", "root": str(pathlib.Path(project).expanduser())}
    return {"type": "global"}


def _import_skill(args: argparse.Namespace) -> dict[str, Any]:
    payload = {
        "github": args.github,
        "local": args.local,
        "zip": args.zip,
        "editor_json": args.editor_json,
        "path": args.path,
        "revision": args.revision,
        "scope": args.scope,
        "project": args.project,
        "targets": args.targets,
        "version_label": args.version_label,
    }

    def operation() -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="ui-skill-cli-import-") as value:
            imported_root = pathlib.Path(value) / "package"
            if args.github:
                if not args.path or not args.revision:
                    raise ValueError("GitHub import requires --path and --revision")
                source = sources.import_github(
                    args.github, args.path, args.revision, imported_root
                )
            elif args.local:
                source = sources.import_local(pathlib.Path(args.local), imported_root)
            elif args.zip:
                source = sources.import_zip(pathlib.Path(args.zip), imported_root)
            else:
                files = _read_json(args.editor_json)
                if not isinstance(files, dict):
                    raise ValueError("editor JSON must be an object of path to content")
                source = sources.import_editor(files, imported_root)
            installed = set(registry.load_registry()["packages"])
            report = validator.validate_package(
                imported_root, installed_names=installed
            )
            if not report["valid"]:
                raise ValueError(json.dumps(report, ensure_ascii=False))
            draft = registry.create_draft(
                name=report["name"],
                source=source,
                package_root=imported_root,
                scope=_scope(args.scope, args.project),
                targets=_targets(args.targets),
                version_label=args.version_label,
            )
            return registry.set_validation_report(
                draft["id"], report, expected_digest=draft["digest"]
            )

    return _idempotent(args.idempotency_key, "ui-skill.import", payload, operation)


def _validate_bundle(
    root: pathlib.Path, source: dict[str, Any]
) -> dict[str, Any]:
    installed = set(registry.load_registry()["packages"])
    report = validator.validate_package(root, installed_names=installed)
    variant_reports: dict[str, dict[str, Any]] = {}
    for agent, descriptor in source.get("variants", {}).items():
        if not isinstance(descriptor, dict):
            raise ValueError(f"invalid variant metadata for {agent}")
        relative = pathlib.PurePosixPath(str(descriptor.get("path", "")))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe variant path for {agent}")
        variant_root = root.joinpath(*relative.parts)
        variant_report = validator.validate_package(
            variant_root, installed_names=installed
        )
        if variant_report.get("digest") != descriptor.get("digest"):
            variant_report.setdefault("errors", []).append(
                {
                    "code": "variant_digest_mismatch",
                    "message": f"variant digest mismatch for {agent}",
                }
            )
            variant_report["valid"] = False
        variant_reports[agent] = variant_report
    report["variant_reports"] = variant_reports
    report["valid"] = report.get("valid") is True and all(
        item.get("valid") is True for item in variant_reports.values()
    )
    return report


def _bootstrap_skill(args: argparse.Namespace) -> dict[str, Any]:
    payload = {
        "name": args.bootstrap_name,
        "revision": args.revision,
        "release": args.release,
        "cli_version": args.cli_version,
        "expected_npm_shasum": args.expected_npm_shasum,
        "scope": args.scope,
        "project": args.project,
        "targets": args.targets,
    }

    def operation() -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="ui-skill-bootstrap-") as value:
            imported_root = pathlib.Path(value) / "package"
            if args.bootstrap_name == "frontend-design":
                revision = args.revision or sources.FRONTEND_DESIGN_REVISION
                source = sources.bootstrap_frontend_design(
                    imported_root, revision=revision
                )
                version_label = revision[:12]
            elif args.bootstrap_name == "ui-ux-pro-max":
                source = sources.bootstrap_ui_ux_pro_max(
                    imported_root,
                    release=args.release or sources.UI_UX_PRO_MAX_RELEASE,
                    revision=args.revision or sources.UI_UX_PRO_MAX_REVISION,
                    cli_version=args.cli_version or sources.UI_UX_PRO_MAX_CLI_VERSION,
                    expected_npm_shasum=(
                        args.expected_npm_shasum
                        or sources.UI_UX_PRO_MAX_NPM_SHASUM
                    ),
                )
                version_label = source["cli_version"]
            else:
                template = PROJECT_ROOT / "templates/ui_design/skills/ui-design-workflow"
                source = sources.import_local(template, imported_root)
                source.update(
                    {
                        "type": "bootstrap",
                        "source_type": "manager_template",
                        "variants": {
                            "common": {
                                "path": ".",
                                "digest": registry.package_digest(imported_root),
                            }
                        },
                    }
                )
                version_label = "1.0.0"

            report = _validate_bundle(imported_root, source)
            if not report["valid"]:
                raise ValueError(json.dumps(report, ensure_ascii=False))
            draft = registry.create_draft(
                name=report["name"],
                source=source,
                package_root=imported_root,
                scope=_scope(args.scope, args.project),
                targets=_targets(args.targets),
                version_label=version_label,
            )
            return registry.set_validation_report(
                draft["id"], report, expected_digest=draft["digest"]
            )

    return _idempotent(
        args.idempotency_key,
        f"ui-skill.bootstrap.{args.bootstrap_name}",
        payload,
        operation,
    )


def _show_draft(draft_id: str) -> dict[str, Any]:
    draft = registry.get_draft(draft_id)
    skill_path = pathlib.Path(draft["draft_path"]) / "content" / "SKILL.md"
    draft["skill_md"] = skill_path.read_text(encoding="utf-8")
    return draft


def _validate_draft(args: argparse.Namespace) -> dict[str, Any]:
    def operation() -> dict[str, Any]:
        draft = registry.get_draft(args.draft_id)
        content = pathlib.Path(draft["draft_path"]) / "content"
        installed = set(registry.load_registry()["packages"])
        report = validator.validate_package(content, installed_names=installed)
        return registry.set_validation_report(
            args.draft_id, report, expected_digest=draft["digest"]
        )

    return _idempotent(
        args.idempotency_key,
        "ui-skill.validate",
        {"draft_id": args.draft_id},
        operation,
    )


def _destination_paths(record: dict[str, Any], project: str | None) -> dict[str, pathlib.Path]:
    configured_root = record.get("scope", {}).get("root")
    project_root = pathlib.Path(project or configured_root) if (project or configured_root) else None
    bases = publisher.resolve_targets(record["scope"], project_root=project_root)
    return {agent: bases[agent] / record["name"] for agent in record["targets"]}


def _previous_digests(
    name: str, targets: dict[str, pathlib.Path]
) -> dict[str, str | None]:
    latest = registry.latest_deployment(name)
    if latest is None:
        return {}
    recorded_targets = latest.get("targets", {})
    if {
        agent: str(path) for agent, path in targets.items()
    } != recorded_targets:
        return {}
    return dict(latest.get("target_digests", {}))


def _scan(args: argparse.Namespace) -> dict[str, Any]:
    if args.project:
        bases = publisher.resolve_targets(
            {"type": "project"}, project_root=pathlib.Path(args.project)
        )
    else:
        bases = publisher.resolve_targets({"type": "global"})
    target_roots = {agent: [root] for agent, root in bases.items()}
    managed_targets: dict[str, dict[str, str]] = {}
    for report in registry.load_registry()["deployments"].values():
        if report.get("status") != "published":
            continue
        for agent, digest in report.get("target_digests", {}).items():
            if digest:
                managed_targets.setdefault(agent, {})[report["name"]] = digest
    return {"items": discovery.scan_and_persist(target_roots, {"targets": managed_targets})}


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "ui-skill":
        command = args.ui_skill_command
        if command == "list":
            return {"items": registry.list_drafts()}
        if command == "show":
            return _show_draft(args.draft_id)
        if command == "import":
            return _import_skill(args)
        if command == "bootstrap":
            return _bootstrap_skill(args)
        if command == "validate":
            return _validate_draft(args)
        if command == "request-revision":
            return _idempotent(
                args.idempotency_key,
                "ui-skill.request-revision",
                {"draft_id": args.draft_id, "reason": args.reason},
                lambda: registry.request_revision(args.draft_id, args.reason),
            )
        if command == "approve":
            return _idempotent(
                args.idempotency_key,
                "ui-skill.approve",
                {"draft_id": args.draft_id, "digest": args.digest},
                lambda: registry.approve_draft(
                    args.draft_id, expected_digest=args.digest
                ),
            )
        if command == "reject":
            return _idempotent(
                args.idempotency_key,
                "ui-skill.reject",
                {"draft_id": args.draft_id, "reason": args.reason},
                lambda: registry.reject_draft(args.draft_id, args.reason),
            )
        if command == "publish":
            approved = registry.get_draft(args.draft_id)
            if approved.get("digest") != args.digest:
                raise registry.DigestConflict("approval digest does not match draft")
            targets = _destination_paths(approved, args.project)
            approved["previous_target_digests"] = _previous_digests(
                approved["name"], targets
            )
            return publisher.publish(
                approved,
                targets=targets,
                idempotency_key=args.idempotency_key,
            )
        if command == "rollback":
            version = registry.get_version(args.name, args.version)
            latest = registry.latest_deployment(args.name)
            if latest is None:
                raise ValueError(f"skill has no deployment: {args.name}")
            targets = {
                agent: pathlib.Path(path)
                for agent, path in latest.get("targets", {}).items()
            }
            return publisher.rollback(
                version,
                targets=targets,
                expected_target_digests=dict(latest["target_digests"]),
                idempotency_key=args.idempotency_key,
            )
        if command == "disable":
            latest = registry.latest_deployment(args.name)
            if latest is None:
                raise ValueError(f"skill has no deployment: {args.name}")
            targets = {
                agent: pathlib.Path(path)
                for agent, path in latest.get("targets", {}).items()
            }
            return publisher.disable(
                name=args.name,
                targets=targets,
                expected_target_digests=dict(latest["target_digests"]),
                idempotency_key=args.idempotency_key,
            )
        if command == "scan":
            if args.idempotency_key:
                return _idempotent(
                    args.idempotency_key,
                    "ui-skill.scan",
                    {"project": args.project},
                    lambda: _scan(args),
                )
            return _scan(args)
        if command == "ignore-unmanaged":
            return _idempotent(
                args.idempotency_key,
                "ui-skill.ignore-unmanaged",
                {"digest": args.digest},
                lambda: discovery.ignore_fingerprint(args.digest),
            )

    if args.command == "ui-design" and args.ui_design_command == "preferences":
        if args.preference_command == "show":
            project = pathlib.Path(args.project)
            return {
                "global": preferences.load_global_preferences(),
                "project": preferences.load_project_overrides(project),
                "effective": preferences.effective_preferences(project),
            }
        value = _read_json(args.json_file)
        if args.preference_command == "set-global":
            return _idempotent(
                args.idempotency_key,
                "ui-design.preferences.set-global",
                {"value": value},
                lambda: _save_global(value),
            )
        if args.preference_command == "set-project":
            if isinstance(value, dict) and "overrides" in value:
                value = value["overrides"]
            return _idempotent(
                args.idempotency_key,
                "ui-design.preferences.set-project",
                {"project": args.project, "value": value},
                lambda: _save_project(pathlib.Path(args.project), value),
            )
    raise ValueError("unsupported UI design command")


def _save_global(value: dict[str, Any]) -> dict[str, Any]:
    path = preferences.save_global_preferences(value)
    return {"status": "saved", "path": str(path)}


def _save_project(
    project: pathlib.Path, value: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    path = preferences.save_project_overrides(project, value)
    return {"status": "saved", "path": str(path)}
