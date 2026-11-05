"""Bounded-child entry point for Windows project queue refresh."""
from __future__ import annotations
import json
import pathlib
import sys
import runtime_platform


def main() -> int:
    try:
        if not runtime_platform.IS_WINDOWS or len(sys.argv) != 2:
            raise ValueError("invalid Windows queue worker invocation")
        project = runtime_platform.validate_path(pathlib.Path(sys.argv[1]))
        runtime_platform.validate_path(project / "codex")
        import memory_review_queue as queue
        if queue.PROJECT_ROOT != project:
            raise ValueError("queue worker requires a registered project")
        for name in ("memory_proposals.md", "memory_review_state.json", "memory_review_queue.json"):
            target = runtime_platform.validate_path(project / "codex" / name)
            if target.exists():
                runtime_platform.read_regular(target)
        counts = queue.build_queue()["counts"]
        print(json.dumps(counts, ensure_ascii=True, separators=(",", ":")))
        return 0
    except Exception:
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
