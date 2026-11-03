from __future__ import annotations
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import vibe_memory_linux as linux
import vibe_memory_paths
import vibe_memory_settings as settings


class LinuxFirstRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name) / 'home'
        self.home.mkdir()
        self.paths = vibe_memory_paths.for_home(self.home)
        self.runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, '', ''))
        self.register = mock.Mock()

    def tearDown(self):
        self.tmp.cleanup()

    def apply(self, **request):
        return linux.apply_first_run(self.paths, request, manager_source_root=ROOT,
                                     register_workspace=self.register, runner=self.runner)

    def test_source_first_run_creates_complete_runtime_and_preserves_other_hooks(self):
        hooks = self.home / '.codex/hooks.json'
        hooks.parent.mkdir()
        other = {'hooks': [{'type': 'command', 'command': 'echo third-party'}]}
        hooks.write_text(json.dumps({'hooks': {'SessionStart': [other]}}))
        result = self.apply(service_port=8897, start_at_login=True)
        persisted = json.loads((self.paths.install_root / 'config.json').read_text())
        self.assertEqual(persisted['port'], 8897)
        self.assertEqual(persisted['service_port'], 8897)
        self.assertTrue(persisted['first_run_complete'])
        self.assertTrue(persisted['formal_memory_requires_approval'])
        self.assertIn('python_executable', persisted)
        self.assertEqual(persisted['app_version'], '1.0.0')
        self.assertIn(other, json.loads(hooks.read_text())['hooks']['SessionStart'])
        self.assertEqual(result['service']['manager'], 'systemd')
        self.assertFalse(self.paths.launch_agent.exists())
        self.assertFalse(result['linux_restart_required'])
        self.assertIn('127.0.0.1', (ROOT / 'scripts/nexarl_linux_service.py').read_text())
        self.register.assert_not_called()

    def test_disabling_autostart_keeps_current_service_running(self):
        result = self.apply(start_at_login=False, codex_hooks=False)
        self.assertFalse(result['settings']['start_at_login'])
        self.assertNotIn('bootout_after_response', result)
        calls = [item.args[0] for item in self.runner.call_args_list]
        self.assertIn(['systemctl', '--user', 'disable', linux.UNIT], calls)
        self.assertFalse(any(any(value in call for value in ('stop', 'restart', 'start')) for call in calls))

    def test_systemd_failure_rolls_back_config_hooks_and_launcher(self):
        original = settings.default_settings()
        original['personal_short_retention_days'] = 14
        settings.save_settings(self.paths, original)
        config = self.paths.install_root / 'config.json'
        before = config.read_bytes()
        def run(command, **kwargs):
            return subprocess.CompletedProcess(command, 1 if any(value in command for value in ('enable', 'is-enabled')) else 0, '', '')
        self.runner.side_effect = run
        with self.assertRaisesRegex(RuntimeError, 'systemd user service operation failed'):
            self.apply(start_at_login=True)
        self.assertEqual(config.read_bytes(), before)
        self.assertFalse(pathlib.Path(self.paths.launcher).exists())
        self.assertFalse((self.home / '.codex/hooks.json').exists())
        self.assertFalse(linux.unit_path(self.paths).exists())

    def test_invalid_workspace_is_rejected_before_side_effects(self):
        with self.assertRaisesRegex(ValueError, 'existing directory'):
            self.apply(workspace=str(self.home / 'missing'))
        self.runner.assert_not_called()
        self.assertFalse((self.paths.install_root / 'config.json').exists())

    def test_port_change_is_persisted_and_marks_restart(self):
        import socket
        with socket.socket() as available:
            available.bind(('127.0.0.1', 0))
            port = available.getsockname()[1]
        result = self.apply(service_port=port)
        self.assertTrue(result['linux_restart_required'])
        saved = settings.load_settings(self.paths)
        self.assertEqual(saved['service_port'], port)

    def test_unrelated_launcher_is_never_overwritten(self):
        launcher = pathlib.Path(self.paths.launcher)
        launcher.parent.mkdir(parents=True)
        launcher.write_text('#!/bin/sh\necho custom\n')
        with self.assertRaisesRegex(ValueError, 'unrelated launcher'):
            self.apply()
        self.assertEqual(launcher.read_text(), '#!/bin/sh\necho custom\n')

    def test_service_unit_quotes_paths_and_reads_persisted_settings(self):
        unit = linux.render_service_unit(ROOT)
        self.assertIn('nexarl_linux_service.py', unit)
        self.assertNotIn('launchctl', unit)
        self.assertNotIn('MEMORY_REVIEW_PORT=', unit)
        self.assertIn('Restart=on-failure', unit)

    def test_first_run_javascript_parses_and_uses_json_submit(self):
        import re
        import shutil
        import memory_review_server as server
        node = shutil.which('node') or shutil.which('node.exe')
        if node is None:
            self.skipTest('node is required for browser JavaScript syntax check')
        with mock.patch.object(server, 'settings_payload', return_value=settings.default_settings()):
            page = server.first_run_page()
        script = re.search(r'<script>(.*?)</script>', page, re.S)[1]
        checked = subprocess.run([node, '--check'], input=script, capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn('method="post"', page)
        self.assertIn('e.preventDefault()', script)

    def test_linux_http_save_dispatches_to_adapter(self):
        import memory_review_server as server
        with mock.patch.object(server.sys, 'platform', 'linux'), \
             mock.patch.object(server.vibe_memory_paths, 'for_home', return_value=self.paths), \
             mock.patch.object(linux, 'apply_first_run', return_value={'saved': True}) as apply, \
             mock.patch.object(settings, 'apply_first_run') as mac:
            self.assertEqual(server.save_first_run_settings({'service_port': 8897}), {'saved': True})
        apply.assert_called_once()
        mac.assert_not_called()

    def test_occupied_port_is_rejected_before_saving(self):
        import socket
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
            with self.assertRaisesRegex(ValueError, 'already in use'):
                self.apply(service_port=port)
        self.runner.assert_not_called()
        self.assertFalse((self.paths.install_root / 'config.json').exists())

    def test_unrelated_service_unit_is_never_overwritten(self):
        unit = linux.unit_path(self.paths)
        unit.parent.mkdir(parents=True)
        unit.write_text('[Service]\nExecStart=/bin/true\n')
        with self.assertRaisesRegex(ValueError, 'unrelated service unit'):
            self.apply()
        self.assertEqual(unit.read_text(), '[Service]\nExecStart=/bin/true\n')

    def test_service_path_rejects_unit_directive_injection(self):
        with self.assertRaisesRegex(ValueError, 'control characters'):
            linux.render_service_unit(ROOT, python_executable='/usr/bin/python3\nExecStart=/bin/false')

    def test_failed_save_restores_pruned_short_memory(self):
        short = pathlib.Path(self.paths.personal_memory) / 'short.md'
        short.parent.mkdir(parents=True)
        short.write_text('## expired\n<!-- vibe-memory:managed-short -->\nexpires_on: 2000-01-01\nold\n')
        before = short.read_bytes()
        def run(command, **kwargs):
            return subprocess.CompletedProcess(command, 1 if 'enable' in command else 0, '', '')
        self.runner.side_effect = run
        with self.assertRaises(RuntimeError):
            self.apply()
        self.assertEqual(short.read_bytes(), before)

    def test_successful_save_applies_retention_policy(self):
        short = pathlib.Path(self.paths.personal_memory) / 'short.md'
        short.parent.mkdir(parents=True)
        short.write_text('## expired\n<!-- vibe-memory:managed-short -->\nexpires_on: 2000-01-01\nold\n')
        self.apply()
        self.assertNotIn('old', short.read_text())

    def test_hook_conflict_keeps_external_edit_during_rollback(self):
        hooks = self.home / '.codex/hooks.json'
        hooks.parent.mkdir(parents=True)
        hooks.write_text('{"hooks": {}}')
        external = '{"hooks": {}, "external_change": true}'
        def conflict(*args):
            hooks.write_text(external)
            raise RuntimeError('concurrent edit')
        with mock.patch.object(settings, 'reconcile_hooks', side_effect=conflict):
            with self.assertRaisesRegex(RuntimeError, 'rollback incomplete'):
                self.apply()
        self.assertEqual(hooks.read_text(), external)
        self.assertFalse(pathlib.Path(self.paths.launcher).exists())
        self.assertFalse((self.paths.install_root / 'config.json').exists())

    def test_failed_workspace_registration_keeps_external_registry(self):
        workspace = self.home / 'workspace'
        workspace.mkdir()
        registry = pathlib.Path(self.paths.project_registry)
        external = '{"projects": [], "external_change": true}'
        def conflict(*args):
            registry.parent.mkdir(parents=True, exist_ok=True)
            registry.write_text(external)
            raise RuntimeError('registry concurrently edited')
        self.register.side_effect = conflict
        with self.assertRaisesRegex(RuntimeError, 'rollback incomplete'):
            self.apply(workspace=str(workspace))
        self.assertEqual(registry.read_text(), external)

if __name__ == '__main__':
    unittest.main()