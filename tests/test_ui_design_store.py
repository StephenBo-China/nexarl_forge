from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ui_design_store as store


class UIStoreTest(unittest.TestCase):
    def test_atomic_json_round_trip_and_backup(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = pathlib.Path(value) / "registry.json"

            store.atomic_write_json(path, {"version": 1})
            store.atomic_write_json(path, {"version": 2}, backup=True)

            self.assertEqual(store.read_json_strict(path), {"version": 2})
            backups = list(path.parent.glob("registry.json.bak.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(json.loads(backups[0].read_text()), {"version": 1})

    def test_tree_digest_ignores_mtime_and_orders_paths(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            (root / "b.txt").write_text("b", encoding="utf-8")
            (root / "a.txt").write_text("a", encoding="utf-8")

            first = store.tree_digest(root)
            os.utime(root / "a.txt", None)

            self.assertEqual(first, store.tree_digest(root))

    def test_append_jsonl_writes_one_complete_record_per_line(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = pathlib.Path(value) / "audit.jsonl"

            store.append_jsonl(path, {"event": "created", "label": "设计"})
            store.append_jsonl(path, {"event": "approved"})

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual([json.loads(line) for line in lines], [
                {"event": "created", "label": "设计"},
                {"event": "approved"},
            ])

    def test_exclusive_lock_times_out_with_stale_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = pathlib.Path(value) / "registry.lock"
            path.write_text('{"pid": 42}\n', encoding="utf-8")
            old = time.time() - 120
            os.utime(path, (old, old))

            with self.assertRaises(store.LockTimeout) as caught:
                with store.exclusive_lock(
                    path, timeout=0.01, stale_after=60, poll_interval=0.001
                ):
                    self.fail("contended lock must not be acquired")

            self.assertTrue(caught.exception.stale)
            self.assertIn(str(path), str(caught.exception))
            self.assertTrue(path.exists())

    def test_exclusive_lock_removes_its_lock_file_on_exit(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = pathlib.Path(value) / "registry.lock"

            with store.exclusive_lock(path, timeout=0.1):
                self.assertTrue(path.exists())
                metadata = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(metadata["pid"], os.getpid())

            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
