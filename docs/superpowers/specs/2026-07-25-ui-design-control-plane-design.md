# UI Design Control Plane Design

**Status:** User-approved design, pending written-spec review

**Date:** 2026-07-25

## 1. Objective

Extend the local memory review console into a UI design control plane shared by
Codex and Claude Code. For Web, app, mini-program, and other visible-interface
work, the agents must prepare a design package and obtain user approval before
modifying formal frontend business code.

The control plane also manages global and project-specific design preferences,
installs and versions UI design skills for both agents, and supports adding new
UI skills from the console, Codex, Claude Code, or the CLI.

## 2. Confirmed Product Decisions

1. Use a control-plane plus local-snapshot architecture. Agents do not depend
   on the review server being online during normal work.
2. Install Anthropic `frontend-design` and UI UX Pro Max for both Codex and
   Claude Code.
3. Add a manager-owned `ui-design-workflow` orchestration skill. Third-party
   skill packages remain read-only.
4. Store design preferences as global defaults with field-level project
   overrides.
5. Use a mixed hard gate. Before approval, agents may research, read code, and
   create design artifacts, prototypes, and interaction specifications, but
   may not modify formal frontend business code.
6. Pure backend and non-visual tasks bypass the UI design gate.
7. Each project chooses one approval mode:
   - `project_global`: one approved project design baseline unlocks all
     frontend code until the user relocks the project or changes mode.
   - `design_package`: approval applies only to one task, one design version,
     and its declared page, component, and file scope.
8. New projects default to `design_package`.
9. UI skills added by an agent appear immediately as pending drafts in the
   review console and require explicit approval before publication.
10. The console discovers unregistered skills already present in Codex and
    Claude Code skill directories. Discovery is read-only and never publishes,
    executes, overwrites, or deletes a skill.

## 3. Scope

### 3.1 Included

- Web pages and Web applications.
- iOS, Android, macOS, React Native, Flutter, and other app interfaces.
- WeChat and other mini-program interfaces.
- Visible desktop interfaces, design systems, component libraries, navigation,
  forms, interactions, responsive behavior, motion, loading, empty, error, and
  accessibility states.
- Global and project design preferences.
- UI skill import, validation, approval, publication, update, rollback,
  discovery, enablement, and audit history.
- Codex and Claude Code managed rules and hooks required to enforce the gate.

### 3.2 Excluded

- Automatic production deployment or main-branch merge.
- Executing third-party skill scripts during import or validation.
- Treating ordinary backend code, API work, database work, or headless services
  as frontend work merely because the repository also contains a frontend.
- Editing third-party skill contents to inject user preferences.
- Automatically trusting or synchronizing an unmanaged discovered skill.
- Replacing project-specific framework linting, tests, or visual regression
  suites.

## 4. Architecture

The design uses four layers.

### 4.1 Review Console Control Plane

The existing local review console gains:

- a design-preference editor;
- project UI workflow settings;
- design-package review and approval;
- a UI skill registry;
- skill draft inspection and approval;
- skill publication and rollback status;
- unmanaged-skill discovery;
- local audit history.

The server and CLI share the same Python domain modules. HTTP handlers contain
no separate business rules.

### 4.2 Canonical Registry and Immutable Packages

The global canonical store lives under:

```text
~/.codex/ui_design/
├── registry.json
├── preferences.json
├── packages/<skill-name>/<version-id>/
├── drafts/<draft-id>/
├── deployments/
└── audit.jsonl
```

Published package versions are immutable. A version ID includes a normalized
version label and SHA-256 content digest. Updating a skill creates a new
version rather than rewriting the prior package.

The registry records:

- skill ID and normalized name;
- source type, source location, source revision, and retrieved timestamp;
- content digest and license metadata;
- common, Codex-specific, and Claude-specific variants;
- validation report and script inventory;
- approval status and approval timestamp;
- global or project scope;
- target agents;
- current and previous deployed versions;
- ignored unmanaged fingerprints.

### 4.3 Local Agent Snapshots

Publication produces validated local snapshots in the supported Codex and
Claude Code skill locations. Global skills are published to the agents' user
skill directories. Project-only skills are published to repository-local skill
directories supported by each agent.

Publication does not require the review server afterward. Managed project
rules, project UI configuration, design approvals, and installed skill
snapshots are sufficient for offline work.

### 4.4 Project UI State

Each initialized project contains:

```text
codex/ui_design/
├── config.json
├── preferences.json
├── active-skills.json
├── design-packages/<task-id>/
│   ├── manifest.json
│   ├── design-brief.md
│   ├── interaction-spec.md
│   ├── responsive-spec.md
│   ├── assets/
│   └── prototypes/
└── approvals.json
```

`config.json` stores the gate mode, relock state, frontend path rules, design
artifact path rules, and project-level workflow enablement. `preferences.json`
contains field-level overrides only. `active-skills.json` pins the skill
versions and execution order active for the project.

## 5. Runtime Skill Composition

The manager-owned `ui-design-workflow` skill is the stable entry point for
visible-interface work. Its responsibilities are:

1. Classify whether a task includes visible-interface work.
2. Load global preferences and merge project overrides.
3. Load the project's enabled UI skill manifest.
4. Use `frontend-design` for subject grounding, intentional aesthetic
   direction, typography, layout, signature elements, and anti-template
   critique.
5. Use UI UX Pro Max for industry rules, product-type recommendations, platform
   and framework guidance, accessibility, responsive behavior, and
   anti-pattern reference.
6. Create or update a versioned design package.
7. Stop at the user-approval boundary.
8. After approval, expose the permitted implementation scope to the gate.
9. Require post-implementation visual, interaction, responsive, and
   accessibility verification.

User preferences have higher priority than third-party recommendations.
Project overrides have higher priority than global defaults. A user instruction
for the current task has higher priority than stored preferences.

Third-party skills are installed independently and are not modified by the
manager. This prevents an upstream update from replacing user preferences or
gate behavior.

## 6. Design Preference Model

Global preferences and project overrides use the same schema. Fields include:

- brand personality and desired emotional tone;
- target audiences and usability priorities;
- preferred and disallowed visual styles;
- color principles and disallowed color treatments;
- typography roles and language-specific requirements;
- spacing, density, radius, elevation, border, and surface preferences;
- icon, illustration, photography, and generated-asset preferences;
- motion intensity, timing, reduced-motion behavior, and interaction feedback;
- navigation, forms, loading, empty, success, and error behavior;
- accessibility minimums;
- platform-specific Web, iOS, Android, macOS, and mini-program guidance;
- references such as screenshots, local assets, and Figma links;
- free-form design principles and explicit anti-preferences.

Project override fields may inherit, replace, append, or explicitly clear a
global field. The console shows the effective merged value and its source.

## 7. Frontend Task Lifecycle

### 7.1 Classification

The workflow classifies the requested change using task wording, affected
paths, project framework, and planned outputs. Classification returns:

- `non_visual`: bypass the UI gate;
- `visual_new`: new interface or visible feature;
- `visual_change`: material visual or interaction change;
- `visual_maintenance`: implementation-only repair that does not change the
  approved appearance or interaction contract.

Ambiguous tasks default to locked and require an agent explanation before the
user can bypass them. The classifier result and rationale are recorded in the
design package or audit log.

### 7.2 Design Phase

While locked, agents may:

- inspect code and existing design-system assets;
- research references and competitors;
- prepare information architecture, user flows, visual directions, tokens,
  wireframes, mockups, prototypes, and interaction specifications;
- create files inside the configured design-artifact directories;
- run read-only analysis and render design artifacts.

While locked, agents may not change configured formal frontend business-code
paths, production styles, themes, application pages, or business components.

### 7.3 Approval

The console displays the design package, interaction specification, declared
implementation scope, design changes since the prior version, and content
digest. The user may approve, reject, or request revision.

Approval records include:

- project and task ID;
- gate mode;
- design version and digest;
- approved page, component, and file scope;
- user decision and timestamp;
- superseded approval, when applicable.

For `design_package`, modifying an approved design artifact or declared scope
creates a new digest and invalidates the prior approval. For `project_global`,
the approved baseline unlock remains active until the user explicitly relocks
the project or switches gate mode.

Switching from `project_global` to `design_package` immediately relocks formal
frontend paths. Existing global approval does not authorize a design package.
All gate-mode changes are audited.

### 7.4 Implementation and Verification

After approval, the gate permits only the scope authorized by the active mode.
Implementation must still follow project tests and Loop Engineering rules when
Loop is active.

Completion requires proportional verification covering applicable viewport
sizes, interaction states, keyboard and touch behavior, reduced motion,
accessibility, and screenshot or design-reference comparison. Verification does
not silently change the approved design contract.

## 8. Gate Enforcement

Managed Codex and Claude Code instructions provide the semantic workflow.
Lifecycle hooks provide mechanical enforcement.

The gate hook receives the proposed tool operation, resolves the project root,
loads project UI configuration, identifies affected paths, and evaluates the
current approval. It returns one of:

- `allow_non_visual`;
- `allow_design_artifact`;
- `allow_approved_frontend_scope`;
- `deny_missing_design`;
- `deny_pending_approval`;
- `deny_scope_mismatch`;
- `deny_invalidated_approval`;
- `deny_invalid_configuration`.

Path matching is project-configurable. Initial project setup proposes framework
defaults but requires review before the hard gate is enabled. Generated files,
dependency directories, build outputs, design artifacts, test artifacts, and
formal frontend code are separate path classes.

If configuration is missing or corrupt, formal frontend mutations fail closed,
while read-only and non-visual work remains available. The error explains the
exact file and recovery action.

## 9. UI Skill Import and Review

### 9.1 Entry Points

All entry points call the same domain service:

- review-console UI;
- Codex through the manager CLI;
- Claude Code through the same manager CLI;
- direct terminal CLI.

Supported sources are:

- GitHub repository plus skill path and pinned tag, commit, or release;
- local skill directory;
- uploaded ZIP archive;
- skill created or edited in the review console.

An agent import creates a draft and returns its draft ID and validation report.
It does not publish the package. The draft appears immediately in the review
console.

### 9.2 Draft Inspection

The review page exposes:

- full `SKILL.md` content;
- references, assets, and scripts inventory;
- source and pinned revision;
- license and attribution information;
- content digest;
- comparison with the deployed version;
- trigger-description and naming conflicts;
- script and permission risks;
- target agents and final destination paths;
- global or selected-project scope.

The user may approve and publish, request revision, reject, or retain the draft.

### 9.3 Validation

Validation checks:

- archive size and file-count limits;
- ZIP slip and path traversal;
- symlinks and files that escape the package root;
- presence and parseability of `SKILL.md`;
- valid skill name and trigger description;
- referenced file existence;
- duplicate or conflicting skill names;
- script inventory, executable files, and suspicious command patterns;
- external dependencies and network requirements;
- license metadata and source provenance;
- deterministic content digest.

Validation never executes imported scripts. Script execution remains subject to
the normal agent sandbox and approval policy after publication.

## 10. Publication and Rollback

Publication is a transaction:

1. Copy the approved immutable package to per-agent staging directories.
2. Validate the exact staged snapshots.
3. Back up currently deployed managed versions.
4. Atomically replace the Codex target.
5. Atomically replace the Claude Code target.
6. Verify both destinations against the approved digest.
7. Update the registry and audit log only after both verifications pass.

If either target fails, restore both prior snapshots. The registry remains on
the prior deployed version, and the console displays the failed phase and
recovery result.

Deleting or disabling a managed skill uses the same staged transaction. The
system never deletes an unmanaged skill automatically.

## 11. Initial Skill Bootstrap

### 11.1 `frontend-design`

Import the pinned `skills/frontend-design` package from the Anthropic skills
repository. The same normalized package is published to Codex and Claude Code.

### 11.2 UI UX Pro Max

Pin a specific UI UX Pro Max release and installer version. Generate Codex and
Claude Code snapshots in isolated temporary directories. Treat them as two
agent variants under one registry record, validate all generated scripts and
data, and publish the correct variant to each target.

Published operation does not depend on a future online `npx` invocation. A
version update creates a new draft and requires review of its changes.

### 11.3 Manager Orchestration Skill

Create and validate `ui-design-workflow` as a manager-owned skill. It references
the project preference snapshot, active skill manifest, design package schema,
and gate status without copying large preference data into `SKILL.md`.

## 12. Unmanaged Skill Discovery

The scanner runs when the console starts, when the UI skill page refreshes, or
when the user requests a scan. It reads supported Codex and Claude Code skill
locations, computes fingerprints, and compares them with managed deployments.

An unknown fingerprint appears as `unmanaged_discovered`. The user may:

- import it as a draft for normal validation and approval;
- ignore that exact fingerprint;
- leave it visible without action.

Changing an ignored skill changes its fingerprint and makes it visible again.
Discovery never runs skill code and never automatically copies it to the other
agent.

## 13. API and CLI Surface

The exact command parser may follow existing project conventions, but the
domain operations are:

- list, show, import, validate, approve, reject, publish, rollback, disable,
  and scan UI skills;
- get and update global preferences;
- get and update project preference overrides;
- get and update project gate mode;
- create, revise, approve, reject, invalidate, and list design packages;
- inspect effective project UI context and gate status.

HTTP and CLI responses use structured status codes and machine-readable error
details. Mutating operations include an idempotency token to prevent duplicate
publication or approval after retries.

## 14. Error Handling

- Network or GitHub failure leaves a retryable draft with no deployment.
- Invalid ZIP, path traversal, missing `SKILL.md`, or invalid metadata rejects
  the import before registry publication.
- Unknown metadata is preserved and reported unless it conflicts with the
  supported skill format.
- Name collisions require an explicit replace-version or rename decision.
- A target directory changed outside the manager causes a digest conflict and
  blocks publication until the user imports, restores, or ignores the change.
- Failure to write one agent target rolls back both targets.
- A corrupt registry is not overwritten; the console reports the path and uses
  the most recent validated backup for read-only recovery.
- A corrupt project gate configuration fails closed only for formal frontend
  mutations.
- Stale approval, changed design digest, or out-of-scope file mutation returns
  an actionable denial explaining how to revise or obtain approval.

## 15. Testing Strategy

### 15.1 Unit Tests

- preference inheritance, append, replace, clear, and source reporting;
- task classification and pure-backend bypass;
- path classification and file-scope matching;
- approval digest and invalidation behavior;
- both gate modes and mode-switch relocking;
- archive traversal, symlink, size, metadata, and reference validation;
- registry transitions and immutable version behavior;
- unmanaged discovery, ignore fingerprints, and rediscovery after changes;
- deterministic package hashing.

### 15.2 Integration Tests

- console and CLI create identical drafts;
- a Codex-created draft appears in the review API;
- approval publishes matching Codex and Claude snapshots;
- simulated second-target failure restores both prior deployments;
- project-local and global skill scopes publish to the correct locations;
- managed rule and hook upgrades are idempotent and preserve user text;
- locked projects allow design artifacts but deny formal frontend changes;
- approved package scope allows only declared frontend paths;
- `project_global` remains unlocked until explicit relock or mode change.

### 15.3 End-to-End Acceptance

1. Install the two initial third-party skills and the orchestration skill.
2. Confirm Codex and Claude Code report the same approved versions.
3. Configure global preferences and a project override, then confirm both agents
   receive the same effective values.
4. Start a Web, app, or mini-program UI task and verify formal frontend edits
   are blocked before approval.
5. Produce and approve a design package, then verify the authorized paths are
   unlocked.
6. Change the approved design package and verify `design_package` approval is
   invalidated.
7. Switch a project to `project_global`, approve its baseline, and verify all
   formal frontend paths remain unlocked until relock.
8. Add a UI skill from Codex and confirm it is visible as a pending console
   draft before publication.
9. Place an unmanaged skill in one agent directory and confirm discovery offers
   import or ignore without modifying either directory.
10. Simulate a Claude publication failure and confirm both agents retain their
    prior versions.

## 16. Rollout Sequence

1. Add the canonical schemas, registry, validation, and audit primitives.
2. Add global and project design preferences.
3. Add skill drafts, review UI, CLI operations, and safe source adapters.
4. Add atomic dual-agent publication and unmanaged discovery.
5. Bootstrap `frontend-design` and UI UX Pro Max.
6. Create `ui-design-workflow` and managed agent instructions.
7. Add project gate modes, design packages, approval UI, and enforcement hooks.
8. Add visual and interaction verification surfaces.
9. Forward-test representative Web, app, and mini-program workflows before
   enabling the hard gate by default for initialized projects.

## 17. Success Criteria

- Both initial third-party skills are installed, versioned, and usable in Codex
  and Claude Code.
- Both agents receive the same effective design preferences and approved skill
  versions for a project.
- Visible-interface tasks consistently stop for design and interaction approval
  before formal frontend implementation.
- Pure backend and non-visual work is not blocked.
- Both project approval modes behave exactly as configured and are audited.
- UI skills added by Codex are visible and reviewable before publication.
- Third-party scripts never execute during import or validation.
- Partial publication cannot leave Codex and Claude Code on different managed
  versions.
- Unmanaged skills are visible without being modified automatically.
- Existing memory governance, Loop Engineering boundaries, worktree safety,
  and production approval rules remain unchanged.
