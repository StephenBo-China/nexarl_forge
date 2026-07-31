# Universal Memory Manager Release Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every release-blocking gap in the universal local memory manager so a fresh macOS user can install, configure, migrate, operate, recover, and remove the manager without source-tree coupling or silent control-plane loss.

**Architecture:** Keep persistent user data outside versioned releases, but make every executable entry point resolve a validated, persisted Python 3.10+ interpreter and the `current` release. Treat the LaunchAgent, launcher, hooks, settings, registry, migration records, and control-plane inventory as one lifecycle transaction: validate the next state, switch it, restart the local service, and prove health before reporting success. The first-run review console becomes the single explicit configuration surface, while unregistered directories remain personal-memory-only.

**Tech Stack:** Python 3.10+ standard library, Bash, macOS `launchctl`, LaunchAgent plist, JSON and Markdown stores, dependency-free HTTP server, `unittest`, `curl`, and `plutil`.

---

## Scope, dependency order, and execution rules

1. Release-gate diagnostics and runtime interpreter contracts establish observable failure data used by all later work.
2. Launcher and lifecycle coordination precede first-run, migration, and installed-runtime E2E because those flows must run through the installed release.
3. First-run, project boundaries, and complete inventory precede the final E2E and release evidence.
4. On non-Darwin hosts, macOS-only tests use `@unittest.skipUnless(sys.platform == "darwin", "macOS lifecycle contract")`; unit tests inject a discovered Python 3.10+ path and a fake `launchctl` runner. They must not claim a real LaunchAgent pass.
5. Every task starts RED, observes the named failure, adds only the behavior required by that test, then runs the named GREEN command and its focused regression command. Each commit is confined to the files listed in that task.

## Planned file map

- `scripts/public_release_check.py` — public-tree violations with release-relative diagnostic paths and pattern names.
- `scripts/verify_release.py` — release-gate status, installed-runtime probes, and diagnostic forwarding.
- `scripts/vibe_memory_paths.py` — persisted-runtime and stable-launcher paths.
- `scripts/vibe_memory_install.py` — interpreter selection, launcher creation, LaunchAgent bootstrap/bootout/kickstart, transactional lifecycle operations, and managed-release cleanup.
- `scripts/vibe_memory_cli.py` — lifecycle commands, health probe, first-run open behavior, and installed-runtime invocation.
- `scripts/vibe_memory_settings.py` — atomic first-run configuration and short-memory expiration enforcement.
- `scripts/memory_review_server.py` — usable first-run wizard plus settings and workspace APIs.
- `scripts/memory_project.py`, `scripts/vibe_memory_router.py`, `scripts/vibe_memory_migration.py` — registration boundary, legacy-hook removal, migration audit, inventory, and validation.
- `templates/macos/com.noema.vibe-memory.plist`, `install.sh` — interpreter-parametric LaunchAgent and clone-to-launcher installer.
- `tests/test_public_release_check.py`, `tests/test_vibe_memory_paths.py`, `tests/test_vibe_memory_install.py`, `tests/test_vibe_memory_cli.py`, `tests/test_vibe_memory_settings.py`, `tests/test_ui_design_server.py`, `tests/test_vibe_memory_router.py`, `tests/test_vibe_memory_migration.py`, `tests/test_installed_control_plane.py`, `tests/test_macos_install_e2e.py` — focused RED/GREEN coverage and installed-release E2E.
- `README.md`, `docs/MEMORY_REVIEW_USER_GUIDE.zh-CN.md`, `docs/RELEASE_CHECKLIST.md` — user flow and release policy.
- `docs/reports/universal-memory-manager-functional-report.md`, `docs/reports/universal-memory-manager-code-change-report.md`, `docs/reports/universal-memory-manager-evaluation-report.md` — the three user-requested delivery reports.
- `loop/reports/*.md`, `loop/reports/*.json` — local independent-evaluation evidence; these runtime artifacts remain ignored by Git.

### Task 1: Make public-release and gate failures actionable

**Files:**
- Modify: `scripts/public_release_check.py`
- Modify: `scripts/verify_release.py`
- Modify: `tests/test_public_release_check.py`
- Modify: `docs/RELEASE_CHECKLIST.md`

- [ ] **Step 1: Write the failing diagnostics tests**

```python
def test_scan_tree_reports_root_relative_path_and_pattern(self) -> None:
    with tempfile.TemporaryDirectory() as value:
        root = pathlib.Path(value)
        leaked = root / "docs" / "guide.md"
        leaked.parent.mkdir()
        leaked.write_text("path=/Users/example", encoding="utf-8")
        self.assertEqual(public_release_check.scan_tree(root), [{
            "path": "docs/guide.md", "pattern": "personal_path", "match": "/Users/",
        }])

def test_release_gate_exposes_public_tree_violations(self) -> None:
    with mock.patch.object(verify_release.public_release_check, "scan_tree", return_value=[{
        "path": "README.md", "pattern": "credential_assignment", "match": "token=abc123456789",
    }]):
        result = verify_release.evaluate_checks(ROOT)
    self.assertEqual(result["public_tree"], "failed: README.md [credential_assignment]")
```

- [ ] **Step 2: Run the RED test and record the observed old output**

Run: `python3 -m unittest tests.test_public_release_check.PublicReleaseCheckTest.test_scan_tree_reports_root_relative_path_and_pattern tests.test_public_release_check.PublicReleaseCheckTest.test_release_gate_exposes_public_tree_violations -v`

Expected: FAIL because `scan_tree()` emits absolute `path` values and `evaluate_checks()` collapses all public-tree failures into a generic message.

- [ ] **Step 3: Implement the minimum diagnostic contract**

```python
def _violation(base: pathlib.Path, path: pathlib.Path, pattern: str, match: str) -> dict[str, str]:
    return {"path": path.relative_to(base).as_posix(), "pattern": pattern, "match": match}

def _public_tree_status(base: pathlib.Path) -> str:
    violations = public_release_check.scan_tree(base)
    if not violations:
        return "ok"
    first = violations[0]
    return f"failed: {first['path']} [{first['pattern']}]"
```

Use `_violation()` for text, unreadable-file, and symlink reports; do not serialize the scanned root. Replace the generic `public_tree` expression in `evaluate_checks()` with `_public_tree_status(base)`. Update the checklist to require an empty JSON violation list or `public release tree check: ok` before release.

- [ ] **Step 4: Run GREEN and focused regression checks**

Run: `python3 -m unittest tests.test_public_release_check -v && python3 scripts/public_release_check.py --tree . && python3 scripts/verify_release.py --tree .`

Expected: all tests PASS; the scanner prints the success line for this tree; JSON includes `public_tree: "ok"` or a relative path and bracketed pattern.

- [ ] **Step 5: Commit**

```bash
git add scripts/public_release_check.py scripts/verify_release.py tests/test_public_release_check.py docs/RELEASE_CHECKLIST.md
git commit -m "fix: expose release gate diagnostics"
```

### Task 2: Select, persist, and use a Python 3.10+ interpreter and stable launcher

**Files:**
- Modify: `scripts/vibe_memory_paths.py`
- Modify: `scripts/vibe_memory_install.py`
- Modify: `scripts/vibe_memory_hooks.py`
- Modify: `scripts/vibe_memory_cli.py`
- Modify: `templates/macos/com.noema.vibe-memory.plist`
- Modify: `install.sh`
- Modify: `tests/test_vibe_memory_paths.py`
- Modify: `tests/test_vibe_memory_hooks.py`
- Modify: `tests/test_vibe_memory_install.py`
- Modify: `tests/test_vibe_memory_cli.py`

- [ ] **Step 1: Write failing interpreter and launcher tests**

```python
def test_discover_python_rejects_39_and_selects_first_310_candidate(self) -> None:
    probes = {"/usr/bin/python3": (3, 9), "/opt/homebrew/bin/python3": (3, 11)}
    self.assertEqual(install.discover_python(lambda path: probes.get(path)), "/opt/homebrew/bin/python3")

def test_install_persists_interpreter_and_launcher_uses_current_runtime(self) -> None:
    result = install.install_runtime_config(paths, port=18997, app_version="1.0.0", python_executable="/opt/homebrew/bin/python3")
    self.assertEqual(result["python_executable"], "/opt/homebrew/bin/python3")
    self.assertIn('exec "/opt/homebrew/bin/python3" "$RUNTIME/scripts/vibe_memory_cli.py" "$@"', install.render_launcher(paths))

def test_managed_hook_uses_launcher_not_usr_bin_python3(self) -> None:
    command = hooks.command(pathlib.Path("/Users/test/.local/bin/vibe-memory"), "codex", "Stop")
    self.assertEqual(command, '"/Users/test/.local/bin/vibe-memory" hook --agent codex --event Stop')
```

- [ ] **Step 2: Run the RED tests**

Run: `python3 -m unittest tests.test_vibe_memory_paths tests.test_vibe_memory_hooks.ManagedCommandTest tests.test_vibe_memory_install.RuntimeInstallTest tests.test_vibe_memory_cli.VibeMemoryLifecycleTest -v`

Expected: FAIL because no discovery contract or persisted interpreter exists and generated hooks/plist still contain `/usr/bin/python3`.

- [ ] **Step 3: Implement the minimum portable runtime contract**

```python
MINIMUM_PYTHON = (3, 10)

def validate_python(executable: str, probe: Callable[[str], tuple[int, int] | None]) -> str:
    version = probe(executable)
    if version is None or version < MINIMUM_PYTHON:
        raise InstallError("Python 3.10 or newer is required")
    return executable

def discover_python(probe: Callable[[str], tuple[int, int] | None] = probe_python) -> str:
    for candidate in (os.environ.get("VIBE_MEMORY_PYTHON"), shutil.which("python3"), "/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"):
        if candidate:
            try:
                return validate_python(candidate, probe)
            except InstallError:
                continue
    raise InstallError("Python 3.10 or newer was not found")
```

Add `python_executable` to the validated `config.json` runtime section and to `install.json`; add `RuntimePaths.launcher` for `~/.local/bin/vibe-memory`. Render a mode-`0700` launcher that resolves `Application Support/VibeMemory/current` on every invocation and `exec`s the persisted interpreter with `scripts/vibe_memory_cli.py`. Make hook commands call that launcher; render the plist `ProgramArguments` from `${PYTHON}` and `${RUNTIME}`. `install.sh` must invoke a discovered Python, then verify `vibe-memory doctor`; it must not embed the clone path. `doctor` must fail with `python: error` when the persisted executable is absent or below 3.10.

- [ ] **Step 4: Run GREEN and source-coupling regressions**

Run: `python3 -m unittest tests.test_vibe_memory_paths tests.test_vibe_memory_hooks tests.test_vibe_memory_install tests.test_vibe_memory_cli -v`

Expected: PASS; injected 3.9 candidates are rejected, injected 3.10+ candidates are selected, and no managed command contains `/usr/bin/python3`.

- [ ] **Step 5: Commit**

```bash
git add scripts/vibe_memory_paths.py scripts/vibe_memory_install.py scripts/vibe_memory_hooks.py scripts/vibe_memory_cli.py templates/macos/com.noema.vibe-memory.plist install.sh tests/test_vibe_memory_paths.py tests/test_vibe_memory_hooks.py tests/test_vibe_memory_install.py tests/test_vibe_memory_cli.py
git commit -m "fix: use persisted python launcher"
```

### Task 3: Coordinate LaunchAgent lifecycle and post-transition health

**Files:**
- Modify: `scripts/vibe_memory_install.py`
- Modify: `scripts/vibe_memory_cli.py`
- Modify: `tests/test_vibe_memory_install.py`
- Modify: `tests/test_vibe_memory_cli.py`
- Modify: `tests/test_macos_install_e2e.py`

- [ ] **Step 1: Write failing lifecycle tests**

```python
def test_install_bootstraps_user_agent_then_kickstarts_and_checks_health(self) -> None:
    calls: list[list[str]] = []
    result = install.activate_launch_agent(paths, runner=lambda command: calls.append(command) or 0, health=lambda: {"ok": True})
    self.assertEqual(calls, [["launchctl", "bootstrap", "gui/501", str(paths.launch_agent)], ["launchctl", "kickstart", "-k", "gui/501/com.noema.vibe-memory"]])
    self.assertEqual(result["health"], "ok")

def test_rollback_restores_matching_config_plist_and_hooks_before_restart(self) -> None:
    result = install.rollback(paths, runner=fake_launchctl, health=healthy)
    self.assertEqual(result["status"], "rolled_back")
    self.assertEqual(result["health"], "ok")

@unittest.skipUnless(sys.platform == "darwin", "macOS lifecycle contract")
def test_clean_install_bootstraps_agent_and_serves_health(self) -> None:
    result = _run_install(home, with_claude_hooks=False)
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertTrue(_health(18997)["ok"])
```

- [ ] **Step 2: Run the RED tests**

Run: `python3 -m unittest tests.test_vibe_memory_install tests.test_vibe_memory_cli tests.test_macos_install_e2e -v`

Expected: non-Darwin macOS E2E is SKIPPED; lifecycle unit tests FAIL because install/update/rollback/repair/uninstall only write files and do not coordinate `launchctl` or health.

- [ ] **Step 3: Implement lifecycle primitives and transaction ordering**

```python
def launchctl_domain(uid: int | None = None) -> str:
    return f"gui/{os.getuid() if uid is None else uid}"

def activate_launch_agent(paths: RuntimePaths, runner: LaunchctlRunner, health: HealthProbe) -> dict[str, str]:
    domain = launchctl_domain()
    runner(["launchctl", "bootstrap", domain, str(paths.launch_agent)])
    runner(["launchctl", "kickstart", "-k", f"{domain}/com.noema.vibe-memory"])
    if not health().get("ok"):
        raise InstallError("LaunchAgent started but health check failed")
    return {"health": "ok"}
```

Add `bootout_managed_launch_agent()` that tolerates an absent service but fails on another `launchctl` error. On initial install: write runtime config, launcher, plist, hooks, and `install.json`; bootstrap/kickstart; run the managed Codex/optional Claude hook smoke by feeding each adapter a harmless JSON event; then require the loopback `/health` service identity. On update, rollback, and repair: restore the matching versioned config/plist/hooks, bootstrap or kickstart, then health-check. On uninstall: boot out first, remove only the known launcher, current symlink, plist, and manager-owned release directories; preserve registry, memories, approvals, skills, and worktree data unless the existing explicit data-deletion flags are supplied. Include `interpreter`, `port`, `installed_clients`, `current_version`, and `previous_version` in initial `install.json`.

- [ ] **Step 4: Run GREEN and Darwin-only E2E**

Run: `python3 -m unittest tests.test_vibe_memory_install tests.test_vibe_memory_cli -v && python3 -m unittest tests.test_macos_install_e2e -v`

Expected: unit tests PASS; on macOS E2E PASS with a real `launchctl` user domain and loopback health; on non-Darwin the E2E module reports only the explicit SKIP reason.

- [ ] **Step 5: Commit**

```bash
git add scripts/vibe_memory_install.py scripts/vibe_memory_cli.py tests/test_vibe_memory_install.py tests/test_vibe_memory_cli.py tests/test_macos_install_e2e.py
git commit -m "fix: coordinate launch agent lifecycle"
```

### Task 4: Make first-run settings, retention, startup, and workspace registration real

**Files:**
- Modify: `scripts/vibe_memory_settings.py`
- Modify: `scripts/memory_review_server.py`
- Modify: `scripts/vibe_memory_cli.py`
- Modify: `tests/test_vibe_memory_settings.py`
- Modify: `tests/test_ui_design_server.py`
- Modify: `tests/test_vibe_memory_cli.py`

- [ ] **Step 1: Write failing first-run and retention tests**

```python
def test_first_run_transaction_sets_choices_reconciles_clients_and_registers_only_explicit_root(self) -> None:
    result = server.save_first_run_settings({"workspace": str(project), "retention_days": 14, "start_at_login": True, "codex_hooks": True, "claude_hooks": False})
    self.assertTrue(result["settings"]["first_run_complete"])
    self.assertEqual(result["settings"]["personal_short_retention_days"], 14)
    self.assertEqual(result["registered_project"], str(project.resolve()))

def test_retention_zero_removes_expired_and_unbounded_short_sections(self) -> None:
    settings.save_settings(paths, {**settings.default_settings(), "personal_short_retention_days": 0})
    self.assertEqual(settings.prune_personal_short_memory(paths, today=date(2026, 7, 31)), {"removed": 2})

def test_first_run_rejects_workspace_equal_to_runtime_clone(self) -> None:
    with self.assertRaisesRegex(ValueError, "workspace must not be the installed runtime"):
        server.save_first_run_settings({"workspace": str(paths.install_root / "current"), **FIRST_RUN})
```

- [ ] **Step 2: Run the RED tests**

Run: `python3 -m unittest tests.test_vibe_memory_settings tests.test_ui_design_server.UIDesignServerTest.test_first_run_saves_choices_reconciles_and_registers_only_explicit_workspace tests.test_vibe_memory_cli.VibeMemoryLifecycleTest -v`

Expected: FAIL because first-run does not atomically persist/reconcile every choice and retention values do not remove short-memory entries.

- [ ] **Step 3: Implement the first-run transaction and visible wizard**

```python
def apply_first_run(paths: RuntimePaths, request: Mapping[str, object]) -> dict[str, object]:
    normalized = normalize_first_run(request)
    with lifecycle_lock(paths):
        saved = save_settings(paths, normalized.settings)
        reconcile_clients(paths, normalized.clients)
        prune_personal_short_memory(paths, retention_days=int(saved["personal_short_retention_days"]))
        registered = register_explicit_workspace(normalized.workspace)
        restart_and_require_health(paths)
    return {"settings": saved, "registered_project": registered}
```

Expose a first-run page at `/?first-run=1` when `first_run_complete` is false, with controls for Codex hooks, optional Claude hooks, automatic candidate checks, retention radio values `0`, `14`, `30`, login startup, port, and an explicit workspace picker. Wire `POST /api/settings/first-run` to `apply_first_run`; validate 0/14/30 only, valid loopback port, and an existing user-selected workspace that is neither the installed runtime nor the manager source root. Implement `prune_personal_short_memory()` so 0 removes every managed short-memory candidate section, 14/30 writes an `expires_on` date and removes expired managed sections, while leaving unmarked user content unchanged. Reconcile `start_at_login` by installing/bootstrapping or booting out the managed LaunchAgent. Make `vibe-memory open` call health first and open `/?first-run=1` until completion.

- [ ] **Step 4: Run GREEN and browserless HTTP regression**

Run: `python3 -m unittest tests.test_vibe_memory_settings tests.test_ui_design_server tests.test_vibe_memory_cli -v`

Expected: PASS; requests persist a single coherent config, only explicit workspaces enter the registry, and retention tests prove distinct 0/14/30 behavior.

- [ ] **Step 5: Commit**

```bash
git add scripts/vibe_memory_settings.py scripts/memory_review_server.py scripts/vibe_memory_cli.py tests/test_vibe_memory_settings.py tests/test_ui_design_server.py tests/test_vibe_memory_cli.py
git commit -m "feat: complete first run configuration"
```

### Task 5: Enforce project registration boundaries and audited legacy migration

**Files:**
- Modify: `scripts/memory_project.py`
- Modify: `scripts/vibe_memory_router.py`
- Modify: `scripts/vibe_memory_migration.py`
- Modify: `scripts/vibe_memory_cli.py`
- Modify: `tests/test_vibe_memory_cli.py`
- Modify: `tests/test_vibe_memory_router.py`
- Modify: `tests/test_vibe_memory_migration.py`

- [ ] **Step 1: Write failing boundary and migration tests**

```python
def test_project_init_creates_memory_files_without_legacy_shared_memory_hooks(self) -> None:
    result = memory_project.init_project(project)
    self.assertTrue((project / "codex" / "codex_long_memory.md").exists())
    self.assertFalse((project / ".codex" / "hooks.json").exists())
    self.assertFalse((project / ".claude" / "settings.json").exists())

def test_unregister_removes_only_manager_owned_legacy_hooks_and_router_becomes_personal_only(self) -> None:
    result = memory_project.unregister_project(project)
    self.assertEqual(result["removed_legacy_hooks"], [str(project / ".codex" / "hooks.json")])
    context = router.build_context(paths, cwd=project, agent="codex", event="Stop")
    self.assertNotIn("project_memory", context)

def test_migrate_apply_writes_audit_per_project_and_preserves_unmanaged_hook_text(self) -> None:
    result = migration.apply_legacy_hooks([project], paths=paths)
    self.assertEqual(result["projects"][0]["audit"]["status"], "applied")
    self.assertIn("custom-hook", (project / ".codex" / "hooks.json").read_text(encoding="utf-8"))
```

- [ ] **Step 2: Run the RED tests**

Run: `python3 -m unittest tests.test_vibe_memory_cli.VibeMemoryLifecycleTest tests.test_vibe_memory_router.VibeMemoryRouterTest tests.test_vibe_memory_migration.VibeMemoryMigrationTest -v`

Expected: FAIL because initialization still creates legacy project hook files, unregister only edits registry state, and apply lacks per-project audit evidence.

- [ ] **Step 3: Implement explicit ownership and migration audit**

```python
def unregister_project(root: str | pathlib.Path) -> dict[str, Any]:
    project = normalize_project_root(root)
    removed = migration.remove_managed_legacy_hooks(project)
    registry = remove_project_from_registry(project)
    return {"status": "unregistered", "project": str(project), "removed_legacy_hooks": removed, "registry": registry}

def resolve_registered_project(paths: RuntimePaths, cwd: pathlib.Path) -> pathlib.Path | None:
    return next((root for root in registered_roots(paths) if cwd == root or root in cwd.parents), None)
```

Split `init_project()` from legacy migration: it creates only `codex/` memory files and managed instruction blocks, never `.codex/hooks.json` or `.claude/settings.json`. Keep universal user hooks as the router entry. `unregister_project()` must call a marker-aware removal that preserves unrelated commands and delete no project memory. `apply_legacy_hooks()` must require the already-existing `--approved`, process each explicitly selected registered root under a lock, write a timestamped audit JSON record containing root, before/after digest, changed paths, backups, and result, and refuse roots absent from the registry. Router context must include personal references for every cwd and project references only after `resolve_registered_project()` succeeds.

- [ ] **Step 4: Run GREEN and multi-project regression**

Run: `python3 -m unittest tests.test_vibe_memory_cli tests.test_vibe_memory_router tests.test_vibe_memory_migration -v`

Expected: PASS; a new project has no legacy hook copies, unregister preserves custom content, unregistered cwd returns personal-only context, and two registered roots receive separate migration audits.

- [ ] **Step 5: Commit**

```bash
git add scripts/memory_project.py scripts/vibe_memory_router.py scripts/vibe_memory_migration.py scripts/vibe_memory_cli.py tests/test_vibe_memory_cli.py tests/test_vibe_memory_router.py tests/test_vibe_memory_migration.py
git commit -m "fix: enforce registered project migration boundary"
```

### Task 6: Validate every control-plane area without false green states

**Files:**
- Modify: `scripts/vibe_memory_migration.py`
- Modify: `scripts/vibe_memory_cli.py`
- Modify: `scripts/verify_release.py`
- Modify: `tests/test_vibe_memory_migration.py`
- Modify: `tests/test_installed_control_plane.py`

- [ ] **Step 1: Write failing complete-inventory tests**

```python
EXPECTED_AREAS = {"personal_memory", "projects", "memory_review", "policy", "design_preferences", "ui_design_packages", "ui_design_approvals", "ui_design_audit", "ui_skills", "ui_skill_digests", "ui_skill_deployments", "ui_skill_audit", "loop", "worktrees", "active_worktrees", "pending_worktrees", "legacy_hooks"}

def test_inventory_covers_every_control_plane_area(self) -> None:
    self.assertEqual(set(migration.inventory(paths, registry)), EXPECTED_AREAS)

def test_validate_control_plane_marks_dangling_policy_and_skill_deployment_references_as_error(self) -> None:
    result = migration.validate_control_plane(paths, registry)
    self.assertEqual(result["policy"], "error")
    self.assertEqual(result["ui_skill_deployments"], "error")

def test_validate_control_plane_accepts_empty_optional_design_and_skill_areas(self) -> None:
    fixture = build_empty_but_valid_control_plane(paths)
    result = migration.validate_control_plane(fixture.paths, fixture.registry)
    self.assertTrue(all(status == "ok" for status in result.values()))
```

- [ ] **Step 2: Run the RED tests**

Run: `python3 -m unittest tests.test_vibe_memory_migration.VibeMemoryMigrationTest tests.test_installed_control_plane.InstalledControlPlaneTest -v`

Expected: FAIL because the current inventory omits policy, UI design package/audit, UI Skill digest/deployment/audit, and active/pending worktree status; omitted data cannot fail validation.

- [ ] **Step 3: Implement explicit inventory inspectors and strict status**

```python
def validate_control_plane(paths: RuntimePaths, registry: Mapping[str, object]) -> dict[str, str]:
    snapshot = inventory(paths, registry)
    return {
        area: "ok" if isinstance(value, dict) and not value.get("errors") else "error"
        for area, value in snapshot.items()
    }
```

Add dedicated `inspect_policy`, `inspect_ui_design_packages`, `inspect_ui_design_audit`, `inspect_ui_skill_digests`, `inspect_ui_skill_deployments`, `inspect_ui_skill_audit`, `inspect_active_worktrees`, and `inspect_pending_worktrees` functions. Each returns `{\"present\": bool, \"errors\": list[str], \"records\": list[dict[str, object]]}` and rejects malformed JSON, missing required schema fields, dangling digest/deployment references, approvals without a package, and worktree status inconsistent with its registry. Retain existing inspector data but return `present: True` only after the expected canonical source is readable. Extend CLI doctor and `verify_release._control_plane_check()` to list all named non-`ok` areas instead of declaring a partial map healthy.

An optional area with no records is a valid empty control plane and must not make `doctor` fail. `present: False` is diagnostic metadata, not automatically an error; only malformed canonical data, missing data referenced by another record, digest mismatch, or an inconsistent state transition is an error.

- [ ] **Step 4: Run GREEN and installed-control-plane regression**

Run: `python3 -m unittest tests.test_vibe_memory_migration tests.test_installed_control_plane -v && python3 scripts/vibe_memory_cli.py doctor`

Expected: tests PASS; doctor exits nonzero and names every missing or malformed area, then exits zero only with the full fixture.

- [ ] **Step 5: Commit**

```bash
git add scripts/vibe_memory_migration.py scripts/vibe_memory_cli.py scripts/verify_release.py tests/test_vibe_memory_migration.py tests/test_installed_control_plane.py
git commit -m "fix: validate complete memory control plane"
```

### Task 7: Replace dummy installed-runtime coverage with a real installed-release E2E

**Files:**
- Modify: `tests/test_installed_control_plane.py`
- Modify: `tests/test_macos_install_e2e.py`
- Modify: `tests/test_vibe_memory_cli.py`
- Modify: `scripts/verify_release.py`

- [ ] **Step 1: Write failing installed-executable E2E tests**

```python
@unittest.skipUnless(sys.platform == "darwin", "macOS installed-runtime E2E")
def test_installed_launcher_serves_console_and_migrates_fixture(self) -> None:
    self.assertEqual(_run_install_script(home).returncode, 0)
    launcher = home / ".local" / "bin" / "vibe-memory"
    self.assertEqual(subprocess.run([launcher, "doctor"], text=True, capture_output=True).returncode, 0)
    self.assertEqual(_json_get(18997, "/health")["service"], "vibe-memory")
    self.assertEqual(_json_post(18997, "/api/settings/first-run", FIRST_RUN)["settings"]["first_run_complete"], True)
    self.assertEqual(subprocess.run([launcher, "migrate", "apply", "--approved", "--project-root", str(project)], text=True, capture_output=True).returncode, 0)

def test_release_gate_install_e2e_invokes_installed_launcher_not_runtime_marker(self) -> None:
    with mock.patch("verify_release._run_installed_release_e2e", return_value="ok") as probe:
        self.assertEqual(verify_release.evaluate_checks(ROOT)["install_e2e"], "ok")
    probe.assert_called_once_with(ROOT)
```

- [ ] **Step 2: Run the RED tests**

Run: `python3 -m unittest tests.test_installed_control_plane tests.test_macos_install_e2e tests.test_vibe_memory_cli -v`

Expected: non-Darwin E2E is SKIPPED; the new gate test FAILS because tests create a marker file and `verify_release` does not launch an installed service.

- [ ] **Step 3: Implement the release-owned E2E harness**

```python
def _run_installed_release_e2e(root: pathlib.Path) -> str:
    if sys.platform != "darwin":
        return "skipped: macOS installed-runtime E2E"
    with temporary_home_and_port() as (home, port, project):
        installed = run([root / "install.sh", "--port", str(port)], env={"HOME": str(home)})
        require_zero(installed, "install.sh")
        launcher = home / ".local" / "bin" / "vibe-memory"
        require_zero(run([launcher, "doctor"], env={"HOME": str(home)}), "doctor")
        require_health(port)
        require_console_flow(port, project)
        require_zero(run([launcher, "migrate", "apply", "--approved", "--project-root", str(project)], env={"HOME": str(home)}), "migration")
    return "ok"
```

Remove `# installed runtime marker` construction and every source-import shortcut from installed tests. The harness must use a disposable HOME, `install.sh` or the generated launcher, a dynamically reserved loopback port, one registered fixture project, and a real installed server. Probe `/health`, `/`, `/api/settings`, `/api/projects`, the first-run save endpoint, a primary review endpoint (`/api/queue`), and `migrate apply --approved`; assert that response service identity/version matches `release.json`. Terminate the test-owned LaunchAgent/service in `finally`. `verify_release` must expose the Darwin skip distinctly and must never substitute a source-tree marker for install E2E.

- [ ] **Step 4: Run GREEN in the correct environment**

Run: `python3 -m unittest tests.test_installed_control_plane tests.test_macos_install_e2e -v && python3 scripts/verify_release.py --tree .`

Expected: on macOS, installed-runtime tests and `install_e2e` PASS after launching the installed service; elsewhere tests SKIP with the named Darwin reason and release JSON reports the distinct skip rather than `ok`.

- [ ] **Step 5: Commit**

```bash
git add tests/test_installed_control_plane.py tests/test_macos_install_e2e.py tests/test_vibe_memory_cli.py scripts/verify_release.py
git commit -m "test: run installed memory manager e2e"
```

### Task 8: Document the clone-to-uninstall user journey and corrected memory policy

**Files:**
- Modify: `README.md`
- Modify: `docs/MEMORY_REVIEW_USER_GUIDE.zh-CN.md`
- Modify: `docs/RELEASE_CHECKLIST.md`
- Modify: `tests/test_ui_design_server.py`

- [ ] **Step 1: Write failing documentation-contract tests**

```python
def test_user_docs_cover_installed_lifecycle_and_model_distilled_short_memory(self) -> None:
    for path in (ROOT / "README.md", ROOT / "docs/MEMORY_REVIEW_USER_GUIDE.zh-CN.md"):
        text = path.read_text(encoding="utf-8")
        for command in ("install.sh", "vibe-memory doctor", "vibe-memory open", "vibe-memory update", "vibe-memory rollback", "vibe-memory repair", "vibe-memory uninstall", "vibe-memory migrate apply --approved"):
            self.assertIn(command, text)
        self.assertNotIn("Project short memory stores a bounded prompt summary", text)
```

- [ ] **Step 2: Run the RED test**

Run: `python3 -m unittest tests.test_ui_design_server.UIDesignServerTest.test_user_docs_cover_installed_lifecycle_and_model_distilled_short_memory -v`

Expected: FAIL because the existing guide describes legacy direct scripts and says a prompt summary is stored.

- [ ] **Step 3: Write the precise user and release guidance**

Add one ordered, copyable flow in both documents: clone; run `./install.sh`; complete `vibe-memory open` first-run choices; register/init a workspace; preview then apply migration with `--approved`; run `vibe-memory doctor`; update from a local clone; rollback after a failed update; repair hooks/LaunchAgent; uninstall while retaining data. State that hooks write event metadata only and the active Codex or Claude Code model distills short-memory candidates; no hook stores prompt text or manufactures a candidate. Include recovery output expectations, the data retained by uninstall, macOS-only service notes, and the required fresh-session hook trust step. Update the checklist with the same order and link it from README.

- [ ] **Step 4: Run GREEN and prose scans**

Run: `python3 -m unittest tests.test_ui_design_server.UIDesignServerTest.test_user_docs_cover_installed_lifecycle_and_model_distilled_short_memory -v && rg -n "bounded prompt summary|/usr/bin/python3" README.md docs/MEMORY_REVIEW_USER_GUIDE.zh-CN.md docs/RELEASE_CHECKLIST.md`

Expected: test PASS; `rg` returns no matches.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/MEMORY_REVIEW_USER_GUIDE.zh-CN.md docs/RELEASE_CHECKLIST.md tests/test_ui_design_server.py
git commit -m "docs: explain universal memory manager lifecycle"
```

### Task 9: Produce fresh release evidence, independent Claude evaluation, and three delivery reports

**Files:**
- Create: `loop/reports/universal_memory_manager_claude_eval.md`
- Create: `loop/reports/universal_memory_manager_claude_eval.json`
- Create: `docs/reports/universal-memory-manager-functional-report.md`
- Create: `docs/reports/universal-memory-manager-code-change-report.md`
- Create: `docs/reports/universal-memory-manager-evaluation-report.md`
- Modify: `docs/RELEASE_CHECKLIST.md`

- [ ] **Step 1: Freeze the implementation commit and clean verification state**

```bash
git status --short --branch
git rev-parse HEAD
git diff --check
```

Expected: the feature worktree has no uncommitted production-code or test changes. Record this SHA as the tested implementation commit. Report files are documentation evidence, so this task does not manufacture an artificial failing product test; the prior eight tasks already provide RED/GREEN behavior coverage.

- [ ] **Step 2: Execute fresh Codex-owned release verification**

Run: `python3 -m unittest discover -s tests -v`

Run: `python3 scripts/verify_release.py --tree .`

Run: `python3 scripts/public_release_check.py --tree .`

Run: `python3 -m unittest tests.test_macos_install_e2e -v`

Expected: all commands exit `0`; on Darwin the installed-runtime E2E executes rather than skips. Capture exact command, timestamp, platform, Python version, pass/fail/skip counts, and exit status. Do not summarize from an earlier run.

- [ ] **Step 3: Run a fresh independent Claude Code evaluation**

Give a fresh Claude Code session the tested implementation commit, the original acceptance criteria, the completion plan, and a disposable installed-runtime URL. Require it to inspect code, run the release gate, exercise the installed launcher and first-run/migration/lifecycle flows, and return structured P0/P1/P2/P3 findings. Store its real local evidence in `loop/reports/universal_memory_manager_claude_eval.md` and `.json`; never invent a command, screenshot, browser result, or verdict. If Claude reports any P0/P1 or a reproducible acceptance failure, return to the relevant implementation task, fix it through RED/GREEN and two-stage review, then repeat this step on the new commit. The evaluation passes only with verdict `pass` and zero open P0/P1.

- [ ] **Step 4: Write the three user-requested Markdown reports from actual evidence**

`functional-report.md` must enumerate every delivered capability and its user-visible behavior: clone/install, Python selection, stable launcher, service startup, first-run, shared Codex/Claude hooks, registered/unregistered routing, review console, memory governance, migration, UI design approvals, UI Skills, Loop, lifecycle recovery, privacy, and uninstall data retention.

`code-change-report.md` must summarize the complete base-to-tested-commit diff by subsystem, list relevant commits and files, explain migrations/backward compatibility, identify security/concurrency decisions, and call out intentionally deferred macOS packaging/other-platform work.

`evaluation-report.md` must include the exact commands and outcomes from Step 2, the installed-runtime evidence, Claude Code method/verdict/findings from Step 3, the tested commit, environment limits such as untested Intel hardware, remaining P2/P3 risks, and Codex's final release decision. Each report must contain `Tested implementation commit:`, `Command:`, and `Outcome:` fields and link to the other two reports and the local Claude evidence path.

- [ ] **Step 5: Validate and commit the reports**

Run: `rg -n "Tested implementation commit:|Command:|Outcome:" docs/reports/universal-memory-manager-*.md`

Run: `python3 scripts/public_release_check.py --tree . && git diff --check`

Expected: all three reports contain the required evidence fields; the public scan and diff check pass. Commit only the three tracked reports plus the checklist. The report content names the tested implementation commit from Step 1; the later report-only commit is identified in the final user response to avoid circular self-reference.

```bash
git add docs/reports/universal-memory-manager-functional-report.md docs/reports/universal-memory-manager-code-change-report.md docs/reports/universal-memory-manager-evaluation-report.md docs/RELEASE_CHECKLIST.md
git commit -m "docs: record universal memory manager release evidence"
```

## Final implementation handoff checks

- [ ] Confirm every task was executed in order and each named RED command failed for the behavior under construction before its implementation.
- [ ] Confirm every macOS-only E2E has `skipUnless(sys.platform == "darwin", ...)`; non-Darwin output must say SKIPPED, never PASS.
- [ ] Confirm tests use injected Python discovery probes and fake `launchctl` runners for portable unit coverage, while release evidence includes a real Darwin installed-runtime run.
- [ ] Confirm `verify_release.evaluate_checks()` returns a visible non-`ok` status for every missing control-plane area and relative `path` plus `pattern` for public-tree failures.
- [ ] Confirm update, rollback, repair, and uninstall leave personal/project data intact unless explicit approved deletion paths are supplied, and that uninstall removes manager-owned releases and launcher.
- [ ] Confirm the final full suite and Claude report are newly generated for the commit named in all three delivery reports; do not merge, push, stage deploy, or alter production without later explicit user authorization.
