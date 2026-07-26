"""Immutable draft and package registry for managed UI skills."""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import uuid
from typing import Any

import ui_design_store as store


DRAFT_TRANSITIONS = {
    "draft": {"validated", "rejected"},
    "validated": {"approved", "rejected", "draft"},
    "approved": {"publishing", "rejected"},
    "publishing": {"published", "publish_failed"},
    "publish_failed": {"publishing", "rejected"},
    "published": {"disabled", "superseded"},
}

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
TARGETS = {"codex", "claude"}


class RegistryError(RuntimeError):
    pass


class InvalidTransition(RegistryError):
    pass


class DigestConflict(RegistryError):
    pass


class DraftNotFound(RegistryError):
    pass


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def registry_path() -> pathlib.Path:
    return store.ui_design_home() / "registry.json"


def audit_path() -> pathlib.Path:
    return store.ui_design_home() / "audit.jsonl"


def registry_lock_path() -> pathlib.Path:
    return store.ui_design_home() / "registry.lock"


def _empty_registry() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "drafts": {},
        "packages": {},
        "deployments": {},
        "idempotency": {},
    }


def load_registry() -> dict[str, Any]:
    path = registry_path()
    if not path.exists():
        return _empty_registry()
    value = store.read_json_strict(path)
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise RegistryError(f"invalid UI skill registry: {path}")
    for key in ("drafts", "packages", "deployments", "idempotency"):
        if not isinstance(value.get(key), dict):
            raise RegistryError(f"invalid UI skill registry field {key}: {path}")
    return value


def package_digest(root: pathlib.Path) -> str:
    return store.tree_digest(pathlib.Path(root))


def _copy_package(source: pathlib.Path, destination: pathlib.Path) -> None:
    source = pathlib.Path(source)
    if not source.is_dir():
        raise RegistryError(f"skill package is not a directory: {source}")
    symlink = next((path for path in source.rglob("*") if path.is_symlink()), None)
    if symlink is not None:
        raise RegistryError(f"skill package contains a symlink: {symlink}")
    shutil.copytree(source, destination)


def _write_registry(value: dict[str, Any]) -> None:
    path = registry_path()
    store.atomic_write_json(path, value, backup=path.exists())


def _audit(event: str, record: dict[str, Any]) -> None:
    store.append_jsonl(
        audit_path(),
        {
            "at": _now(),
            "event": event,
            "draft_id": record.get("id"),
            "digest": record.get("digest"),
            "name": record.get("name"),
            "status": record.get("status"),
        },
    )


def create_draft(
    *,
    name: str,
    source: dict[str, Any],
    package_root: pathlib.Path,
    scope: dict[str, Any],
    targets: list[str],
    version_label: str = "1.0.0",
) -> dict[str, Any]:
    if not NAME_PATTERN.fullmatch(name):
        raise RegistryError(f"invalid skill name: {name}")
    normalized_targets = sorted(set(targets))
    if not normalized_targets or set(normalized_targets) - TARGETS:
        raise RegistryError(f"invalid skill targets: {targets}")
    if scope.get("type") not in {"global", "project"}:
        raise RegistryError(f"invalid skill scope: {scope}")
    draft_id = f"draft-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:10]}"
    draft_root = store.ui_design_home() / "drafts" / draft_id
    content_root = draft_root / "content"
    with store.exclusive_lock(registry_lock_path()):
        _copy_package(pathlib.Path(package_root), content_root)
        digest = package_digest(content_root)
        record = {
            "id": draft_id,
            "name": name,
            "source": copy.deepcopy(source),
            "scope": copy.deepcopy(scope),
            "targets": normalized_targets,
            "version_label": version_label,
            "digest": digest,
            "draft_path": str(draft_root),
            "status": "validated",
            "validation_report": {"valid": True, "stage": "basic_ingestion"},
            "created_at": _now(),
            "updated_at": _now(),
        }
        value = load_registry()
        value["drafts"][draft_id] = record
        _write_registry(value)
    _audit("draft_created", record)
    return copy.deepcopy(record)


def get_draft(draft_id: str) -> dict[str, Any]:
    record = load_registry()["drafts"].get(draft_id)
    if not isinstance(record, dict):
        raise DraftNotFound(draft_id)
    return copy.deepcopy(record)


def list_drafts() -> list[dict[str, Any]]:
    records = load_registry()["drafts"].values()
    return sorted((copy.deepcopy(item) for item in records), key=lambda item: item["created_at"])


def approve_draft(draft_id: str, *, expected_digest: str) -> dict[str, Any]:
    with store.exclusive_lock(registry_lock_path()):
        value = load_registry()
        record = value["drafts"].get(draft_id)
        if not isinstance(record, dict):
            raise DraftNotFound(draft_id)
        if "approved" not in DRAFT_TRANSITIONS.get(record.get("status"), set()):
            raise InvalidTransition(f"cannot approve draft from {record.get('status')}")
        content_root = pathlib.Path(record["draft_path"]) / "content"
        current_digest = package_digest(content_root)
        if expected_digest != record.get("digest") or current_digest != expected_digest:
            raise DigestConflict(
                f"draft digest changed: expected {expected_digest}, current {current_digest}"
            )
        version_id = f"{record['version_label']}+{current_digest[:12]}"
        version_root = store.ui_design_home() / "packages" / record["name"] / version_id
        if version_root.exists():
            raise RegistryError(f"immutable package already exists: {version_root}")
        version_root.parent.mkdir(parents=True, exist_ok=True)
        stage = version_root.with_name(f".{version_root.name}.stage-{uuid.uuid4().hex}")
        try:
            _copy_package(content_root, stage)
            if package_digest(stage) != current_digest:
                raise DigestConflict("staged immutable package digest mismatch")
            os.replace(stage, version_root)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        record["status"] = "approved"
        record["package_path"] = str(version_root)
        record["version_id"] = version_id
        record["approved_at"] = _now()
        record["updated_at"] = _now()
        versions = value["packages"].setdefault(record["name"], [])
        versions.append(
            {
                "draft_id": draft_id,
                "digest": current_digest,
                "package_path": str(version_root),
                "version_id": version_id,
            }
        )
        _write_registry(value)
        approved = copy.deepcopy(record)
    _audit("draft_approved", approved)
    return approved
