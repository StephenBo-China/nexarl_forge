# UI Design Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the memory review console into a local UI design control plane that manages design preferences and UI skills for Codex and Claude Code, and enforces user approval before formal frontend implementation.

**Architecture:** Keep the existing standard-library Python service, but move new behavior into focused domain modules. Persist immutable skill packages globally under `~/.codex/ui_design`, persist project design state under `codex/ui_design`, expose the same operations through CLI and HTTP adapters, and enforce approval with generated project-local `PreToolUse` hooks. Publish Codex and Claude skill snapshots as a two-target transaction with rollback.

**Tech Stack:** Python 3.10+ standard library, `unittest`, `http.server`, JSON/JSONL, SHA-256, `zipfile`, `urllib`, filesystem atomic rename, HTML/CSS/vanilla JavaScript.

---

## Delivery Phases

1. **Foundation:** atomic JSON store, global/project preferences, project UI initialization.
2. **Skill control plane:** immutable packages, validation, drafts, approval, publication, rollback, unmanaged discovery, CLI/API/UI.
3. **Managed workflow skills:** `ui-design-workflow`, pinned `frontend-design`, pinned UI UX Pro Max variants.
4. **Approval gate:** design packages, project gate modes, path decisions, generated hooks, design review UI, end-to-end verification.

Each phase ends with a passing full suite and a feature-branch commit. Do not merge `master`, update production, or deploy staging while executing this plan without the user's later explicit authorization.

## File Map

### New domain modules

- `scripts/ui_design_store.py` — paths, atomic JSON/JSONL writes, digests, backups, and lock files.
- `scripts/ui_design_preferences.py` — preference schemas and global/project merge semantics.
- `scripts/ui_skill_registry.py` — draft/version/deployment state machine and immutable package metadata.
- `scripts/ui_skill_validator.py` — safe package traversal, metadata, references, script inventory, license, and conflict checks.
- `scripts/ui_skill_sources.py` — GitHub, local directory, ZIP, and editor-created import adapters.
- `scripts/ui_skill_publisher.py` — staged two-agent publication, verification, rollback, disable, and restore.
- `scripts/ui_skill_discovery.py` — read-only scan of managed and unmanaged skill locations.
- `scripts/ui_design_gate.py` — design packages, approval digests, gate modes, path classification, and tool decisions.
- `scripts/ui_design_cli.py` — nested CLI parser and dispatch used by `memory_review.py`.

### New managed templates

- `templates/ui_design/skills/ui-design-workflow/SKILL.md` — stable orchestration workflow.
- `templates/ui_design/skills/ui-design-workflow/agents/openai.yaml` — Codex UI metadata.
- `templates/ui_design/skills/ui-design-workflow/references/design-package-schema.md` — package contract.
- `templates/ui_design/skills/ui-design-workflow/references/preference-schema.md` — effective preference contract.
- `templates/ui_design/ui_design_gate_hook.py` — generated cross-agent `PreToolUse` hook.

### Existing files to modify

- `scripts/memory_project.py` — initialize/upgrade project UI state, managed instructions, hook configuration, project status.
- `scripts/memory_review.py` — delegate `ui-skill` and `ui-design` commands.
- `scripts/memory_review_server.py` — UI design APIs and console views.
- `README.md` — operator workflow, storage, CLI, approval and recovery instructions.

### New tests

- `tests/test_ui_design_store.py`
- `tests/test_ui_design_preferences.py`
- `tests/test_ui_skill_registry.py`
- `tests/test_ui_skill_publication.py`
- `tests/test_ui_design_gate.py`
- `tests/test_ui_design_server.py`
- `tests/fixtures/ui_skills/minimal/SKILL.md`
- `tests/fixtures/ui_skills/with-script/SKILL.md`
- `tests/fixtures/ui_skills/with-script/scripts/build.py`

## Phase 1: Foundation

### Task 1: Add atomic store and deterministic package digest

**Files:**
- Create: `scripts/ui_design_store.py`
- Create: `tests/test_ui_design_store.py`

- [ ] **Step 1: Write failing store tests**

```python
class UIStoreTest(unittest.TestCase):
    def test_atomic_json_round_trip_and_backup(self):
        with tempfile.TemporaryDirectory() as value:
            path = pathlib.Path(value) / "registry.json"
            store.atomic_write_json(path, {"version": 1})
            store.atomic_write_json(path, {"version": 2}, backup=True)
            self.assertEqual(store.read_json_strict(path), {"version": 2})
            backups = list(path.parent.glob("registry.json.bak.*"))
            self.assertEqual(len(backups), 1)

    def test_tree_digest_ignores_mtime_and_orders_paths(self):
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            (root / "b.txt").write_text("b", encoding="utf-8")
            (root / "a.txt").write_text("a", encoding="utf-8")
            first = store.tree_digest(root)
            os.utime(root / "a.txt", None)
            self.assertEqual(first, store.tree_digest(root))
```

- [ ] **Step 2: Run the focused test and confirm the missing-module failure**

Run: `python3 -m unittest tests.test_ui_design_store -v`

Expected: `ModuleNotFoundError: No module named 'ui_design_store'`.

- [ ] **Step 3: Implement the store primitives**

```python
def ui_design_home() -> pathlib.Path:
    return pathlib.Path(os.environ.get(
        "UI_DESIGN_HOME", pathlib.Path.home() / ".codex" / "ui_design"
    )).expanduser().resolve()

def atomic_write_json(path: pathlib.Path, value: Any, backup: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        shutil.copy2(path, timestamped_backup(path))
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        pathlib.Path(temp_name).unlink(missing_ok=True)

def tree_digest(root: pathlib.Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()
```

Also implement `read_json_strict`, `append_jsonl`, `timestamped_backup`, and a standard-library lock context using exclusive lock-file creation with timeout and stale-lock diagnostics.

- [ ] **Step 4: Run focused and full tests**

Run: `python3 -m unittest tests.test_ui_design_store -v`

Expected: all `UIStoreTest` tests pass.

Run: `python3 -m unittest discover -s tests -v`

Expected: 30 existing tests plus new store tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/ui_design_store.py tests/test_ui_design_store.py
git commit -m "feat: add UI design atomic store"
```

### Task 2: Add global preferences and field-level project overrides

**Files:**
- Create: `scripts/ui_design_preferences.py`
- Create: `tests/test_ui_design_preferences.py`

- [ ] **Step 1: Write failing merge tests**

```python
class UIPreferencesTest(unittest.TestCase):
    def test_project_override_can_inherit_replace_append_and_clear(self):
        global_value = {
            "visual": {"preferred_styles": ["editorial"], "radius": "8px"},
            "anti_preferences": ["purple AI gradients"],
        }
        override = {
            "visual.preferred_styles": {"mode": "append", "value": ["industrial"]},
            "visual.radius": {"mode": "replace", "value": "4px"},
            "anti_preferences": {"mode": "clear"},
        }
        effective = preferences.merge_preferences(global_value, override)
        self.assertEqual(effective["value"]["visual"]["preferred_styles"], ["editorial", "industrial"])
        self.assertEqual(effective["value"]["visual"]["radius"], "4px")
        self.assertEqual(effective["value"]["anti_preferences"], [])
        self.assertEqual(effective["sources"]["visual.radius"], "project")
```

- [ ] **Step 2: Verify failure**

Run: `python3 -m unittest tests.test_ui_design_preferences -v`

Expected: import failure for `ui_design_preferences`.

- [ ] **Step 3: Implement schema validation and merge behavior**

```python
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
        "typography": {"display": "", "body": "", "utility": "", "language_rules": []},
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
    "platform": {"web": {}, "ios": {}, "android": {}, "macos": {}, "mini_program": {}},
    "references": [],
    "design_principles": [],
    "anti_preferences": [],
}

def global_preferences_path() -> pathlib.Path:
    return store.ui_design_home() / "preferences.json"

def project_preferences_path(project_root: pathlib.Path) -> pathlib.Path:
    return project_root / "codex" / "ui_design" / "preferences.json"

def effective_preferences(project_root: pathlib.Path) -> dict[str, Any]:
    global_value = load_global_preferences()
    overrides = load_project_overrides(project_root)
    return merge_preferences(global_value, overrides)
```

Implement dot-path reads/writes without `eval`, reject unknown override modes, require lists for append, preserve explicit empty values, and return a `sources` map so the UI can label inherited/project values.

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.test_ui_design_preferences -v`

Expected: all preference tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/ui_design_preferences.py tests/test_ui_design_preferences.py
git commit -m "feat: add layered UI design preferences"
```

### Task 3: Initialize project UI state and report readiness

**Files:**
- Modify: `scripts/memory_project.py`
- Modify: `tests/test_memory_review.py`
- Create: `tests/test_ui_design_gate.py`

- [ ] **Step 1: Add failing initialization tests**

```python
def test_init_project_creates_safe_ui_design_defaults(self):
    with tempfile.TemporaryDirectory() as value:
        root = pathlib.Path(value)
        (root / ".git").mkdir()
        result = memory_project.init_project(root)
        config = json.loads((root / "codex/ui_design/config.json").read_text())
        self.assertEqual(config["gate_mode"], "design_package")
        self.assertFalse(config["hard_gate_enabled"])
        self.assertEqual(config["schema_version"], 1)
        self.assertTrue((root / "codex/ui_design/active-skills.json").exists())
        self.assertIn("ui_design_status", result["project"])
```

- [ ] **Step 2: Confirm the test fails because UI state is absent**

Run: `python3 -m unittest tests.test_memory_review.MemoryReviewQualityTest.test_init_project_creates_safe_ui_design_defaults -v`

Expected: missing `config.json` assertion failure.

- [ ] **Step 3: Implement defaults and status reporting**

Add to `memory_project.py`:

```python
def ui_design_config(root: pathlib.Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "enabled": True,
        "hard_gate_enabled": False,
        "gate_mode": "design_package",
        "relocked": True,
        "formal_frontend_paths": [],
        "design_artifact_paths": ["codex/ui_design/design-packages/**"],
        "generated_paths": [],
        "test_artifact_paths": [],
    }
```

Create `config.json`, `preferences.json`, `active-skills.json`, and an empty `approvals.json` only when absent. Report `not_initialized`, `configuration_required`, `locked`, or `ready` from `project_entry` without overwriting user configuration.

- [ ] **Step 4: Run project and full tests**

Run: `python3 -m unittest tests.test_memory_review -v`

Expected: project tests pass.

Run: `python3 -m unittest discover -s tests -v`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/memory_project.py tests/test_memory_review.py tests/test_ui_design_gate.py
git commit -m "feat: initialize project UI design state"
```

## Phase 2: UI Skill Control Plane

### Task 4: Add registry state machine and immutable draft/package records

**Files:**
- Create: `scripts/ui_skill_registry.py`
- Create: `tests/test_ui_skill_registry.py`

- [ ] **Step 1: Write failing registry tests**

```python
class UISkillRegistryTest(unittest.TestCase):
    def test_draft_approval_creates_immutable_version(self):
        draft = registry.create_draft(
            name="sample-ui", source={"type": "local", "path": "/fixture"},
            package_root=self.fixture, scope={"type": "global"}, targets=["codex", "claude"],
        )
        approved = registry.approve_draft(draft["id"], expected_digest=draft["digest"])
        self.assertEqual(approved["status"], "approved")
        self.assertTrue(pathlib.Path(approved["package_path"]).exists())
        with self.assertRaises(registry.InvalidTransition):
            registry.approve_draft(draft["id"], expected_digest=draft["digest"])
```

- [ ] **Step 2: Verify failure**

Run: `python3 -m unittest tests.test_ui_skill_registry -v`

Expected: missing registry module.

- [ ] **Step 3: Implement registry transitions**

Use explicit statuses:

```python
DRAFT_TRANSITIONS = {
    "draft": {"validated", "rejected"},
    "validated": {"approved", "rejected", "draft"},
    "approved": {"publishing", "rejected"},
    "publishing": {"published", "publish_failed"},
    "publish_failed": {"publishing", "rejected"},
    "published": {"disabled", "superseded"},
}
```

Store draft content beneath `drafts/<draft-id>/content`, move approved content by copy into `packages/<name>/<version-id>`, recompute the digest at every transition, and require the caller's expected digest for approval and publication.

- [ ] **Step 4: Run tests and commit**

Run: `python3 -m unittest tests.test_ui_skill_registry -v`

Expected: all registry tests pass.

```bash
git add scripts/ui_skill_registry.py tests/test_ui_skill_registry.py tests/fixtures/ui_skills/minimal/SKILL.md
git commit -m "feat: add immutable UI skill registry"
```

### Task 5: Add package validation without executing scripts

**Files:**
- Create: `scripts/ui_skill_validator.py`
- Modify: `tests/test_ui_skill_registry.py`
- Create: `tests/fixtures/ui_skills/with-script/SKILL.md`
- Create: `tests/fixtures/ui_skills/with-script/scripts/build.py`

- [ ] **Step 1: Write validation tests**

```python
def test_validator_reports_scripts_without_running_them(self):
    marker = self.temp / "executed"
    report = validator.validate_package(self.with_script, installed_names=set())
    self.assertTrue(report["valid"])
    self.assertEqual(report["scripts"][0]["path"], "scripts/build.py")
    self.assertFalse(marker.exists())

def test_validator_rejects_missing_reference_and_name_conflict(self):
    report = validator.validate_package(self.broken, installed_names={"sample-ui"})
    self.assertFalse(report["valid"])
    self.assertIn("name_conflict", {item["code"] for item in report["errors"]})
    self.assertIn("missing_reference", {item["code"] for item in report["errors"]})
```

- [ ] **Step 2: Run and see missing validator failure**

Run: `python3 -m unittest tests.test_ui_skill_registry -v`

- [ ] **Step 3: Implement safe metadata/reference/script inspection**

Implement a minimal YAML-frontmatter reader that accepts scalar metadata without adding PyYAML, validates `name` against `^[a-z0-9][a-z0-9-]{0,63}$`, requires a non-empty description, walks only regular files beneath the root, records executable bits and script extensions, extracts local Markdown references, and reports rather than executes suspicious commands.

The report shape must be stable:

```python
{
    "valid": not errors,
    "name": name,
    "description": description,
    "license": metadata.get("license", "unknown"),
    "errors": errors,
    "warnings": warnings,
    "files": files,
    "scripts": scripts,
    "network_references": network_references,
}
```

- [ ] **Step 4: Run tests and commit**

Run: `python3 -m unittest tests.test_ui_skill_registry -v`

Expected: validation and registry tests pass.

```bash
git add scripts/ui_skill_validator.py tests/test_ui_skill_registry.py tests/fixtures/ui_skills
git commit -m "feat: validate UI skill packages safely"
```

### Task 6: Add local, ZIP, editor, and injectable GitHub source adapters

**Files:**
- Create: `scripts/ui_skill_sources.py`
- Modify: `tests/test_ui_skill_registry.py`

- [ ] **Step 1: Write source adapter tests**

Test local copy, a valid ZIP, ZIP slip (`../../escape`), symlink rejection, file-count/size limits, editor content creation, and a GitHub downloader injected as a fixture function so tests never access the network.

```python
def test_zip_slip_is_rejected_before_extraction(self):
    with self.assertRaises(sources.SourceError):
        sources.import_zip(self.bad_zip, self.destination)

def test_github_adapter_records_pinned_revision(self):
    result = sources.import_github(
        "owner/repo", "skills/sample", "abc123", self.destination,
        downloader=lambda request, target: shutil.copytree(self.fixture, target),
    )
    self.assertEqual(result["revision"], "abc123")
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `python3 -m unittest tests.test_ui_skill_registry -v`

- [ ] **Step 3: Implement adapters**

Use `zipfile.ZipFile.infolist()` to validate every member before extraction. Reject absolute paths, `..`, symlinks, device files, excessive uncompressed size, excessive file count, and multiple ambiguous `SKILL.md` roots. Use `urllib.request` only in the production GitHub downloader and require an explicit pinned revision.

- [ ] **Step 4: Run and commit**

Run: `python3 -m unittest tests.test_ui_skill_registry -v`

```bash
git add scripts/ui_skill_sources.py tests/test_ui_skill_registry.py
git commit -m "feat: import UI skills from safe sources"
```

### Task 7: Implement two-target atomic publication and rollback

**Files:**
- Create: `scripts/ui_skill_publisher.py`
- Create: `tests/test_ui_skill_publication.py`

- [ ] **Step 1: Write failure-injection tests**

```python
def test_second_target_failure_restores_both_previous_versions(self):
    self.seed_target(self.codex_dir, "old")
    self.seed_target(self.claude_dir, "old")
    with self.assertRaises(publisher.PublishError):
        publisher.publish(
            self.approved,
            targets={"codex": self.codex_dir, "claude": self.claude_dir},
            replace=lambda source, target: (_ for _ in ()).throw(OSError("boom"))
            if "claude" in str(target) else os.replace(source, target),
        )
    self.assertEqual((self.codex_dir / "VERSION").read_text(), "old")
    self.assertEqual((self.claude_dir / "VERSION").read_text(), "old")
```

Also cover an explicit rollback to the previous managed version, a transactional
disable that removes only manager-owned snapshots, an external target-digest
conflict, and repeated calls with the same idempotency key. A rollback or
disable must restore both agents on second-target failure and must never remove
an unmanaged directory. Cover target resolution for global scope
(`~/.codex/skills`, `~/.claude/skills`) and project scope
(`<project>/.agents/skills`, `<project>/.claude/skills`) using injected roots so
tests never write to real agent directories.

- [ ] **Step 2: Run and confirm missing publisher failure**

Run: `python3 -m unittest tests.test_ui_skill_publication -v`

- [ ] **Step 3: Implement transactional publication**

Default target roots must be configurable for tests:

```python
DEFAULT_TARGETS = {
    "codex": lambda: pathlib.Path(os.environ.get(
        "CODEX_UI_SKILLS_DIR", pathlib.Path.home() / ".codex" / "skills")),
    "claude": lambda: pathlib.Path(os.environ.get(
        "CLAUDE_UI_SKILLS_DIR", pathlib.Path.home() / ".claude" / "skills")),
}
```

Add `resolve_targets(scope, project_root=None)` so a global registry record uses
the configurable user roots above and a project-scoped record requires an
explicit project root and returns `.agents/skills` for Codex plus
`.claude/skills` for Claude.

Implement `publish`, `rollback`, and `disable` through one transaction runner.
Stage both desired snapshots first (an empty managed-state marker for disable),
validate their digests, verify the live targets still match the registry's
expected digests, rename existing managed directories to transaction backups,
replace both, verify both, then update the registry and audit log. On any
failure, restore both backups and preserve the failed transaction report in
`deployments/`. Require an idempotency key on every mutating operation and
return the prior result for an exact retry while rejecting reuse with different
arguments.

- [ ] **Step 4: Run publication and full tests**

Run: `python3 -m unittest tests.test_ui_skill_publication -v`

Run: `python3 -m unittest discover -s tests -v`

Expected: all tests pass without touching real home skill directories.

- [ ] **Step 5: Commit**

```bash
git add scripts/ui_skill_publisher.py tests/test_ui_skill_publication.py
git commit -m "feat: publish UI skills to both agents atomically"
```

### Task 8: Add read-only unmanaged skill discovery

**Files:**
- Create: `scripts/ui_skill_discovery.py`
- Modify: `tests/test_ui_skill_publication.py`

- [ ] **Step 1: Write unmanaged discovery tests**

Cover managed match, unknown skill, changed ignored fingerprint, duplicate name with different digest, and the guarantee that scan does not modify mtimes or copy files.

- [ ] **Step 2: Implement scanner**

```python
def scan(target_roots: dict[str, list[pathlib.Path]], managed: dict[str, Any]) -> list[dict[str, Any]]:
    # Read only direct skill directories containing SKILL.md.
    # Compute tree digest and compare with managed deployment digests.
    # Return managed, unmanaged_discovered, unmanaged_ignored, or drifted.
```

Scan on explicit request, console skill-page refresh, and server startup. Persist only ignored fingerprints and scan summaries; never alter scanned skill directories.

- [ ] **Step 3: Run tests and commit**

Run: `python3 -m unittest tests.test_ui_skill_publication -v`

```bash
git add scripts/ui_skill_discovery.py tests/test_ui_skill_publication.py
git commit -m "feat: discover unmanaged agent skills"
```

### Task 9: Expose UI skill and preference operations through the CLI

**Files:**
- Create: `scripts/ui_design_cli.py`
- Modify: `scripts/memory_review.py`
- Modify: `tests/test_ui_skill_registry.py`

- [ ] **Step 1: Write parser/dispatch tests**

Test:

```text
memory_review.py ui-skill list
memory_review.py ui-skill show draft-20260725-001
memory_review.py ui-skill import --github owner/repo --path skills/x --revision abc123 --scope global --targets codex,claude
memory_review.py ui-skill validate draft-20260725-001
memory_review.py ui-skill request-revision draft-20260725-001 --reason "Clarify the trigger description" --idempotency-key revise-001
memory_review.py ui-skill approve draft-20260725-001 --digest 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef --idempotency-key approve-001
memory_review.py ui-skill publish draft-20260725-001 --digest 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef --idempotency-key publish-001
memory_review.py ui-skill rollback sample-ui --version 1.0.0+abc123 --idempotency-key rollback-001
memory_review.py ui-skill disable sample-ui --idempotency-key disable-001
memory_review.py ui-skill scan
memory_review.py ui-design preferences show --project /tmp/ui-control-fixture
memory_review.py ui-design preferences set-global --json-file /tmp/global-ui-preferences.json --idempotency-key pref-global-001
memory_review.py ui-design preferences set-project --project /tmp/ui-control-fixture --json-file /tmp/project-ui-preferences.json --idempotency-key pref-project-001
```

- [ ] **Step 2: Add nested parser and dispatch**

`memory_review.py` imports `ui_design_cli`, passes its top-level subparsers during parser construction, and returns `ui_design_cli.dispatch(args)` for `ui-skill` and `ui-design`. Add list/show/import/validate/request-revision/approve/reject/publish/rollback/disable/scan operations. All mutating commands require an idempotency key and print JSON with IDs, digest, status, and audit event.

- [ ] **Step 3: Run CLI tests and commit**

Run: `python3 -m unittest tests.test_ui_skill_registry -v`

```bash
git add scripts/ui_design_cli.py scripts/memory_review.py tests/test_ui_skill_registry.py
git commit -m "feat: add UI design control CLI"
```

### Task 10: Add HTTP APIs and console UI for preferences and UI skills

**Files:**
- Modify: `scripts/memory_review_server.py`
- Create: `tests/test_ui_design_server.py`

- [ ] **Step 1: Add failing API route tests**

Instantiate `Handler` operations through small extracted route functions rather than opening a port. Cover:

- `GET /api/ui-design/context`
- `POST /api/ui-design/preferences/global`
- `POST /api/ui-design/preferences/project`
- `GET /api/ui-skills`
- `POST /api/ui-skills/import`
- `POST /api/ui-skills/validate`
- `POST /api/ui-skills/request-revision`
- `POST /api/ui-skills/approve`
- `POST /api/ui-skills/publish`
- `POST /api/ui-skills/rollback`
- `POST /api/ui-skills/disable`
- `POST /api/ui-skills/reject`
- `POST /api/ui-skills/scan`
- `POST /api/ui-skills/ignore-unmanaged`

Verify 400 for validation errors, 403 for missing explicit confirmation, 409 for digest/idempotency conflict, and 500 only for unexpected errors. Verify every mutating route rejects a missing idempotency key and returns the stored result for an exact retry.

- [ ] **Step 2: Extract route functions and implement handlers**

Add `ui_design_get(path, query)` and `ui_design_post(path, body)` near existing project operations. `Handler` delegates to these functions, allowing unit tests without sockets.

- [ ] **Step 3: Add console tabs and renderers**

Add tabs:

- `设计偏好`
- `UI Skills`

The preference view shows global/project/effective values and source labels. The skill view shows pending, published, failed, disabled, unmanaged, ignored, source, digest, scripts, license, targets, scope, diff, and approve/request-revision/reject/publish/rollback/disable actions. HTML must escape all imported content using the existing `esc()` helper.

- [ ] **Step 4: Run server tests and commit**

Run: `python3 -m unittest tests.test_ui_design_server -v`

Run: `python3 -m unittest discover -s tests -v`

```bash
git add scripts/memory_review_server.py tests/test_ui_design_server.py
git commit -m "feat: add UI design control console"
```

## Phase 3: Managed Workflow Skills

### Task 11: Create and validate the manager-owned `ui-design-workflow` skill

**Files:**
- Create: `templates/ui_design/skills/ui-design-workflow/SKILL.md`
- Create: `templates/ui_design/skills/ui-design-workflow/agents/openai.yaml`
- Create: `templates/ui_design/skills/ui-design-workflow/references/design-package-schema.md`
- Create: `templates/ui_design/skills/ui-design-workflow/references/preference-schema.md`
- Modify: `tests/test_ui_skill_registry.py`

- [ ] **Step 1: Add failing template-contract test**

```python
def test_owned_workflow_skill_contains_required_approval_gate(self):
    root = APP_ROOT / "templates/ui_design/skills/ui-design-workflow"
    report = validator.validate_package(root, installed_names=set())
    self.assertTrue(report["valid"], report)
    text = (root / "SKILL.md").read_text(encoding="utf-8")
    self.assertIn("frontend-design", text)
    self.assertIn("ui-ux-pro-max", text)
    self.assertIn("Do not modify formal frontend business code", text)
    self.assertIn("design-package-schema.md", text)
```

- [ ] **Step 2: Author the concise orchestration skill**

The skill description must trigger for Web, app, mini-program, desktop UI, component library, style, interaction, responsive, or visual-change tasks. The body must:

1. classify the task;
2. load the effective preference and active-skill snapshots;
3. use `frontend-design` before UI UX Pro Max;
4. create the design package files;
5. stop for explicit user approval;
6. consult gate status before implementation;
7. verify visual and interaction results afterward.

Keep detailed schemas in direct references and keep `SKILL.md` below 500 lines.

- [ ] **Step 3: Validate with both project validator and system skill validator**

Run: `python3 -m unittest tests.test_ui_skill_registry -v`

Run: `python3 /Users/stephenbo/.codex/skills/.system/skill-creator/scripts/quick_validate.py templates/ui_design/skills/ui-design-workflow`

Expected: both validations pass.

- [ ] **Step 4: Commit**

```bash
git add templates/ui_design/skills/ui-design-workflow tests/test_ui_skill_registry.py
git commit -m "feat: add managed UI design workflow skill"
```

### Task 12: Add pinned bootstrap adapters for `frontend-design` and UI UX Pro Max

**Files:**
- Modify: `scripts/ui_skill_sources.py`
- Modify: `scripts/ui_design_cli.py`
- Modify: `tests/test_ui_skill_registry.py`
- Modify: `README.md`

- [ ] **Step 1: Add fixture-driven bootstrap tests**

Inject fetchers and UI UX CLI runners. Verify:

- Anthropic source is pinned and yields one common variant.
- UI UX Pro Max source and CLI version are pinned.
- UI UX generation runs only in temporary staging directories.
- Codex and Claude variants are stored under one registry record.
- runtime publication does not invoke `npx`.

- [ ] **Step 2: Implement bootstrap operations**

Add:

```text
memory_review.py ui-skill bootstrap frontend-design --revision b29e7cf65e5cb78a5ac33d582270551bc74a14eb
memory_review.py ui-skill bootstrap ui-ux-pro-max --release v2.11.0 --revision 6142b073958df645d0fb27e682428e69599386dc --cli-version 2.11.0 --expected-npm-shasum 2ff4d811cf1dded593b9d1f37bad65ffa80cb87c
memory_review.py ui-skill bootstrap ui-design-workflow
```

Bootstrap creates validated drafts only. The user still approves and publishes each draft. Production fetch/generation commands require normal sandbox/network approval. Capture source URL, revision, CLI version, command, stdout summary, variants, and digests in the draft report without storing credentials.

The initial approved pins are:

- Anthropic skills repository commit `b29e7cf65e5cb78a5ac33d582270551bc74a14eb`, path `skills/frontend-design`.
- UI UX Pro Max Git tag `v2.11.0`, commit `6142b073958df645d0fb27e682428e69599386dc`.
- npm package `ui-ux-pro-max-cli@2.11.0`, shasum `2ff4d811cf1dded593b9d1f37bad65ffa80cb87c`.

Treat a future upstream release as a new draft; never silently advance these pins.

- [ ] **Step 3: Document and test**

Run: `python3 -m unittest tests.test_ui_skill_registry -v`

```bash
git add scripts/ui_skill_sources.py scripts/ui_design_cli.py tests/test_ui_skill_registry.py README.md
git commit -m "feat: bootstrap initial UI design skills"
```

### Task 13: Publish effective context and managed agent instructions

**Files:**
- Modify: `scripts/memory_project.py`
- Modify: `tests/test_memory_review.py`

- [ ] **Step 1: Add failing managed-rule tests**

Require `AGENTS.md`, `CLAUDE.md`, and `.claude/rules/shared-memory.md` managed blocks to tell both agents to load `codex/ui_design/config.json`, effective preference/context snapshot, active skills, and current design approval for visible-interface tasks.

- [ ] **Step 2: Implement context snapshot generation**

Generate `codex/ui_design/effective-context.json` atomically from global preferences, project overrides, active skill versions, and gate status. Add this generation to project initialization/upgrade and to relevant UI preference/skill publication operations.

- [ ] **Step 3: Verify idempotent upgrades preserve user text**

Run: `python3 -m unittest tests.test_memory_review -v`

Expected: existing memory managed-rule tests and new UI context tests pass.

- [ ] **Step 4: Commit**

```bash
git add scripts/memory_project.py tests/test_memory_review.py
git commit -m "feat: publish shared UI design context"
```

## Phase 4: Design Approval Gate

### Task 14: Implement design packages and digest-bound approvals

**Files:**
- Create: `scripts/ui_design_gate.py`
- Modify: `tests/test_ui_design_gate.py`

- [ ] **Step 1: Write package lifecycle tests**

```python
def test_design_package_change_invalidates_approval(self):
    package = gate.create_design_package(self.project, "task-1", self.manifest)
    approval = gate.approve_design_package(
        self.project, "task-1", expected_digest=package["digest"]
    )
    (pathlib.Path(package["root"]) / "interaction-spec.md").write_text("changed")
    status = gate.gate_status(self.project, task_id="task-1")
    self.assertEqual(status["decision"], "deny_invalidated_approval")
    self.assertNotEqual(status["current_digest"], approval["digest"])
```

Also test create, reject, revision, explicit invalidation, scope change, duplicate mutation idempotency, and missing package.

- [ ] **Step 2: Implement package and approval functions**

Use a manifest with:

```json
{
  "schema_version": 1,
  "task_id": "task-1",
  "title": "Checkout redesign",
  "classification": "visual_change",
  "pages": ["checkout"],
  "components": ["CheckoutForm"],
  "allowed_file_patterns": ["web/src/checkout/**"],
  "design_files": ["design-brief.md", "interaction-spec.md", "responsive-spec.md"],
  "status": "pending_approval"
}
```

Compute approval digest over the normalized manifest and every declared design file. Reject undeclared traversal and absolute patterns.

- [ ] **Step 3: Run and commit**

Run: `python3 -m unittest tests.test_ui_design_gate -v`

```bash
git add scripts/ui_design_gate.py tests/test_ui_design_gate.py
git commit -m "feat: add digest-bound UI design approvals"
```

### Task 15: Implement project gate modes and path/tool decisions

**Files:**
- Modify: `scripts/ui_design_gate.py`
- Modify: `tests/test_ui_design_gate.py`

- [ ] **Step 1: Add gate decision tests**

Test:

- pure backend path bypass;
- design artifact allowed while locked;
- formal frontend denied before approval;
- approved package allows only declared paths;
- modified design digest denies;
- `project_global` approval unlocks all configured frontend paths;
- relock and switch to `design_package` deny immediately;
- corrupt configuration fails closed only for formal frontend mutation;
- unresolved mutating shell command is denied while locked with advice to use `apply_patch`.

- [ ] **Step 2: Implement path and tool-input extraction**

```python
def decide_tool_use(project_root: pathlib.Path, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    paths = extract_candidate_paths(tool_name, tool_input)
    mutation = classify_mutation(tool_name, tool_input)
    # Allow read-only/non-visual operations.
    # Allow configured design artifact paths while locked.
    # Apply project_global or design_package approval to formal frontend paths.
    # Deny unresolved mutating commands while locked.
```

Support `apply_patch` patch headers, `Edit`/`Write` file fields, unified `Bash`/`exec_command`, and common MCP filesystem write path keys. Normalize paths beneath the project root and reject traversal.

Represent the `project_global` baseline with an ordinary design package whose
task ID is recorded in `config.json` as `project_global_baseline_task`. Add
`approve_project_baseline(project_root, task_id, expected_digest,
idempotency_key)`; it may run only in `project_global` mode, records the package
digest in `approvals.json`, clears `relocked`, and remains valid until explicit
relock or a mode change. Changing the baseline package does not silently update
the approval: gate status reports `deny_invalidated_approval` until the revised
baseline is approved again.

- [ ] **Step 3: Run tests and commit**

Run: `python3 -m unittest tests.test_ui_design_gate -v`

```bash
git add scripts/ui_design_gate.py tests/test_ui_design_gate.py
git commit -m "feat: enforce configurable UI design gates"
```

### Task 16: Generate and configure cross-agent `PreToolUse` hooks

**Files:**
- Create: `templates/ui_design/ui_design_gate_hook.py`
- Modify: `scripts/memory_project.py`
- Modify: `tests/test_memory_review.py`
- Modify: `tests/test_ui_design_gate.py`

- [ ] **Step 1: Add hook protocol tests**

Feed representative Codex and Claude `PreToolUse` JSON on stdin. Require allow to exit 0 without a deny payload and deny to emit the cross-compatible `hookSpecificOutput.permissionDecision = "deny"` shape with an actionable reason. Add an integration fixture for `apply_patch`, `Edit`, `Write`, and `Bash` matcher inputs.

- [ ] **Step 2: Implement the generated hook**

The installed project hook imports no third-party modules. It reads project `codex/ui_design` state and mirrors only the deterministic gate-decision subset needed at runtime. Keep a version marker so central upgrades can detect drift.

- [ ] **Step 3: Add hooks without overwriting existing definitions**

Update `codex_hooks_json()` and `claude_settings_json()` to include a managed `PreToolUse` matcher for `apply_patch|Edit|Write|Bash|mcp__filesystem__.*`. Upgrade functions merge the manager-owned hook entry, preserve unrelated user hook entries, create timestamped backups, and report conflicts rather than replacing malformed JSON.

Keep `hard_gate_enabled` false until project path configuration is reviewed and the hook smoke test succeeds in both clients.

- [ ] **Step 4: Run tests and commit**

Run: `python3 -m unittest tests.test_memory_review tests.test_ui_design_gate -v`

```bash
git add templates/ui_design/ui_design_gate_hook.py scripts/memory_project.py tests/test_memory_review.py tests/test_ui_design_gate.py
git commit -m "feat: install cross-agent UI design gate hooks"
```

### Task 17: Add project settings and design-package approval UI/API/CLI

**Files:**
- Modify: `scripts/ui_design_cli.py`
- Modify: `scripts/memory_review_server.py`
- Modify: `tests/test_ui_design_server.py`

- [ ] **Step 1: Add route and CLI tests**

Cover gate-mode read/change, explicit relock, frontend/design path configuration, hard-gate enable confirmation, design-package list/show/approve/reject/revise, stale digest conflict, and project-global baseline approval.

- [ ] **Step 2: Implement CLI commands**

```text
memory_review.py ui-design project-config show --project /tmp/ui-control-fixture
memory_review.py ui-design project-config set-mode --project /tmp/ui-control-fixture --mode design_package --confirmed --idempotency-key mode-001
memory_review.py ui-design project-config set-paths --project /tmp/ui-control-fixture --json-file /tmp/ui-paths.json --idempotency-key paths-001
memory_review.py ui-design project-config enable-hard-gate --project /tmp/ui-control-fixture --confirmed --idempotency-key gate-001
memory_review.py ui-design project-config relock --project /tmp/ui-control-fixture --confirmed --idempotency-key relock-001
memory_review.py ui-design package list --project /tmp/ui-control-fixture
memory_review.py ui-design package create --project /tmp/ui-control-fixture --manifest /tmp/checkout-design-package.json --idempotency-key package-create-001
memory_review.py ui-design package revise --project /tmp/ui-control-fixture --task checkout-redesign --manifest /tmp/checkout-design-package-v2.json --idempotency-key package-revise-001
memory_review.py ui-design package approve --project /tmp/ui-control-fixture --task checkout-redesign --digest 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef --idempotency-key package-approve-001
memory_review.py ui-design package reject --project /tmp/ui-control-fixture --task checkout-redesign --idempotency-key package-reject-001
memory_review.py ui-design package invalidate --project /tmp/ui-control-fixture --task checkout-redesign --reason "Scope changed" --idempotency-key package-invalidate-001
memory_review.py ui-design baseline approve --project /tmp/ui-control-fixture --task project-ui-baseline --digest 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef --confirmed --idempotency-key baseline-approve-001
```

- [ ] **Step 3: Implement console views**

Extend project management with:

- current gate status;
- mode selector with `design_package` as default;
- warning and relock behavior for `project_global`;
- frontend/design/generated/test path editors;
- hard-gate smoke-test result and enable button;
- pending design packages with rendered Markdown text, declared scope, digest, diff, approve/reject/revise controls;
- audit history.

- [ ] **Step 4: Run tests and commit**

Run: `python3 -m unittest tests.test_ui_design_server -v`

```bash
git add scripts/ui_design_cli.py scripts/memory_review_server.py tests/test_ui_design_server.py
git commit -m "feat: add UI design approval console"
```

### Task 18: End-to-end verification, docs, and forward-test preparation

**Files:**
- Modify: `README.md`
- Modify: `tests/test_ui_design_server.py`
- Modify: `tests/test_ui_skill_publication.py`
- Modify: `tests/test_ui_design_gate.py`

- [ ] **Step 1: Add one deterministic end-to-end test**

The test must use temporary global/project/Codex/Claude directories and fixture skills:

1. initialize project UI state;
2. set global preferences and project override;
3. import and approve a skill draft;
4. publish both targets;
5. create a design package;
6. prove formal frontend edit is denied;
7. approve package;
8. prove declared path is allowed and undeclared path is denied;
9. change design file and prove approval is invalidated;
10. discover an unmanaged skill without modifying it.

- [ ] **Step 2: Update operator documentation**

Document storage paths, backup/recovery, global/project preferences, both gate modes, UI Skill import sources, agent-created draft review, request-revision/rollback/disable workflows, bootstrap workflow, unmanaged discovery, CLI examples, hook trust/restart expectations, idempotency rules, and production/main boundaries.

- [ ] **Step 3: Run all static and behavioral verification**

Run: `python3 -m compileall -q scripts templates/ui_design`

Expected: exit 0.

Run: `python3 -m unittest discover -s tests -v`

Expected: all existing and new tests pass.

Run: `python3 /Users/stephenbo/.codex/skills/.system/skill-creator/scripts/quick_validate.py templates/ui_design/skills/ui-design-workflow`

Expected: validation succeeds.

Run: `git diff --check`

Expected: no output.

- [ ] **Step 4: Perform controlled real-client smoke tests**

After separate user approval for writes to real home skill directories:

1. publish the manager-owned skill to both clients;
2. install approved pinned `frontend-design` and UI UX Pro Max drafts;
3. start fresh Codex and Claude Code sessions;
4. verify each lists the same versions;
5. test an allowed design-artifact write and a blocked frontend write in a disposable fixture project;
6. record exact hook payload/decision compatibility;
7. keep hard gate disabled if either client fails the block contract.

- [ ] **Step 5: Prepare forward-test prompts, but do not dispatch without user authorization**

Prepare three isolated prompts using disposable fixture projects:

- Web SaaS checkout redesign;
- React Native onboarding flow;
- mini-program appointment booking page.

Each must verify that the agent produces a design package, stops for approval, and does not modify formal frontend code before approval. Subagents remain prohibited unless the user explicitly authorizes them for the forward test.

- [ ] **Step 6: Commit**

```bash
git add README.md tests/test_ui_design_server.py tests/test_ui_skill_publication.py tests/test_ui_design_gate.py
git commit -m "test: verify UI design control plane"
```

## Final Branch Verification

- [ ] Run `git status --short` and require no unintended files.
- [ ] Run `python3 -m unittest discover -s tests -v` and record the test count.
- [ ] Run `python3 -m compileall -q scripts templates/ui_design`.
- [ ] Run `git diff master...HEAD --check`.
- [ ] Review `git diff --stat master...HEAD` for scope.
- [ ] Confirm no credentials, tokens, verification codes, production secrets, or generated skill packages are tracked.
- [ ] Confirm real global Codex/Claude skill directories were not modified unless separately approved.
- [ ] Request code review using `superpowers:requesting-code-review` before claiming implementation completion.
- [ ] Follow the repository's Loop/worktree finish flow; do not merge or push `master` without the user's explicit command.
