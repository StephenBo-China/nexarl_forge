"""Durable storage primitives for the local UI design control plane."""

from __future__ import annotations

import datetime as dt
import contextlib
import hashlib
import json
import os
import pathlib
import shutil
import socket
import tempfile
import time
import uuid
from typing import Any


def ui_design_home() -> pathlib.Path:
    configured = os.environ.get("UI_DESIGN_HOME")
    value = pathlib.Path(configured) if configured else pathlib.Path.home() / ".codex" / "ui_design"
    return value.expanduser().resolve()


def timestamped_backup(path: pathlib.Path) -> pathlib.Path:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return path.with_name(f"{path.name}.bak.{timestamp}")


def atomic_write_json(path: pathlib.Path, value: Any, backup: bool = False) -> None:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        shutil.copy2(path, timestamped_backup(path))
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        pathlib.Path(temp_name).unlink(missing_ok=True)


def read_json_strict(path: pathlib.Path) -> Any:
    with pathlib.Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def append_jsonl(path: pathlib.Path, value: Any) -> None:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        written = os.write(descriptor, data)
        if written != len(data):
            raise OSError(f"short JSONL append: wrote {written} of {len(data)} bytes")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class LockTimeout(TimeoutError):
    def __init__(self, path: pathlib.Path, holder: str, stale: bool) -> None:
        state = "stale" if stale else "active"
        super().__init__(f"timed out waiting for {state} lock {path}; holder={holder}")
        self.path = path
        self.holder = holder
        self.stale = stale


@contextlib.contextmanager
def exclusive_lock(
    path: pathlib.Path,
    *,
    timeout: float = 10.0,
    stale_after: float = 300.0,
    poll_interval: float = 0.05,
):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    metadata = {
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "token": token,
    }
    encoded = (json.dumps(metadata, sort_keys=True) + "\n").encode("utf-8")
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if time.monotonic() >= deadline:
                try:
                    age = max(0.0, time.time() - path.stat().st_mtime)
                    holder = path.read_text(encoding="utf-8").strip() or "unknown"
                except OSError as error:
                    age = 0.0
                    holder = f"unreadable: {error}"
                raise LockTimeout(path, holder, stale=age >= stale_after) from None
            time.sleep(max(poll_interval, 0.001))
            continue
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        break
    try:
        yield metadata
    finally:
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {}
        if current.get("token") == token:
            path.unlink(missing_ok=True)


def tree_digest(root: pathlib.Path) -> str:
    root = pathlib.Path(root)
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()
