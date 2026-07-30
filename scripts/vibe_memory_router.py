"""Resolve workspace directories to registered memory projects."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import shlex
import stat
import subprocess
import sys
import time
import uuid
from typing import Any, Mapping

import memory_project
import vibe_memory_paths
from loop_superpowers import atomic_write_text
from ui_design_store import atomic_write_json
from vibe_memory_events import NormalizedEvent, normalize_event


PERSONAL_CATEGORIES = (
    "development_habit",
    "collaboration_preference",
    "work_style",
    "thinking_style",
    "user_profile",
    "workflow_preference",
)
PROJECT_CATEGORIES = (
    "project_architecture",
    "deployment_rule",
    "product_direction",
    "technical_constraint",
    "project_workflow",
)
IDEMPOTENCY_FILENAME = "hook_events.json"
PACKET_NAMES = ("codex_context_packet.md", "shared_memory_context_packet.md")
PACKET_LOCK_NAME = ".vibe-memory-packets.lock"
PACKET_JOURNAL_NAME = ".vibe-memory-packets-journal.json"
PROTECTED_CODEX_NAMES = (
    "memory_review_queue.json",
    "memory_review_queue.json.lock",
    "memory_review_state.json",
    "memory_review_state.json.lock",
    *PACKET_NAMES,
    PACKET_LOCK_NAME,
    PACKET_JOURNAL_NAME,
)


def _markdown_escape(value: object) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("\r", " ")
        .replace("\n", " ")
    )


class IdempotencyStore:
    """Reserve hook events transactionally with crash-released advisory locking."""

    def __init__(
        self,
        path: pathlib.Path,
        ttl_seconds: float = 30,
        reservation_timeout: float = 30,
    ) -> None:
        self.path = pathlib.Path(path)
        self.ttl_seconds = ttl_seconds
        self.reservation_timeout = reservation_timeout
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    @staticmethod
    def _key(event: NormalizedEvent) -> str:
        key_material = json.dumps(
            [
                event.agent,
                event.session_id,
                event.event,
                str(event.cwd),
                event.payload_digest,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(key_material.encode("utf-8")).hexdigest()

    @contextlib.contextmanager
    def _locked(self):
        parent = self.path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError("unsafe idempotency state directory")
        parent.chmod(0o700)
        for target in (self.path, self.lock_path):
            if target.is_symlink():
                raise ValueError("unsafe idempotency state path")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.lock_path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("unsafe idempotency lock")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def reserve(self, event: NormalizedEvent) -> str | None:
        key = self._key(event)
        with self._locked():
            current = time.time()
            live = self._live_entries(self._read_entries(), current)
            if key in live:
                return None
            reservation = uuid.uuid4().hex
            live[key] = {
                "status": "in_flight",
                "owner": reservation,
                "pid": os.getpid(),
                "timestamp": current,
            }
            atomic_write_json(self.path, live)
            return reservation

    def commit(self, event: NormalizedEvent, reservation: str | None) -> bool:
        if not reservation:
            return False
        key = self._key(event)
        with self._locked():
            current = time.time()
            live = self._live_entries(self._read_entries(), current)
            entry = live.get(key)
            if not self._owned(entry, reservation):
                return False
            live[key] = {"status": "committed", "timestamp": current}
            atomic_write_json(self.path, live)
            return True

    def release(self, event: NormalizedEvent, reservation: str | None) -> bool:
        if not reservation:
            return False
        key = self._key(event)
        with self._locked():
            current = time.time()
            live = self._live_entries(self._read_entries(), current)
            if not self._owned(live.get(key), reservation):
                return False
            del live[key]
            atomic_write_json(self.path, live)
            return True

    @staticmethod
    def _owned(entry: object, reservation: str) -> bool:
        return (
            isinstance(entry, dict)
            and entry.get("status") == "in_flight"
            and entry.get("owner") == reservation
        )

    def _live_entries(
        self, entries: dict[str, object], current: float
    ) -> dict[str, object]:
        live: dict[str, object] = {}
        for key, entry in entries.items():
            if isinstance(entry, (int, float)) and not isinstance(entry, bool):
                if entry > current - self.ttl_seconds:
                    live[key] = {"status": "committed", "timestamp": entry}
                continue
            if not isinstance(entry, dict):
                continue
            timestamp = entry.get("timestamp")
            if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
                continue
            status_value = entry.get("status")
            if status_value == "committed" and timestamp > current - self.ttl_seconds:
                live[key] = entry
            elif (
                status_value == "in_flight"
                and timestamp > current - self.reservation_timeout
                and self._pid_is_alive(entry.get("pid"))
            ):
                live[key] = entry
        return live

    @staticmethod
    def _pid_is_alive(value: object) -> bool:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            return False
        try:
            os.kill(value, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _read_entries(self) -> dict[str, object]:
        if self.path.is_symlink():
            raise ValueError("unsafe idempotency state path")
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid idempotency store {self.path}: {error}") from error
        if not isinstance(value, dict):
            raise ValueError(f"invalid idempotency store {self.path}: root must be an object")
        return value


def build_context(
    event: NormalizedEvent,
    project_root: pathlib.Path | None,
    pending: Mapping[str, int],
) -> str:
    """Build one policy packet for registered projects or personal-only events."""
    personal_root = pathlib.Path.home() / ".codex" / "personal_memory"
    required = [
        personal_root / "long.md",
        personal_root / "short.md",
        personal_root / "proposals.md",
    ]
    project_section = ""
    project_pending_line = ""
    project_category_line = ""
    approval_scope = "Official personal long/short memory"
    command_parts = ["python3"]
    if project_root is not None:
        root = pathlib.Path(project_root)
        required = [
            root / "README.md",
            root / "codex" / "codex_long_memory.md",
            root / "codex" / "codex_short_memory.md",
            root / "codex" / "memory_proposals.md",
            root / "codex" / "codex_context_packet.md",
            *required,
        ]
        project_section = f"\nRegistered project: `{_markdown_escape(root)}`\n"
        project_pending_line = (
            f'\n- project candidates: {pending.get("project_pending", 0)}'
        )
        project_category_line = f"\n- Project categories: {', '.join(PROJECT_CATEGORIES)}."
        approval_scope += " and project long memory"
        command_parts = [
            "env",
            f"MEMORY_REVIEW_PROJECT_ROOT={root}",
            *command_parts,
        ]
    required_lines = "\n".join(f"- `{_markdown_escape(path)}`" for path in required)
    personal_categories = ", ".join(PERSONAL_CATEGORIES)
    command_parts.extend(
        [
            str(pathlib.Path(__file__).resolve().parent / "memory_review.py"),
            "propose",
            "--scope",
            "personal",
            "--target",
            "long",
            "--category",
            "CATEGORY",
            "--title",
            "TITLE",
            "--summary",
            "SUMMARY",
            "--source-event",
            "agent_summary",
            "--source-agent",
            event.agent,
            "--policy-version",
            "1",
        ]
    )
    command = " ".join(shlex.quote(part) for part in command_parts)
    return f"""# Shared Memory Context

- source agent: {_markdown_escape(event.agent)}
- event: {_markdown_escape(event.event)}
- pending total: {pending.get("pending", 0)}
- personal candidates: {pending.get("personal_pending", 0)}{project_pending_line}
{project_section}
## Required Memory

{required_lines}

## Approval and Candidate Governance

- {approval_scope} may change only
  after explicit approval of the exact candidate content.
- Write candidates only to the proposals files; never write directly to
  approved long or short memory.
- The active conversation model may create at most two distilled candidates.
- Personal categories: {personal_categories}.{project_category_line}
- Never capture raw prompts, secrets, filesystem paths, one-off tasks,
  screenshots, URLs, credentials, tokens, or uncertain assumptions.
- Hooks provide metadata and policy context only; they do not summarize prompts
  or call another model.

Candidate CLI:

    {command}
"""


def resolve_registered_project(
    cwd: pathlib.Path, projects: list[dict[str, object]]
) -> pathlib.Path | None:
    """Return the deepest registered root containing *cwd*, if any."""
    current = cwd.expanduser().resolve()
    matches: list[pathlib.Path] = []
    for entry in projects:
        raw_root = entry.get("root") if isinstance(entry, dict) else None
        if not isinstance(raw_root, str) or not raw_root:
            continue
        try:
            root = pathlib.Path(raw_root).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if current == root or root in current.parents:
            matches.append(root)
    return max(matches, key=lambda root: len(root.parts)) if matches else None


def _idempotency_path() -> pathlib.Path:
    runtime = vibe_memory_paths.for_home()
    return runtime.install_root / "state" / IDEMPOTENCY_FILENAME


def _safe_codex_dir(project_root: pathlib.Path) -> pathlib.Path:
    root = pathlib.Path(project_root).resolve()
    codex = root / "codex"
    if codex.is_symlink():
        raise ValueError("unsafe project memory directory")
    codex.mkdir(parents=False, exist_ok=True)
    if not codex.is_dir() or codex.resolve() != codex:
        raise ValueError("unsafe project memory directory")
    for name in PROTECTED_CODEX_NAMES:
        if (codex / name).is_symlink():
            raise ValueError("unsafe project memory path")
    return codex


def _refresh_review_queue(project_root: pathlib.Path) -> Mapping[str, int]:
    """Refresh and return counts for exactly one registered project."""
    codex = _safe_codex_dir(project_root)
    script = pathlib.Path(__file__).resolve().parent / "memory_review_queue.py"
    environment = os.environ.copy()
    environment["MEMORY_REVIEW_PROJECT_ROOT"] = str(project_root)
    subprocess.run(
        [sys.executable, str(script), "refresh"],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=8,
        check=True,
    )
    _safe_codex_dir(project_root)
    queue_path = codex / "memory_review_queue.json"
    try:
        queue_text = _read_packet(queue_path)
        if queue_text is None:
            raise ValueError("missing queue")
        queue = json.loads(queue_text)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid memory review queue {queue_path}: {error}") from error
    if not isinstance(queue, dict) or not isinstance(queue.get("counts"), dict):
        raise ValueError(f"invalid memory review queue {queue_path}: missing counts")
    return queue["counts"]


@contextlib.contextmanager
def _project_packet_lock(codex: pathlib.Path):
    lock_path = codex / PACKET_LOCK_NAME
    if lock_path.is_symlink():
        raise ValueError("unsafe packet lock")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("unsafe packet lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _fsync_directory(path: pathlib.Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_packet(path: pathlib.Path, content: str) -> None:
    if path.is_symlink():
        raise ValueError("unsafe packet path")
    atomic_write_text(path, content)


def _read_packet(path: pathlib.Path) -> str | None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError("unsafe packet path") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("unsafe packet path")
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = -1
            return handle.read()
    except OSError as error:
        raise ValueError("unsafe packet path") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _restore_packet(path: pathlib.Path, content: object) -> None:
    if path.is_symlink():
        raise ValueError("unsafe packet path")
    if content is None:
        path.unlink(missing_ok=True)
    elif isinstance(content, str):
        _atomic_write_packet(path, content)
    else:
        raise ValueError("invalid packet rollback journal")


def _remove_journal(journal: pathlib.Path) -> None:
    journal.unlink(missing_ok=True)
    _fsync_directory(journal.parent)


def _recover_packet_transaction(codex: pathlib.Path) -> None:
    journal = codex / PACKET_JOURNAL_NAME
    if not journal.exists():
        return
    if journal.is_symlink():
        raise ValueError("unsafe packet journal")
    try:
        journal_text = _read_packet(journal)
        if journal_text is None:
            return
        value = json.loads(journal_text)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("invalid packet transaction journal") from error
    if not isinstance(value, dict):
        raise ValueError("invalid packet transaction journal")
    content = value.get("new")
    if value.get("version") != 1 or not isinstance(content, str):
        raise ValueError("invalid packet transaction journal")
    for name in PACKET_NAMES:
        _atomic_write_packet(codex / name, content)
    _remove_journal(journal)


def _write_context_packets(project_root: pathlib.Path, content: str) -> None:
    codex = _safe_codex_dir(project_root)
    with _project_packet_lock(codex):
        codex = _safe_codex_dir(project_root)
        _recover_packet_transaction(codex)
        targets = tuple(codex / name for name in PACKET_NAMES)
        previous = [_read_packet(path) for path in targets]
        journal = codex / PACKET_JOURNAL_NAME
        atomic_write_json(
            journal,
            {"version": 1, "new": content, "previous": previous},
        )
        _fsync_directory(codex)
        try:
            for path in targets:
                _atomic_write_packet(path, content)
            _remove_journal(journal)
        except BaseException:
            try:
                for path, old_content in zip(targets, previous):
                    _restore_packet(path, old_content)
                _remove_journal(journal)
            except BaseException:
                pass
            raise


def _registry_projects() -> list[dict[str, object]]:
    value = memory_project.registry().get("projects", [])
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def handle_event(
    agent: str,
    event: str,
    payload: object,
    cwd: pathlib.Path,
) -> dict[str, Any]:
    """Route one shared hook event without retaining its raw payload."""
    normalized = normalize_event(agent, event, payload, cwd)
    store = IdempotencyStore(_idempotency_path())
    reservation = store.reserve(normalized)
    if reservation is None:
        return {"status": "duplicate"}
    try:
        project_root = resolve_registered_project(normalized.cwd, _registry_projects())
        counts: Mapping[str, int]
        if project_root is None:
            counts = {"pending": 0, "personal_pending": 0, "project_pending": 0}
        else:
            counts = _refresh_review_queue(project_root)

        context = build_context(normalized, project_root, counts)
        if project_root is not None:
            _write_context_packets(project_root, context)
        if not store.commit(normalized, reservation):
            raise RuntimeError("idempotency reservation ownership lost")
        return {
            "status": "ok",
            "hookSpecificOutput": {"additionalContext": context},
        }
    except BaseException:
        store.release(normalized, reservation)
        raise
