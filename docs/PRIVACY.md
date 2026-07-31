# Privacy

Vibe Memory is designed to run locally on macOS and bind only to loopback.

- No secondary model or API calls are made by the review console itself.
- Personal memory stays approval-gated.
- Project memory is written only for registered projects.
- Runtime and hook state are stored locally and can be exported or removed with
  explicit approval.
- Uninstall only removes managed assets unless the user explicitly approves
  data deletion for named paths.
