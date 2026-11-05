import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import memory_project
import memory_review_queue
import vibe_memory_router
from vibe_memory_events import normalize_event

@unittest.skipUnless(os.name == "nt", "Windows registry implementation")
class WindowsRegistryTests(unittest.TestCase):
    def test_registry_round_trip_and_invalid_document(self):
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "中文 user" / "projects.json"
            with mock.patch.object(memory_project, "REGISTRY_PATH", target):
                self.assertEqual(memory_project.registry(), {"current_project": "", "projects": []})
                memory_project._mutate_registry(lambda data: data.update(current_project="中文", projects=[{"root": "D:/中文"}]))
                self.assertEqual(memory_project.registry()["current_project"], "中文")
                target.write_text("[]", encoding="utf-8")
                with self.assertRaises(ValueError):
                    memory_project.registry()

    def test_mutator_exception_preserves_existing_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "projects.json"
            with mock.patch.object(memory_project, "REGISTRY_PATH", target):
                memory_project._mutate_registry(lambda data: data.update(current_project="before"))
                before = target.read_bytes()
                def fail(data):
                    data["current_project"] = "after"
                    raise RuntimeError("interrupted")
                with self.assertRaises(RuntimeError):
                    memory_project._mutate_registry(fail)
                self.assertEqual(target.read_bytes(), before)

    def test_queue_lock_releases_after_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = pathlib.Path(directory) / "queue.lock"
            with self.assertRaises(RuntimeError):
                with memory_review_queue.queue_lock(lock):
                    raise RuntimeError("interrupted")
            with memory_review_queue.queue_lock(lock, timeout=0.1):
                pass

    def test_hook_idempotency_reserve_commit_and_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            store = vibe_memory_router.IdempotencyStore(pathlib.Path(directory) / "events.json")
            event = normalize_event("codex", "SessionStart", {"session_id": "native-test"}, pathlib.Path(directory))
            reservation = store.reserve(event)
            self.assertIsInstance(reservation, str)
            self.assertTrue(store.commit(event, reservation))
            self.assertIsNone(store.reserve(event))

    def test_context_packets_written_with_transaction_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            project = pathlib.Path(directory)
            vibe_memory_router._write_context_packets(project, "中文上下文")
            for name in vibe_memory_router.PACKET_NAMES:
                self.assertEqual((project / "codex" / name).read_text(encoding="utf-8"), "中文上下文")
            self.assertFalse((project / "codex" / vibe_memory_router.PACKET_JOURNAL_NAME).exists())

    def test_queue_refresh_subprocess_uses_registered_windows_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            project = root / "project"
            project.mkdir()
            registry = root / "registry.json"
            registry.write_text(json.dumps({"current_project": str(project), "projects": [{"root": str(project)}]}), encoding="utf-8")
            home = root / "home"
            home.mkdir()
            with mock.patch.dict(os.environ, {"USERPROFILE": str(home), "HOME": str(home), "MEMORY_REVIEW_PROJECT_REGISTRY": str(registry)}):
                counts = vibe_memory_router._refresh_review_queue(project)
            self.assertEqual(counts["pending"], 0)
            self.assertTrue((project / "codex/memory_review_queue.json").is_file())

    def test_idempotency_pid_probe_does_not_signal_current_process(self):
        with mock.patch.object(vibe_memory_router.os, "kill", side_effect=AssertionError("Windows must not use kill(pid, 0)")):
            self.assertTrue(vibe_memory_router.IdempotencyStore._pid_is_alive(os.getpid()))
            self.assertFalse(vibe_memory_router.IdempotencyStore._pid_is_alive(0x7fffffff))

    def test_context_candidate_command_uses_project_argument_not_unix_env(self):
        with tempfile.TemporaryDirectory() as directory:
            project = pathlib.Path(directory)
            event = normalize_event("codex", "SessionStart", {"session_id": "command-test"}, project)
            context = vibe_memory_router.build_context(event, project, {})
            command = context.split("Candidate CLI:\n\n", 1)[1].strip().splitlines()[0]
            self.assertNotIn("MEMORY_REVIEW_PROJECT_ROOT=", command)
            self.assertIn("--project-root", command)
            self.assertNotIn("'", command)
