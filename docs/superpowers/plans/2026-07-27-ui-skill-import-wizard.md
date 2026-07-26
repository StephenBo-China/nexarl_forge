# UI Skill Import Wizard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. The user selected inline execution; do not dispatch subagents.

**Goal:** Replace the editor-only UI Skill import control with an accessible three-step wizard supporting editor, local directory, local ZIP, and pinned GitHub sources, plus selectable scope, version, and Codex/Claude targets.

**Architecture:** Keep the existing `/api/ui-skills/import` domain endpoint unchanged. Add client-only wizard state and rendering inside the single-file local console, map the final reviewed state to the existing source payload shapes, and retain the current validated-draft lifecycle. Verify the generated page contract with unit tests and exercise real imports through the existing server-domain tests.

**Tech Stack:** Python 3 standard library, embedded HTML/CSS/vanilla JavaScript, `unittest`.

---

### Task 1: Lock the page and request contracts with failing tests

**Files:**
- Modify: `tests/test_ui_design_server.py`
- Test: `tests/test_ui_design_server.py`

- [ ] **Step 1: Add a page-contract test for the three-step wizard**

Add assertions to `test_console_exposes_design_preferences_and_ui_skill_review_safely` for the new entry point, source choices, step labels, target controls, accessibility hooks, and safety copy, while asserting the old `importEditorSkill()` entry point is absent:

```python
self.assertIn('openUISkillImportWizard()', html)
self.assertIn('uiSkillImportWizard', html)
self.assertIn('1 选择来源', html)
self.assertIn('2 配置导入', html)
self.assertIn('3 确认并校验', html)
for source in ('editor', 'local', 'zip', 'github'):
    self.assertIn(f'data-skill-source="{source}"', html)
self.assertIn('uiSkillTargetCodex', html)
self.assertIn('uiSkillTargetClaude', html)
self.assertIn('aria-current', html)
self.assertIn('不会自动批准、发布或执行包内脚本', html)
self.assertNotIn('importEditorSkill()', html)
```

- [ ] **Step 2: Add source-payload domain tests for all four sources**

Patch `server.ui_design_cli.dispatch`, call `server.ui_design_post('/api/ui-skills/import', ...)` with editor, local, ZIP, and GitHub bodies, and assert the namespace passed to dispatch contains the exact source-specific values, scope, project, targets, and version label. This protects the existing server mapper while the UI starts exercising all variants.

- [ ] **Step 3: Run the focused tests and confirm the page test fails**

Run:

```bash
python3 -m unittest tests.test_ui_design_server.UIDesignServerTest.test_console_exposes_design_preferences_and_ui_skill_review_safely -v
```

Expected: `FAIL` because the wizard entry point and markup do not exist.

- [ ] **Step 4: Run the new payload test separately**

Run:

```bash
python3 -m unittest tests.test_ui_design_server.UIDesignServerTest.test_ui_skill_import_maps_all_source_payloads -v
```

Expected: `PASS`, proving no backend API change is required, or a focused mapper failure that is fixed before UI work.

### Task 2: Implement wizard structure and visual states

**Files:**
- Modify: `scripts/memory_review_server.py`
- Test: `tests/test_ui_design_server.py`

- [ ] **Step 1: Replace the toolbar controls**

Replace the editor-specific scope selector/button with a single entry point:

```html
<button class="primary" onclick="openUISkillImportWizard()">导入 UI Skill</button>
```

Keep “扫描并刷新” unchanged.

- [ ] **Step 2: Add scoped wizard CSS**

Add `.skill-wizard`, `.skill-wizard-steps`, `.skill-source-grid`, `.skill-source-card`, `.skill-field`, `.skill-error`, `.skill-wizard-actions`, and responsive rules. Reuse existing CSS variables; source cards are two columns above 768px and one column below it. Include visible `:focus-visible`, disabled, selected, error, and reduced-motion rules.

- [ ] **Step 3: Add the wizard host to the page**

Add a hidden named `<section>` with `id="uiSkillImportWizard"`, `role="dialog"`, `aria-labelledby="uiSkillWizardTitle"`, an `aria-live="polite"` status node, and a content node rendered by JavaScript. Keep it in the page DOM so the page-contract test can verify semantics.

- [ ] **Step 4: Add explicit wizard state and render functions**

Use one state object:

```javascript
let uiSkillWizard = {
  open: false,
  step: 1,
  sourceType: '',
  fields: {
    skillMD: '', localPath: '', zipPath: '', githubRepo: '',
    githubPath: '', revision: '', scope: 'global', versionLabel: '1.0.0',
    codex: true, claude: true
  },
  errors: {},
  submitting: false
};
```

Implement `openUISkillImportWizard`, `closeUISkillImportWizard`, `selectUISkillSource`, `renderUISkillImportWizard`, `validateUISkillWizardStep`, `nextUISkillWizardStep`, and `previousUISkillWizardStep`. Render only the active step, preserve shared fields, and clear old source-specific fields when changing source.

- [ ] **Step 5: Run the page-contract test**

Run:

```bash
python3 -m unittest tests.test_ui_design_server.UIDesignServerTest.test_console_exposes_design_preferences_and_ui_skill_review_safely -v
```

Expected: `PASS`.

### Task 3: Implement validation, review, and submission

**Files:**
- Modify: `scripts/memory_review_server.py`
- Test: `tests/test_ui_design_server.py`

- [ ] **Step 1: Implement source-specific validation**

Validate editor content as non-empty with `name` and `description` frontmatter hints; local and ZIP paths as absolute paths; GitHub repo/path as non-empty and revision with `/^[0-9a-fA-F]{40}$/`; version as non-empty; and at least one target. Render cause-and-recovery error text below each field and focus the first invalid field after a continue action.

- [ ] **Step 2: Build an escaped read-only review**

Create `uiSkillImportSummary()` and render source, locator, scope/current project, version, and selected targets. For editor input show character count and first non-empty line; for local/ZIP show the full path with wrapping; for GitHub show the complete revision.

- [ ] **Step 3: Build the existing API payload without changing the endpoint**

Implement `uiSkillImportPayload()` to emit one of:

```javascript
{source: {type: 'editor', files: {'SKILL.md': skillMD}}, ...shared}
{source: {type: 'local', path: localPath}, ...shared}
{source: {type: 'zip', path: zipPath}, ...shared}
{source: {type: 'github', repo: githubRepo, path: githubPath, revision}, ...shared}
```

`shared` contains `scope`, project only for project scope, selected targets, version label, and a unique idempotency key.

- [ ] **Step 4: Implement submission states**

During submission set `submitting`, disable navigation, announce “正在导入并校验…”, and call the existing API helper. On success close the wizard, call `loadUISkills()`, show the returned Skill name/draft ID/digest when available, and focus the new draft region. On error keep all state, announce the error, and expose retry/return actions.

- [ ] **Step 5: Add unsaved-change protection**

Track whether source or fields differ from defaults. Require confirmation before cancel/Escape when dirty; do not add browser `beforeunload` behavior because the wizard is local and same-page.

- [ ] **Step 6: Run focused UI Skill server tests**

Run:

```bash
python3 -m unittest tests.test_ui_design_server -v
```

Expected: all tests pass.

### Task 4: Update the Chinese guide

**Files:**
- Modify: `docs/MEMORY_REVIEW_USER_GUIDE.zh-CN.md`

- [ ] **Step 1: Replace editor-only UI instructions**

Document the three steps, the four source-specific fields, local absolute-path behavior, scope/version/targets, and the fact that import performs static validation but does not approve or publish.

- [ ] **Step 2: Keep CLI alternatives**

Retain GitHub/local/ZIP/editor CLI examples for automation and recovery. Clarify that the web wizard and CLI use the same backend operation.

- [ ] **Step 3: Check terminology consistency**

Run:

```bash
rg -n "从编辑器导入|导入 UI Skill|三步|本地目录|ZIP|GitHub" docs/MEMORY_REVIEW_USER_GUIDE.zh-CN.md scripts/memory_review_server.py
```

Expected: old editor-only wording is absent from web instructions; safety lifecycle wording remains.

### Task 5: Full verification, browser QA, review, and delivery

**Files:**
- Modify only if a verified issue is found within the approved scope.

- [ ] **Step 1: Run the entire test suite**

Run:

```bash
python3 -m unittest discover -s tests -v
```

Expected: all tests pass.

- [ ] **Step 2: Review the diff and approved scope**

Run `git diff --check`, inspect the three approved files, confirm no unrelated file is staged, and verify the design package digest remains `d033055469b8bb66c43b1eabea6d77fa12fc61f1d25346d8a6e94ae28d4be28f`.

- [ ] **Step 3: Restart the service from canonical master for QA**

Stop only the verified process listening on 8897, then start through `scripts/start_memory_review.sh /Users/stephenbo/Noema/Projects/vibe_coding_manage_platform`. Confirm `/health` succeeds.

- [ ] **Step 4: Verify in the browser**

Exercise all three steps at 1440×900 and 375×812, keyboard navigation, editor/local/ZIP/GitHub field changes, target selection, validation errors, review copy, cancel confirmation, and reduced-motion. Use disposable invalid values unless a real import is intentionally being tested; do not approve or publish a test Skill.

- [ ] **Step 5: Commit and push**

Stage only the approved implementation, test, guide, and plan files. Commit with `feat: add UI skill import wizard`, push local `master` to `origin/master`, and verify local HEAD equals `origin/master`.

- [ ] **Step 6: Restart the final pushed commit**

Restart 8897 from the canonical repository after push, confirm the running command path and commit, and run `/health` plus a final browser smoke test.
