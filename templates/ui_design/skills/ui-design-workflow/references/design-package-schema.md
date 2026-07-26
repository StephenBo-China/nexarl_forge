# Design Package Schema

Create each task package under `codex/ui_design/design-packages/<task-id>/`.

## Required files

- `manifest.json`: normalized scope and lifecycle state.
- `design-brief.md`: goals, users, information architecture, visual direction, tokens, references, and rationale.
- `interaction-spec.md`: user flows, actions, feedback, navigation, forms, and loading/empty/error/success states.
- `responsive-spec.md`: viewport, density, input-mode, orientation, and platform rules.

Place optional generated assets in `assets/` and runnable or rendered prototypes in `prototypes/`.

## Manifest

```json
{
  "schema_version": 1,
  "task_id": "checkout-redesign",
  "title": "Checkout redesign",
  "classification": "visual_change",
  "pages": ["checkout"],
  "components": ["CheckoutForm"],
  "allowed_file_patterns": ["web/src/checkout/**"],
  "design_files": [
    "design-brief.md",
    "interaction-spec.md",
    "responsive-spec.md"
  ],
  "status": "pending_approval"
}
```

Use only project-relative paths. Reject absolute paths, traversal, undeclared design files, and scope broader than the described feature. Compute the approval digest over the normalized manifest and every declared design file.

## Lifecycle

1. Create or revise the package with status `pending_approval`.
2. Present its digest and declared implementation scope.
3. Record `approved`, `rejected`, or `revision_requested` only from an explicit user decision.
4. Recompute the digest before implementation. In `design_package` mode, any changed design file or scope invalidates prior approval.
5. Record verification evidence without rewriting the approved design contract.

For `project_global`, use an ordinary package as the named project baseline. Its approval remains active only until explicit relock, mode change, or approval invalidation.
