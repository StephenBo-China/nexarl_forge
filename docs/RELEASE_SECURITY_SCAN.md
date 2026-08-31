# v1.0.0 publication checks

Run against the clean release clone before publication.

| Check | Result |
| --- | --- |
| Distributable tree (`python3 scripts/public_release_check.py --tree .`) | Passed: no personal paths, credential assignments, private-memory headings, or local runtime assets detected. |
| Git history high-confidence secret pattern scan | Passed: no credential-like assignments detected in reachable history. |
| Tracked private artifact inventory | Passed: no environment files, keys, certificates, databases, PDFs, personal memory files, or generated context packets tracked. |
| Third-party runtime inventory | Passed: Python standard library and macOS system services only; see [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md). |

The scan intentionally does not publish scan logs containing user paths or repository contents. Repeat these checks for every release candidate and review GitHub's secret-scanning alerts after publication.
