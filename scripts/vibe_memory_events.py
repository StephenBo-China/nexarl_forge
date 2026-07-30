"""Normalize Codex and Claude Code hook payloads without retaining their contents."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
from datetime import datetime


@dataclasses.dataclass(frozen=True)
class NormalizedEvent:
    agent: str
    event: str
    cwd: pathlib.Path
    session_id: str
    timestamp: str
    payload_digest: str


def normalize_event(
    agent: str, event: str, payload: object, fallback_cwd: pathlib.Path
) -> NormalizedEvent:
    """Return safe metadata for a supported hook event and a digest of its payload."""
    if agent not in {"codex", "claude-code"}:
        raise ValueError(f"unsupported agent: {agent}")

    payload_digest = _payload_digest(payload)
    metadata = payload if isinstance(payload, dict) else {}
    raw_cwd = metadata.get("cwd")
    raw_session_id = metadata.get("session_id", metadata.get("sessionId"))

    cwd = _canonical_cwd(raw_cwd, fallback_cwd)
    session_id = raw_session_id if isinstance(raw_session_id, str) and raw_session_id else "unknown"
    timestamp = datetime.now().astimezone().replace(microsecond=0).isoformat()
    return NormalizedEvent(agent, event, cwd, session_id, timestamp, payload_digest)


def _payload_digest(payload: object) -> str:
    try:
        canonical_payload = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise ValueError("payload must be JSON-serializable") from None
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


def _canonical_cwd(raw_cwd: object, fallback_cwd: pathlib.Path) -> pathlib.Path:
    candidate = raw_cwd if isinstance(raw_cwd, str) and raw_cwd else fallback_cwd
    return pathlib.Path(candidate).expanduser().resolve()
