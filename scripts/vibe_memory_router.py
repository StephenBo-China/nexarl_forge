"""Resolve workspace directories to registered memory projects."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import pathlib
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any, Mapping

import memory_project
import memory_review_queue
import vibe_memory_install
import vibe_memory_paths
import vibe_memory_settings
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
QUEUE_LOCK_NAME = memory_review_queue.QUEUE_LOCK_FILENAME
MAX_QUEUE_INPUT_BYTES = 4 * 1024 * 1024
QUEUE_REFRESH_TIMEOUT_SECONDS = 8.0
READ_CHUNK_BYTES = 64 * 1024
MAX_QUEUE_WORKER_OUTPUT_BYTES = 4096
QUEUE_COUNT_NAMES = {
    "pending",
    "actionable_pending",
    "checkpoint_pending",
    "project_pending",
    "personal_pending",
    "approved",
    "rejected",
    "deferred",
}
PROTECTED_CODEX_NAMES = (
    "memory_review_queue.json",
    QUEUE_LOCK_NAME,
    "memory_review_state.json",
    "memory_review_state.json.lock",
    *PACKET_NAMES,
    PACKET_LOCK_NAME,
    PACKET_JOURNAL_NAME,
)


def _candidate_cli_parts(paths: Any | None = None) -> list[str]:
    """Return an executable command that survives PATH/runtime changes."""
    runtime_paths = paths or vibe_memory_paths.for_home()
    try:
        launcher = pathlib.Path(runtime_paths.launcher)
    except TypeError:
        launcher = pathlib.Path("")
    if launcher.is_file() and os.access(launcher, os.X_OK):
        return [str(launcher)]
    config = pathlib.Path(runtime_paths.install_root) / "config.json"
    try:
        value = json.loads(config.read_text(encoding="utf-8"))
        interpreter = value.get("python_executable") if isinstance(value, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        interpreter = None
    cli = pathlib.Path(runtime_paths.install_root) / "current" / "scripts" / "vibe_memory_cli.py"
    if isinstance(interpreter, str) and interpreter and cli.is_file():
        try:
            validated_interpreter = vibe_memory_install.validate_python(interpreter)
        except vibe_memory_install.InstallError:
            pass
        else:
            return [validated_interpreter, str(cli)]
    return [sys.executable, str(pathlib.Path(__file__).with_name("vibe_memory_cli.py"))]


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
        try:
            text = _read_bounded_regular(path=self.path)
            value = json.loads(text) if text is not None else {}
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid idempotency store {self.path}: {error}") from error
        if not isinstance(value, dict):
            raise ValueError(f"invalid idempotency store {self.path}: root must be an object")
        return value


def build_context(
    event: NormalizedEvent,
    project_root: pathlib.Path | None,
    pending: Mapping[str, int],
    automatic_candidate_checks: bool = True,
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
    command_parts = _candidate_cli_parts()
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
            "memory",
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
    candidate_reminder = ""
    if automatic_candidate_checks:
        candidate_reminder = f"""- The active conversation model may create at most two distilled candidates.
- Personal categories: {personal_categories}.{project_category_line}
- Never capture raw prompts, secrets, filesystem paths, one-off tasks,
  screenshots, URLs, credentials, tokens, or uncertain assumptions.
- Hooks provide metadata and policy context only; they do not summarize prompts
  or call another model.

Candidate CLI:

    {command}
"""
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
{candidate_reminder}
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
        if not root.is_dir():
            continue
        if current == root or root in current.parents:
            matches.append(root)
    return max(matches, key=lambda root: len(root.parts)) if matches else None


def _idempotency_path() -> pathlib.Path:
    runtime = vibe_memory_paths.for_home()
    return runtime.install_root / "state" / IDEMPOTENCY_FILENAME


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _validate_name(name: str) -> None:
    if not name or pathlib.PurePath(name).name != name or name in {".", ".."}:
        raise ValueError("unsafe project memory filename")


def _stat_at(directory_fd: int, name: str) -> os.stat_result | None:
    _validate_name(name)
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


@contextlib.contextmanager
def _open_codex_dir(project_root: pathlib.Path):
    root = pathlib.Path(project_root).resolve()
    root_fd = os.open(root, _directory_open_flags())
    codex_fd = -1
    try:
        if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
            raise ValueError("unsafe registered project root")
        try:
            os.mkdir("codex", mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        try:
            codex_fd = os.open("codex", _directory_open_flags(), dir_fd=root_fd)
        except OSError as error:
            raise ValueError("unsafe project memory directory") from error
        opened = os.fstat(codex_fd)
        linked = _stat_at(root_fd, "codex")
        if (
            linked is None
            or not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(linked.st_mode)
            or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
        ):
            raise ValueError("unsafe project memory directory")
        for name in PROTECTED_CODEX_NAMES:
            target = _stat_at(codex_fd, name)
            if target is not None and stat.S_ISLNK(target.st_mode):
                raise ValueError("unsafe project memory path")
        yield codex_fd
    finally:
        if codex_fd >= 0:
            os.close(codex_fd)
        os.close(root_fd)


def _check_queue_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("queue refresh deadline exceeded")


def _read_bounded_regular(
    *,
    path: pathlib.Path | None = None,
    directory_fd: int | None = None,
    name: str | None = None,
    max_bytes: int = MAX_QUEUE_INPUT_BYTES,
    deadline: float | None = None,
) -> str | None:
    if (path is None) == (directory_fd is None):
        raise ValueError("queue input requires exactly one location")
    if directory_fd is not None:
        if name is None:
            raise ValueError("missing queue input name")
        _validate_name(name)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if deadline is not None:
        _check_queue_deadline(deadline)
    try:
        if path is not None:
            descriptor = os.open(path, flags)
        else:
            assert directory_fd is not None and name is not None
            descriptor = os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError("unsafe queue input") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("unsafe queue input")
        if opened.st_size > max_bytes:
            raise ValueError("queue input exceeds size limit")
        content = bytearray()
        while True:
            if deadline is not None:
                _check_queue_deadline(deadline)
            remaining = max_bytes + 1 - len(content)
            if remaining <= 0:
                raise ValueError("queue input exceeds size limit")
            try:
                chunk = os.read(descriptor, min(READ_CHUNK_BYTES, remaining))
            except BlockingIOError as error:
                raise ValueError("unsafe queue input") from error
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > max_bytes:
                raise ValueError("queue input exceeds size limit")
        if deadline is not None:
            _check_queue_deadline(deadline)
        try:
            return bytes(content).decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("invalid queue input encoding") from error
    finally:
        os.close(descriptor)


def _refresh_review_queue(
    project_root: pathlib.Path,
    *,
    timeout_seconds: float = QUEUE_REFRESH_TIMEOUT_SECONDS,
) -> Mapping[str, int]:
    """Refresh in a killable child while retaining the verified directory fd."""
    if timeout_seconds <= 0:
        raise TimeoutError("queue refresh deadline exceeded")
    project_root = pathlib.Path(project_root).resolve()
    with _open_codex_dir(project_root) as codex_fd:
        command = _queue_worker_command(codex_fd, project_root, timeout_seconds)
        environment = os.environ.copy()
        environment["HOME"] = str(pathlib.Path.home())
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                env=environment,
                close_fds=True,
                pass_fds=(codex_fd,),
            )
            try:
                process.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.communicate(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise TimeoutError("queue refresh deadline exceeded") from None
            output.seek(0)
            encoded_output = output.read(MAX_QUEUE_WORKER_OUTPUT_BYTES + 1)
    if process.returncode != 0:
        raise ValueError("queue worker failed")
    if len(encoded_output) > MAX_QUEUE_WORKER_OUTPUT_BYTES:
        raise ValueError("invalid queue worker output")
    try:
        stdout = encoded_output.decode("utf-8")
        counts = json.loads(stdout)
    except (UnicodeDecodeError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("invalid queue worker output") from error
    if (
        not isinstance(counts, dict)
        or set(counts) != QUEUE_COUNT_NAMES
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in counts.values()
        )
    ):
        raise ValueError("invalid queue worker output")
    return counts


def _queue_worker_command(
    codex_fd: int, project_root: pathlib.Path, timeout_seconds: float
) -> list[str]:
    return [
        sys.executable,
        str(pathlib.Path(__file__).resolve()),
        "--queue-worker",
        str(codex_fd),
        str(project_root),
        repr(timeout_seconds),
    ]


def _refresh_review_queue_on_fd(
    codex_fd: int,
    project_root: pathlib.Path,
    *,
    timeout_seconds: float,
) -> Mapping[str, int]:
    opened = os.fstat(codex_fd)
    if codex_fd < 3 or not stat.S_ISDIR(opened.st_mode):
        raise ValueError("invalid queue worker directory")
    deadline = time.monotonic() + timeout_seconds
    _check_queue_deadline(deadline)
    personal_source = pathlib.Path.home() / ".codex" / "personal_memory" / "proposals.md"
    personal_text = _read_bounded_regular(
        path=personal_source, deadline=deadline
    ) or ""
    _check_queue_deadline(deadline)
    with memory_review_queue.queue_lock(
        directory_fd=codex_fd,
        name=QUEUE_LOCK_NAME,
        timeout=timeout_seconds,
        deadline=deadline,
    ):
        project_text = _read_bounded_regular(
            directory_fd=codex_fd,
            name="memory_proposals.md",
            deadline=deadline,
        ) or ""
        state_text = _read_bounded_regular(
            directory_fd=codex_fd,
            name="memory_review_state.json",
            deadline=deadline,
        )
        default_state: dict[str, Any] = {"items": {}, "last_reminder_at": ""}
        try:
            state = json.loads(state_text) if state_text is not None else default_state
        except json.JSONDecodeError:
            state = default_state
        if not isinstance(state, dict):
            state = default_state
        state.setdefault("items", {})
        state.setdefault("last_reminder_at", "")
        _check_queue_deadline(deadline)
        if state_text is None:
            _atomic_write_json_at(codex_fd, "memory_review_state.json", state)
            _check_queue_deadline(deadline)
        queue = memory_review_queue.build_queue_from_documents(
            project_text,
            personal_text,
            state,
            project_source_path=project_root / "codex" / "memory_proposals.md",
            personal_source_path=personal_source,
        )
        _check_queue_deadline(deadline)
        _atomic_write_json_at(codex_fd, "memory_review_queue.json", queue)
        _check_queue_deadline(deadline)
        return queue["counts"]


@contextlib.contextmanager
def _advisory_lock_at(directory_fd: int, name: str):
    _validate_name(name)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        os.fchmod(descriptor, 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("unsafe packet lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _fsync_directory(directory_fd: int) -> None:
    os.fsync(directory_fd)


def _atomic_write_at(
    directory_fd: int,
    name: str,
    content: str,
    mode: int = 0o644,
) -> None:
    _validate_name(name)
    target = _stat_at(directory_fd, name)
    if target is not None and stat.S_ISLNK(target.st_mode):
        raise ValueError("unsafe packet path")
    temporary_name = f".{name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(
        temporary_name,
        flags,
        mode,
        dir_fd=directory_fd,
    )
    temporary_exists = True
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, mode="w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        target = _stat_at(directory_fd, name)
        if target is not None and stat.S_ISLNK(target.st_mode):
            raise ValueError("unsafe packet path")
        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_exists = False
        _fsync_directory(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_exists:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


def _atomic_write_json_at(
    directory_fd: int,
    name: str,
    payload: object,
    mode: int = 0o600,
) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_write_at(directory_fd, name, content, mode)


def _read_at(directory_fd: int, name: str) -> str | None:
    try:
        return _read_bounded_regular(directory_fd=directory_fd, name=name)
    except (OSError, ValueError) as error:
        raise ValueError("unsafe packet path") from error


def _restore_at(directory_fd: int, name: str, content: object) -> None:
    if content is None:
        _unlink_at(directory_fd, name)
    elif isinstance(content, str):
        _atomic_write_at(directory_fd, name, content)
    else:
        raise ValueError("invalid packet rollback journal")


def _unlink_at(directory_fd: int, name: str) -> None:
    target = _stat_at(directory_fd, name)
    if target is None:
        return
    if stat.S_ISLNK(target.st_mode):
        raise ValueError("unsafe packet path")
    os.unlink(name, dir_fd=directory_fd)
    _fsync_directory(directory_fd)


def _recover_packet_transaction(directory_fd: int) -> None:
    try:
        journal_text = _read_at(directory_fd, PACKET_JOURNAL_NAME)
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
        _atomic_write_at(directory_fd, name, content)
    _unlink_at(directory_fd, PACKET_JOURNAL_NAME)


def _write_context_packets(project_root: pathlib.Path, content: str) -> None:
    with _open_codex_dir(project_root) as codex_fd, _advisory_lock_at(
        codex_fd, PACKET_LOCK_NAME
    ):
        _recover_packet_transaction(codex_fd)
        previous = [_read_at(codex_fd, name) for name in PACKET_NAMES]
        _atomic_write_json_at(
            codex_fd,
            PACKET_JOURNAL_NAME,
            {"version": 1, "new": content, "previous": previous},
        )
        try:
            for name in PACKET_NAMES:
                _atomic_write_at(codex_fd, name, content)
            _unlink_at(codex_fd, PACKET_JOURNAL_NAME)
        except BaseException:
            try:
                for name, old_content in zip(PACKET_NAMES, previous):
                    _restore_at(codex_fd, name, old_content)
                _unlink_at(codex_fd, PACKET_JOURNAL_NAME)
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
        paths = vibe_memory_paths.for_home()
        settings = vibe_memory_settings.load_settings(paths)
        if normalized.event == "SessionStart":
            vibe_memory_settings.prune_personal_short(
                pathlib.Path(paths.personal_memory) / "short.md",
                retention_days=int(settings["personal_short_retention_days"]),
            )
        project_root = resolve_registered_project(normalized.cwd, _registry_projects())
        counts: Mapping[str, int]
        if project_root is None:
            counts = {"pending": 0, "personal_pending": 0, "project_pending": 0}
        else:
            counts = _refresh_review_queue(project_root)

        context = build_context(
            normalized,
            project_root,
            counts,
            automatic_candidate_checks=bool(settings["automatic_candidate_checks"]),
        )
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


def _queue_worker_main(arguments: list[str]) -> int:
    try:
        if len(arguments) != 4 or arguments[0] != "--queue-worker":
            raise ValueError("invalid queue worker arguments")
        descriptor_text, project_root_text, timeout_text = arguments[1:]
        if not descriptor_text.isascii() or not descriptor_text.isdecimal():
            raise ValueError("invalid queue worker descriptor")
        codex_fd = int(descriptor_text)
        if codex_fd < 3 or codex_fd > 1_000_000:
            raise ValueError("invalid queue worker descriptor")
        project_root = pathlib.Path(project_root_text)
        if not project_root.is_absolute():
            raise ValueError("invalid queue worker project root")
        timeout_seconds = float(timeout_text)
        if (
            not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > QUEUE_REFRESH_TIMEOUT_SECONDS
        ):
            raise ValueError("invalid queue worker timeout")
        counts = _refresh_review_queue_on_fd(
            codex_fd,
            project_root,
            timeout_seconds=timeout_seconds,
        )
        payload = json.dumps(dict(counts), ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) > MAX_QUEUE_WORKER_OUTPUT_BYTES:
            raise ValueError("invalid queue worker output")
    except BaseException:
        return 70
    sys.stdout.write(payload + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_queue_worker_main(sys.argv[1:]))
