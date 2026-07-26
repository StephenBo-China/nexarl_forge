"""Read-only discovery of managed and unmanaged agent skills."""

from __future__ import annotations

import copy
import pathlib
import re
from typing import Any

import ui_design_store as store
import ui_skill_validator as validator


DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def discovery_state_path() -> pathlib.Path:
    return store.ui_design_home() / "discovery.json"


def load_discovery_state() -> dict[str, Any]:
    path = discovery_state_path()
    if not path.exists():
        return {"schema_version": 1, "ignored_fingerprints": [], "last_scan": []}
    value = store.read_json_strict(path)
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError(f"invalid discovery state: {path}")
    ignored = value.get("ignored_fingerprints", [])
    last_scan = value.get("last_scan", [])
    if not isinstance(ignored, list) or not isinstance(last_scan, list):
        raise ValueError(f"invalid discovery state fields: {path}")
    return value


def ignore_fingerprint(digest: str) -> dict[str, Any]:
    if not DIGEST_PATTERN.fullmatch(digest):
        raise ValueError("ignored fingerprint must be a SHA-256 digest")
    state = load_discovery_state()
    state["ignored_fingerprints"] = sorted(
        set(state.get("ignored_fingerprints", [])) | {digest}
    )
    path = discovery_state_path()
    store.atomic_write_json(path, state, backup=path.exists())
    return copy.deepcopy(state)


def _managed_targets(managed: dict[str, Any]) -> dict[str, dict[str, str]]:
    targets = managed.get("targets", {})
    if not isinstance(targets, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for agent, values in targets.items():
        if isinstance(values, dict):
            result[str(agent)] = {
                str(name): str(digest) for name, digest in values.items()
            }
    return result


def scan(
    target_roots: dict[str, list[pathlib.Path]], managed: dict[str, Any]
) -> list[dict[str, Any]]:
    managed_targets = _managed_targets(managed)
    ignored = set(managed.get("ignored_fingerprints", []))
    results: list[dict[str, Any]] = []
    for agent in sorted(target_roots):
        for root_value in target_roots[agent]:
            root = pathlib.Path(root_value)
            if not root.is_dir():
                continue
            for skill_root in sorted(root.iterdir(), key=lambda path: path.name):
                if skill_root.is_symlink() or not skill_root.is_dir():
                    continue
                skill_file = skill_root / "SKILL.md"
                if not skill_file.is_file() or skill_file.is_symlink():
                    continue
                report = validator.validate_package(skill_root, installed_names=set())
                digest = report.get("digest", "")
                expected = managed_targets.get(agent, {}).get(skill_root.name)
                if expected is not None:
                    status = "managed" if digest == expected else "drifted"
                elif digest in ignored:
                    status = "unmanaged_ignored"
                else:
                    status = "unmanaged_discovered"
                results.append(
                    {
                        "agent": agent,
                        "root": str(root),
                        "path": str(skill_root),
                        "name": skill_root.name,
                        "digest": digest,
                        "expected_digest": expected,
                        "status": status,
                        "name_conflict": False,
                        "validation": {
                            "valid": report["valid"],
                            "errors": report["errors"],
                            "warnings": report["warnings"],
                        },
                    }
                )

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in results:
        groups.setdefault((item["agent"], item["name"]), []).append(item)
    for items in groups.values():
        if len({item["digest"] for item in items}) > 1:
            for item in items:
                item["name_conflict"] = True
    return results


def scan_and_persist(
    target_roots: dict[str, list[pathlib.Path]], managed: dict[str, Any]
) -> list[dict[str, Any]]:
    state = load_discovery_state()
    effective_managed = copy.deepcopy(managed)
    effective_managed["ignored_fingerprints"] = sorted(
        set(effective_managed.get("ignored_fingerprints", []))
        | set(state.get("ignored_fingerprints", []))
    )
    results = scan(target_roots, effective_managed)
    state["last_scan"] = copy.deepcopy(results)
    path = discovery_state_path()
    store.atomic_write_json(path, state, backup=path.exists())
    return results
