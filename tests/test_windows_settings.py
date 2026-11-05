import json
import datetime
import os
import pathlib
import sys
import tempfile
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import runtime_platform as storage
import vibe_memory_paths
import vibe_memory_settings as settings
import windows_runtime

@unittest.skipUnless(os.name == "nt", "Windows first-run settings")
class WindowsSettingsTests(unittest.TestCase):
    def prepare(self, home):
        paths = vibe_memory_paths.for_home(home, platform="win32")
        storage.private_directory(paths.install_root)
        value = dict(settings.default_settings(), service="vibe-memory", port=8897, python_executable=sys.executable, app_version="1.0.0", python_version="3.13")
        storage.atomic_write(paths.install_root / "config.json", json.dumps(value).encode())
        return paths

    def test_first_run_adds_and_removes_owned_startup_without_scheduler(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.prepare(pathlib.Path(directory))
            result = settings.apply_first_run(paths, {"start_at_login": True}, manager_source_root=pathlib.Path(__file__).resolve().parents[1], register_workspace=lambda value: {})
            self.assertTrue(result["settings"]["first_run_complete"])
            self.assertEqual(paths.launch_agent.suffix, ".cmd")
            content = paths.launch_agent.read_text(encoding="utf-8")
            self.assertIn("vibe_memory_cli.py", content)
            self.assertNotIn("powershell", content.lower())
            self.assertNotIn("schtasks", content.lower())
            settings.apply_first_run(paths, {"start_at_login": False}, manager_source_root=pathlib.Path(__file__).resolve().parents[1], register_workspace=lambda value: {})
            self.assertFalse(paths.launch_agent.exists())

    def test_unknown_startup_file_preserved_and_configuration_rolled_back(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.prepare(pathlib.Path(directory))
            paths.launch_agent.parent.mkdir(parents=True)
            paths.launch_agent.write_text("custom content", encoding="utf-8")
            original = (paths.install_root / "config.json").read_bytes()
            with self.assertRaises(ValueError):
                settings.apply_first_run(paths, {"start_at_login": True}, manager_source_root=pathlib.Path(__file__).resolve().parents[1], register_workspace=lambda value: {})
            self.assertEqual(paths.launch_agent.read_text(encoding="utf-8"), "custom content")
            self.assertEqual((paths.install_root / "config.json").read_bytes(), original)

    def test_retention_understands_windows_crlf_legacy_records(self):
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "short.md"
            target.write_bytes(b"## expired\r\n<!-- vibe-memory:managed-short -->\r\nexpires_on: 2026-01-01\r\n\r\nbody\r\n")
            removed = settings.prune_personal_short(target, retention_days=30, today=datetime.date(2026, 11, 5))
            self.assertEqual(removed, ["expired"])
            self.assertNotIn("body", target.read_text(encoding="utf-8"))

    def test_approved_short_envelope_preserves_byte_digest_and_backup(self):
        import datetime
        import memory_review_queue
        import vibe_memory_settings as settings
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "short.md"
            memory_review_queue.append_official_memory(path, {"id": "test-approved"}, "line one\nline two\n## user heading", managed_short=True)
            original = path.read_bytes()
            self.assertNotIn(b"\r\n", original)
            self.assertEqual(settings.prune_personal_short(path, today=datetime.date(2026, 11, 5), retention_days=30), [])
            before_prune = path.read_bytes()
            self.assertTrue(settings.prune_personal_short(path, retention_days=0))
            backups = list(path.parent.glob("short.md.bak.*"))
            self.assertIn(before_prune, [backup.read_bytes() for backup in backups])
            self.assertNotIn(b"line one", path.read_bytes())

    def test_runtime_config_rejects_invalid_ports_python_and_safety_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.prepare(pathlib.Path(directory))
            target = paths.install_root / "config.json"
            valid = json.loads(target.read_text(encoding="utf-8"))
            for changes in ({"port": True}, {"port": "8897"}, {"service_port": 8898}, {"python_executable": "python"}, {"python_version": "3.9"}, {"formal_memory_requires_approval": False}):
                with self.subTest(changes=changes):
                    target.write_text(json.dumps({**valid, **changes}), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        windows_runtime.config(paths)
