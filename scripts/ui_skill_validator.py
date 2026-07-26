"""Static, non-executing validation for imported UI skill packages."""

from __future__ import annotations

import os
import pathlib
import re
from typing import Any

import ui_design_store as store


NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
NETWORK_REFERENCE = re.compile(r"https?://[^\s)>\]]+")
SCRIPT_SUFFIXES = {
    ".bash",
    ".js",
    ".pl",
    ".ps1",
    ".py",
    ".rb",
    ".sh",
    ".ts",
    ".zsh",
}
SUSPICIOUS_PATTERNS = {
    "destructive_remove": re.compile(r"\brm\s+-[^\n]*r[^\n]*f|Remove-Item\s+.*-Recurse", re.I),
    "privilege_escalation": re.compile(r"\bsudo\b|Start-Process\s+.*-Verb\s+RunAs", re.I),
    "network_download": re.compile(r"\bcurl\b|\bwget\b|Invoke-WebRequest", re.I),
    "shell_execution": re.compile(r"os\.system\s*\(|subprocess\.|child_process", re.I),
}


def _issue(code: str, message: str, path: str = "") -> dict[str, str]:
    value = {"code": code, "message": message}
    if path:
        value["path"] = path
    return value


def _frontmatter(text: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    errors: list[dict[str, str]] = []
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, [_issue("missing_frontmatter", "SKILL.md must start with YAML frontmatter")]
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        return {}, [_issue("invalid_frontmatter", "SKILL.md frontmatter is not closed")]
    metadata: dict[str, str] = {}
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            errors.append(_issue("invalid_frontmatter", f"invalid metadata line: {line}"))
            continue
        key, raw = stripped.split(":", 1)
        key = key.strip()
        if not key or key in metadata:
            errors.append(_issue("invalid_frontmatter", f"invalid or duplicate key: {key}"))
            continue
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        metadata[key] = value
    return metadata, errors


def _local_reference(raw: str) -> str | None:
    value = raw.strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1]
    value = value.split(" ", 1)[0]
    if not value or value.startswith(("#", "http://", "https://", "mailto:", "data:")):
        return None
    return value.split("#", 1)[0]


def validate_package(
    root: pathlib.Path,
    *,
    installed_names: set[str],
    max_files: int = 2_000,
    max_bytes: int = 100 * 1024 * 1024,
) -> dict[str, Any]:
    root = pathlib.Path(root)
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    files: list[dict[str, Any]] = []
    scripts: list[dict[str, Any]] = []
    network_references: list[dict[str, str]] = []
    text_files: dict[pathlib.Path, str] = {}
    metadata: dict[str, str] = {}
    total_bytes = 0

    if not root.is_dir():
        errors.append(_issue("missing_package", f"package directory does not exist: {root}"))
    else:
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                errors.append(_issue("symlink", "symlinks are not allowed", relative))
                continue
            if path.is_dir():
                continue
            if not path.is_file():
                errors.append(_issue("non_regular_file", "only regular files are allowed", relative))
                continue
            size = path.stat().st_size
            total_bytes += size
            executable = bool(path.stat().st_mode & 0o111)
            files.append({"path": relative, "size": size, "executable": executable})
            is_script = executable or path.suffix.lower() in SCRIPT_SUFFIXES
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = ""
            else:
                text_files[path] = text
            if is_script:
                findings = [
                    code for code, pattern in SUSPICIOUS_PATTERNS.items() if pattern.search(text)
                ]
                scripts.append(
                    {
                        "path": relative,
                        "executable": executable,
                        "findings": findings,
                    }
                )
                for finding in findings:
                    warnings.append(
                        _issue(f"script_{finding}", f"script contains {finding}", relative)
                    )
            for url in NETWORK_REFERENCE.findall(text):
                network_references.append({"path": relative, "url": url})

    if len(files) > max_files:
        errors.append(_issue("file_count_limit", f"package has {len(files)} files; limit is {max_files}"))
    if total_bytes > max_bytes:
        errors.append(_issue("size_limit", f"package has {total_bytes} bytes; limit is {max_bytes}"))

    skill_path = root / "SKILL.md"
    skill_text = ""
    if not skill_path.is_file() or skill_path.is_symlink():
        errors.append(_issue("missing_skill_md", "package root must contain SKILL.md"))
    else:
        try:
            skill_text = skill_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            errors.append(_issue("invalid_skill_md", "SKILL.md must be UTF-8 text"))
        else:
            metadata, metadata_errors = _frontmatter(skill_text)
            errors.extend(metadata_errors)

    name = metadata.get("name", "")
    description = metadata.get("description", "").strip()
    if not NAME_PATTERN.fullmatch(name):
        errors.append(_issue("invalid_name", f"invalid skill name: {name!r}"))
    if not description:
        errors.append(_issue("missing_description", "skill description is required"))
    if name and name in installed_names:
        errors.append(_issue("name_conflict", f"skill name already exists: {name}"))

    root_resolved = root.resolve()
    for markdown_path, markdown_text in text_files.items():
        if markdown_path.suffix.lower() != ".md":
            continue
        markdown_relative = markdown_path.relative_to(root).as_posix()
        for match in MARKDOWN_LINK.finditer(markdown_text):
            reference = _local_reference(match.group(1))
            if not reference:
                continue
            target = (markdown_path.parent / reference).resolve(strict=False)
            if not target.is_relative_to(root_resolved):
                errors.append(
                    _issue(
                        "reference_traversal",
                        f"reference escapes package: {reference}",
                        markdown_relative,
                    )
                )
            elif not target.exists():
                errors.append(
                    _issue(
                        "missing_reference",
                        f"missing reference: {reference}",
                        f"{markdown_relative} -> {reference}",
                    )
                )

    if network_references:
        warnings.append(
            _issue("network_requirements", "package contains external network references")
        )
    if not metadata.get("license"):
        warnings.append(_issue("unknown_license", "package does not declare a license"))

    digest = ""
    if root.is_dir() and not any(item["code"] == "symlink" for item in errors):
        digest = store.tree_digest(root)
    return {
        "valid": not errors,
        "name": name,
        "description": description,
        "license": metadata.get("license", "unknown"),
        "metadata": metadata,
        "digest": digest,
        "errors": errors,
        "warnings": warnings,
        "files": files,
        "scripts": scripts,
        "network_references": network_references,
    }
