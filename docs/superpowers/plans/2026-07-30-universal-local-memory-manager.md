# Universal Local Memory Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a macOS clone-to-install local manager that gives Codex and optional Claude Code one approval-gated personal/project memory system while preserving every current review-console capability.

**Architecture:** Install a versioned copy of the repository under the user's macOS Application Support directory and expose it through a stable `vibe-memory` CLI. User-level Codex and Claude Code hook adapters normalize lifecycle payloads into one shared router, which loads personal memory for every conversation and project memory only for registered working directories. Existing Markdown/JSON memory, UI design, UI Skill, Loop, and worktree data remain canonical and are migrated in place with backups.

**Tech Stack:** Python 3.10+ standard library, Bash, JSON/Markdown storage, macOS LaunchAgent, `unittest`, existing dependency-free HTTP review server.

---

## Milestones and file map

The work is intentionally split into four independently verifiable milestones:

1. **Portable shared core:** paths, registry routing, event normalization, policy context, idempotency.
2. **macOS product install:** safe hook merge, versioned runtime, LaunchAgent, CLI lifecycle.
3. **Full migration compatibility:** inventory, legacy-hook migration, complete console/data validation.
4. **Public release gate:** repository hygiene, documentation, install/migration E2E, release verification.

New focused modules:

- `scripts/vibe_memory_paths.py`: resolve all runtime and legacy-compatible data paths.
- `scripts/vibe_memory_events.py`: normalize Codex and Claude Code hook payloads.
- `scripts/vibe_memory_router.py`: registered-project routing, context generation, event deduplication.
- `scripts/vibe_memory_hooks.py`: structural client-config merge, backup, status, repair, uninstall.
- `scripts/vibe_memory_settings.py`: first-run choices and short-memory retention.
- `scripts/vibe_memory_migration.py`: inventory and legacy per-project hook preview/apply.
- `scripts/vibe_memory_install.py`: versioned runtime, LaunchAgent, update, rollback, uninstall.
- `scripts/vibe_memory_cli.py`: stable command surface that delegates to existing subsystems.
- `install.sh`: minimal macOS bootstrap into the Python installer.
- `templates/macos/com.noema.vibe-memory.plist`: LaunchAgent template.
- `release.json`: application, schema, hook protocol, platform, and Python compatibility.

Existing large modules remain in place. The plan changes only their path/config seams rather than rewriting the review server or UI control plane.

## Milestone 1: Portable shared core

### Task 1: Centralize portable paths and release metadata

**Files:**
- Create: `release.json`
- Create: `scripts/vibe_memory_paths.py`
- Create: `tests/test_vibe_memory_paths.py`
- Modify: `scripts/worktree_flow.py:24-35`
- Modify: `scripts/memory_project.py:18-35`

- [ ] **Step 1: Write failing path tests**

```python
class RuntimePathsTest(unittest.TestCase):
    def test_paths_are_derived_from_supplied_home(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            home = pathlib.Path(value)
            result = vibe_memory_paths.for_home(home)
            self.assertEqual(result.personal_memory, home / ".codex/personal_memory")
            self.assertEqual(result.project_registry, home / ".codex/memory_review/projects.json")
            self.assertEqual(result.install_root, home / "Library/Application Support/VibeMemory")
            self.assertEqual(result.worktree_root, home / "Projects/worktrees")

    def test_release_manifest_is_supported_on_macos(self) -> None:
        manifest = vibe_memory_paths.read_release_manifest(ROOT / "release.json")
        self.assertEqual(manifest["app_version"], "1.0.0")
        self.assertEqual(manifest["data_schema_version"], 1)
        self.assertEqual(manifest["hook_protocol_version"], 1)
        self.assertEqual(manifest["platform"], "macOS")
```

- [ ] **Step 2: Verify the tests fail before implementation**

Run: `python3 -m unittest tests.test_vibe_memory_paths -v`

Expected: `ModuleNotFoundError: No module named 'vibe_memory_paths'`.

- [ ] **Step 3: Add the manifest and path model**

```json
{
  "app_version": "1.0.0",
  "data_schema_version": 1,
  "hook_protocol_version": 1,
  "minimum_python": "3.10",
  "platform": "macOS"
}
```

```python
@dataclasses.dataclass(frozen=True)
class RuntimePaths:
    home: pathlib.Path
    install_root: pathlib.Path
    personal_memory: pathlib.Path
    project_registry: pathlib.Path
    ui_design_home: pathlib.Path
    worktree_manager: pathlib.Path
    worktree_root: pathlib.Path


def for_home(home: pathlib.Path | None = None) -> RuntimePaths:
    root = (home or pathlib.Path.home()).expanduser().resolve()
    return RuntimePaths(
        home=root,
        install_root=root / "Library/Application Support/VibeMemory",
        personal_memory=root / ".codex/personal_memory",
        project_registry=root / ".codex/memory_review/projects.json",
        ui_design_home=root / ".codex/ui_design",
        worktree_manager=root / ".codex/worktree_manager",
        worktree_root=root / "Projects/worktrees",
    )


def read_release_manifest(path: pathlib.Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {"app_version", "data_schema_version", "hook_protocol_version", "minimum_python", "platform"}
    if not isinstance(value, dict) or required - value.keys():
        raise ValueError(f"invalid release manifest: {path}")
    return value
```

Replace the hard-coded worktree default with `vibe_memory_paths.for_home().worktree_root`; derive project registry and loop directories from the same path module. Keep existing environment-variable overrides higher priority.

- [ ] **Step 4: Run focused and existing path-sensitive tests**

Run: `python3 -m unittest tests.test_vibe_memory_paths tests.test_worktree_flow tests.test_memory_review -v`

Expected: all tests pass and no generated instruction contains `/Users/stephenbo`.

- [ ] **Step 5: Commit the portable path layer**

```bash
git add release.json scripts/vibe_memory_paths.py scripts/worktree_flow.py scripts/memory_project.py tests/test_vibe_memory_paths.py
git commit -m "feat: add portable memory manager paths"
```

### Task 2: Resolve registered code and non-code projects

**Files:**
- Create: `tests/test_vibe_memory_router.py`
- Create: `scripts/vibe_memory_router.py`
- Modify: `scripts/memory_project.py:40-75`

- [ ] **Step 1: Write failing registry-routing tests**

```python
def test_longest_registered_parent_wins(self) -> None:
    projects = [
        {"root": "/work"},
        {"root": "/work/research"},
    ]
    self.assertEqual(
        router.resolve_registered_project(pathlib.Path("/work/research/notes"), projects),
        pathlib.Path("/work/research"),
    )

def test_unregistered_directory_returns_none(self) -> None:
    self.assertIsNone(
        router.resolve_registered_project(pathlib.Path("/private/notes"), [{"root": "/work"}])
    )

def test_register_accepts_non_git_directory(self) -> None:
    with tempfile.TemporaryDirectory() as value:
        root = pathlib.Path(value) / "book"
        root.mkdir()
        result = memory_project.register_project(root)
        self.assertEqual(pathlib.Path(result["current_project"]), root.resolve())
```

- [ ] **Step 2: Run tests and confirm the current Git assumption fails**

Run: `python3 -m unittest tests.test_vibe_memory_router -v`

Expected: failures for missing router and non-Git registration behavior.

- [ ] **Step 3: Implement canonical longest-parent routing**

```python
def resolve_registered_project(cwd: pathlib.Path, projects: list[dict[str, object]]) -> pathlib.Path | None:
    current = cwd.expanduser().resolve()
    matches: list[pathlib.Path] = []
    for entry in projects:
        raw = entry.get("root") if isinstance(entry, dict) else None
        if not isinstance(raw, str) or not raw:
            continue
        root = pathlib.Path(raw).expanduser().resolve()
        if current == root or root in current.parents:
            matches.append(root)
    return max(matches, key=lambda item: len(item.parts)) if matches else None
```

Change `register_project` to require an existing directory, not a Git root. Keep Git-only checks inside Loop/worktree commands.

- [ ] **Step 4: Run routing and project-management regression tests**

Run: `python3 -m unittest tests.test_vibe_memory_router tests.test_memory_review tests.test_worktree_flow -v`

Expected: all pass; Loop commands still reject non-Git projects where Git is required.

- [ ] **Step 5: Commit project routing**

```bash
git add scripts/vibe_memory_router.py scripts/memory_project.py tests/test_vibe_memory_router.py
git commit -m "feat: route memory by registered workspace"
```

### Task 3: Normalize Codex and Claude Code hook events

**Files:**
- Create: `scripts/vibe_memory_events.py`
- Create: `tests/fixtures/hooks/codex_user_prompt.json`
- Create: `tests/fixtures/hooks/claude_user_prompt.json`
- Create: `tests/test_vibe_memory_events.py`

- [ ] **Step 1: Add client fixtures and failing normalization tests**

```python
def test_codex_event_normalizes_without_prompt_body(self) -> None:
    payload = json.loads((FIXTURES / "codex_user_prompt.json").read_text())
    event = normalize_event("codex", "UserPromptSubmit", payload, pathlib.Path("/work"))
    self.assertEqual(event.agent, "codex")
    self.assertEqual(event.session_id, "codex-session-1")
    self.assertEqual(event.cwd, pathlib.Path("/work"))
    self.assertNotIn("write my plan", dataclasses.asdict(event).values())

def test_claude_event_uses_same_contract(self) -> None:
    payload = json.loads((FIXTURES / "claude_user_prompt.json").read_text())
    event = normalize_event("claude-code", "UserPromptSubmit", payload, pathlib.Path("/work"))
    self.assertEqual(set(dataclasses.asdict(event)), {"agent", "event", "cwd", "session_id", "timestamp", "payload_digest"})
```

Fixtures contain a session identifier, cwd, hook event name, and a harmless prompt used only to verify it is hashed rather than retained.

- [ ] **Step 2: Verify tests fail**

Run: `python3 -m unittest tests.test_vibe_memory_events -v`

Expected: missing module failure.

- [ ] **Step 3: Implement the normalized event contract**

```python
@dataclasses.dataclass(frozen=True)
class NormalizedEvent:
    agent: str
    event: str
    cwd: pathlib.Path
    session_id: str
    timestamp: str
    payload_digest: str


def normalize_event(agent: str, event: str, payload: object, fallback_cwd: pathlib.Path) -> NormalizedEvent:
    if agent not in {"codex", "claude-code"}:
        raise ValueError(f"unsupported agent: {agent}")
    value = payload if isinstance(payload, dict) else {}
    cwd = pathlib.Path(str(value.get("cwd") or fallback_cwd)).expanduser().resolve()
    session_id = str(value.get("session_id") or value.get("sessionId") or "unknown")
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return NormalizedEvent(agent, event, cwd, session_id, now(), hashlib.sha256(encoded.encode()).hexdigest())
```

- [ ] **Step 4: Run event contract tests**

Run: `python3 -m unittest tests.test_vibe_memory_events -v`

Expected: both client fixtures pass and serialized events contain no prompt body.

- [ ] **Step 5: Commit event adapters**

```bash
git add scripts/vibe_memory_events.py tests/fixtures/hooks tests/test_vibe_memory_events.py
git commit -m "feat: normalize codex and claude hook events"
```

### Task 4: Add event idempotency and shared policy context

**Files:**
- Modify: `scripts/vibe_memory_router.py`
- Modify: `scripts/memory_project.py:521-730`
- Modify: `scripts/memory_review_queue.py:280-350`
- Modify: `tests/test_vibe_memory_router.py`
- Modify: `tests/test_memory_review.py:160-190`

- [ ] **Step 1: Write failing idempotency and context tests**

```python
def test_duplicate_event_executes_once(self) -> None:
    store = router.IdempotencyStore(self.temp / "events.json", ttl_seconds=30)
    self.assertTrue(store.claim(self.event))
    self.assertFalse(store.claim(self.event))

def test_unregistered_context_is_personal_only(self) -> None:
    context = router.build_context(self.event, project_root=None, pending={})
    self.assertIn("personal_memory/long.md", context)
    self.assertNotIn("codex/codex_long_memory.md", context)

def test_registered_context_includes_shared_project_memory(self) -> None:
    context = router.build_context(self.event, project_root=pathlib.Path("/work/project"), pending={})
    self.assertIn("codex/codex_long_memory.md", context)
    self.assertIn("active codex conversation model", context.lower())

def test_candidate_records_agent_and_policy_version(self) -> None:
    review.create_agent_candidate(
        "personal", "long", "work_style", "Concise decisions",
        "The user prefers concise decision records.",
        source_agent="codex", policy_version=1,
    )
    text = review.PERSONAL_PROPOSALS.read_text(encoding="utf-8")
    self.assertIn("source_agent: codex", text)
    self.assertIn("policy_version: 1", text)
```

Replace the legacy hook test that expects a truncated prompt summary with an assertion that hook-generated project short memory contains event metadata only.

- [ ] **Step 2: Verify focused failures**

Run: `python3 -m unittest tests.test_vibe_memory_router tests.test_memory_review.MemoryReviewQualityTest.test_generated_project_hook_summarizes_short_memory -v`

Expected: failures because idempotency/context APIs do not exist and the legacy hook still stores prompt text.

- [ ] **Step 3: Implement bounded idempotency and remove raw prompt capture**

```python
class IdempotencyStore:
    def __init__(self, path: pathlib.Path, ttl_seconds: int = 30):
        self.path, self.ttl_seconds = path, ttl_seconds

    def claim(self, event: NormalizedEvent) -> bool:
        key = hashlib.sha256(f"{event.agent}|{event.session_id}|{event.event}|{event.cwd}|{event.payload_digest}".encode()).hexdigest()
        with ui_design_store.exclusive_lock(self.path.with_suffix(".lock")):
            now_value = time.time()
            values = {k: v for k, v in read_json(self.path, {}).items() if now_value - float(v) < self.ttl_seconds}
            if key in values:
                return False
            values[key] = now_value
            ui_design_store.atomic_write_json(self.path, values)
        return True
```

Make `build_context` include personal files for all events, project files only when a root is supplied, fixed candidate categories, the two-candidate limit, approval requirements, and the active-client source. Change generated legacy hooks to append only timestamp, agent, event, cwd, and session identifier.

Extend `create_agent_candidate` and its CLI parser with persisted
`source_agent` and `policy_version` metadata. Legacy direct calls default to
`unknown` and `1`; new hook-injected commands always supply both. Parse the
fields into review/audit records without adding them to approved memory prose.

- [ ] **Step 4: Run shared-core regression tests**

Run: `python3 -m unittest tests.test_vibe_memory_router tests.test_vibe_memory_events tests.test_memory_review -v`

Expected: all pass; no assertion accepts raw prompt storage.

- [ ] **Step 5: Commit shared context and idempotency**

```bash
git add scripts/vibe_memory_router.py scripts/memory_project.py scripts/memory_review_queue.py tests/test_vibe_memory_router.py tests/test_memory_review.py
git commit -m "feat: add shared memory event routing"
```

## Milestone 2: macOS product installation

### Task 5: Build managed user-hook documents and safe merge

**Files:**
- Create: `scripts/vibe_memory_hooks.py`
- Create: `tests/test_vibe_memory_hooks.py`

- [ ] **Step 1: Write failing merge, backup, and uninstall tests**

```python
def test_merge_preserves_custom_hooks_and_is_idempotent(self) -> None:
    original = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "custom-hook"}]}]}}
    first = hooks.merge_document(original, "codex", pathlib.Path("/opt/vibe-memory"))
    second = hooks.merge_document(first, "codex", pathlib.Path("/opt/vibe-memory"))
    self.assertEqual(first, second)
    self.assertIn("custom-hook", json.dumps(first))
    self.assertEqual(json.dumps(first).count("vibe-memory hook --agent codex"), 5)

def test_remove_managed_entries_keeps_custom_hook(self) -> None:
    merged = hooks.merge_document(self.original, "claude-code", pathlib.Path("/opt/vibe-memory"))
    cleaned = hooks.remove_managed_entries(merged)
    self.assertIn("custom-hook", json.dumps(cleaned))
    self.assertNotIn("vibe-memory hook", json.dumps(cleaned))
```

- [ ] **Step 2: Verify hook tests fail**

Run: `python3 -m unittest tests.test_vibe_memory_hooks -v`

Expected: missing module failure.

- [ ] **Step 3: Implement stable entries and structural merge**

```python
MANAGED_SIGNATURE = "vibe-memory hook --agent"
EVENTS = ("SessionStart", "UserPromptSubmit", "PreCompact", "PostCompact", "Stop")


def command(runtime: pathlib.Path, agent: str, event: str) -> str:
    cli = runtime / "scripts/vibe_memory_cli.py"
    return f'/usr/bin/python3 "{cli}" hook --agent {agent} --event {event}'


def merge_document(value: dict[str, object], agent: str, runtime: pathlib.Path) -> dict[str, object]:
    result = copy.deepcopy(value)
    hook_map = result.setdefault("hooks", {})
    for event in EVENTS:
        entries = hook_map.setdefault(event, [])
        entries[:] = [entry for entry in entries if MANAGED_SIGNATURE not in json.dumps(entry)]
        entries.append({"hooks": [{"type": "command", "command": command(runtime, agent, event), "timeout": 10, "statusMessage": "Updating shared memory context"}]})
    return result
```

Add `load_document`, `write_with_backup`, `status`, and `remove_managed_entries`. Reject a non-object root or non-object `hooks` without writing.

- [ ] **Step 4: Run hook and existing config-merge tests**

Run: `python3 -m unittest tests.test_vibe_memory_hooks tests.test_memory_review -v`

Expected: all pass; custom hooks survive install and uninstall.

- [ ] **Step 5: Commit user-hook management**

```bash
git add scripts/vibe_memory_hooks.py tests/test_vibe_memory_hooks.py
git commit -m "feat: manage global codex and claude hooks"
```

### Task 6: Expose the hook router command

**Files:**
- Create: `scripts/vibe_memory_cli.py`
- Modify: `scripts/vibe_memory_router.py`
- Create: `tests/test_vibe_memory_cli.py`

- [ ] **Step 1: Write failing CLI hook tests**

```python
def test_hook_returns_personal_context_for_unregistered_cwd(self) -> None:
    result = self.run_cli("hook", "--agent", "codex", "--event", "UserPromptSubmit", stdin=self.fixture)
    self.assertEqual(result.returncode, 0)
    output = json.loads(result.stdout)
    self.assertIn("additionalContext", output["hookSpecificOutput"])
    self.assertNotIn("codex/codex_long_memory.md", output["hookSpecificOutput"]["additionalContext"])

def test_duplicate_hook_is_successful_noop(self) -> None:
    first = self.run_cli("hook", "--agent", "codex", "--event", "Stop", stdin=self.fixture)
    second = self.run_cli("hook", "--agent", "codex", "--event", "Stop", stdin=self.fixture)
    self.assertEqual((first.returncode, second.returncode), (0, 0))
    self.assertEqual(json.loads(second.stdout)["status"], "duplicate")
```

- [ ] **Step 2: Verify CLI tests fail**

Run: `python3 -m unittest tests.test_vibe_memory_cli -v`

Expected: CLI file is missing.

- [ ] **Step 3: Implement `hook` and fail-open output**

```python
def hook_command(args: argparse.Namespace) -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        result = router.handle_event(args.agent, args.event, payload, pathlib.Path.cwd())
        print(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        print(json.dumps({"status": "degraded", "error": str(error)}, ensure_ascii=False))
    return 0
```

`handle_event` normalizes, claims the idempotency key, resolves the registry,
refreshes the applicable queue, writes context packets only for a registered
project, and returns additional context. It must never create `cwd/codex` for
an unregistered directory.

- [ ] **Step 4: Run CLI, router, and event tests**

Run: `python3 -m unittest tests.test_vibe_memory_cli tests.test_vibe_memory_router tests.test_vibe_memory_events -v`

Expected: all pass, including fail-open behavior.

- [ ] **Step 5: Commit the shared hook entry point**

```bash
git add scripts/vibe_memory_cli.py scripts/vibe_memory_router.py tests/test_vibe_memory_cli.py
git commit -m "feat: expose shared memory hook router"
```

### Task 7: Install a versioned runtime and LaunchAgent

**Files:**
- Create: `templates/macos/com.noema.vibe-memory.plist`
- Create: `scripts/vibe_memory_install.py`
- Create: `tests/test_vibe_memory_install.py`

- [ ] **Step 1: Write failing install-layout tests**

```python
def test_install_copies_release_and_switches_current(self) -> None:
    result = installer.install_runtime(self.source, self.paths)
    self.assertEqual(result["version"], "1.0.0")
    self.assertTrue((self.paths.install_root / "releases/1.0.0/scripts/memory_review_server.py").is_file())
    self.assertEqual((self.paths.install_root / "current").resolve().name, "1.0.0")

def test_launch_agent_binds_loopback_and_uses_current_runtime(self) -> None:
    text = installer.render_launch_agent(self.paths, port=8897)
    self.assertIn("127.0.0.1", text)
    self.assertIn("Application Support/VibeMemory/current", text)
    self.assertNotIn("/Users/stephenbo", text)
```

- [ ] **Step 2: Verify installer tests fail**

Run: `python3 -m unittest tests.test_vibe_memory_install -v`

Expected: missing installer module.

- [ ] **Step 3: Implement atomic runtime and plist rendering**

The installer copies `scripts/`, `templates/`, `docs/`, `README.md`, and
`release.json`, plus `LICENSE` when present, into a temporary release directory.
It validates the manifest, renames the directory to `releases/1.0.0`, then
atomically replaces the `current` symlink. It creates directories with `0700`
and private files with `0600`. Task 14 makes `LICENSE` mandatory for the public
release gate.

Use this plist program contract:

```xml
<array>
  <string>/usr/bin/python3</string>
  <string>${RUNTIME}/scripts/memory_review_server.py</string>
</array>
<key>EnvironmentVariables</key>
<dict>
  <key>MEMORY_REVIEW_HOST</key><string>127.0.0.1</string>
  <key>MEMORY_REVIEW_PORT</key><string>${PORT}</string>
</dict>
<key>KeepAlive</key><true/>
<key>RunAtLoad</key><true/>
```

Render `${RUNTIME}` and `${PORT}` with `string.Template`; validate the result
with `plistlib.loads` before writing.

- [ ] **Step 4: Run installer tests and syntax checks**

Run: `python3 -m unittest tests.test_vibe_memory_install -v && python3 -m py_compile scripts/vibe_memory_install.py`

Expected: all pass and the module compiles.

- [ ] **Step 5: Commit runtime installation**

```bash
git add templates/macos/com.noema.vibe-memory.plist scripts/vibe_memory_install.py tests/test_vibe_memory_install.py
git commit -m "feat: install versioned macos runtime"
```

### Task 8: Add bootstrap, status, doctor, open, and project commands

**Files:**
- Create: `install.sh`
- Modify: `scripts/vibe_memory_cli.py`
- Modify: `scripts/vibe_memory_install.py`
- Modify: `tests/test_vibe_memory_cli.py`
- Modify: `tests/test_vibe_memory_install.py`

- [ ] **Step 1: Write failing lifecycle-command tests**

```python
def test_doctor_reports_runtime_hooks_service_and_data(self) -> None:
    output = json.loads(self.run_cli("doctor", "--json").stdout)
    self.assertEqual(set(output), {"runtime", "codex_hooks", "claude_hooks", "service", "data"})

def test_project_register_delegates_to_existing_registry(self) -> None:
    root = self.temp / "notes"
    root.mkdir()
    output = json.loads(self.run_cli("project", "register", str(root)).stdout)
    self.assertEqual(pathlib.Path(output["current_project"]), root.resolve())
```

- [ ] **Step 2: Verify lifecycle tests fail**

Run: `python3 -m unittest tests.test_vibe_memory_cli tests.test_vibe_memory_install -v`

Expected: argparse rejects the new commands.

- [ ] **Step 3: Implement CLI lifecycle and bootstrap**

`install.sh` contains only:

```bash
#!/usr/bin/env bash
set -euo pipefail
SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec /usr/bin/python3 "${SOURCE_ROOT}/scripts/vibe_memory_cli.py" install --source-root "${SOURCE_ROOT}" "$@"
```

Add CLI subcommands `install`, `status`, `doctor`, `open`, nested `project
register|unregister|list|init`, and nested `memory propose|list|show|approve`.
Memory commands delegate to `memory_review_queue` so injected context names a
stable CLI rather than a clone path. `open` uses `/usr/bin/open` only after the
local health endpoint succeeds. `doctor --json` returns stable keys and nonzero
exit status only for actionable failures.

- [ ] **Step 4: Run lifecycle and existing project tests**

Run: `python3 -m unittest tests.test_vibe_memory_cli tests.test_vibe_memory_install tests.test_memory_review -v && bash -n install.sh`

Expected: all pass and the bootstrap script parses.

- [ ] **Step 5: Commit bootstrap and CLI**

```bash
git add install.sh scripts/vibe_memory_cli.py scripts/vibe_memory_install.py tests/test_vibe_memory_cli.py tests/test_vibe_memory_install.py
git commit -m "feat: add macos memory manager bootstrap"
```

### Task 9: Add first-run settings and personal short-memory retention

**Files:**
- Create: `scripts/vibe_memory_settings.py`
- Create: `tests/test_vibe_memory_settings.py`
- Modify: `scripts/vibe_memory_router.py`
- Modify: `scripts/memory_review_server.py:150-280`
- Modify: `tests/test_ui_design_server.py`

- [ ] **Step 1: Write failing settings, retention, and API tests**

```python
def test_defaults_keep_approval_and_loopback_invariants(self) -> None:
    value = settings.default_settings()
    self.assertTrue(value["codex_hooks_enabled"])
    self.assertTrue(value["automatic_candidate_checks"])
    self.assertTrue(value["formal_memory_requires_approval"])
    self.assertEqual(value["service_host"], "127.0.0.1")

def test_prune_removes_only_expired_personal_short_entries(self) -> None:
    removed = settings.prune_personal_short(self.short_memory, today=date(2026, 7, 30))
    self.assertEqual(removed, ["temporary-project-context"])
    self.assertIn("active temporary context", self.short_memory.read_text())

def test_first_run_api_cannot_disable_formal_approval(self) -> None:
    response = self.api_post("/api/settings/first-run", {"formal_memory_requires_approval": False})
    self.assertEqual(response.status, 400)
```

- [ ] **Step 2: Verify the settings tests fail**

Run: `python3 -m unittest tests.test_vibe_memory_settings tests.test_ui_design_server -v`

Expected: missing settings module and first-run endpoint.

- [ ] **Step 3: Implement versioned settings and retention**

```python
def default_settings() -> dict[str, object]:
    return {
        "schema_version": 1,
        "first_run_complete": False,
        "codex_hooks_enabled": True,
        "claude_hooks_enabled": False,
        "automatic_candidate_checks": True,
        "personal_short_retention_days": 30,
        "start_at_login": True,
        "formal_memory_requires_approval": True,
        "service_host": "127.0.0.1",
        "service_port": 8897,
    }
```

Validate the fixed safety values as `True` for formal approval and
`127.0.0.1` for the service host. Personal short entries eligible for expiry
carry `expires_on: YYYY-MM-DD`; pruning creates a source backup, removes only
expired structured sections, and writes atomically. The router runs pruning on
`SessionStart`, never on every prompt.

Implement `load_settings(paths)` and `save_settings(paths, value)` against
`paths.install_root / "config.json"`; both validate schema 1 and use atomic
writes. When `automatic_candidate_checks` is false, context still loads active
memory but omits the candidate-generation reminder.

Add `GET /api/settings` and `POST /api/settings/first-run`. The first-run page
edits client enablement, automatic checks, retention, login startup, port, and
an optional workspace to register. Saving calls the hook reconciler and
LaunchAgent reconciler so client and login choices take effect immediately.
It never registers the clone implicitly.

- [ ] **Step 4: Run settings, server, router, and review tests**

Run: `python3 -m unittest tests.test_vibe_memory_settings tests.test_ui_design_server tests.test_vibe_memory_router tests.test_memory_review -v`

Expected: all pass; unsafe settings receive HTTP 400.

- [ ] **Step 5: Commit first-run settings**

```bash
git add scripts/vibe_memory_settings.py scripts/vibe_memory_router.py scripts/memory_review_server.py tests/test_vibe_memory_settings.py tests/test_ui_design_server.py
git commit -m "feat: add memory manager first-run settings"
```

## Milestone 3: Complete migration compatibility

### Task 10: Inventory all current control-plane data

**Files:**
- Create: `scripts/vibe_memory_migration.py`
- Create: `tests/test_vibe_memory_migration.py`

- [ ] **Step 1: Write a failing inventory golden test**

```python
def test_inventory_covers_every_control_plane(self) -> None:
    fixture = build_complete_legacy_fixture(self.temp)
    result = migration.inventory(fixture.paths, fixture.registry)
    self.assertEqual(set(result), {
        "personal_memory", "projects", "memory_review", "design_preferences",
        "ui_design_approvals", "ui_skills", "loop", "worktrees", "legacy_hooks",
    })
    self.assertEqual(result["projects"]["registered"], 2)
    self.assertEqual(result["ui_skills"]["published"], 1)
```

- [ ] **Step 2: Verify the inventory test fails**

Run: `python3 -m unittest tests.test_vibe_memory_migration -v`

Expected: missing migration module.

- [ ] **Step 3: Implement read-only inventory**

```python
def inventory(paths: RuntimePaths, registry: dict[str, object]) -> dict[str, object]:
    projects = valid_project_roots(registry)
    return {
        "personal_memory": inspect_personal(paths.personal_memory),
        "projects": inspect_projects(projects),
        "memory_review": inspect_review_state(projects, paths.personal_memory),
        "design_preferences": inspect_preferences(projects, paths.ui_design_home),
        "ui_design_approvals": inspect_design_approvals(projects),
        "ui_skills": inspect_ui_skills(paths.ui_design_home),
        "loop": inspect_loop(projects),
        "worktrees": inspect_worktrees(paths.worktree_manager),
        "legacy_hooks": inspect_legacy_hooks(projects),
    }
```

Inspectors return counts, schema versions, and errors; they never create or
rewrite files. Invalid JSON is reported with its path.

- [ ] **Step 4: Run migration and subsystem tests**

Run: `python3 -m unittest tests.test_vibe_memory_migration tests.test_ui_design_preferences tests.test_ui_skill_registry tests.test_worktree_flow -v`

Expected: all pass and the fixture reports all nine control-plane areas.

- [ ] **Step 5: Commit inventory support**

```bash
git add scripts/vibe_memory_migration.py tests/test_vibe_memory_migration.py
git commit -m "feat: inventory existing memory control plane"
```

### Task 11: Preview and apply legacy per-project hook migration

**Files:**
- Modify: `scripts/vibe_memory_migration.py`
- Modify: `scripts/vibe_memory_cli.py`
- Modify: `tests/test_vibe_memory_migration.py`

- [ ] **Step 1: Write failing preview/apply tests**

```python
def test_preview_is_read_only_and_targets_only_managed_entries(self) -> None:
    before = digest_tree(self.project)
    preview = migration.preview_legacy_hooks([self.project])
    self.assertEqual(digest_tree(self.project), before)
    self.assertEqual(preview[0]["managed_entries"], 5)
    self.assertEqual(preview[0]["custom_entries"], 1)

def test_apply_preserves_custom_hooks_and_backs_up_scripts(self) -> None:
    result = migration.apply_legacy_hooks([self.project])
    self.assertIn("custom-hook", (self.project / ".codex/hooks.json").read_text())
    self.assertNotIn("shared_memory_hook.py", (self.project / ".codex/hooks.json").read_text())
    self.assertTrue(result[0]["backups"])
```

- [ ] **Step 2: Verify migration behavior fails**

Run: `python3 -m unittest tests.test_vibe_memory_migration -v`

Expected: preview/apply functions are missing.

- [ ] **Step 3: Implement managed-only migration**

Identify legacy manager entries by command references to
`.codex/hooks/shared_memory_hook.py` or `.claude/hooks/shared_memory_hook.py`.
`preview_legacy_hooks` returns exact target paths and counts. `apply_legacy_hooks`
backs up changed config and scripts, removes only matching entries, validates
JSON, writes atomically, and records an audit result. UI design gate entries
remain project-local in v1 and are not removed by the memory-hook migration.

Add `vibe-memory migrate preview` and `vibe-memory migrate apply`; `apply`
requires `--approved` and refuses otherwise.

- [ ] **Step 4: Run migration, hook, and UI gate regressions**

Run: `python3 -m unittest tests.test_vibe_memory_migration tests.test_vibe_memory_hooks tests.test_ui_design_gate -v`

Expected: all pass; the UI design hard gate is unchanged.

- [ ] **Step 5: Commit managed legacy migration**

```bash
git add scripts/vibe_memory_migration.py scripts/vibe_memory_cli.py tests/test_vibe_memory_migration.py
git commit -m "feat: migrate legacy project memory hooks"
```

### Task 12: Validate complete review-console compatibility from installed runtime

**Files:**
- Modify: `scripts/vibe_memory_migration.py`
- Modify: `scripts/vibe_memory_install.py`
- Modify: `scripts/memory_review_server.py:1-110`
- Create: `tests/test_installed_control_plane.py`

- [ ] **Step 1: Write failing installed-runtime compatibility tests**

```python
def test_installed_runtime_reads_all_existing_control_plane_data(self) -> None:
    runtime = self.install_complete_fixture()
    result = run_installed_doctor(runtime, self.fixture_home)
    self.assertEqual(result["memory_review"], "ok")
    self.assertEqual(result["projects"], "ok")
    self.assertEqual(result["design_preferences"], "ok")
    self.assertEqual(result["ui_design_approvals"], "ok")
    self.assertEqual(result["ui_skills"], "ok")
    self.assertEqual(result["loop"], "ok")

def test_server_binds_configured_loopback_port(self) -> None:
    self.assertEqual(server_address({"MEMORY_REVIEW_HOST": "127.0.0.1", "MEMORY_REVIEW_PORT": "19097"}), ("127.0.0.1", 19097))
```

- [ ] **Step 2: Verify installed-runtime tests fail**

Run: `python3 -m unittest tests.test_installed_control_plane -v`

Expected: missing compatibility validator or environment-based server address.

- [ ] **Step 3: Add compatibility validation and environment-based server address**

```python
def server_address(environ: dict[str, str] | None = None) -> tuple[str, int]:
    values = environ or os.environ
    host = values.get("MEMORY_REVIEW_HOST", "127.0.0.1")
    port = int(values.get("MEMORY_REVIEW_PORT", "8897"))
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("memory review server must bind to loopback")
    return host, port
```

Add `validate_control_plane` that calls the existing memory, project, UI
preference, UI gate, UI Skill, Loop, and worktree readers against inventory
paths and returns per-area status without rewriting data. Installation must
run it before switching `current`.

- [ ] **Step 4: Run the complete existing regression suite**

Run: `python3 -m unittest discover -s tests -v`

Expected: all existing and new tests pass.

- [ ] **Step 5: Commit installed control-plane validation**

```bash
git add scripts/vibe_memory_migration.py scripts/vibe_memory_install.py scripts/memory_review_server.py tests/test_installed_control_plane.py
git commit -m "test: verify installed control plane compatibility"
```

### Task 13: Implement update, rollback, repair, and safe uninstall

**Files:**
- Modify: `scripts/vibe_memory_install.py`
- Modify: `scripts/vibe_memory_hooks.py`
- Modify: `scripts/vibe_memory_cli.py`
- Modify: `tests/test_vibe_memory_install.py`
- Modify: `tests/test_vibe_memory_hooks.py`

- [ ] **Step 1: Write failing lifecycle-safety tests**

```python
def test_failed_update_keeps_previous_current_release(self) -> None:
    self.install("1.0.0")
    with self.assertRaises(installer.InstallError):
        self.install("1.1.0", fail_validation=True)
    self.assertEqual((self.paths.install_root / "current").resolve().name, "1.0.0")

def test_rollback_switches_runtime_without_reverting_memory(self) -> None:
    memory = self.paths.personal_memory / "long.md"
    memory.write_text("approved after upgrade", encoding="utf-8")
    installer.rollback(self.paths)
    self.assertEqual(memory.read_text(encoding="utf-8"), "approved after upgrade")

def test_uninstall_removes_only_managed_assets(self) -> None:
    result = installer.uninstall(self.paths, remove_data=False)
    self.assertTrue((self.paths.personal_memory / "long.md").exists())
    self.assertNotIn("vibe-memory hook", self.codex_hooks.read_text())
    self.assertIn("custom-hook", self.codex_hooks.read_text())
```

- [ ] **Step 2: Verify lifecycle tests fail**

Run: `python3 -m unittest tests.test_vibe_memory_install tests.test_vibe_memory_hooks -v`

Expected: update/rollback/uninstall APIs are missing.

- [ ] **Step 3: Implement atomic lifecycle operations**

Maintain `state/install.json` with `current_version`, `previous_version`, hook
protocol, data schema, port, and installed clients. Update validates before
switching. Rollback changes the runtime link and restores the matching managed
config backup but leaves memory data intact. Repair re-renders only managed
assets. Uninstall unloads the LaunchAgent, removes managed hook entries, CLI
link, and selected runtime; `--remove-data` is rejected unless paired with
`--approved-data-deletion` and explicit resolved data paths.

- [ ] **Step 4: Run lifecycle and full regression tests**

Run: `python3 -m unittest tests.test_vibe_memory_install tests.test_vibe_memory_hooks tests.test_vibe_memory_cli -v && python3 -m unittest discover -s tests -v`

Expected: all pass.

- [ ] **Step 5: Commit safe lifecycle management**

```bash
git add scripts/vibe_memory_install.py scripts/vibe_memory_hooks.py scripts/vibe_memory_cli.py tests/test_vibe_memory_install.py tests/test_vibe_memory_hooks.py
git commit -m "feat: add safe update rollback and uninstall"
```

## Milestone 4: Public release gate

### Task 14: Separate public assets from runtime/private data

**Files:**
- Create: `LICENSE`
- Create: `SECURITY.md`
- Create: `docs/PRIVACY.md`
- Create: `templates/loop/config.example.json`
- Modify: `.gitignore`
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`
- Modify: `README.md`
- Modify: `docs/MEMORY_REVIEW_USER_GUIDE.zh-CN.md`
- Untrack, preserve locally: `.codex/hooks.json`
- Untrack, preserve locally: `.claude/settings.json`
- Untrack, preserve locally: `.loop/config.json`
- Untrack, preserve locally: `codex/codex_long_memory.md`
- Untrack, preserve locally: `codex/codex_short_memory.md`
- Untrack, preserve locally: `codex/memory_proposals.md`
- Create: `scripts/public_release_check.py`
- Create: `tests/test_public_release_check.py`

- [ ] **Step 1: Write failing repository-hygiene tests**

```python
def test_active_release_files_contain_no_personal_absolute_path(self) -> None:
    violations = public_release_check.scan_tree(ROOT)
    self.assertEqual(violations, [])

def test_runtime_state_patterns_are_ignored(self) -> None:
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("codex/memory_review_queue.json", "codex/memory_review_state.json", "*.bak.*"):
        self.assertIn(pattern, text)
```

- [ ] **Step 2: Run hygiene tests and capture current violations**

Run: `python3 -m unittest tests.test_public_release_check -v`

Expected: failure listing current personal absolute paths and missing public documents.

- [ ] **Step 3: Add public policy files and deterministic scanner**

Use the standard MIT license text with copyright `2026 vibe_coding_manage_platform contributors`.
`SECURITY.md` documents private reporting and forbids including memory files or
credentials in issues. `docs/PRIVACY.md` states loopback-only operation, no
secondary model/API calls, approval gates, retained paths, export, and uninstall.

`scan_tree` checks active source, templates, top-level docs, and release files
for `/Users/`, credential assignments, tokens, verification codes, and private
memory headings. It excludes historical plans/specs and test fixtures only when
their content is synthetic. Remove active hard-coded personal paths and replace
examples with home-relative or `/path/to/project` forms.

Preserve the current local files while removing them from the distributable
index:

```bash
git rm --cached .codex/hooks.json .claude/settings.json .loop/config.json
git rm --cached codex/codex_long_memory.md codex/codex_short_memory.md codex/memory_proposals.md
```

Add those runtime paths and `codex/ui_design/` to `.gitignore`. Keep generic
repository instructions in `AGENTS.md` and `CLAUDE.md`, but replace personal
paths with `$HOME`-relative or manager-CLI commands. The safe Loop example has
no host, credentials, database, bucket, production path, or user worktree root.

- [ ] **Step 4: Run hygiene and documentation checks**

Run: `python3 -m unittest tests.test_public_release_check -v && python3 scripts/public_release_check.py --tree .`

Expected: tests pass and the scanner prints `public release tree check: ok`.

- [ ] **Step 5: Commit public repository hygiene**

```bash
git add LICENSE SECURITY.md docs/PRIVACY.md templates/loop/config.example.json .gitignore AGENTS.md CLAUDE.md README.md docs/MEMORY_REVIEW_USER_GUIDE.zh-CN.md scripts/public_release_check.py tests/test_public_release_check.py
git commit -m "docs: prepare public memory manager distribution"
```

### Task 15: Add clean-install and legacy-migration E2E tests

**Files:**
- Create: `tests/test_macos_install_e2e.py`
- Create: `tests/fixtures/legacy_install/README.md`
- Modify: `tests/test_installed_control_plane.py`

- [ ] **Step 1: Write the end-to-end scenarios**

```python
@unittest.skipUnless(sys.platform == "darwin", "macOS installer contract")
class MacOSInstallE2ETest(unittest.TestCase):
    def test_clean_install_codex_only(self) -> None:
        result = run_install(self.home, clients=["codex"])
        self.assertEqual(result.returncode, 0)
        self.assertTrue((self.home / "Library/Application Support/VibeMemory/current").exists())
        self.assertIn("vibe-memory hook --agent codex", (self.home / ".codex/hooks.json").read_text())

    def test_legacy_install_preserves_every_control_plane(self) -> None:
        fixture = create_legacy_install(self.home)
        before = fixture.business_data_digest()
        result = run_install(self.home, clients=["codex", "claude-code"], migrate=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(fixture.business_data_digest(), before)
        self.assert_control_plane_pages_respond()
```

- [ ] **Step 2: Run E2E tests before wiring helpers**

Run: `python3 -m unittest tests.test_macos_install_e2e -v`

Expected: failures for missing install helpers and fixture builder.

- [ ] **Step 3: Build isolated HOME fixtures and service probes**

Run the installer in a temporary HOME with `VIBE_MEMORY_TEST_MODE=1` so
LaunchAgent commands are recorded rather than changing the developer account.
The complete legacy fixture contains personal memory, two registered projects,
pending and active memories, preferences, a design approval, a published UI
Skill, Loop config, worktree state, custom hooks, and legacy managed hooks.
Probe `/health` and the existing memory, project, preferences, UI approval, UI
Skill, and documentation endpoints on an ephemeral loopback port.

- [ ] **Step 4: Run all unit and E2E tests**

Run: `python3 -m unittest discover -s tests -v`

Expected: all tests pass; macOS E2E does not touch the real home directory.

- [ ] **Step 5: Commit public installation E2E coverage**

```bash
git add tests/test_macos_install_e2e.py tests/fixtures/legacy_install tests/test_installed_control_plane.py
git commit -m "test: cover clean install and legacy migration"
```

### Task 16: Run the release-candidate verification gate

**Files:**
- Create: `scripts/verify_release.py`
- Create: `docs/RELEASE_CHECKLIST.md`
- Modify: `README.md`
- Modify: `release.json`
- Modify: `tests/test_public_release_check.py`
- Test: complete `tests/` suite

- [ ] **Step 1: Write a failing release-gate test**

```python
def test_release_gate_has_all_required_checks(self) -> None:
    result = verify_release.checks(ROOT)
    self.assertEqual(set(result), {
        "manifest", "python", "unit_tests", "install_e2e", "public_tree",
        "plist", "loopback", "permissions", "codex_hook", "claude_hook",
        "control_plane", "rollback", "uninstall",
    })
```

- [ ] **Step 2: Verify the release-gate test fails**

Run: `python3 -m unittest tests.test_public_release_check.PublicReleaseCheckTest.test_release_gate_has_all_required_checks -v`

Expected: `verify_release` module or `checks` is missing.

- [ ] **Step 3: Implement one release verification entry point**

`scripts/verify_release.py` executes the manifest validation, Python compile,
full unit suite, macOS isolated install E2E, public tree scan, plist parse,
loopback and permissions assertions, both client hook smoke tests, complete
control-plane validation, rollback, and uninstall checks. It prints a JSON
result with one key per required check and exits nonzero if any value is not
`ok`.

`docs/RELEASE_CHECKLIST.md` requires manual confirmation on both Apple Silicon
and Intel, a fresh Codex session, an optional fresh Claude Code session, review
console visual smoke test, and a separate Git-history secret scan. A material
history finding stops publication and requires explicit approval before any
history rewrite.

- [ ] **Step 4: Execute the complete release gate**

Run: `python3 scripts/verify_release.py`

Expected: exit `0`; every JSON check is `ok`.

Run: `git diff --check && git status --short`

Expected: no whitespace errors; only intentionally uncommitted local runtime
data may remain, and none is staged.

- [ ] **Step 5: Commit release verification**

```bash
git add scripts/verify_release.py docs/RELEASE_CHECKLIST.md README.md release.json tests/test_public_release_check.py
git commit -m "chore: add universal memory manager release gate"
```

## Final execution checkpoint

After Task 16:

1. Run `python3 scripts/verify_release.py` once more from a clean feature worktree.
2. Review `git log --oneline` to confirm each task is independently reversible.
3. Do not rewrite Git history, publish a release, push a public repository,
   merge the main branch, or deploy production without the user's explicit
   command for that separate external action.
4. Present the clean-install command, migration report, release-gate JSON, and
   known macOS compatibility results for final product acceptance.
