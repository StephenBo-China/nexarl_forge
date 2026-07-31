# Release Checklist

Before publishing a new release candidate:

- Verify the tree on both Apple Silicon and Intel hardware.
- Start from a fresh Codex session.
- Optionally start a fresh Claude Code session for independent smoke testing.
- Run a visual smoke test of the review console.
- Run a separate Git-history secret scan.
- Stop publication immediately if the history scan finds a material secret.
- Run the public release tree check and require an empty violation list (`ok`).
- If the public-tree gate fails, its result must contain only the root-relative
  path and violation pattern needed to locate the issue; matched content is
  intentionally omitted.
- Do not commit personal filesystem paths or other local-only data.

The public release gate should remain local-only and should not require
production credentials, production deployment, or public network access.
