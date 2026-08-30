from __future__ import annotations

import concurrent.futures
import io
import json
import os
import pathlib
import signal
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import memory_project
import memory_review_queue
from vibe_memory_events import NormalizedEvent
import vibe_memory_router
from vibe_memory_router import IdempotencyStore, build_context, resolve_registered_project


class VibeMemoryRouterTest(unittest.TestCase):
    def event(self, **changes: object) -> NormalizedEvent:
        values: dict[str, object] = {
            "agent": "codex",
            "event": "user_prompt_submit",
            "cwd": pathlib.Path("/tmp/workspace"),
            "session_id": "session-1",
            "timestamp": "2026-07-30T12:00:00+08:00",
            "payload_digest": "a" * 64,
        }
        values.update(changes)
        return NormalizedEvent(**values)  # type: ignore[arg-type]

    def test_resolve_registered_project_prefers_deepest_matching_root(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = pathlib.Path(value)
            outer = base / "workspace"
            inner = outer / "nested-project"
            cwd = inner / "src" / "feature"
            cwd.mkdir(parents=True)

            resolved = resolve_registered_project(
                cwd,
                [{"root": str(outer)}, {"root": str(inner)}],
            )

            self.assertEqual(resolved, inner.resolve())

    def test_resolve_registered_project_returns_none_for_unregistered_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = pathlib.Path(value)
            registered = base / "registered"
            cwd = base / "unregistered"
            registered.mkdir()
            cwd.mkdir()

            self.assertIsNone(resolve_registered_project(cwd, [{"root": str(registered)}]))
            self.assertFalse((cwd / "codex").exists())

    def test_resolve_registered_project_ignores_malformed_entries(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value) / "project"
            cwd = root / "src"
            cwd.mkdir(parents=True)

            resolved = resolve_registered_project(
                cwd,
                [None, "not a project", {}, {"root": None}, {"root": ""}, {"root": str(root)}],
            )

            self.assertEqual(resolved, root.resolve())

    def test_resolve_registered_project_ignores_invalid_root_strings(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value) / "project"
            cwd = root / "src"
            cwd.mkdir(parents=True)

            for invalid_root in ("~nonexistent-user/project", "bad\x00root"):
                with self.subTest(invalid_root=invalid_root):
                    resolved = resolve_registered_project(
                        cwd,
                        [{"root": invalid_root}, {"root": str(root)}],
                    )

                    self.assertEqual(resolved, root.resolve())

    def test_resolve_registered_project_uses_canonical_symlink_paths(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = pathlib.Path(value)
            actual = base / "actual-project"
            actual.mkdir()
            link = base / "linked-project"
            try:
                link.symlink_to(actual, target_is_directory=True)
            except (NotImplementedError, OSError) as error:
                self.skipTest(f"symlinks unavailable: {error}")
            cwd = link / "src"
            cwd.mkdir()

            resolved = resolve_registered_project(cwd, [{"root": str(actual)}])

            self.assertEqual(resolved, actual.resolve())

    def test_resolve_registered_project_ignores_non_directory_registered_root(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = pathlib.Path(value)
            missing = base / "missing"

            self.assertIsNone(
                resolve_registered_project(missing / "child", [{"root": str(missing)}])
            )

    def test_register_project_allows_non_git_directory_and_makes_it_current(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = pathlib.Path(value)
            project = base / "ordinary-directory"
            project.mkdir()
            registry_path = base / "projects.json"
            original_registry = memory_project.REGISTRY_PATH
            memory_project.REGISTRY_PATH = registry_path
            try:
                data = memory_project.register_project(project)
            finally:
                memory_project.REGISTRY_PATH = original_registry

            self.assertEqual(data["current_project"], str(project.resolve()))
            self.assertEqual(data["projects"][0]["root"], str(project.resolve()))
            self.assertFalse(data["projects"][0]["is_git_repo"])
            self.assertEqual(json.loads(registry_path.read_text(encoding="utf-8")), data)
            self.assertFalse((project / "codex").exists())

    def test_register_project_rejects_invalid_roots_without_mutating_registry(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = pathlib.Path(value)
            registry_path = base / "projects.json"
            original = {"current_project": "/existing", "projects": [{"root": "/existing"}]}
            registry_path.write_text(json.dumps(original), encoding="utf-8")
            regular_file = base / "not-a-directory"
            regular_file.write_text("nope\n", encoding="utf-8")
            original_registry = memory_project.REGISTRY_PATH
            memory_project.REGISTRY_PATH = registry_path
            try:
                for invalid in (base / "does-not-exist", regular_file):
                    with self.subTest(invalid=invalid):
                        with self.assertRaisesRegex(ValueError, "existing directory"):
                            memory_project.register_project(invalid)
                        self.assertEqual(
                            json.loads(registry_path.read_text(encoding="utf-8")), original
                        )
            finally:
                memory_project.REGISTRY_PATH = original_registry

    def test_idempotency_store_reserves_then_commits_duplicate_event(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            store = IdempotencyStore(pathlib.Path(value) / "events.json")
            event = self.event()

            reservation = store.reserve(event)

            self.assertIsInstance(reservation, str)
            self.assertIsNone(store.reserve(event))
            self.assertTrue(store.commit(event, reservation))
            self.assertIsNone(store.reserve(event))

    def test_idempotency_store_reclaims_expired_event(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            store = IdempotencyStore(pathlib.Path(value) / "events.json", ttl_seconds=30)
            event = self.event()

            with mock.patch("vibe_memory_router.time.time", return_value=100.0):
                reservation = store.reserve(event)
                self.assertTrue(store.commit(event, reservation))
            with mock.patch("vibe_memory_router.time.time", return_value=131.0):
                self.assertIsNotNone(store.reserve(event))

    def test_idempotency_store_distinguishes_each_key_field(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            store = IdempotencyStore(pathlib.Path(value) / "events.json")
            base = self.event()
            variants = [
                self.event(agent="claude-code"),
                self.event(session_id="session-2"),
                self.event(event="stop"),
                self.event(cwd=pathlib.Path("/tmp/other")),
                self.event(payload_digest="b" * 64),
            ]

            self.assertIsNotNone(store.reserve(base))
            for event in variants:
                with self.subTest(event=event):
                    self.assertIsNotNone(store.reserve(event))

    def test_idempotency_store_does_not_collide_on_pipe_delimited_fields(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            store = IdempotencyStore(pathlib.Path(value) / "events.json")
            first = self.event(session_id="session|event", event="stop")
            second = self.event(session_id="session", event="event|stop")

            self.assertIsNotNone(store.reserve(first))
            self.assertIsNotNone(store.reserve(second))

    def test_idempotency_store_preserves_corrupt_or_nonobject_data(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = pathlib.Path(value) / "events.json"
            for original in ("not json\n", "[]\n"):
                with self.subTest(original=original):
                    path.write_text(original, encoding="utf-8")
                    store = IdempotencyStore(path)

                    with self.assertRaisesRegex(ValueError, "idempotency store"):
                        store.reserve(self.event())

                    self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_idempotency_release_and_commit_require_reservation_owner(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            store = IdempotencyStore(pathlib.Path(value) / "events.json")
            event = self.event()
            reservation = store.reserve(event)
            self.assertIsInstance(reservation, str)

            self.assertFalse(store.release(event, "not-the-owner"))
            self.assertFalse(store.commit(event, "not-the-owner"))
            self.assertIsNone(store.reserve(event))
            self.assertTrue(store.release(event, reservation))
            self.assertIsNotNone(store.reserve(event))

    def test_idempotency_concurrent_reserve_has_one_owner(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = pathlib.Path(value) / "events.json"
            event = self.event()

            def reserve() -> str | None:
                return IdempotencyStore(path).reserve(event)

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                reservations = list(executor.map(lambda _: reserve(), range(8)))

            self.assertEqual(sum(item is not None for item in reservations), 1)

    def test_handle_event_releases_reservation_after_downstream_failure(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            state = pathlib.Path(value) / "events.json"
            event_payload = {"session_id": "retry-session"}
            safe_context = "personal-only context"

            with mock.patch(
                "vibe_memory_router._idempotency_path", return_value=state
            ), mock.patch(
                "vibe_memory_router._registry_projects", return_value=[]
            ), mock.patch(
                "vibe_memory_router.build_context",
                side_effect=[RuntimeError("downstream failed"), safe_context],
            ):
                with self.assertRaisesRegex(RuntimeError, "downstream failed"):
                    vibe_memory_router.handle_event(
                        "codex", "SessionStart", event_payload, pathlib.Path(value)
                    )
                retried = vibe_memory_router.handle_event(
                    "codex", "SessionStart", event_payload, pathlib.Path(value)
                )

            self.assertEqual(retried["status"], "ok")

    def test_dead_reservation_owner_is_reclaimed_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = pathlib.Path(value) / "events.json"
            code = (
                "import pathlib, sys\n"
                "from vibe_memory_events import NormalizedEvent\n"
                "from vibe_memory_router import IdempotencyStore\n"
                "event = NormalizedEvent('codex', 'SessionStart', pathlib.Path(sys.argv[2]), "
                "'dead-session', '2026-07-30T12:00:00+08:00', 'd' * 64)\n"
                "print(IdempotencyStore(pathlib.Path(sys.argv[1])).reserve(event), flush=True)\n"
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(ROOT / "scripts")
            completed = subprocess.run(
                [sys.executable, "-c", code, str(path), value],
                env=environment,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertTrue(completed.stdout.strip())
            event = self.event(
                event="SessionStart",
                cwd=pathlib.Path(value),
                session_id="dead-session",
                payload_digest="d" * 64,
            )

            self.assertIsNotNone(IdempotencyStore(path).reserve(event))

    def test_killed_lock_holder_does_not_block_next_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = pathlib.Path(value) / "events.json"
            code = (
                "import pathlib, sys, time\n"
                "from vibe_memory_router import IdempotencyStore\n"
                "store = IdempotencyStore(pathlib.Path(sys.argv[1]))\n"
                "with store._locked():\n"
                "    print('locked', flush=True)\n"
                "    time.sleep(60)\n"
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(ROOT / "scripts")
            process = subprocess.Popen(
                [sys.executable, "-c", code, str(path)],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(process.stdout.readline().strip(), "locked")
                process.kill()
                process.wait(timeout=5)
                started = time.monotonic()
                reservation = IdempotencyStore(path).reserve(self.event())
                elapsed = time.monotonic() - started
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

            self.assertIsNotNone(reservation)
            self.assertLess(elapsed, 2.0)

    def test_context_packet_transaction_rolls_back_both_on_second_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            project = pathlib.Path(value)
            codex = project / "codex"
            codex.mkdir()
            first = codex / "codex_context_packet.md"
            second = codex / "shared_memory_context_packet.md"
            first.write_text("old\n", encoding="utf-8")
            second.write_text("old\n", encoding="utf-8")
            original_write = vibe_memory_router._atomic_write_at
            failed = False

            def fail_second_once(
                directory_fd: int, name: str, content: str, mode: int = 0o644
            ) -> None:
                nonlocal failed
                if (
                    name == "shared_memory_context_packet.md"
                    and content == "new\n"
                    and not failed
                ):
                    failed = True
                    raise OSError("injected second write failure")
                original_write(directory_fd, name, content, mode)

            with mock.patch(
                "vibe_memory_router._atomic_write_at", side_effect=fail_second_once
            ):
                with self.assertRaisesRegex(OSError, "second write failure"):
                    vibe_memory_router._write_context_packets(project, "new\n")

            self.assertEqual(first.read_text(encoding="utf-8"), "old\n")
            self.assertEqual(second.read_text(encoding="utf-8"), "old\n")
            self.assertFalse((codex / ".vibe-memory-packets-journal.json").exists())

    def test_context_packet_transaction_recovers_interrupted_journal(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            project = pathlib.Path(value)
            codex = project / "codex"
            codex.mkdir()
            first = codex / "codex_context_packet.md"
            second = codex / "shared_memory_context_packet.md"
            first.write_text("old\n", encoding="utf-8")
            second.write_text("old\n", encoding="utf-8")
            original_write = vibe_memory_router._atomic_write_at

            def interrupt_and_break_rollback(
                directory_fd: int, name: str, content: str, mode: int = 0o644
            ) -> None:
                if name == "shared_memory_context_packet.md" and content == "new\n":
                    raise OSError("injected crash before second write")
                if content == "old\n":
                    raise OSError("injected crash during rollback")
                original_write(directory_fd, name, content, mode)

            with mock.patch(
                "vibe_memory_router._atomic_write_at",
                side_effect=interrupt_and_break_rollback,
            ):
                with self.assertRaises(OSError):
                    vibe_memory_router._write_context_packets(project, "new\n")

            journal = codex / ".vibe-memory-packets-journal.json"
            self.assertTrue(journal.exists())
            vibe_memory_router._write_context_packets(project, "new\n")

            self.assertEqual(first.read_text(encoding="utf-8"), "new\n")
            self.assertEqual(second.read_text(encoding="utf-8"), "new\n")
            self.assertFalse(journal.exists())

    def test_context_packet_transaction_serializes_interleaved_writers(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            project = pathlib.Path(value)
            (project / "codex").mkdir()
            first_written = threading.Event()
            original_write = vibe_memory_router._atomic_write_at

            def slow_first_writer(
                directory_fd: int, name: str, content: str, mode: int = 0o644
            ) -> None:
                original_write(directory_fd, name, content, mode)
                if name == "codex_context_packet.md" and content == "first\n":
                    first_written.set()
                    time.sleep(0.15)

            with mock.patch(
                "vibe_memory_router._atomic_write_at", side_effect=slow_first_writer
            ):
                first_thread = threading.Thread(
                    target=vibe_memory_router._write_context_packets,
                    args=(project, "first\n"),
                )
                second_thread = threading.Thread(
                    target=vibe_memory_router._write_context_packets,
                    args=(project, "second\n"),
                )
                first_thread.start()
                self.assertTrue(first_written.wait(timeout=2))
                second_thread.start()
                first_thread.join(timeout=3)
                second_thread.join(timeout=3)

            self.assertFalse(first_thread.is_alive())
            self.assertFalse(second_thread.is_alive())
            first = (project / "codex" / "codex_context_packet.md").read_text(
                encoding="utf-8"
            )
            second = (project / "codex" / "shared_memory_context_packet.md").read_text(
                encoding="utf-8"
            )
            self.assertEqual(first, second)

    def test_packet_io_helpers_never_follow_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            outside = root / "outside.txt"
            outside.write_text("sentinel\n", encoding="utf-8")
            (root / "packet.md").symlink_to(outside)
            directory_fd = os.open(root, vibe_memory_router._directory_open_flags())
            try:
                with self.assertRaisesRegex(ValueError, "unsafe packet path"):
                    vibe_memory_router._read_at(directory_fd, "packet.md")
                with self.assertRaisesRegex(ValueError, "unsafe packet path"):
                    vibe_memory_router._atomic_write_at(
                        directory_fd, "packet.md", "replacement\n"
                    )
            finally:
                os.close(directory_fd)

            self.assertEqual(outside.read_text(encoding="utf-8"), "sentinel\n")

    def test_packet_transaction_parent_swap_cannot_escape_open_codex_fd(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            project = pathlib.Path(value)
            codex = project / "codex"
            codex.mkdir()
            outside = project / "outside"
            outside.mkdir()
            held = project / "codex-held"
            original_write = vibe_memory_router._atomic_write_at
            swapped = False

            def swap_parent_then_write(
                directory_fd: int, name: str, content: str, mode: int = 0o644
            ) -> None:
                nonlocal swapped
                if not swapped:
                    codex.rename(held)
                    codex.symlink_to(outside, target_is_directory=True)
                    swapped = True
                original_write(directory_fd, name, content, mode)

            with mock.patch(
                "vibe_memory_router._atomic_write_at",
                side_effect=swap_parent_then_write,
            ):
                vibe_memory_router._write_context_packets(project, "safe\n")

            self.assertEqual(list(outside.iterdir()), [])
            self.assertEqual(
                (held / "codex_context_packet.md").read_text(encoding="utf-8"),
                "safe\n",
            )
            self.assertEqual(
                (held / "shared_memory_context_packet.md").read_text(encoding="utf-8"),
                "safe\n",
            )

    def test_queue_refresh_parent_swap_cannot_escape_open_codex_fd(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            project = pathlib.Path(value)
            codex = project / "codex"
            codex.mkdir()
            outside = project / "outside"
            outside.mkdir()
            held = project / "codex-held"
            original_popen = subprocess.Popen
            swapped = False

            def swap_parent_then_spawn(
                *args: object, **kwargs: object
            ) -> subprocess.Popen[str]:
                nonlocal swapped
                if not swapped:
                    codex.rename(held)
                    codex.symlink_to(outside, target_is_directory=True)
                    swapped = True
                return original_popen(*args, **kwargs)  # type: ignore[arg-type]

            with mock.patch(
                "vibe_memory_router.subprocess.Popen",
                side_effect=swap_parent_then_spawn,
            ), mock.patch(
                "vibe_memory_router.pathlib.Path.home", return_value=project / "home"
            ):
                counts = vibe_memory_router._refresh_review_queue(project)

            self.assertEqual(counts["pending"], 0)
            self.assertEqual(list(outside.iterdir()), [])
            self.assertTrue((held / "memory_review_state.json").exists())
            self.assertTrue((held / "memory_review_queue.json").exists())
            self.assertTrue((held / "memory_review_queue.json.lock").exists())
            self.assertFalse((held / ".vibe-memory-packets.lock").exists())

    def test_hook_refresh_does_not_block_normal_queue_build_on_persistent_lock(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            project = pathlib.Path(value)
            codex = project / "codex"
            codex.mkdir()
            personal = project / "home" / ".codex" / "personal_memory"
            personal.mkdir(parents=True)
            (personal / "proposals.md").write_text("# Proposals\n", encoding="utf-8")
            original_lock = memory_review_queue.exclusive_lock
            paths = {
                "PROJECT_PROPOSALS": codex / "memory_proposals.md",
                "PROJECT_QUEUE": codex / "memory_review_queue.json",
                "PROJECT_STATE": codex / "memory_review_state.json",
                "PERSONAL_PROPOSALS": personal / "proposals.md",
            }

            with mock.patch(
                "vibe_memory_router.pathlib.Path.home", return_value=project / "home"
            ):
                vibe_memory_router._refresh_review_queue(project)
            with mock.patch.multiple(memory_review_queue, **paths), mock.patch(
                "memory_review_queue.exclusive_lock",
                side_effect=lambda path: original_lock(path, timeout=0.05),
            ):
                started = time.monotonic()
                queue = memory_review_queue.build_queue()

            self.assertEqual(queue["counts"]["pending"], 0)
            self.assertLess(time.monotonic() - started, 1.0)

    def test_path_and_descriptor_queue_producers_are_mutually_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            project = pathlib.Path(value)
            codex = project / "codex"
            codex.mkdir()
            home = project / "home"
            (home / ".codex" / "personal_memory").mkdir(parents=True)
            script = (
                "import sys\n"
                f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
                "import memory_review_queue as queue\n"
                "original = queue.parse_project_candidates\n"
                "def parse_while_locked():\n"
                " print('locked', flush=True)\n"
                " sys.stdin.readline()\n"
                " return original()\n"
                "queue.parse_project_candidates = parse_while_locked\n"
                "queue.build_queue()\n"
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(home),
                    "MEMORY_REVIEW_PROJECT_ROOT": str(project),
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
            )
            process = subprocess.Popen(
                [sys.executable, "-c", script],
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            finished = threading.Event()
            errors: list[BaseException] = []

            def refresh() -> None:
                try:
                    with mock.patch(
                        "vibe_memory_router.pathlib.Path.home", return_value=home
                    ):
                        vibe_memory_router._refresh_review_queue(project)
                except BaseException as error:
                    errors.append(error)
                finally:
                    finished.set()

            try:
                self.assertEqual(process.stdout.readline().strip(), "locked")
                thread = threading.Thread(target=refresh)
                thread.start()
                self.assertFalse(finished.wait(timeout=0.15))
                assert process.stdin is not None
                process.stdin.write("release\n")
                process.stdin.flush()
                process.wait(timeout=3)
                self.assertEqual(process.returncode, 0, process.stderr.read())
                thread.join(timeout=3)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()

            self.assertTrue(finished.is_set())
            self.assertEqual(errors, [])

    def test_queue_lock_is_released_when_holder_is_killed(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            lock_path = pathlib.Path(value) / "memory_review_queue.json.lock"
            script = (
                "import pathlib,sys,time\n"
                f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
                "import memory_review_queue as queue\n"
                "with queue.queue_lock(path=pathlib.Path(sys.argv[1]), timeout=2):\n"
                " print('locked', flush=True)\n"
                " time.sleep(30)\n"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(lock_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(process.stdout.readline().strip(), "locked")
                process.kill()
                process.wait(timeout=3)
                started = time.monotonic()
                with memory_review_queue.queue_lock(path=lock_path, timeout=1):
                    pass
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

            self.assertLess(time.monotonic() - started, 1.0)

    def test_queue_lock_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            outside = root / "outside.lock"
            outside.write_text("sentinel\n", encoding="utf-8")
            lock_path = root / "memory_review_queue.json.lock"
            lock_path.symlink_to(outside)

            with self.assertRaisesRegex(ValueError, "unsafe queue lock"):
                with memory_review_queue.queue_lock(path=lock_path, timeout=0):
                    pass

            self.assertEqual(outside.read_text(encoding="utf-8"), "sentinel\n")

    def test_queue_refresh_enforces_total_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            project = pathlib.Path(value)
            (project / "codex").mkdir()

            with self.assertRaisesRegex(TimeoutError, "queue refresh deadline"):
                vibe_memory_router._refresh_review_queue(
                    project, timeout_seconds=0.0
                )

            self.assertFalse((project / "codex" / "memory_review_queue.json").exists())

    def test_queue_refresh_hard_deadline_interrupts_blocking_stage(self) -> None:
        if not hasattr(signal, "setitimer"):
            self.skipTest("interval timers unavailable")
        with tempfile.TemporaryDirectory() as value:
            project = pathlib.Path(value)
            (project / "codex").mkdir()
            previous_handler = signal.getsignal(signal.SIGALRM)

            def existing_handler(_signum: int, _frame: object) -> None:
                pass

            signal.signal(signal.SIGALRM, existing_handler)
            signal.setitimer(signal.ITIMER_REAL, 5.0)
            timer_before = signal.getitimer(signal.ITIMER_REAL)
            blocking_command = [
                sys.executable,
                "-c",
                "import time; time.sleep(30)",
            ]

            started = time.monotonic()
            try:
                with mock.patch(
                    "vibe_memory_router._queue_worker_command",
                    return_value=blocking_command,
                ), self.assertRaisesRegex(TimeoutError, "queue refresh deadline"):
                    vibe_memory_router._refresh_review_queue(
                        project, timeout_seconds=0.05
                    )
                elapsed = time.monotonic() - started
                timer_after = signal.getitimer(signal.ITIMER_REAL)
                self.assertIs(signal.getsignal(signal.SIGALRM), existing_handler)
                self.assertAlmostEqual(
                    timer_after[0], timer_before[0] - elapsed, delta=0.2
                )
                self.assertEqual(timer_after[1], timer_before[1])
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous_handler)

            self.assertLess(elapsed, 0.5)

    def test_queue_worker_timeout_from_thread_kills_reaps_and_cannot_write_late(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            project = pathlib.Path(value)
            (project / "codex").mkdir()
            marker = project / "late-worker-write.txt"
            command = [
                sys.executable,
                "-c",
                (
                    "import pathlib,sys,time; time.sleep(0.5); "
                    "pathlib.Path(sys.argv[1]).write_text('late')"
                ),
                str(marker),
            ]
            original_popen = subprocess.Popen
            children: list[subprocess.Popen[str]] = []
            errors: list[BaseException] = []

            def capture_popen(*args: object, **kwargs: object) -> subprocess.Popen[str]:
                process = original_popen(*args, **kwargs)  # type: ignore[arg-type]
                children.append(process)
                return process

            def refresh() -> None:
                try:
                    vibe_memory_router._refresh_review_queue(
                        project, timeout_seconds=0.05
                    )
                except BaseException as error:
                    errors.append(error)

            with mock.patch(
                "vibe_memory_router._queue_worker_command", return_value=command
            ), mock.patch(
                "vibe_memory_router.subprocess.Popen", side_effect=capture_popen
            ):
                thread = threading.Thread(target=refresh)
                thread.start()
                thread.join(timeout=0.5)

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], TimeoutError)
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())
            children[0].wait(timeout=0.1)
            time.sleep(0.6)
            self.assertFalse(marker.exists())
            self.assertFalse(
                (project / "codex" / "memory_review_queue.json").exists()
            )

    def test_queue_worker_failures_and_oversized_output_are_safe(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            project = pathlib.Path(value)
            (project / "codex").mkdir()
            commands = {
                "exit": [sys.executable, "-c", "raise SystemExit(3)"],
                "invalid": [
                    sys.executable,
                    "-c",
                    "print('SECRET_WORKER_OUTPUT')",
                ],
                "oversized": [
                    sys.executable,
                    "-c",
                    (
                        "import sys; sys.stdout.write('x' * "
                        f"{vibe_memory_router.MAX_QUEUE_WORKER_OUTPUT_BYTES + 1})"
                    ),
                ],
            }

            for kind, command in commands.items():
                with self.subTest(kind=kind), mock.patch(
                    "vibe_memory_router._queue_worker_command", return_value=command
                ):
                    with self.assertRaises(ValueError) as raised:
                        vibe_memory_router._refresh_review_queue(
                            project, timeout_seconds=1.0
                        )
                    message = str(raised.exception)
                    self.assertNotIn("SECRET_WORKER_OUTPUT", message)
                    self.assertNotIn(str(project), message)

    def test_queue_worker_entry_rejects_untrusted_arguments(self) -> None:
        cases = (
            [],
            ["--queue-worker", "not-a-fd", "/tmp/project", "1"],
            ["--queue-worker", "0", "/tmp/project", "1"],
            ["--queue-worker", "3", "relative-project", "1"],
            ["--queue-worker", "3", "/tmp/project", "nan"],
            ["--queue-worker", "3", "/tmp/project", "9"],
        )
        for arguments in cases:
            with self.subTest(arguments=arguments), io.StringIO() as stdout, mock.patch(
                "vibe_memory_router.sys.stdout", stdout
            ):
                self.assertEqual(vibe_memory_router._queue_worker_main(list(arguments)), 70)
                self.assertEqual(stdout.getvalue(), "")

    def test_build_context_for_unregistered_cwd_is_personal_only(self) -> None:
        event = self.event(cwd=pathlib.Path("/tmp/not-registered"))

        context = build_context(
            event,
            project_root=None,
            pending={"pending": 4, "personal_pending": 4, "project_pending": 0},
        )

        personal = pathlib.Path.home() / ".codex" / "personal_memory"
        for name in ("long.md", "short.md", "proposals.md"):
            self.assertIn(str(personal / name), context)
        self.assertNotIn("README.md", context)
        self.assertNotIn("codex/codex_long_memory.md", context)
        self.assertNotIn("Repository:", context)
        self.assertNotIn(str(event.cwd), context)

    def test_build_context_for_registered_project_includes_shared_policy(self) -> None:
        project = pathlib.Path("/tmp/registered-project")
        event = self.event(agent="claude-code", cwd=project / "src")

        context = build_context(
            event,
            project_root=project,
            pending={"pending": 5, "personal_pending": 2, "project_pending": 3},
        )

        for relative in (
            "README.md",
            "codex/codex_long_memory.md",
            "codex/codex_short_memory.md",
            "codex/memory_proposals.md",
            "codex/codex_context_packet.md",
        ):
            self.assertIn(str(project / relative), context)
        self.assertIn("source agent: claude-code", context)
        self.assertIn("pending total: 5", context)
        self.assertIn("personal candidates: 2", context)
        self.assertIn("project candidates: 3", context)
        self.assertIn("development_habit", context)
        self.assertIn("workflow_preference", context)
        self.assertIn("project_architecture", context)
        self.assertIn("project_workflow", context)
        self.assertIn("at most two", context.lower())
        self.assertIn("raw prompts", context.lower())
        self.assertIn("secrets", context.lower())
        self.assertIn("paths", context.lower())
        self.assertIn("one-off", context.lower())
        self.assertIn("explicit approval of the exact candidate content", context.lower())
        self.assertIn("vibe_memory_cli.py", context)
        self.assertIn("memory propose", context)
        self.assertIn("--source-agent claude-code", context)
        self.assertIn("--policy-version 1", context)

    def test_build_context_quotes_hostile_paths_and_escapes_markdown(self) -> None:
        project = pathlib.Path("/tmp/project with spaces ' $(touch nope) `tick`")
        event = self.event(event="prompt `tick` $(touch nope)")

        context = build_context(event, project_root=project, pending={})

        escaped_project = str(project).replace("\\", "\\\\").replace("`", "\\`")
        escaped_event = event.event.replace("\\", "\\\\").replace("`", "\\`")
        self.assertIn(f"Registered project: `{escaped_project}`", context)
        self.assertIn(f"- event: {escaped_event}", context)
        marker = "Candidate CLI:\n\n    "
        self.assertIn(marker, context)
        command = context.split(marker, 1)[1].splitlines()[0]
        script = pathlib.Path(vibe_memory_router.__file__).with_name("vibe_memory_cli.py")
        expected_parts = [
            "env",
            f"MEMORY_REVIEW_PROJECT_ROOT={project}",
            sys.executable,
            str(script),
            "memory",
            "propose",
            "--scope", "personal",
            "--target", "long",
            "--category", "CATEGORY",
            "--title", "TITLE",
            "--summary", "SUMMARY",
            "--source-event", "agent_summary",
            "--source-agent", "codex",
            "--policy-version", "1",
        ]
        self.assertEqual(shlex.split(command), expected_parts)
        self.assertEqual(command, " ".join(shlex.quote(part) for part in expected_parts))

    def test_build_context_prefers_stable_launcher_for_candidate_cli(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            temp = pathlib.Path(value)
            launcher = temp / "bin" / "vibe-memory"
            launcher.parent.mkdir()
            launcher.write_text("#!/bin/sh\n", encoding="utf-8")
            launcher.chmod(0o755)
            paths = mock.Mock(install_root=temp / "install", launcher=launcher)
            with mock.patch("vibe_memory_router.vibe_memory_paths.for_home", return_value=paths):
                context = build_context(self.event(), project_root=None, pending={})
            command = context.split("Candidate CLI:\n\n    ", 1)[1].splitlines()[0]
            self.assertEqual(shlex.split(command)[0], str(launcher))
            self.assertNotIn("python3", command)

    def test_build_context_uses_persisted_python_when_launcher_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            temp = pathlib.Path(value)
            cli = temp / "install" / "current" / "scripts" / "vibe_memory_cli.py"
            cli.parent.mkdir(parents=True)
            cli.write_text("# cli\n", encoding="utf-8")
            (temp / "install" / "config.json").write_text(
                json.dumps({"python_executable": "/opt/vibe/python3"}), encoding="utf-8"
            )
            paths = mock.Mock(install_root=temp / "install", launcher=temp / "missing")
            with mock.patch("vibe_memory_router.vibe_memory_paths.for_home", return_value=paths):
                context = build_context(self.event(), project_root=None, pending={})
            command = context.split("Candidate CLI:\n\n    ", 1)[1].splitlines()[0]
            self.assertEqual(shlex.split(command)[:2], ["/opt/vibe/python3", str(cli)])

    def test_build_context_command_executes_hostile_project_root_with_bin_sh(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            temp = pathlib.Path(value)
            install_root = temp / "fake install"
            scripts = install_root / "current/scripts"
            scripts.mkdir(parents=True)
            fake_script = scripts / "vibe_memory_cli.py"
            fake_script.write_text(
                "import json, os, sys\n"
                "print(json.dumps({'root': os.environ.get('MEMORY_REVIEW_PROJECT_ROOT'), "
                "'argv': sys.argv[1:]}))\n",
                encoding="utf-8",
            )
            project = temp / "project with spaces ' $(touch injected) `touch injected2`"
            fake_paths = mock.Mock(install_root=install_root)
            with mock.patch("vibe_memory_router.vibe_memory_paths.for_home", return_value=fake_paths):
                context = build_context(self.event(), project_root=project, pending={})
            command = context.split("Candidate CLI:\n\n    ", 1)[1].splitlines()[0]

            completed = subprocess.run(
                ["/bin/sh", "-c", command],
                cwd=temp,
                check=True,
                capture_output=True,
                text=True,
            )

            payload = json.loads(completed.stdout)
            self.assertEqual(payload["root"], str(project))
            self.assertEqual(payload["argv"][:2], ["memory", "propose"])
            self.assertFalse((temp / "injected").exists())
            self.assertFalse((temp / "injected2").exists())


if __name__ == "__main__":
    unittest.main()
