"""Layered global and project preferences for UI design work."""

from __future__ import annotations

import copy
import pathlib
from typing import Any

import ui_design_store as store


OVERRIDE_MODES = {"inherit", "replace", "append", "clear"}

DEFAULT_GLOBAL_PREFERENCES = {
    "schema_version": 1,
    "brand": {
        "personality": [],
        "emotional_tone": [],
        "audiences": [],
        "usability_priorities": [],
    },
    "visual": {
        "preferred_styles": [],
        "prohibited_styles": [],
        "color_principles": [],
        "prohibited_color_treatments": [],
        "typography": {
            "display": "",
            "body": "",
            "utility": "",
            "language_rules": [],
        },
        "spacing_density": "balanced",
        "radius": "contextual",
        "elevation": "subtle",
        "borders": "functional",
        "surfaces": [],
    },
    "imagery": {
        "icons": [],
        "illustration": [],
        "photography": [],
        "generated_assets": [],
    },
    "interaction": {
        "motion_intensity": "moderate",
        "timing": [],
        "reduced_motion": "required",
        "feedback": [],
        "navigation": [],
        "forms": [],
        "loading": [],
        "empty": [],
        "success": [],
        "error": [],
    },
    "accessibility": {"minimum": ["WCAG 2.2 AA"], "additional_rules": []},
    "platform": {
        "web": {},
        "ios": {},
        "android": {},
        "macos": {},
        "mini_program": {},
    },
    "references": [],
    "design_principles": [],
    "anti_preferences": [],
}


class PreferenceValidationError(ValueError):
    """A preference document or override violates the supported schema."""


def default_global_preferences() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_GLOBAL_PREFERENCES)


def global_preferences_path() -> pathlib.Path:
    return store.ui_design_home() / "preferences.json"


def project_preferences_path(project_root: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(project_root).expanduser() / "codex" / "ui_design" / "preferences.json"


def _validate_like(value: Any, schema: Any, path: str) -> None:
    if isinstance(schema, dict):
        if not isinstance(value, dict):
            raise PreferenceValidationError(f"{path or 'preferences'} must be an object")
        if not schema:
            return
        unknown = sorted(set(value) - set(schema))
        if unknown:
            raise PreferenceValidationError(
                f"unknown preference field: {path + '.' if path else ''}{unknown[0]}"
            )
        missing = sorted(set(schema) - set(value))
        if missing:
            raise PreferenceValidationError(
                f"missing preference field: {path + '.' if path else ''}{missing[0]}"
            )
        for key, child_schema in schema.items():
            child_path = f"{path}.{key}" if path else key
            _validate_like(value[key], child_schema, child_path)
        return
    if isinstance(schema, list):
        if not isinstance(value, list):
            raise PreferenceValidationError(f"{path} must be a list")
        return
    if isinstance(schema, str):
        if not isinstance(value, str):
            raise PreferenceValidationError(f"{path} must be a string")
        return
    if isinstance(schema, int) and not isinstance(value, int):
        raise PreferenceValidationError(f"{path} must be an integer")


def validate_global_preferences(value: Any) -> dict[str, Any]:
    _validate_like(value, DEFAULT_GLOBAL_PREFERENCES, "")
    if value["schema_version"] != 1:
        raise PreferenceValidationError("schema_version must be 1")
    return value


def _path_parts(dot_path: str) -> list[str]:
    parts = dot_path.split(".")
    if not dot_path or any(not part for part in parts):
        raise PreferenceValidationError(f"invalid preference path: {dot_path!r}")
    return parts


def _get_path(value: dict[str, Any], dot_path: str) -> Any:
    current: Any = value
    for part in _path_parts(dot_path):
        if not isinstance(current, dict) or part not in current:
            raise PreferenceValidationError(f"unknown preference path: {dot_path}")
        current = current[part]
    return current


def _set_path(value: dict[str, Any], dot_path: str, replacement: Any) -> None:
    parts = _path_parts(dot_path)
    current = value
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            raise PreferenceValidationError(f"unknown preference path: {dot_path}")
        current = child
    if parts[-1] not in current:
        raise PreferenceValidationError(f"unknown preference path: {dot_path}")
    current[parts[-1]] = replacement


def _leaf_sources(value: Any, prefix: str = "") -> dict[str, str]:
    if isinstance(value, dict) and value:
        result: dict[str, str] = {}
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else key
            result.update(_leaf_sources(child, path))
        return result
    return {prefix: "global"} if prefix else {}


def validate_project_overrides(overrides: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(overrides, dict):
        raise PreferenceValidationError("project overrides must be an object")
    for dot_path, instruction in overrides.items():
        schema_value = _get_path(DEFAULT_GLOBAL_PREFERENCES, dot_path)
        if not isinstance(instruction, dict):
            raise PreferenceValidationError(f"override {dot_path} must be an object")
        mode = instruction.get("mode")
        if mode not in OVERRIDE_MODES:
            raise PreferenceValidationError(f"unsupported override mode for {dot_path}: {mode}")
        if mode in {"replace", "append"} and "value" not in instruction:
            raise PreferenceValidationError(f"override {dot_path} requires value")
        if mode == "append":
            if not isinstance(schema_value, list) or not isinstance(instruction["value"], list):
                raise PreferenceValidationError(f"append override {dot_path} requires lists")
        if mode == "replace":
            _validate_like(instruction["value"], schema_value, dot_path)
    return overrides


def merge_preferences(
    global_value: dict[str, Any], overrides: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    if not isinstance(global_value, dict):
        raise PreferenceValidationError("global preferences must be an object")
    validate_project_overrides(overrides)
    effective = copy.deepcopy(global_value)
    sources = _leaf_sources(effective)
    for dot_path, instruction in overrides.items():
        mode = instruction["mode"]
        if mode == "inherit":
            _get_path(effective, dot_path)
            continue
        current = _get_path(effective, dot_path)
        if mode == "replace":
            replacement = copy.deepcopy(instruction["value"])
        elif mode == "append":
            if not isinstance(current, list):
                raise PreferenceValidationError(f"append target {dot_path} must be a list")
            replacement = copy.deepcopy(current) + copy.deepcopy(instruction["value"])
        elif mode == "clear":
            if isinstance(current, list):
                replacement = []
            elif isinstance(current, dict):
                replacement = {}
            elif isinstance(current, str):
                replacement = ""
            else:
                replacement = None
        else:  # pragma: no cover - validated above
            raise PreferenceValidationError(f"unsupported override mode: {mode}")
        _set_path(effective, dot_path, replacement)
        sources.update({key: "project" for key in _leaf_sources(replacement, dot_path)} or {dot_path: "project"})
        sources[dot_path] = "project"
    return {"value": effective, "sources": sources}


def load_global_preferences() -> dict[str, Any]:
    path = global_preferences_path()
    if not path.exists():
        return default_global_preferences()
    value = store.read_json_strict(path)
    return validate_global_preferences(value)


def save_global_preferences(value: dict[str, Any]) -> pathlib.Path:
    validate_global_preferences(value)
    path = global_preferences_path()
    store.atomic_write_json(path, value, backup=path.exists())
    return path


def load_project_overrides(project_root: pathlib.Path) -> dict[str, dict[str, Any]]:
    path = project_preferences_path(project_root)
    if not path.exists():
        return {}
    document = store.read_json_strict(path)
    if isinstance(document, dict) and "overrides" in document:
        if document.get("schema_version") != 1:
            raise PreferenceValidationError("project preference schema_version must be 1")
        overrides = document["overrides"]
    else:
        overrides = document
    return validate_project_overrides(overrides)


def save_project_overrides(
    project_root: pathlib.Path, overrides: dict[str, dict[str, Any]]
) -> pathlib.Path:
    validate_project_overrides(overrides)
    path = project_preferences_path(project_root)
    document = {"schema_version": 1, "overrides": overrides}
    store.atomic_write_json(path, document, backup=path.exists())
    return path


def effective_preferences(project_root: pathlib.Path) -> dict[str, Any]:
    return merge_preferences(
        load_global_preferences(), load_project_overrides(project_root)
    )
