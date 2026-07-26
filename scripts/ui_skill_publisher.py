"""Two-target transactional publication for managed UI skills."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import shutil
import uuid
from collections.abc import Callable
from typing import Any

import ui_design_store as store
import ui_skill_registry as registry


DEFAULT_TARGETS = {
    "codex": lambda: pathlib.Path(
        os.environ.get("CODEX_UI_SKILLS_DIR", pathlib.Path.home() / ".codex" / "skills")
    ),
    "claude": lambda: pathlib.Path(
        os.environ.get("CLAUDE_UI_SKILLS_DIR", pathlib.Path.home() / ".claude" / "skills")
    ),
}


class PublishError(RuntimeError):
    pass


class TargetDigestConflict(PublishError):
    pass


class IdempotencyConflict(PublishError):
    pass


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def resolve_targets(
    scope: dict[str, Any], project_root: pathlib.Path | None = None
) -> dict[str, pathlib.Path]:
    scope_type = scope.get("type")
    if scope_type == "global":
        return {name: resolver().expanduser() for name, resolver in DEFAULT_TARGETS.items()}
    if scope_type == "project":
        if project_root is None:
            raise PublishError("project scope requires project_root")
        root = pathlib.Path(project_root).expanduser()
        return {"codex": root / ".agents/skills", "claude": root / ".claude/skills"}
    raise PublishError(f"unsupported publication scope: {scope}")


def _ordered_agents(targets: dict[str, pathlib.Path]) -> list[str]:
    preferred = [name for name in ("codex", "claude") if name in targets]
    return preferred + sorted(set(targets) - set(preferred))


def _fingerprint(
    operation: str,
    name: str,
    digest: str | None,
    targets: dict[str, pathlib.Path],
    expected: dict[str, str | None],
    desired: dict[str, str | None],
) -> str:
    payload = {
        "operation": operation,
        "name": name,
        "digest": digest,
        "targets": {key: str(value) for key, value in sorted(targets.items())},
        "expected": expected,
        "desired": desired,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _validate_targets(name: str, targets: dict[str, pathlib.Path]) -> dict[str, pathlib.Path]:
    if not targets:
        raise PublishError("at least one publication target is required")
    normalized = {agent: pathlib.Path(path) for agent, path in targets.items()}
    for agent, path in normalized.items():
        if agent not in {"codex", "claude"}:
            raise PublishError(f"unsupported publication target: {agent}")
        if path.name != name:
            raise PublishError(f"target must be the final {name} skill directory: {path}")
    return normalized


def _current_digest(path: pathlib.Path) -> str | None:
    return registry.package_digest(path) if path.is_dir() else None


def _package_variants(
    package_path: pathlib.Path,
    digest: str,
    agents: set[str],
    variants: dict[str, Any] | None,
) -> tuple[dict[str, pathlib.Path], dict[str, str]]:
    package_path = pathlib.Path(package_path)
    if not variants:
        return (
            {agent: package_path for agent in agents},
            {agent: digest for agent in agents},
        )
    roots: dict[str, pathlib.Path] = {}
    digests: dict[str, str] = {}
    package_resolved = package_path.resolve()
    for agent in agents:
        descriptor = variants.get(agent) or variants.get("common")
        if not isinstance(descriptor, dict):
            raise PublishError(f"approved package has no variant for {agent}")
        relative = pathlib.PurePosixPath(str(descriptor.get("path", "")))
        if relative.is_absolute() or ".." in relative.parts:
            raise PublishError(f"unsafe approved variant path for {agent}")
        root = package_path.joinpath(*relative.parts).resolve()
        if not root.is_relative_to(package_resolved):
            raise PublishError(f"approved variant escapes package for {agent}")
        variant_digest = descriptor.get("digest")
        if not isinstance(variant_digest, str) or len(variant_digest) != 64:
            raise PublishError(f"approved variant digest is invalid for {agent}")
        roots[agent] = root
        digests[agent] = variant_digest
    return roots, digests


def _transition_if_registered(
    approved: dict[str, Any], status: str, details: dict[str, Any]
) -> None:
    draft_id = approved.get("id")
    if not draft_id:
        return
    try:
        registry.transition_draft(
            draft_id,
            status,
            expected_digest=approved.get("digest"),
            details=details,
        )
    except registry.DraftNotFound:
        return


def _complete_if_registered(
    approved: dict[str, Any],
    report: dict[str, Any],
    *,
    idempotency_key: str,
    fingerprint: str,
) -> bool:
    draft_id = approved.get("id")
    if not draft_id:
        return False
    try:
        registry.complete_publication(
            draft_id,
            report,
            expected_digest=approved["digest"],
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
        )
    except registry.DraftNotFound:
        return False
    return True


def _report_path(transaction_id: str) -> pathlib.Path:
    return store.ui_design_home() / "deployments" / f"{transaction_id}.json"


def _run_transaction(
    *,
    operation: str,
    name: str,
    package_path: pathlib.Path | None,
    digest: str | None,
    targets: dict[str, pathlib.Path],
    expected_target_digests: dict[str, str | None],
    idempotency_key: str,
    replace: Callable[[pathlib.Path, pathlib.Path], None],
    approved: dict[str, Any] | None = None,
    variants: dict[str, Any] | None = None,
    record_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not idempotency_key:
        raise PublishError("idempotency_key is required")
    targets = _validate_targets(name, targets)
    package_paths: dict[str, pathlib.Path] = {}
    desired_target_digests: dict[str, str | None] = {
        agent: None for agent in targets
    }
    if package_path is not None:
        package_paths, resolved_digests = _package_variants(
            pathlib.Path(package_path), str(digest), set(targets), variants
        )
        desired_target_digests.update(resolved_digests)
    fingerprint = _fingerprint(
        operation,
        name,
        digest,
        targets,
        expected_target_digests,
        desired_target_digests,
    )
    try:
        prior = registry.idempotency_result(idempotency_key, fingerprint)
    except registry.RegistryError as error:
        raise IdempotencyConflict(str(error)) from error
    if prior is not None:
        return prior
    if package_path is not None:
        package_path = pathlib.Path(package_path)
        if not package_path.is_dir() or registry.package_digest(package_path) != digest:
            raise PublishError("approved package is missing or its digest changed")
        for agent, source in package_paths.items():
            if (
                not source.is_dir()
                or registry.package_digest(source) != desired_target_digests[agent]
            ):
                raise PublishError(f"approved package variant changed for {agent}")

    transaction_id = f"deploy-{uuid.uuid4().hex}"
    stages: dict[str, pathlib.Path] = {}
    backups: dict[str, pathlib.Path] = {}
    previous: dict[str, str | None] = {}
    installed: set[str] = set()
    order = _ordered_agents(targets)
    lock_path = store.ui_design_home() / "deployments.lock"
    with store.exclusive_lock(lock_path, timeout=30):
        try:
            for agent in order:
                target = targets[agent]
                target.parent.mkdir(parents=True, exist_ok=True)
                current = _current_digest(target)
                previous[agent] = current
                if agent not in expected_target_digests:
                    if current is not None:
                        raise TargetDigestConflict(
                            f"unmanaged or unrecorded target exists for {agent}: {target}"
                        )
                elif current != expected_target_digests[agent]:
                    raise TargetDigestConflict(
                        f"target digest changed for {agent}: expected "
                        f"{expected_target_digests[agent]}, current {current}"
                    )
                if package_path is not None:
                    stage = target.with_name(f".{target.name}.stage-{transaction_id}")
                    shutil.copytree(package_paths[agent], stage)
                    if registry.package_digest(stage) != desired_target_digests[agent]:
                        raise PublishError(f"staged digest mismatch for {agent}")
                    stages[agent] = stage

            if operation == "publish" and approved is not None:
                _transition_if_registered(
                    approved, "publishing", {"transaction_id": transaction_id}
                )

            for agent in order:
                target = targets[agent]
                if target.exists():
                    backup = target.with_name(f".{target.name}.backup-{transaction_id}")
                    os.replace(target, backup)
                    backups[agent] = backup
                    if registry.package_digest(backup) != previous[agent]:
                        raise TargetDigestConflict(
                            f"target changed during publication for {agent}"
                        )
                if package_path is not None:
                    replace(stages[agent], target)
                    installed.add(agent)

            target_digests = {
                agent: _current_digest(target) for agent, target in targets.items()
            }
            if any(
                value != desired_target_digests[agent]
                for agent, value in target_digests.items()
            ):
                raise PublishError("one or more published targets failed digest verification")
            report = {
                "transaction_id": transaction_id,
                "operation": operation,
                "name": name,
                "status": "disabled" if operation == "disable" else "published",
                "digest": digest,
                "previous_target_digests": previous,
                "target_digests": target_digests,
                "targets": {agent: str(path) for agent, path in targets.items()},
                "at": _now(),
            }
            if record_metadata is not None:
                report["scope"] = record_metadata.get("scope", {})
                report["version_id"] = record_metadata.get("version_id", "")
            store.atomic_write_json(_report_path(transaction_id), report)
            completed = False
            if operation == "publish" and approved is not None:
                completed = _complete_if_registered(
                    approved,
                    report,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                )
            if not completed:
                registry.record_deployment(
                    report,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                )
        except Exception as error:
            recovery_errors: list[str] = []
            for agent in reversed(order):
                target = targets[agent]
                try:
                    backup = backups.get(agent)
                    if backup is not None:
                        if target.exists():
                            shutil.rmtree(target)
                    elif agent in installed and target.exists():
                        shutil.rmtree(target)
                    if backup is not None and backup.exists():
                        os.replace(backup, target)
                except OSError as recovery_error:
                    recovery_errors.append(f"{agent}: {recovery_error}")
            for stage in stages.values():
                if stage.exists():
                    shutil.rmtree(stage, ignore_errors=True)
            report = {
                "transaction_id": transaction_id,
                "operation": operation,
                "name": name,
                "status": "publish_failed",
                "error": str(error),
                "recovery_errors": recovery_errors,
                "previous_target_digests": previous,
                "at": _now(),
            }
            store.atomic_write_json(_report_path(transaction_id), report)
            if operation == "publish" and approved is not None:
                try:
                    _transition_if_registered(approved, "publish_failed", report)
                except registry.InvalidTransition:
                    pass
            if isinstance(error, TargetDigestConflict):
                raise
            raise PublishError(str(error)) from error

        for backup in backups.values():
            if backup.exists():
                shutil.rmtree(backup)
        return report


def publish(
    approved: dict[str, Any],
    *,
    targets: dict[str, pathlib.Path],
    idempotency_key: str,
    replace: Callable[[pathlib.Path, pathlib.Path], None] = os.replace,
) -> dict[str, Any]:
    return _run_transaction(
        operation="publish",
        name=approved["name"],
        package_path=pathlib.Path(approved["package_path"]),
        digest=approved["digest"],
        targets=targets,
        expected_target_digests=approved.get("previous_target_digests", {}),
        idempotency_key=idempotency_key,
        replace=replace,
        approved=approved,
        variants=approved.get("source", {}).get("variants"),
        record_metadata=approved,
    )


def rollback(
    approved_version: dict[str, Any],
    *,
    targets: dict[str, pathlib.Path],
    expected_target_digests: dict[str, str | None],
    idempotency_key: str,
) -> dict[str, Any]:
    return _run_transaction(
        operation="rollback",
        name=approved_version["name"],
        package_path=pathlib.Path(approved_version["package_path"]),
        digest=approved_version["digest"],
        targets=targets,
        expected_target_digests=expected_target_digests,
        idempotency_key=idempotency_key,
        replace=os.replace,
        variants=approved_version.get("source", {}).get("variants"),
        record_metadata=approved_version,
    )


def disable(
    *,
    name: str,
    targets: dict[str, pathlib.Path],
    expected_target_digests: dict[str, str | None],
    idempotency_key: str,
) -> dict[str, Any]:
    return _run_transaction(
        operation="disable",
        name=name,
        package_path=None,
        digest=None,
        targets=targets,
        expected_target_digests=expected_target_digests,
        idempotency_key=idempotency_key,
        replace=os.replace,
    )
