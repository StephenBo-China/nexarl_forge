import os
import json
import urllib.request
import urllib.error
import pathlib
import socket
import sys
import tempfile
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import vibe_memory_paths
import windows_service

@unittest.skipUnless(os.name == "nt", "native Windows service")
class WindowsServiceTests(unittest.TestCase):
    def test_background_start_health_duplicate_and_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = vibe_memory_paths.for_home(pathlib.Path(directory), platform="win32")
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            try:
                first = windows_service.start(paths, port=port)
                self.assertTrue(first["ok"])
                self.assertTrue(windows_service.status(paths)["ok"])
                request = urllib.request.Request(f"http://127.0.0.1:{port}/internal/native-stop", data=b"{}", headers={"Authorization": "Bearer " + "0" * 64})
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5)
                self.assertEqual(denied.exception.code, 403)
                self.assertTrue(windows_service.status(paths)["ok"])
                second = windows_service.start(paths, port=port)
                self.assertEqual(first["instance"], second["instance"])
                self.assertTrue(windows_service.stop(paths)["ok"])
                self.assertFalse(windows_service.status(paths)["ok"])
            finally:
                windows_service.stop(paths)

    def test_corrupt_state_and_foreign_port_fail_without_signalling(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = vibe_memory_paths.for_home(pathlib.Path(directory), platform="win32")
            record = paths.install_root / "state/service.json"
            record.parent.mkdir(parents=True)
            record.write_text('{"port": -1}', encoding="utf-8")
            with self.assertRaises(ValueError):
                windows_service.start(paths)
            record.unlink()
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                with self.assertRaises(RuntimeError):
                    windows_service.start(paths, port=listener.getsockname()[1])
                self.assertIsNone(listener.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) or None)
