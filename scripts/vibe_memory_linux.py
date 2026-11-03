"""Linux source checkout adapter for first-run settings and systemd lifecycle."""
from __future__ import annotations

import json
import os
import pathlib
import shlex
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable

import vibe_memory_install as install
import vibe_memory_paths
import vibe_memory_settings as settings

UNIT = 'nexarl-forge.service'
LAUNCHER_MARKER = '# nexarl-forge:linux-source-launcher'


def unit_path(paths):
    return settings._home(paths) / '.config/systemd/user' / UNIT


def _quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def render_service_unit(source_root, python_executable=sys.executable):
    root = pathlib.Path(source_root).resolve(strict=True)
    if any(character in str(root) + str(python_executable) for character in ('\n', '\r', '\x00')):
        raise ValueError('service paths must not contain control characters')
    entry = root / 'scripts/nexarl_linux_service.py'
    if not entry.is_file():
        raise ValueError('Linux service entry is missing')
    return '\n'.join((
        '# nexarl-forge:linux-source-service',
        '[Unit]', 'Description=Nexarl Forge local review console',
        'After=network.target', '', '[Service]', 'Type=simple',
        'WorkingDirectory=' + str(root).replace('%', '%%'),
        'ExecStart=' + _quote(python_executable) + ' ' + _quote(entry),
        'Environment=PYTHONDONTWRITEBYTECODE=1',
        'Restart=on-failure', 'RestartSec=2', '', '[Install]',
        'WantedBy=default.target', '',
    ))


def _systemctl(*arguments, runner=subprocess.run, allow_disabled=False):
    result = runner(['systemctl', '--user', *arguments], capture_output=True,
                    text=True, check=False, timeout=10)
    if result.returncode and not (allow_disabled and result.returncode in (1, 3, 4)):
        raise RuntimeError('systemd user service operation failed: ' + arguments[0])
    return result


def _snapshot(path):
    install._validate_install_ancestor_chain(path.parent)
    if path.is_symlink():
        raise ValueError('managed path must not be a symlink')
    if path.exists() and not path.is_file():
        raise ValueError('managed path must be a regular file')
    return (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None


def _write(path, content, mode=0o600):
    install._validate_install_ancestor_chain(path.parent)
    if path.is_symlink():
        raise ValueError('managed path must not be a symlink')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        pathlib.Path(name).unlink(missing_ok=True)


def apply_first_run(paths, request, *, manager_source_root, register_workspace: Callable,
                    runner=subprocess.run):
    normalized = settings.normalize_first_run_request(
        paths, request, manager_source_root=manager_source_root)
    selected = normalized['settings']
    root = pathlib.Path(manager_source_root).resolve(strict=True)
    home = settings._home(paths)
    config = paths.install_root / 'config.json'
    launcher = pathlib.Path(paths.launcher)
    managed = [config, launcher, home / '.codex/hooks.json', home / '.claude/settings.json',
               pathlib.Path(paths.project_registry), unit_path(paths),
               pathlib.Path(paths.personal_memory) / 'short.md']
    with settings.lifecycle_lock(paths):
        previous = settings.load_settings(paths)
        if previous['service_port'] != selected['service_port']:
            try:
                with socket.socket() as listener:
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    listener.bind(('127.0.0.1', selected['service_port']))
            except OSError as error:
                raise ValueError('selected service port is already in use') from error
        snapshots = {path: _snapshot(path) for path in managed}
        if snapshots[launcher] is not None:
            content = snapshots[launcher][0].decode('utf-8')
            if LAUNCHER_MARKER not in content:
                raise ValueError('an unrelated launcher exists; refusing to overwrite it')
        if snapshots[unit_path(paths)] is not None:
            unit_content = snapshots[unit_path(paths)][0].decode('utf-8')
            if '# nexarl-forge:linux-source-service' not in unit_content:
                raise ValueError('an unrelated service unit exists; refusing to overwrite it')
        was_enabled = _systemctl('is-enabled', UNIT, runner=runner,
                                 allow_disabled=True).returncode == 0
        # Generate a complete runtime document even for a source-only installation.
        version = vibe_memory_paths.read_release_manifest(root / 'release.json')['app_version']
        runtime = json.loads(install.render_runtime_config(
            selected['service_port'], version, python_executable=sys.executable))
        persisted = {**runtime, **selected}
        written = {}
        uncertain = set()

        def run_owned(operation, affected):
            try:
                result = operation()
            except Exception:
                # A failing operation may have observed an external editor's write.
                # Never claim its current bytes as our own commit.
                uncertain.update(affected)
                raise
            for path in affected:
                written[path] = _snapshot(path)
            return result

        service_changed = False
        try:
            for path, content, mode in (
                (config, (json.dumps(persisted, ensure_ascii=False, indent=2) + '\n').encode(), 0o600),
                (launcher, ('#!/usr/bin/env bash\n' + LAUNCHER_MARKER + '\nexec ' +
                            shlex.quote(sys.executable) + ' ' +
                            shlex.quote(str(root / 'scripts/vibe_memory_cli.py')) +
                            ' "$@"\n').encode(), 0o700),
                (unit_path(paths), render_service_unit(root).encode(), 0o600),
            ):
                _write(path, content, mode)
                written[path] = _snapshot(path)
            run_owned(lambda: settings.reconcile_hooks(paths, selected), managed[2:4])
            short_memory = pathlib.Path(paths.personal_memory) / 'short.md'
            run_owned(lambda: settings.prune_personal_short(
                short_memory, retention_days=int(selected['personal_short_retention_days'])),
                [short_memory])
            registered = None
            if normalized['workspace']:
                registered = run_owned(
                    lambda: register_workspace(normalized['workspace']),
                    [pathlib.Path(paths.project_registry)])
            _systemctl('daemon-reload', runner=runner)
            service_changed = True
            _systemctl('enable' if selected['start_at_login'] else 'disable', UNIT, runner=runner)
            return {'settings': selected, 'registered_project': registered,
                    'service': {'manager': 'systemd', 'unit': UNIT,
                                'enabled': selected['start_at_login']},
                    'linux_restart_required': previous['service_port'] != selected['service_port']}
        except Exception as original:
            failures = []
            if service_changed:
                try:
                    _systemctl('enable' if was_enabled else 'disable', UNIT, runner=runner)
                except Exception:
                    failures.append('systemd enabled state')
            for path, snapshot in snapshots.items():
                if path in uncertain:
                    try:
                        unchanged = _snapshot(path) == snapshot
                    except Exception:
                        unchanged = False
                    if not unchanged:
                        failures.append(str(path))
                    continue
                if path not in written:
                    continue
                try:
                    if _snapshot(path) != written[path]:
                        raise RuntimeError('concurrent change retained')
                    if snapshot is None:
                        path.unlink(missing_ok=True)
                    else:
                        _write(path, snapshot[0], snapshot[1])
                except Exception:
                    failures.append(str(path))
            if service_changed:
                try:
                    _systemctl('daemon-reload', runner=runner)
                    if snapshots[unit_path(paths)] is not None:
                        _systemctl('enable' if was_enabled else 'disable', UNIT, runner=runner)
                except Exception:
                    failures.append('systemd')
            if failures:
                raise RuntimeError('Linux first-run rollback incomplete: ' + ', '.join(failures)) from original
            raise


def restart_service():
    # Non-blocking restart avoids waiting on the process that is being replaced.
    _systemctl('--no-block', 'restart', UNIT)