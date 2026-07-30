from __future__ import annotations

import json
import pathlib
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import memory_project
from vibe_memory_events import NormalizedEvent
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

    def test_idempotency_store_claims_duplicate_event_once(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            store = IdempotencyStore(pathlib.Path(value) / "events.json")
            event = self.event()

            self.assertTrue(store.claim(event))
            self.assertFalse(store.claim(event))

    def test_idempotency_store_reclaims_expired_event(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            store = IdempotencyStore(pathlib.Path(value) / "events.json", ttl_seconds=30)
            event = self.event()

            with mock.patch("vibe_memory_router.time.time", side_effect=[100.0, 131.0]):
                self.assertTrue(store.claim(event))
                self.assertTrue(store.claim(event))

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

            self.assertTrue(store.claim(base))
            for event in variants:
                with self.subTest(event=event):
                    self.assertTrue(store.claim(event))

    def test_idempotency_store_does_not_collide_on_pipe_delimited_fields(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            store = IdempotencyStore(pathlib.Path(value) / "events.json")
            first = self.event(session_id="session|event", event="stop")
            second = self.event(session_id="session", event="event|stop")

            self.assertTrue(store.claim(first))
            self.assertTrue(store.claim(second))

    def test_idempotency_store_preserves_corrupt_or_nonobject_data(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = pathlib.Path(value) / "events.json"
            for original in ("not json\n", "[]\n"):
                with self.subTest(original=original):
                    path.write_text(original, encoding="utf-8")
                    store = IdempotencyStore(path)

                    with self.assertRaisesRegex(ValueError, "idempotency store"):
                        store.claim(self.event())

                    self.assertEqual(path.read_text(encoding="utf-8"), original)

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
        self.assertIn("memory_review.py propose", context)
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
        script = ROOT / "scripts" / "memory_review.py"
        expected_parts = [
            "env",
            f"MEMORY_REVIEW_PROJECT_ROOT={project}",
            "python3",
            str(script),
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

    def test_build_context_command_executes_hostile_project_root_with_bin_sh(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            temp = pathlib.Path(value)
            scripts = temp / "fake scripts"
            scripts.mkdir()
            fake_router = scripts / "vibe_memory_router.py"
            fake_script = scripts / "memory_review.py"
            fake_script.write_text(
                "import json, os, sys\n"
                "print(json.dumps({'root': os.environ.get('MEMORY_REVIEW_PROJECT_ROOT'), "
                "'argv': sys.argv[1:]}))\n",
                encoding="utf-8",
            )
            project = temp / "project with spaces ' $(touch injected) `touch injected2`"
            with mock.patch("vibe_memory_router.__file__", str(fake_router)):
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
            self.assertEqual(payload["argv"][0], "propose")
            self.assertFalse((temp / "injected").exists())
            self.assertFalse((temp / "injected2").exists())


if __name__ == "__main__":
    unittest.main()
