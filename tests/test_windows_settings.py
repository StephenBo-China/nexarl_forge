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
