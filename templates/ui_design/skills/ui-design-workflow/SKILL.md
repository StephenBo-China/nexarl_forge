---
name: ui-design-workflow
description: Use when a task creates or changes a Web, app, mini-program, desktop UI, component library, visual style, interaction, responsive behavior, motion, or other visible interface.
---

# UI Design Workflow

## Core rule

Design visible-interface work before implementation. Do not modify formal frontend business code until the user gives explicit user approval for the active design package or project baseline and the gate reports that the intended file scope is allowed.

Pure backend, API, database, infrastructure, and headless tasks classified as `pure_backend` or `non_visual` bypass this workflow. When classification is ambiguous, remain locked and explain what must be clarified.

## Workflow

1. Classify the task as `non_visual`, `visual_new`, `visual_change`, or `visual_maintenance`. Record the rationale and planned outputs.
2. Read `codex/ui_design/config.json`, `codex/ui_design/effective-context.json`, `codex/ui_design/active-skills.json`, and the current approval state before proposing design work.
3. Resolve instruction priority in this order: current user instruction, project override, global preference, third-party recommendation.
4. Use `frontend-design` first for subject grounding, intentional aesthetic direction, typography, composition, signature elements, and anti-template critique.
5. Use `ui-ux-pro-max` second for product and industry guidance, platform conventions, framework constraints, responsive behavior, accessibility, and anti-pattern checks.
6. Create or revise the design package described in [design-package-schema.md](references/design-package-schema.md). Store design artifacts only inside configured design-artifact paths while locked.
7. Present the design direction, pages, components, interactions, states, responsive rules, accessibility decisions, and allowed implementation file patterns. Stop and request explicit user approval for the exact package digest.
8. Re-read gate status after approval. Implement only when it allows the active mode and every intended formal frontend path. A changed design artifact, scope, or digest requires renewed approval in `design_package` mode.
9. Verify the implementation against the approved package across applicable viewports, pointer and keyboard/touch interactions, loading/empty/error/success states, reduced motion, accessibility, and design-reference comparison.

## Locked-state boundaries

While locked, allow research, code reading, design-system inspection, wireframes, mockups, prototypes, interaction specifications, responsive specifications, and design-artifact rendering.

Do not modify formal frontend business code, production application styles, themes, pages, or business components. Do not treat approval of one task or digest as approval of another. Do not silently widen allowed file patterns.

## Preference handling

Read [preference-schema.md](references/preference-schema.md) when interpreting or proposing preference changes. Preserve explicit empty values and the `inherit`, `replace`, `append`, and `clear` semantics. Report the effective value and source when a preference affects a design decision.

## Quick reference

| Situation | Action |
| --- | --- |
| Pure backend or headless work | Record `pure_backend` or `non_visual`; bypass UI gate |
| New or changed visible interface | Produce a design package before formal frontend edits |
| Design package pending or changed | Stop at approval; do not implement |
| `design_package` approved | Modify only declared file patterns for that digest |
| `project_global` approved | Follow the active baseline until relock or mode change |
| Gate missing, corrupt, or ambiguous | Fail closed only for formal frontend mutations |

## Common mistakes

- Starting component or stylesheet implementation because the design seems obvious.
- Loading third-party advice before the user's effective preferences.
- Treating a prototype or screenshot as approval without a digest-bound decision.
- Omitting interaction, responsive, empty, loading, error, success, or reduced-motion states.
- Reusing approval after the design package or allowed scope changes.
- Claiming completion without visual and interaction verification.
