import json
import os
import pathlib
import socket
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from unittest import mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import vibe_memory_paths
import windows_runtime
import windows_service

@unittest.skipUnless(os.name == "nt", "Windows installer")
class WindowsInstallTests(unittest.TestCase):
    def test_install_copy_launcher_health_and_memory_preservation(self):
        with tempfile.TemporaryDirectory(prefix="forge \u4e2d\u6587 space ") as directory:
            home = pathlib.Path(directory)
            paths = vibe_memory_paths.for_home(home, platform="win32")
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            try:
                result = windows_runtime.install(paths, pathlib.Path(__file__).resolve().parents[1], port=port)
                self.assertTrue(result["ok"])
                self.assertTrue((paths.install_root / "current/scripts/vibe_memory_cli.py").is_file())
                self.assertTrue(paths.launcher.is_file())
                self.assertTrue(windows_service.status(paths)["ok"])
                report = windows_runtime.doctor(paths)
                self.assertTrue(all(area["ok"] for area in report.values()), report)
                self.assertEqual(set(report), {"runtime", "codex_hooks", "claude_hooks", "service", "data"})
                self.assertIn("python", (home / ".codex/hooks.json").read_text(encoding="utf-8"))
                request = urllib.request.Request(f"http://127.0.0.1:{port}/api/settings/first-run",
                    data=json.dumps({"start_at_login": True}).encode(), headers={"Content-Type": "application/json"})
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
                    first_run = json.load(response)
                self.assertTrue(first_run["settings"]["first_run_complete"])
                self.assertTrue(paths.launch_agent.is_file())
                memory = paths.personal_memory / "long.md"
                memory.write_text("keep this memory", encoding="utf-8")
                environment = os.environ.copy()
                environment.update(USERPROFILE=str(home), HOME=str(home), LOCALAPPDATA=str(paths.install_root.parent))
                command = subprocess.run([sys.executable, str(paths.install_root / "current/scripts/vibe_memory_cli.py"), "status"], env=environment, capture_output=True, text=True, encoding="utf-8", timeout=15)
                self.assertEqual(command.returncode, 0, command.stderr)
                self.assertTrue(json.loads(command.stdout)["service"]["ok"])
                batch = subprocess.run(["cmd.exe", "/d", "/c", str(paths.launcher), "status"], env=environment, capture_output=True, encoding="utf-8", timeout=15)
                self.assertEqual(batch.returncode, 0, batch.stderr)
                self.assertTrue(json.loads(batch.stdout)["service"]["ok"])
                bash = pathlib.Path(shutil.which("git")).parents[1] / "bin/bash.exe"
                bash_status = subprocess.run([str(bash), str(paths.launcher.with_suffix("")).replace("\\", "/"), "status"], env=environment, capture_output=True, encoding="utf-8", timeout=15)
                self.assertEqual(bash_status.returncode, 0, bash_status.stderr)
                self.assertTrue(json.loads(bash_status.stdout)["service"]["ok"])
                windows_runtime.uninstall(paths)
                self.assertEqual(memory.read_text(encoding="utf-8"), "keep this memory")
                self.assertFalse(windows_service.status(paths)["ok"])
                self.assertFalse(paths.launch_agent.exists())
            finally:
                windows_service.stop(paths)

    def test_failed_update_restores_original_runtime_and_config(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = vibe_memory_paths.for_home(pathlib.Path(directory), platform="win32")
            source = pathlib.Path(__file__).resolve().parents[1]
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            try:
                windows_runtime.install(paths, source, port=port)
                old = (paths.install_root / "config.json").read_bytes()
                sentinel = paths.install_root / "current/original.txt"
                sentinel.write_text("original", encoding="utf-8")
                with mock.patch.object(windows_runtime, "repair_hooks", side_effect=RuntimeError("injected failure")):
                    with self.assertRaisesRegex(RuntimeError, "injected failure"):
                        windows_runtime.install(paths, source, port=port)
                self.assertEqual((paths.install_root / "config.json").read_bytes(), old)
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "original")
                self.assertTrue(windows_service.status(paths)["ok"])
                real_repair = windows_runtime.repair_hooks
                target = pathlib.Path(directory) / ".codex/hooks.json"
                def concurrent_edit(selected_paths, **options):
                    document = json.loads(target.read_text(encoding="utf-8"))
                    document["custom_external_setting"] = "must survive failed update"
                    target.write_text(json.dumps(document), encoding="utf-8")
                    return real_repair(selected_paths, **options)
                with mock.patch.object(windows_runtime, "repair_hooks", side_effect=concurrent_edit):
                    with self.assertRaisesRegex(RuntimeError, "hook source changed"):
                        windows_runtime.install(paths, source, port=port)
                self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["custom_external_setting"], "must survive failed update")
                self.assertTrue(windows_service.status(paths)["ok"])
            finally:
                windows_service.stop(paths)

    def test_rollback_switches_previous_release_and_repair_preserves_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = vibe_memory_paths.for_home(pathlib.Path(directory), platform="win32")
            source = pathlib.Path(__file__).resolve().parents[1]
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            try:
                windows_runtime.install(paths, source, port=port)
                sentinel = paths.install_root / "current/original.txt"
                sentinel.write_text("first", encoding="utf-8")
                memory = paths.personal_memory / "long.md"
                memory.write_text("keep", encoding="utf-8")
                windows_runtime.install(paths, source, port=port)
                self.assertFalse(sentinel.exists())
                original_rename = pathlib.Path.rename
                def fail_archive(path, target):
                    if path.name.startswith(".rollback-") and not path.name.startswith(".rollback-failed-") and pathlib.Path(target).name == "previous":
                        raise OSError("injected archive failure")
                    return original_rename(path, target)
                with mock.patch.object(pathlib.Path, "rename", fail_archive):
                    with self.assertRaisesRegex(OSError, "injected archive failure"):
                        windows_runtime.rollback(paths)
                self.assertFalse(sentinel.exists())
                self.assertTrue((paths.install_root / "previous/original.txt").exists())
                self.assertTrue(windows_service.status(paths)["ok"])
                self.assertTrue(windows_runtime.rollback(paths)["ok"])
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "first")
                self.assertTrue(windows_service.status(paths)["ok"])
                paths.launcher.with_suffix("").unlink()
                self.assertTrue(windows_runtime.repair(paths)["ok"])
                self.assertTrue(paths.launcher.with_suffix("").is_file())
                self.assertEqual(memory.read_text(encoding="utf-8"), "keep")
            finally:
                windows_service.stop(paths)

    def test_cmd_and_git_bash_installers_survive_source_removal(self):
        with tempfile.TemporaryDirectory(prefix="forge-entry-") as directory:
            base = pathlib.Path(directory)
            home = base / "user"
            home.mkdir()
            paths = vibe_memory_paths.for_home(home, platform="win32")
            source = base / "source"
            source.mkdir()
            original = pathlib.Path(__file__).resolve().parents[1]
            for name in ("scripts", "templates", "docs"):
                shutil.copytree(original / name, source / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            for name in ("README.md", "release.json", "install.cmd", "install.sh"):
                shutil.copy2(original / name, source / name)
            env = os.environ.copy()
            env.update(USERPROFILE=str(home), HOME=str(home), LOCALAPPDATA=str(home / "AppData/Local"), APPDATA=str(home / "AppData/Roaming"), PYTHONUTF8="1")
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            bash = pathlib.Path(shutil.which("git")).parents[1] / "bin/bash.exe"
            try:
                for command in (["cmd.exe", "/d", "/c", str(source / "install.cmd"), "--port", str(port)],
                                [str(bash), str(source / "install.sh").replace("\\", "/"), "--port", str(port)]):
                    result = subprocess.run(command, env=env, capture_output=True, encoding="utf-8", timeout=45)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                source.rename(base / "source-moved")
                command = subprocess.run(["cmd.exe", "/d", "/c", str(paths.launcher), "status"], env=env, capture_output=True, encoding="utf-8", timeout=15)
                self.assertEqual(command.returncode, 0, command.stderr)
                self.assertTrue(json.loads(command.stdout)["service"]["ok"])
            finally:
                windows_service.stop(paths)

    def test_first_run_http_restarts_on_new_port_and_can_stop_itself(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = vibe_memory_paths.for_home(pathlib.Path(directory), platform="win32")
            def unused_port():
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    return listener.getsockname()[1]
            old_port, new_port = unused_port(), unused_port()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            def configure(port, body):
                request = urllib.request.Request(f"http://127.0.0.1:{port}/api/settings/first-run", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
                with opener.open(request, timeout=10) as response:
                    return json.load(response)
            try:
                windows_runtime.install(paths, pathlib.Path(__file__).resolve().parents[1], port=old_port)
                reply = configure(old_port, {"start_at_login": True, "service_port": new_port})
                self.assertTrue(reply["restart_after_response"])
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    observed = windows_service.status(paths)
                    if observed["ok"] and observed["port"] == new_port:
                        break
                    time.sleep(0.1)
                self.assertTrue(observed["ok"], observed)
                self.assertEqual(observed["port"], new_port)
                reply = configure(new_port, {"start_at_login": False, "service_port": new_port})
                self.assertTrue(reply["bootout_after_response"])
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and windows_service.status(paths)["ok"]:
                    time.sleep(0.1)
                self.assertFalse(windows_service.status(paths)["ok"])
                self.assertFalse(paths.launch_agent.exists())
            finally:
                windows_service.stop(paths)
