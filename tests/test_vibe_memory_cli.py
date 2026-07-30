from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
CLI = SCRIPTS / "vibe_memory_cli.py"
sys.path.insert(0, str(SCRIPTS))

import vibe_memory_cli


class VibeMemoryCLIUnitTest(unittest.TestCase):
    def test_hook_command_treats_empty_stdin_as_empty_object(self) -> None:
        args = argparse.Namespace(agent="codex", event="UserPromptSubmit")
        result = {
            "status": "ok",
            "hookSpecificOutput": {"additionalContext": "shared context"},
        }

        router = mock.Mock()
        router.handle_event.return_value = result
        expected_cwd = pathlib.Path.cwd()
        with mock.patch("vibe_memory_cli.sys.stdin", io.StringIO("")), mock.patch(
            "vibe_memory_cli.importlib.import_module", return_value=router
        ), io.StringIO() as stdout, contextlib.redirect_stdout(stdout):
            exit_code = vibe_memory_cli.hook_command(args)
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0)
        router.handle_event.assert_called_once_with(
            "codex", "UserPromptSubmit", {}, expected_cwd
        )
        self.assertEqual(json.loads(output), result)

    def test_hook_command_invalid_json_is_degraded_and_returns_zero(self) -> None:
        args = argparse.Namespace(agent="codex", event="UserPromptSubmit")
        sensitive_input = '{"token":"SECRET_JSON_BODY"'

        with mock.patch(
            "vibe_memory_cli.sys.stdin", io.StringIO(sensitive_input)
        ), io.StringIO() as stdout, contextlib.redirect_stdout(stdout):
            exit_code = vibe_memory_cli.hook_command(args)
            raw_output = stdout.getvalue()
            output = json.loads(raw_output)

        self.assertEqual(exit_code, 0)
        self.assertEqual(output, {"status": "degraded", "error": "钩子处理失败"})
        self.assertNotIn("SECRET_JSON_BODY", raw_output)

    def test_hook_command_internal_error_is_degraded_without_leaking_exception(self) -> None:
        args = argparse.Namespace(agent="claude-code", event="Stop")
        sensitive_message = "SECRET_PAYLOAD=/Users/alice/private/token-123"

        router = mock.Mock()
        router.handle_event.side_effect = RuntimeError(sensitive_message)
        with mock.patch("vibe_memory_cli.sys.stdin", io.StringIO("{}")), mock.patch(
            "vibe_memory_cli.importlib.import_module", return_value=router
        ), io.StringIO() as stdout, contextlib.redirect_stdout(stdout):
            exit_code = vibe_memory_cli.hook_command(args)
            raw_output = stdout.getvalue()

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            json.loads(raw_output),
            {"status": "degraded", "error": "钩子处理失败"},
        )
        for sensitive_value in (
            "SECRET_PAYLOAD",
            "/Users/alice/private",
            "token-123",
        ):
            self.assertNotIn(sensitive_value, raw_output)
        self.assertIn("钩子处理失败", raw_output)
        self.assertNotIn("\\u", raw_output)


class VibeMemoryCLIIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.temporary.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.registry = self.home / ".codex" / "memory_review" / "projects.json"
        self.registry.parent.mkdir(parents=True)
        personal = self.home / ".codex" / "personal_memory"
        personal.mkdir(parents=True)
        for name in ("long.md", "short.md", "proposals.md"):
            (personal / name).write_text(f"# Personal {name}\n", encoding="utf-8")
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "HOME": str(self.home),
                "MEMORY_REVIEW_PROJECT_REGISTRY": str(self.registry),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(
        self,
        cwd: pathlib.Path,
        *,
        agent: str,
        event: str,
        stdin: str = "{}",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(CLI),
                "hook",
                "--agent",
                agent,
                "--event",
                event,
            ],
            cwd=cwd,
            env=self.environment,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )

    def write_registry(self, *roots: pathlib.Path) -> None:
        self.registry.write_text(
            json.dumps(
                {
                    "current_project": str(roots[0]) if roots else "",
                    "projects": [{"root": str(root)} for root in roots],
                }
            ),
            encoding="utf-8",
        )

    def test_unregistered_codex_event_returns_personal_context_without_creating_codex(self) -> None:
        registered = self.base / "registered"
        registered.mkdir()
        unregistered = self.base / "unregistered"
        unregistered.mkdir()
        self.write_registry(registered)

        completed = self.run_cli(
            unregistered,
            agent="codex",
            event="UserPromptSubmit",
            stdin=json.dumps(
                {"session_id": "codex-1", "prompt": "SUPER_SECRET_PAYLOAD_BODY"}
            ),
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads(completed.stdout)
        self.assertEqual(output["status"], "ok")
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn(str(self.home / ".codex" / "personal_memory" / "long.md"), context)
        self.assertNotIn("codex/codex_long_memory.md", context)
        self.assertNotIn("project candidates", context.lower())
        self.assertNotIn("project_architecture", context)
        self.assertNotIn("project long memory", context.lower())
        self.assertNotIn(str(registered), context)
        self.assertNotIn("SUPER_SECRET_PAYLOAD_BODY", context)
        self.assertFalse((unregistered / "codex").exists())

    def test_registered_claude_event_refreshes_queue_and_writes_context_packets(self) -> None:
        project = self.base / "ordinary-project"
        child = project / "src" / "feature"
        child.mkdir(parents=True)
        codex = project / "codex"
        codex.mkdir()
        (codex / "memory_proposals.md").write_text(
            "# Project Memory Proposals\n\n"
            "### 2026-07-30 - Durable architecture\n\n"
            "- category: project_architecture\n"
            "- status: pending\n\n"
            "The application uses one shared hook router.\n",
            encoding="utf-8",
        )
        self.write_registry(project)

        completed = self.run_cli(
            child,
            agent="claude-code",
            event="Stop",
            stdin=json.dumps({"session_id": "claude-1", "transcript": "do not save"}),
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads(completed.stdout)
        self.assertEqual(output["status"], "ok")
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"Registered project: `{project.resolve()}`", context)
        self.assertIn("source agent: claude-code", context)
        self.assertNotIn("do not save", context)
        queue = json.loads((codex / "memory_review_queue.json").read_text(encoding="utf-8"))
        self.assertEqual(queue["counts"]["project_pending"], 1)
        first_packet = (codex / "codex_context_packet.md").read_text(encoding="utf-8")
        second_packet = (codex / "shared_memory_context_packet.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual(first_packet, context)
        self.assertEqual(second_packet, context)

    def test_duplicate_event_is_successful_no_op(self) -> None:
        project = self.base / "project"
        project.mkdir()
        self.write_registry(project)
        payload = json.dumps({"session_id": "same-session"})

        first = self.run_cli(
            project, agent="codex", event="UserPromptSubmit", stdin=payload
        )
        packet = project / "codex" / "codex_context_packet.md"
        first_content = packet.read_text(encoding="utf-8")
        second = self.run_cli(
            project, agent="codex", event="UserPromptSubmit", stdin=payload
        )

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)["status"], "ok")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout), {"status": "duplicate"})
        self.assertEqual(packet.read_text(encoding="utf-8"), first_content)

    def test_hook_arguments_are_required_and_agent_is_validated(self) -> None:
        for arguments in (
            ["hook"],
            ["hook", "--agent", "other", "--event", "Stop"],
        ):
            with self.subTest(arguments=arguments):
                completed = subprocess.run(
                    [sys.executable, str(CLI), *arguments],
                    env=self.environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 2)

    def test_unknown_event_fails_open_with_safe_degraded_json(self) -> None:
        project = self.base / "project"
        project.mkdir()
        self.write_registry(project)

        completed = self.run_cli(
            project,
            agent="codex",
            event="UnknownEvent",
            stdin='{"prompt":"SECRET_UNKNOWN_EVENT"}',
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(
            json.loads(completed.stdout),
            {"status": "degraded", "error": "钩子处理失败"},
        )
        self.assertNotIn("SECRET_UNKNOWN_EVENT", completed.stdout)

    def test_missing_router_import_fails_open_without_traceback_or_path(self) -> None:
        isolated = self.base / "isolated"
        isolated.mkdir()
        isolated_cli = isolated / "vibe_memory_cli.py"
        shutil.copy2(CLI, isolated_cli)

        completed = subprocess.run(
            [
                sys.executable,
                str(isolated_cli),
                "hook",
                "--agent",
                "codex",
                "--event",
                "SessionStart",
            ],
            cwd=isolated,
            env=self.environment,
            input='{"prompt":"SECRET_IMPORT_PAYLOAD"}',
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(
            json.loads(completed.stdout),
            {"status": "degraded", "error": "钩子处理失败"},
        )
        for leaked in ("Traceback", "SECRET_IMPORT_PAYLOAD", str(isolated)):
            self.assertNotIn(leaked, completed.stdout)

    def test_registered_live_or_broken_codex_symlink_is_rejected(self) -> None:
        for kind in ("live", "broken"):
            with self.subTest(kind=kind):
                project = self.base / f"symlink-project-{kind}"
                project.mkdir()
                outside = self.base / f"outside-{kind}"
                if kind == "live":
                    outside.mkdir()
                (project / "codex").symlink_to(outside, target_is_directory=True)
                self.write_registry(project)

                completed = self.run_cli(
                    project,
                    agent="codex",
                    event="SessionStart",
                    stdin=json.dumps({"session_id": f"symlink-{kind}"}),
                )

                self.assertEqual(completed.returncode, 0)
                self.assertEqual(
                    json.loads(completed.stdout),
                    {"status": "degraded", "error": "钩子处理失败"},
                )
                if outside.exists():
                    self.assertEqual(list(outside.iterdir()), [])

    def test_registered_protected_target_symlink_is_rejected_without_outside_write(self) -> None:
        protected_names = (
            "memory_review_queue.json",
            "memory_review_state.json",
            "codex_context_packet.md",
            "shared_memory_context_packet.md",
            ".vibe-memory-packets-journal.json",
            ".vibe-memory-packets.lock",
        )
        for index, name in enumerate(protected_names):
            with self.subTest(name=name):
                project = self.base / f"target-project-{index}"
                codex = project / "codex"
                codex.mkdir(parents=True)
                outside = self.base / f"outside-target-{index}.txt"
                outside.write_text("sentinel\n", encoding="utf-8")
                (codex / name).symlink_to(outside)
                self.write_registry(project)

                completed = self.run_cli(
                    project,
                    agent="claude-code",
                    event="Stop",
                    stdin=json.dumps({"session_id": f"target-{index}"}),
                )

                self.assertEqual(completed.returncode, 0)
                self.assertEqual(
                    json.loads(completed.stdout),
                    {"status": "degraded", "error": "钩子处理失败"},
                )
                self.assertEqual(outside.read_text(encoding="utf-8"), "sentinel\n")


if __name__ == "__main__":
    unittest.main()
