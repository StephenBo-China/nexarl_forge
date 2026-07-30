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
import time
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
CLI = SCRIPTS / "vibe_memory_cli.py"
sys.path.insert(0, str(SCRIPTS))

import vibe_memory_cli
import vibe_memory_router


class VibeMemoryLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.temporary.name) / "home"
        self.home.mkdir()
        self.paths = vibe_memory_cli.vibe_memory_paths.for_home(self.home)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def invoke(self, argv: list[str]) -> tuple[int, object, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("vibe_memory_cli.vibe_memory_paths.for_home", return_value=self.paths), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = vibe_memory_cli.main(argv)
        output = stdout.getvalue()
        return code, json.loads(output) if output else None, stderr.getvalue()

    def test_doctor_json_has_exact_stable_keys_and_exit_semantics(self) -> None:
        healthy = {name: {"ok": True, "status": "current"} for name in (
            "runtime", "codex_hooks", "claude_hooks", "service", "data"
        )}
        with mock.patch("vibe_memory_cli.collect_status", return_value=healthy):
            code, output, _ = self.invoke(["doctor", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(set(output), {"runtime", "codex_hooks", "claude_hooks", "service", "data"})
        self.assertTrue(all(set(item) >= {"ok", "status"} for item in output.values()))

        unhealthy = dict(healthy)
        unhealthy["service"] = {"ok": False, "status": "unreachable", "action": "start service"}
        with mock.patch("vibe_memory_cli.collect_status", return_value=unhealthy):
            code, output, _ = self.invoke(["doctor", "--json"])
        self.assertEqual(code, 1)
        self.assertFalse(output["service"]["ok"])

    def test_install_delegates_runtime_plist_and_hooks_without_launchctl(self) -> None:
        runtime = self.paths.install_root / "current"
        args = ["install", "--source-root", "/portable/source", "--with-claude-hooks"]
        with mock.patch("vibe_memory_cli.vibe_memory_install.install_runtime", return_value={"version": "1.0.0"}) as install, \
                mock.patch("vibe_memory_cli.vibe_memory_install.prepare_data", return_value={"files": []}) as prepare, \
                mock.patch("vibe_memory_cli.vibe_memory_install.render_launch_agent", return_value="<plist/>") as render, \
                mock.patch("vibe_memory_cli.vibe_memory_install.install_launch_agent", return_value={"changed": True, "path": "agent"}) as write, \
                mock.patch("vibe_memory_cli.vibe_memory_hooks.repair", side_effect=[{"status": "created"}, {"status": "created"}]) as repair, \
                mock.patch("vibe_memory_cli.subprocess.run") as run:
            code, output, _ = self.invoke(args)
        self.assertEqual(code, 0)
        install.assert_called_once_with(pathlib.Path("/portable/source"), self.paths)
        prepare.assert_called_once_with(self.paths)
        render.assert_called_once_with(self.paths, port=8897)
        write.assert_called_once()
        self.assertEqual(repair.call_args_list, [
            mock.call(self.home / ".codex/hooks.json", "codex", runtime),
            mock.call(self.home / ".claude/settings.json", "claude-code", runtime),
        ])
        run.assert_not_called()
        self.assertEqual(output["status"], "installed")

    def test_install_error_is_nonzero_and_not_hook_degraded(self) -> None:
        with mock.patch("vibe_memory_cli.vibe_memory_install.install_runtime", side_effect=ValueError("unsafe source")):
            code, output, stderr = self.invoke(["install", "--source-root", "/bad"])
        self.assertEqual(code, 1)
        self.assertIsNone(output)
        self.assertIn("install failed", stderr)
        self.assertNotIn("degraded", stderr)

    def test_real_install_keeps_codex_and_claude_hooks_on_current_symlink(self) -> None:
        code, output, stderr = self.invoke([
            "install", "--source-root", str(ROOT), "--with-claude-hooks"
        ])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output["status"], "installed")
        stable = str(
            self.paths.install_root / "current/scripts/vibe_memory_cli.py"
        )
        for config_path in (
            self.home / ".codex/hooks.json",
            self.home / ".claude/settings.json",
        ):
            document = json.loads(config_path.read_text(encoding="utf-8"))
            commands = [
                handler["command"]
                for groups in document["hooks"].values()
                for group in groups
                for handler in group["hooks"]
            ]
            self.assertTrue(commands)
            self.assertTrue(all(stable in command for command in commands))
            self.assertTrue(all("releases/1.0.0" not in command for command in commands))
        before = {
            path: path.read_bytes()
            for path in (
                self.home / ".codex/hooks.json",
                self.home / ".claude/settings.json",
            )
        }
        code, repeated, stderr = self.invoke([
            "install", "--source-root", str(ROOT), "--with-claude-hooks"
        ])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(repeated["hooks"]["codex"]["status"], "current")
        self.assertEqual(repeated["hooks"]["claude"]["status"], "current")
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_open_only_invokes_usr_bin_open_after_loopback_health(self) -> None:
        with mock.patch("vibe_memory_cli.health_ok", return_value=True), mock.patch(
            "vibe_memory_cli.subprocess.run", return_value=subprocess.CompletedProcess([], 0)
        ) as run:
            code, output, _ = self.invoke(["open"])
        self.assertEqual(code, 0)
        run.assert_called_once_with(["/usr/bin/open", "http://127.0.0.1:8897/"], check=False)
        self.assertEqual(output["status"], "opened")

        with mock.patch("vibe_memory_cli.health_ok", return_value=False), mock.patch(
            "vibe_memory_cli.subprocess.run"
        ) as run:
            code, output, stderr = self.invoke(["open"])
        self.assertEqual(code, 1)
        self.assertIsNone(output)
        self.assertIn("health", stderr)
        run.assert_not_called()

    def test_project_register_list_unregister_and_explicit_init(self) -> None:
        notes = pathlib.Path(self.temporary.name) / "notes"
        notes.mkdir()
        registry = self.paths.project_registry
        with mock.patch.object(vibe_memory_cli.memory_project, "REGISTRY_PATH", registry):
            code, registered, _ = self.invoke(["project", "register", str(notes)])
            self.assertEqual(code, 0)
            self.assertEqual(registered["current_project"], str(notes.resolve()))
            self.assertFalse((notes / "codex").exists())

            code, listed, _ = self.invoke(["project", "list"])
            self.assertEqual(code, 0)
            self.assertEqual(listed["current_project"], str(notes.resolve()))

            code, initialized, _ = self.invoke(["project", "init", str(notes)])
            self.assertEqual(code, 0)
            self.assertTrue(initialized["ok"])
            self.assertTrue((notes / "codex/codex_long_memory.md").exists())

            code, removed, _ = self.invoke(["project", "unregister", str(notes)])
            self.assertEqual(code, 0)
            self.assertEqual(removed["current_project"], "")
            self.assertEqual(removed["projects"], [])

    def test_memory_commands_delegate_to_review_apis(self) -> None:
        candidate = {"id": "candidate-1", "status": "pending", "scope": "personal", "target": "personal_long", "risk_flags": [], "summary": "summary", "content": "content"}
        with mock.patch("vibe_memory_cli.memory_review_queue.create_agent_candidate", return_value=candidate) as propose:
            code, output, _ = self.invoke(["memory", "propose", "--scope", "personal", "--target", "long", "--category", "work_style", "--title", "Title", "--summary", "Summary", "--source-agent", "codex", "--policy-version", "2"])
        self.assertEqual(code, 0)
        self.assertEqual(output["id"], "candidate-1")
        propose.assert_called_once_with("personal", "long", "work_style", "Title", "Summary", "agent_summary", source_agent="codex", policy_version=2)

        with mock.patch("vibe_memory_cli.memory_review_queue.approve", return_value=candidate) as approve:
            code, output, _ = self.invoke(["memory", "approve", "candidate-1", "--target", "personal_long"])
        self.assertEqual(code, 0)
        approve.assert_called_once_with("candidate-1", target="personal_long", content=None)
        self.assertEqual(output["status"], "approved")


class InstallScriptContractTest(unittest.TestCase):
    def test_install_script_exact_contract_and_is_executable(self) -> None:
        script = ROOT / "install.sh"
        self.assertEqual(script.read_text(encoding="utf-8"), """#!/usr/bin/env bash
set -euo pipefail
SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec /usr/bin/python3 "${SOURCE_ROOT}/scripts/vibe_memory_cli.py" install --source-root "${SOURCE_ROOT}" "$@"
""")
        self.assertTrue(os.access(script, os.X_OK))
        completed = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)


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
        timeout: float = 5.0,
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
            timeout=timeout,
        )

    def run_lifecycle(self, cwd: pathlib.Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *arguments],
            cwd=cwd,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
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

    def test_real_project_and_memory_lifecycle_preserves_approval_gate(self) -> None:
        notes = self.base / "plain-notes"
        notes.mkdir()
        registered = self.run_lifecycle(notes, "project", "register", str(notes))
        self.assertEqual(registered.returncode, 0, registered.stderr)
        self.assertEqual(json.loads(registered.stdout)["current_project"], str(notes.resolve()))
        self.assertFalse((notes / "codex").exists())

        initialized = self.run_lifecycle(notes, "project", "init", str(notes))
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        instructions = (notes / "AGENTS.md").read_text(encoding="utf-8")
        stable_cli = self.home / "Library/Application Support/VibeMemory/current/scripts/vibe_memory_cli.py"
        self.assertIn(str(stable_cli), instructions)
        self.assertIn("memory propose", instructions)
        self.assertNotIn(str(ROOT / "scripts/memory_review.py"), instructions)
        proposed = self.run_lifecycle(
            notes,
            "memory", "propose",
            "--scope", "personal",
            "--target", "long",
            "--category", "work_style",
            "--title", "Review before promotion",
            "--summary", "The user prefers approval before durable memory promotion.",
            "--source-agent", "codex",
            "--policy-version", "1",
        )
        self.assertEqual(proposed.returncode, 0, proposed.stderr)
        candidate_id = json.loads(proposed.stdout)["id"]
        long_path = self.home / ".codex/personal_memory/long.md"
        self.assertNotIn("Review before promotion", long_path.read_text(encoding="utf-8"))

        listed = self.run_lifecycle(notes, "memory", "list", "--status", "pending")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn(candidate_id, [item["id"] for item in json.loads(listed.stdout)["items"]])
        shown = self.run_lifecycle(notes, "memory", "show", candidate_id)
        self.assertEqual(json.loads(shown.stdout)["id"], candidate_id)

        approved = self.run_lifecycle(
            notes, "memory", "approve", candidate_id, "--target", "personal_long"
        )
        self.assertEqual(approved.returncode, 0, approved.stderr)
        self.assertIn("Review before promotion", long_path.read_text(encoding="utf-8"))

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
        self.assertIn(
            str(self.home / "Library/Application Support/VibeMemory/current/scripts/vibe_memory_cli.py"),
            context,
        )
        self.assertIn("memory propose", context)
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
            "memory_proposals.md",
            "memory_review_queue.json",
            "memory_review_queue.json.lock",
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

    def test_registered_fifo_queue_inputs_fail_open_promptly(self) -> None:
        sources = (
            "project_proposals",
            "project_state",
            "personal_proposals",
            "packet_journal",
            "codex_packet",
            "shared_packet",
            "idempotency_state",
        )
        for source in sources:
            with self.subTest(source=source):
                project = self.base / f"fifo-{source}"
                codex = project / "codex"
                codex.mkdir(parents=True)
                (codex / "memory_proposals.md").write_text("# Proposals\n", encoding="utf-8")
                idempotency_state = (
                    self.home
                    / "Library"
                    / "Application Support"
                    / "VibeMemory"
                    / "state"
                    / "hook_events.json"
                )
                idempotency_state.parent.mkdir(parents=True, exist_ok=True)
                target = {
                    "project_proposals": codex / "memory_proposals.md",
                    "project_state": codex / "memory_review_state.json",
                    "personal_proposals": self.home
                    / ".codex"
                    / "personal_memory"
                    / "proposals.md",
                    "packet_journal": codex / ".vibe-memory-packets-journal.json",
                    "codex_packet": codex / "codex_context_packet.md",
                    "shared_packet": codex / "shared_memory_context_packet.md",
                    "idempotency_state": idempotency_state,
                }[source]
                target.unlink(missing_ok=True)
                os.mkfifo(target)
                self.write_registry(project)

                started = time.monotonic()
                try:
                    completed = self.run_cli(
                        project,
                        agent="codex",
                        event="SessionStart",
                        stdin=json.dumps({"session_id": f"fifo-{source}"}),
                        timeout=2.0,
                    )
                finally:
                    target.unlink(missing_ok=True)
                    if source == "personal_proposals":
                        target.write_text("# Proposals\n", encoding="utf-8")

                self.assertLess(time.monotonic() - started, 2.0)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(
                    json.loads(completed.stdout),
                    {"status": "degraded", "error": "钩子处理失败"},
                )
                self.assertEqual(completed.stderr, "")

    def test_oversized_queue_inputs_fail_open_without_exposing_paths(self) -> None:
        oversized = b"x" * (vibe_memory_router.MAX_QUEUE_INPUT_BYTES + 1)
        for source in ("project_proposals", "project_state", "personal_proposals"):
            with self.subTest(source=source):
                project = self.base / f"oversized-{source}"
                codex = project / "codex"
                codex.mkdir(parents=True)
                (codex / "memory_proposals.md").write_text("# Proposals\n", encoding="utf-8")
                target = {
                    "project_proposals": codex / "memory_proposals.md",
                    "project_state": codex / "memory_review_state.json",
                    "personal_proposals": self.home
                    / ".codex"
                    / "personal_memory"
                    / "proposals.md",
                }[source]
                target.write_bytes(oversized)
                self.write_registry(project)

                completed = self.run_cli(
                    project,
                    agent="claude-code",
                    event="Stop",
                    stdin=json.dumps({"session_id": f"oversized-{source}"}),
                )
                if source == "personal_proposals":
                    target.write_text("# Proposals\n", encoding="utf-8")

                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(
                    json.loads(completed.stdout),
                    {"status": "degraded", "error": "钩子处理失败"},
                )
                self.assertEqual(completed.stderr, "")
                self.assertNotIn(str(target), completed.stdout)

    def test_personal_proposals_symlink_fails_open_without_reading_target(self) -> None:
        project = self.base / "personal-proposal-symlink"
        project.mkdir()
        proposals = self.home / ".codex" / "personal_memory" / "proposals.md"
        outside = self.base / "outside-personal-proposals.md"
        outside.write_text("SECRET_OUTSIDE_PROPOSAL\n", encoding="utf-8")
        proposals.unlink()
        proposals.symlink_to(outside)
        self.write_registry(project)

        completed = self.run_cli(
            project,
            agent="codex",
            event="SessionStart",
            stdin=json.dumps({"session_id": "personal-symlink"}),
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            {"status": "degraded", "error": "钩子处理失败"},
        )
        self.assertNotIn("SECRET_OUTSIDE_PROPOSAL", completed.stdout)


if __name__ == "__main__":
    unittest.main()
