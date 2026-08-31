# Contributing to Nexarl Forge

Thank you for helping improve Nexarl Forge, a local-first workspace for human–AI coding collaboration.

## Development setup

Nexarl Forge v1.0.0 targets macOS on Apple Silicon and Intel with Python 3.10 or newer. The project has no third-party Python runtime dependencies.

```bash
python3 -m pytest -q
python3 scripts/verify_release.py
```

Run the installer only in a disposable test account or temporary home. Never commit personal memory, hook settings, runtime databases, logs, credentials, or generated context packets.

## Changes and pull requests

- Open an issue for a substantial behavior change before implementation.
- Keep changes focused and include regression tests and documentation.
- Use clear commits such as `feat: ...`, `fix: ...`, `docs: ...`, or `test: ...`.
- Pull requests should explain user impact, macOS versions checked, test commands, and migration or privacy implications.
- Do not force-push shared branches or modify release tags.

## Security

Please do not report vulnerabilities in public issues. Follow [SECURITY.md](SECURITY.md) for private disclosure instructions.

## License

Contributions are made under the [MIT License](LICENSE).
