"""Real filesystem and subprocess checks for platform storage primitives."""
import pathlib
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import runtime_platform as runtime
import vibe_memory_paths


class RuntimePlatformTests(unittest.TestCase):
    def test_windows_paths_use_localappdata_and_startup_folder(self):
        home = pathlib.Path("C:/Users/中文 user")
        local = pathlib.Path("D:/Local Data")
        roaming = pathlib.Path("D:/Roaming Data")
        paths = vibe_memory_paths.for_home(home, platform="win32", local_appdata=local, roaming_appdata=roaming)
        self.assertEqual(paths.install_root, local / "NexarlForge")
        self.assertEqual(paths.personal_memory, home / ".codex/personal_memory")
        self.assertEqual(paths.launcher, local / "NexarlForge/bin/nexarl-forge.cmd")
        self.assertEqual(paths.launch_agent, roaming / "Microsoft/Windows/Start Menu/Programs/Startup/NexarlForge.cmd")

    def test_explicit_home_isolated_from_real_appdata(self):
        home = pathlib.Path("C:/isolated-user")
        paths = vibe_memory_paths.for_home(home, platform="win32")
        self.assertEqual(paths.install_root, home / "AppData/Local/NexarlForge")

    def test_macos_paths_remain_compatible(self):
        home = pathlib.Path("/portable/home")
        paths = vibe_memory_paths.for_home(home, platform="darwin")
        self.assertEqual(paths.install_root, home / "Library/Application Support/VibeMemory")
        self.assertEqual(paths.launcher, home / ".local/bin/vibe-memory")

    def test_unicode_atomic_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "中文 space" / "memory.md"
            runtime.atomic_write(target, "第一版".encode("utf-8"))
            runtime.atomic_write(target, "第二版".encode("utf-8"))
            self.assertEqual(target.read_text(encoding="utf-8"), "第二版")
            self.assertEqual([p.name for p in target.parent.iterdir()], [target.name])

    def test_lock_contends_across_processes_and_releases_on_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = pathlib.Path(directory) / "memory.lock"
            code = "import sys;sys.path.insert(0,sys.argv[1]);import runtime_platform as r;import time;" + "\nwith r.file_lock(sys.argv[2]):\n print('ready',flush=True)\n time.sleep(30)"
            child = subprocess.Popen([sys.executable, "-c", code, str(pathlib.Path(runtime.__file__).parent), str(lock)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), "ready")
                with self.assertRaises(TimeoutError):
                    with runtime.file_lock(lock, timeout=0.1):
                        self.fail("concurrent acquisition")
            finally:
                child.kill()
                child.communicate(timeout=5)
            with runtime.file_lock(lock, timeout=1):
                pass

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_rejects_junction_ancestor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            actual = root / "actual"
            actual.mkdir()
            alias = root / "junction"
            result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(actual)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            try:
                with self.assertRaises(ValueError):
                    runtime.atomic_write(alias / "memory", b"unsafe")
                with self.assertRaises(ValueError):
                    with runtime.file_lock(alias / "lock"):
                        self.fail("junction lock accepted")
                self.assertEqual(list(actual.iterdir()), [])
            finally:
                alias.rmdir()

    def test_rejects_link_ancestor_and_preserves_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            actual = root / "actual"
            actual.mkdir()
            link = root / "alias"
            try:
                link.symlink_to(actual, target_is_directory=True)
            except OSError:
                self.skipTest("symlink privilege unavailable; junction covered separately on Windows")
            with self.assertRaises(ValueError):
                runtime.atomic_write(link / "memory", b"unsafe")
            self.assertFalse((actual / "memory").exists())


if __name__ == "__main__":
    unittest.main()


class WindowsPrivacyTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows ACL")
    def test_private_directory_has_protected_dacl(self):
        import ctypes
        from ctypes import wintypes
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "private"
            runtime.private_directory(target)
            advapi = ctypes.WinDLL("advapi32", use_last_error=True)
            needed = wintypes.DWORD()
            advapi.GetFileSecurityW(str(target), 4, None, 0, ctypes.byref(needed))
            buffer = ctypes.create_string_buffer(needed.value)
            self.assertTrue(advapi.GetFileSecurityW(str(target), 4, buffer, needed, ctypes.byref(needed)))
            control = wintypes.WORD()
            revision = wintypes.DWORD()
            self.assertTrue(advapi.GetSecurityDescriptorControl(buffer, ctypes.byref(control), ctypes.byref(revision)))
            self.assertTrue(control.value & 0x1000)
            (target / "test").write_text("retained", encoding="utf-8")
            runtime.private_directory(target)
            self.assertEqual((target / "test").read_text(encoding="utf-8"), "retained")
