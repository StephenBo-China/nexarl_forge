# UI Design Preference Schema

Load the merged preference document from `codex/ui_design/effective-context.json`. Treat its `value` as the effective configuration and `sources` as field-level provenance.

## Preference groups

- `brand`: personality, emotional tone, audiences, usability priorities.
- `visual`: preferred and prohibited styles, color principles, typography, spacing density, radius, elevation, borders, surfaces.
- `imagery`: icons, illustration, photography, generated assets.
- `interaction`: motion, timing, reduced motion, feedback, navigation, forms, loading, empty, success, error.
- `accessibility`: minimum standard and additional rules.
- `platform`: Web, iOS, Android, macOS, and mini-program guidance.
- `references`, `design_principles`, and `anti_preferences`.

## Project override modes

| Mode | Meaning |
| --- | --- |
| `inherit` | Keep the global value |
| `replace` | Replace the field, including with an explicit empty value |
| `append` | Append values to a list field |
| `clear` | Clear the field according to its type |

Apply priority as: current user instruction, project override, global value, third-party recommendation. Never edit an imported third-party Skill to embed preferences. Keep preferences in the manager-owned context snapshot so Codex and Claude Code receive the same effective values.
