from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import io
import json
import os
import pathlib
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import memory_project
import memory_review as review_cli
import memory_review_queue as review
import loop_superpowers
import ui_design_preferences as preferences
import vibe_memory_install
import vibe_memory_paths


class MemoryReviewQualityTest(unittest.TestCase):
    def test_agent_candidate_protocol_prefers_stable_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            launcher = pathlib.Path(value) / "bin" / "vibe-memory"
            launcher.parent.mkdir()
            launcher.write_text("#!/bin/sh\n", encoding="utf-8")
            launcher.chmod(0o755)
            paths = mock.Mock(launcher=launcher, install_root=pathlib.Path(value) / "install")
            with mock.patch.object(memory_project, "RUNTIME_PATHS", paths):
                text = memory_project.agent_candidate_protocol(pathlib.Path("/tmp/project"))
            command = text.split("MEMORY_REVIEW_PROJECT_ROOT=", 1)[1].split(" memory propose", 1)[0]
            self.assertIn(str(launcher), command)
            self.assertNotIn("python3", command)

    def test_agent_candidate_protocol_uses_valid_persisted_python_with_current_cli(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            temp = pathlib.Path(value)
            install_root = temp / "install"
            cli = install_root / "current" / "scripts" / "vibe_memory_cli.py"
            cli.parent.mkdir(parents=True)
            cli.write_text("# cli\n", encoding="utf-8")
            (install_root / "config.json").write_text(
                json.dumps({"python_executable": sys.executable}), encoding="utf-8"
            )
            paths = mock.Mock(launcher=temp / "missing", install_root=install_root)
            with mock.patch.object(memory_project, "RUNTIME_PATHS", paths):
                text = memory_project.agent_candidate_protocol(pathlib.Path("/tmp/project"))
            line = next(
                line.strip()
                for line in text.splitlines()
                if "MEMORY_REVIEW_PROJECT_ROOT=" in line and " memory propose" in line
            )
            expected = (
                f"MEMORY_REVIEW_PROJECT_ROOT=/tmp/project {shlex.quote(sys.executable)} "
                f"{shlex.quote(str(cli))} memory propose \\"
            )
            self.assertEqual(line, expected)

    def test_agent_candidate_protocol_rejects_invalid_persisted_python_fallback_values(self) -> None:
        invalid_values = [
            None,
            "",
            123,
            [],
            {},
            "/missing/python",
            "\x00",
            "python\x00invalid",
        ]
        for persisted in invalid_values:
            with self.subTest(persisted=persisted), tempfile.TemporaryDirectory() as value:
                temp = pathlib.Path(value)
                install_root = temp / "install"
                cli = install_root / "current" / "scripts" / "vibe_memory_cli.py"
                cli.parent.mkdir(parents=True)
                cli.write_text("# cli\n", encoding="utf-8")
                config = {} if persisted is None else {"python_executable": persisted}
                (install_root / "config.json").write_text(json.dumps(config), encoding="utf-8")
                paths = mock.Mock(launcher=temp / "missing", install_root=install_root)
                with mock.patch.object(memory_project, "RUNTIME_PATHS", paths):
                    text = memory_project.agent_candidate_protocol(pathlib.Path("/tmp/project"))
                command = next(
                    line.strip()
                    for line in text.splitlines()
                    if "MEMORY_REVIEW_PROJECT_ROOT=" in line and " memory propose" in line
                )
                source = memory_project.APP_ROOT / "scripts" / "vibe_memory_cli.py"
                expected = (
                    f"MEMORY_REVIEW_PROJECT_ROOT=/tmp/project {shlex.quote(sys.executable)} "
                    f"{shlex.quote(str(source))} memory propose \\"
                )
                self.assertEqual(command, expected)

        with tempfile.TemporaryDirectory() as value:
            temp = pathlib.Path(value)
            install_root = temp / "install"
            cli = install_root / "current" / "scripts" / "vibe_memory_cli.py"
            cli.parent.mkdir(parents=True)
            cli.write_text("# cli\n", encoding="utf-8")
            (install_root / "config.json").write_text("{malformed", encoding="utf-8")
            paths = mock.Mock(launcher=temp / "missing", install_root=install_root)
            with mock.patch.object(memory_project, "RUNTIME_PATHS", paths):
                text = memory_project.agent_candidate_protocol(pathlib.Path("/tmp/project"))
            line = next(
                line.strip()
                for line in text.splitlines()
                if "MEMORY_REVIEW_PROJECT_ROOT=" in line and " memory propose" in line
            )
            source = memory_project.APP_ROOT / "scripts" / "vibe_memory_cli.py"
            expected = (
                f"MEMORY_REVIEW_PROJECT_ROOT=/tmp/project {shlex.quote(sys.executable)} "
                f"{shlex.quote(str(source))} memory propose \\"
            )
            self.assertEqual(line, expected)

    def test_agent_candidate_protocol_does_not_swallow_non_validation_errors(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            temp = pathlib.Path(value)
            install_root = temp / "install"
            cli = install_root / "current" / "scripts" / "vibe_memory_cli.py"
            cli.parent.mkdir(parents=True)
            cli.write_text("# cli\n", encoding="utf-8")
            (install_root / "config.json").write_text(
                json.dumps({"python_executable": sys.executable}), encoding="utf-8"
            )
            paths = mock.Mock(launcher=temp / "missing", install_root=install_root)
            with mock.patch.object(memory_project, "RUNTIME_PATHS", paths), mock.patch.object(
                memory_project.vibe_memory_install,
                "validate_python",
                side_effect=RuntimeError("business failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "business failure"):
                    memory_project.agent_candidate_protocol(pathlib.Path("/tmp/project"))

    def test_start_script_without_project_does_not_promote_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            temp = pathlib.Path(value)
            home = temp / "home"
            paths = vibe_memory_paths.for_home(home)
            vibe_memory_install.install_runtime(ROOT, paths)
            runtime = paths.install_root / "releases" / "1.0.0"
            registry = temp / "projects.json"
            registry.write_text(
                json.dumps({"current_project": "", "projects": []}),
                encoding="utf-8",
            )
            capture = temp / "project-root.txt"
            shim = temp / "shim"
            shim.mkdir()
            (shim / "sitecustomize.py").write_text(
                "import atexit, http.server, json, os, pathlib, subprocess, sys, time, urllib.error, urllib.request\n"
                "_popen = subprocess.Popen\n"
                "_child = None\n"
                "_evidence = {}\n"
                "if any(str(arg).endswith('memory_review_server.py') for arg in sys.argv):\n"
                "    class _ControlledServer:\n"
                "        def __init__(self, *_args, **_kwargs): pass\n"
                "        def serve_forever(self): time.sleep(60)\n"
                "        def server_close(self): pass\n"
                "    http.server.ThreadingHTTPServer = _ControlledServer\n"
                "def _write_evidence():\n"
                "    pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps(_evidence), encoding='utf-8')\n"
                "class _Response:\n"
                "    status = 200\n"
                "    def __enter__(self): return self\n"
                "    def __exit__(self, *_args): return False\n"
                "def _urlopen(*_args, **_kwargs):\n"
                "    if _child is None: raise urllib.error.URLError('not running')\n"
                "    time.sleep(0.1)\n"
                "    if _child.poll() is not None: raise urllib.error.URLError('child exited')\n"
                "    _evidence['alive_at_health'] = True\n"
                "    return _Response()\n"
                "def _capture_popen(*args, **kwargs):\n"
                "    global _child, _evidence\n"
                "    _child = _popen(*args, **kwargs)\n"
                "    _evidence = {\n"
                "        'command': [str(value) for value in args[0]],\n"
                "        'cwd': kwargs.get('cwd'),\n"
                "        'project_root': kwargs.get('env', {}).get('MEMORY_REVIEW_PROJECT_ROOT'),\n"
                "        'pid': _child.pid,\n"
                "    }\n"
                "    _write_evidence()\n"
                "    return _child\n"
                "def _cleanup():\n"
                "    if _child is None: return\n"
                "    try:\n"
                "        if _child.poll() is None: _child.terminate()\n"
                "        try: _evidence['returncode'] = _child.wait(timeout=5)\n"
                "        except subprocess.TimeoutExpired:\n"
                "            _child.kill()\n"
                "            _evidence['returncode'] = _child.wait(timeout=5)\n"
                "        _evidence['cleaned'] = True\n"
                "    finally:\n"
                "        _write_evidence()\n"
                "urllib.request.urlopen = _urlopen\n"
                "subprocess.Popen = _capture_popen\n"
                "atexit.register(_cleanup)\n",
                encoding="utf-8",
            )
            before = {
                path.relative_to(runtime).as_posix(): (
                    stat.S_IMODE(path.stat(follow_symlinks=False).st_mode),
                    hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "",
                )
                for path in runtime.rglob("*")
            }
            environment = os.environ.copy()
            environment.update(
                {
                    "CAPTURE": str(capture),
                    "HOME": str(home),
                    "MEMORY_REVIEW_PORT": "0",
                    "MEMORY_REVIEW_PROJECT_REGISTRY": str(registry),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONPATH": str(shim),
                }
            )
            environment.pop("MEMORY_REVIEW_PROJECT_ROOT", None)

            started: dict[str, object] = {}
            try:
                completed = subprocess.run(
                    ["/bin/bash", str(runtime / "scripts" / "start_memory_review.sh")],
                    cwd=runtime,
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                started = json.loads(capture.read_text(encoding="utf-8"))
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("started=True", completed.stdout, started)
                self.assertIsNone(started["project_root"])
                self.assertEqual(pathlib.Path(str(started["cwd"])).resolve(), runtime.resolve())
                self.assertEqual(
                    pathlib.Path(str(started["command"][1])).resolve(),
                    (runtime / "scripts" / "memory_review_server.py").resolve(),
                )
                self.assertIs(started["alive_at_health"], True)
                self.assertIs(started["cleaned"], True)
                self.assertIsNotNone(started["returncode"])
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(started["pid"]), 0)
            finally:
                if not started and capture.exists():
                    started = json.loads(capture.read_text(encoding="utf-8"))
                if started.get("pid") is not None:
                    try:
                        os.kill(int(started["pid"]), signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            after = {
                path.relative_to(runtime).as_posix(): (
                    stat.S_IMODE(path.stat(follow_symlinks=False).st_mode),
                    hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "",
                )
                for path in runtime.rglob("*")
            }
            self.assertEqual(after, before)
            self.assertEqual(vibe_memory_install._managed_release_version(runtime), "1.0.0")

    def test_empty_registry_builds_personal_only_queue_without_touching_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            temp = pathlib.Path(value)
            runtime = temp / "releases" / "1.0.0"
            shutil.copytree(
                ROOT / "scripts",
                runtime / "scripts",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            (runtime / "release.json").write_text(
                (ROOT / "release.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            home = temp / "home"
            personal = home / ".codex" / "personal_memory"
            personal.mkdir(parents=True)
            (personal / "long.md").write_text(
                "# Personal Long\n\n## Approved\n\nOriginal personal memory.\n",
                encoding="utf-8",
            )
            (personal / "short.md").write_text(
                "# Personal Short\n\n## Current\n\nShort personal memory.\n",
                encoding="utf-8",
            )
            project_alias = personal / "codex_long_memory.md"
            project_alias.write_text(
                "# Alias\n\n## Must Stay\n\nDo not mutate this file.\n",
                encoding="utf-8",
            )
            (personal / "proposals.md").write_text(
                "# Proposals\n\n"
                "### M-personal\n\n"
                "memory_id: M-personal\nstatus: pending\ntarget: long\n"
                "created: 2026-08-14T00:00:00+08:00\n"
                "source_event: agent_summary\ncategory: work_style\n\n"
                "candidate:\n\n```text\n"
                "**标题：跨项目评审习惯**\n\n**分类：工作方式**\n\n"
                "用户希望跨项目候选始终经过明确审核。\n```\n",
                encoding="utf-8",
            )
            registry = temp / "projects.json"
            registry.write_text(
                json.dumps({"current_project": "", "projects": []}),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(home),
                    "MEMORY_REVIEW_PROJECT_REGISTRY": str(registry),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONPATH": str(runtime / "scripts"),
                }
            )
            environment.pop("MEMORY_REVIEW_PROJECT_ROOT", None)

            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import json, memory_project, memory_review_queue as review, "
                    "memory_review_server as server; "
                    "queue = review.build_queue(); "
                    "project_payload = server.project_payload(); "
                    "active = server.active_memory_payload(); "
                    "project_error = ''; "
                    "project_delete_error = ''; "
                    "\ntry: server.update_active_memory('project_long', 'project_long-0', "
                    "'## Changed\\n\\nShould be rejected.')\n"
                    "except ValueError as error: project_error = str(error)\n"
                    "try: server.update_active_memory('project_long', 'project_long-0', "
                    "None, delete=True)\n"
                    "except ValueError as error: project_delete_error = str(error)\n"
                    "server.update_active_memory('personal_long', 'personal_long-0', "
                    "'## Updated\\n\\nUpdated personal memory.'); "
                    "print(json.dumps({"
                    "'current': str(memory_project.current_project() or ''), "
                    "'project_root': str(review.PROJECT_ROOT or ''), "
                    "'payload_current': project_payload['current_project'], "
                    "'payload_project': project_payload['project'], "
                    "'active_sources': [source['id'] for source in active['sources']], "
                    "'project_error': project_error, "
                    "'project_delete_error': project_delete_error, "
                    "'scopes': [item['scope'] for item in queue['items']]"
                    "}))",
                ],
                cwd=runtime,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["current"], "")
            self.assertEqual(result["project_root"], "")
            self.assertEqual(result["payload_current"], "")
            self.assertIsNone(result["payload_project"])
            self.assertEqual(result["scopes"], ["personal"])
            self.assertEqual(
                result["active_sources"], ["personal_long", "personal_short"]
            )
            self.assertIn("Unknown active memory source", result["project_error"])
            self.assertIn(
                "Unknown active memory source", result["project_delete_error"]
            )
            self.assertIn(
                "Updated personal memory.",
                (personal / "long.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "Do not mutate this file.", project_alias.read_text(encoding="utf-8")
            )
            self.assertFalse((runtime / "codex").exists())
            self.assertTrue((personal / "memory_review_queue.json").is_file())
            self.assertTrue((personal / "memory_review_state.json").is_file())

    def test_init_project_creates_memory_and_instructions_without_project_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value) / "project"
            root.mkdir()
            (root / ".git").mkdir()
            original_registry = memory_project.REGISTRY_PATH
            try:
                memory_project.REGISTRY_PATH = pathlib.Path(value) / "projects.json"

                result = memory_project.init_project(root)
            finally:
                memory_project.REGISTRY_PATH = original_registry

            self.assertTrue((root / "codex/codex_long_memory.md").exists())
            self.assertTrue((root / "codex/codex_short_memory.md").exists())
            self.assertTrue((root / "codex/ui_design/config.json").exists())
            self.assertFalse((root / ".codex/hooks.json").exists())
            self.assertFalse((root / ".claude/settings.json").exists())
            for instructions in (
                root / "AGENTS.md",
                root / "CLAUDE.md",
                root / ".claude/rules/shared-memory.md",
            ):
                text = instructions.read_text(encoding="utf-8")
                self.assertIn("Agent-Generated Memory Candidates", text)
            self.assertEqual(
                result["project"]["memory_status"], "initialized"
            )

    def test_effective_ui_context_merges_preferences_skills_and_gate_state(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            temp = pathlib.Path(value)
            root = temp / "project"
            root.mkdir()
            (root / ".git").mkdir()
            original_registry = memory_project.REGISTRY_PATH
            try:
                memory_project.REGISTRY_PATH = temp / "projects.json"
                with mock.patch.dict(
                    os.environ, {"UI_DESIGN_HOME": str(temp / "ui-design-home")}
                ):
                    memory_project.init_project(root)
                    global_value = preferences.default_global_preferences()
                    global_value["design_principles"] = ["calm hierarchy"]
                    preferences.save_global_preferences(global_value)
                    preferences.save_project_overrides(
                        root,
                        {
                            "design_principles": {
                                "mode": "append",
                                "value": ["clear primary action"],
                            }
                        },
                    )
                    memory_project.write_json(
                        root / "codex/ui_design/active-skills.json",
                        {
                            "schema_version": 1,
                            "execution_order": ["frontend-design", "ui-ux-pro-max"],
                            "skills": [
                                {"name": "frontend-design", "version": "pinned"}
                            ],
                        },
                    )
                    config = memory_project.ui_design_config(root)
                    config.update({"hard_gate_enabled": True, "relocked": False})
                    memory_project.write_json(
                        root / "codex/ui_design/config.json", config
                    )
                    memory_project.write_json(
                        root / "codex/ui_design/approvals.json",
                        {
                            "schema_version": 1,
                            "package_approvals": {"task-1": {"digest": "a" * 64}},
                            "project_global_approval": None,
                        },
                    )

                    snapshot = memory_project.publish_effective_ui_context(root)
            finally:
                memory_project.REGISTRY_PATH = original_registry

            self.assertEqual(
                snapshot["preferences"]["effective"]["value"]["design_principles"],
                ["calm hierarchy", "clear primary action"],
            )
            self.assertEqual(
                snapshot["active_skills"]["execution_order"],
                ["frontend-design", "ui-ux-pro-max"],
            )
            self.assertFalse(snapshot["gate"]["relocked"])
            self.assertIn("task-1", snapshot["gate"]["approvals"]["package_approvals"])

    def test_personal_noise_rejects_project_tasks_and_memory_console_commands(self) -> None:
        base = {
            "scope": "personal",
            "status": "pending",
            "content": "",
        }
        for content in (
            "用户偏好/工作方式：隔离审核台候选记忆并标记为拒绝。",
            "用户偏好/工作方式：更新项目员工开发文档和服务代码。",
        ):
            item = dict(base, content=content)
            self.assertTrue(review.is_noise_personal_candidate(item), content)

        durable = dict(
            base,
            content="**分类：协作偏好**\n\n用户希望跨项目每次修改代码前先确认修改计划。",
        )
        self.assertFalse(review.is_noise_personal_candidate(durable))

    def test_generated_project_hook_records_only_event_metadata(self) -> None:
        hook = memory_project.hook_script(pathlib.Path("/tmp/project"), "codex")
        self.assertNotIn("def find_prompt", hook)
        self.assertNotIn("find_prompt(", hook)
        self.assertNotIn("raw_stdin", hook)
        self.assertNotIn("- summary:", hook)
        self.assertNotIn("compact =", hook)
        self.assertIn('f"- source_agent: {SOURCE}', hook)
        self.assertIn('f"- event: {event}', hook)
        self.assertIn('f"- cwd: `{os.getcwd()}`', hook)
        self.assertIn("session_id", hook)
        self.assertNotIn("append_project_candidate", hook)
        self.assertIn("conversation model reviews memory candidates", hook)

    def test_hook_context_is_conditional_and_safe(self) -> None:
        hook = memory_project.hook_script(pathlib.Path("/tmp/project"), "codex")
        self.assertIn("def loop_context()", hook)
        self.assertIn("Loop × Superpowers", hook)
        self.assertIn("explicit user authorization", hook)
        self.assertIn("Loop configuration is invalid", hook)
        self.assertNotIn("oss_access_key", hook)
        self.assertNotIn("database_password", hook)

    def test_agent_candidate_is_structured_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            originals = {
                name: getattr(review, name)
                for name in (
                    "PERSONAL_PROPOSALS", "PERSONAL_LONG", "PERSONAL_SHORT",
                    "PROJECT_PROPOSALS", "PROJECT_LONG", "PROJECT_QUEUE",
                    "PROJECT_STATE", "CODEX_DIR",
                )
            }
            try:
                review.PERSONAL_PROPOSALS = temp / "personal_proposals.md"
                review.PERSONAL_PROPOSALS.write_text("# Proposals\n", encoding="utf-8")
                review.PERSONAL_LONG = temp / "personal_long.md"
                review.PERSONAL_SHORT = temp / "personal_short.md"
                review.PROJECT_PROPOSALS = temp / "project_proposals.md"
                review.PROJECT_LONG = temp / "project_long.md"
                review.PROJECT_QUEUE = temp / "queue.json"
                review.PROJECT_STATE = temp / "state.json"
                review.CODEX_DIR = temp / "codex"
                first = review.create_agent_candidate(
                    "personal", "long", "collaboration_preference", "修改前确认计划",
                    "用户希望修改代码前先确认修改计划和不确定事项。",
                    source_agent="codex",
                    policy_version=2,
                )
                second = review.create_agent_candidate(
                    "personal", "long", "collaboration_preference", " 修改前确认计划 ",
                    "用户希望修改代码前先确认修改计划和不确定事项。",
                    source_agent="claude-code",
                    policy_version=3,
                )
                personal_item = review.parse_personal_candidates()[0]
                approved_personal = review.approve(first["id"])
                project = review.create_agent_candidate(
                    "project", "long", "project_architecture", "共享路由器",
                    "项目使用共享路由器规范化本地代理事件并生成审核上下文。",
                    source_agent="claude-code",
                    policy_version=4,
                )
                project_item = review.parse_project_candidates()[0]
                approved = review.approve(project["id"])
            finally:
                for name, value in originals.items():
                    setattr(review, name, value)
            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            self.assertTrue(project["created"])
            self.assertEqual(first["id"], personal_item["id"])
            self.assertEqual(project["id"], project_item["id"])
            self.assertEqual(personal_item["source_agent"], "codex")
            self.assertEqual(personal_item["source_agents"], ["claude-code", "codex"])
            self.assertEqual(personal_item["policy_version"], 2)
            self.assertEqual(project_item["source_agent"], "claude-code")
            self.assertEqual(project_item["policy_version"], 4)
            self.assertEqual(approved["decision"]["source_agent"], "claude-code")
            self.assertEqual(approved["decision"]["source_agents"], ["claude-code"])
            self.assertEqual(approved["decision"]["policy_version"], 4)
            self.assertEqual(approved["decision"]["identity"], project_item["identity"])
            self.assertEqual(
                approved["decision"]["equivalence"], project_item["equivalence"]
            )
            self.assertEqual(
                approved_personal["decision"]["source_agents"],
                ["claude-code", "codex"],
            )
            self.assertEqual(
                approved_personal["decision"]["identity"], personal_item["identity"]
            )
            self.assertEqual(
                approved_personal["decision"]["equivalence"],
                personal_item["equivalence"],
            )
            text = (temp / "personal_proposals.md").read_text(encoding="utf-8")
            self.assertIn("status: pending", text)
            self.assertIn("source_agent: codex", text)
            self.assertIn("source_agents: claude-code,codex", text)
            self.assertIn("policy_version: 2", text)
            self.assertIn("**标题：修改前确认计划**", text)
            approved_text = (temp / "project_long.md").read_text(encoding="utf-8")
            self.assertIn("共享路由器", approved_text)
            self.assertIn("项目架构", approved_text)
            self.assertNotIn("source_agent", approved_text)
            self.assertNotIn("policy_version", approved_text)

    def test_candidate_equivalence_ignores_title_but_keeps_other_identity_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            originals = {
                name: getattr(review, name)
                for name in (
                    "PERSONAL_PROPOSALS", "PERSONAL_LONG", "PERSONAL_SHORT",
                    "PROJECT_PROPOSALS", "PROJECT_LONG", "PROJECT_QUEUE",
                    "PROJECT_STATE", "CODEX_DIR", "build_queue",
                )
            }
            try:
                review.PERSONAL_PROPOSALS = temp / "personal_proposals.md"
                review.PERSONAL_PROPOSALS.write_text("# Proposals\n", encoding="utf-8")
                review.PERSONAL_LONG = temp / "personal_long.md"
                review.PERSONAL_SHORT = temp / "personal_short.md"
                review.PROJECT_PROPOSALS = temp / "project_proposals.md"
                review.PROJECT_PROPOSALS.write_text("# Project Proposals\n", encoding="utf-8")
                review.PROJECT_LONG = temp / "project_long.md"
                review.PROJECT_QUEUE = temp / "queue.json"
                review.PROJECT_STATE = temp / "state.json"
                review.CODEX_DIR = temp / "codex"
                review.build_queue = lambda: {}
                summary = "用户希望相同摘要在身份字段不同时仍可形成独立候选。"
                results = [
                    review.create_agent_candidate(
                        "personal", "long", "work_style", "标题一", summary
                    ),
                    review.create_agent_candidate(
                        "personal", "long", "work_style", "标题二", summary
                    ),
                    review.create_agent_candidate(
                        "personal", "short", "work_style", "标题一", summary
                    ),
                    review.create_agent_candidate(
                        "personal", "long", "thinking_style", "标题一", summary
                    ),
                    review.create_agent_candidate(
                        "project", "long", "project_workflow", "标题一", summary
                    ),
                    review.create_agent_candidate(
                        "personal", "long", "work_style", "标题一",
                        "用户希望不同摘要继续形成一条独立候选记录。",
                    ),
                ]
                personal_items = review.parse_personal_candidates()
            finally:
                for name, original in originals.items():
                    setattr(review, name, original)

            self.assertEqual(
                [result["created"] for result in results],
                [True, False, True, True, True, True],
            )
            first = next(item for item in personal_items if "标题一" in item["content"])
            self.assertEqual(
                first["identity"],
                review.candidate_identity(
                    "personal", "long", "work_style", "标题一", summary
                ),
            )
            self.assertEqual(
                first["equivalence"],
                review.candidate_equivalence(
                    "personal", "long", "work_style", summary
                ),
            )

    def test_sensitive_candidate_title_is_rejected_before_write_or_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            originals = {
                name: getattr(review, name)
                for name in (
                    "PERSONAL_PROPOSALS", "PERSONAL_LONG", "PERSONAL_SHORT",
                    "PROJECT_PROPOSALS", "PROJECT_LONG", "PROJECT_QUEUE",
                    "PROJECT_STATE", "CODEX_DIR",
                )
            }
            try:
                review.PERSONAL_PROPOSALS = temp / "personal_proposals.md"
                review.PERSONAL_PROPOSALS.write_text("# Proposals\n", encoding="utf-8")
                review.PERSONAL_LONG = temp / "personal_long.md"
                review.PERSONAL_SHORT = temp / "personal_short.md"
                review.PROJECT_PROPOSALS = temp / "project_proposals.md"
                review.PROJECT_LONG = temp / "project_long.md"
                review.PROJECT_QUEUE = temp / "queue.json"
                review.PROJECT_STATE = temp / "state.json"
                review.CODEX_DIR = temp / "codex"
                with self.assertRaisesRegex(ValueError, "sensitive"):
                    review.create_agent_candidate(
                        "personal", "long", "work_style", "api_key=do-not-store",
                        "用户希望跨项目保留清晰且可审核的工作方式说明。",
                    )
                review.PROJECT_PROPOSALS.write_text(
                    "### 2026-07-30 - api_key=do-not-approve\n\n"
                    "**分类：项目工作流**\n\n这是一条长度足够但标题敏感的候选说明。\n",
                    encoding="utf-8",
                )
                sensitive_id = review.parse_project_candidates()[0]["id"]
                with self.assertRaisesRegex(ValueError, "sensitive"):
                    review.approve(sensitive_id)
            finally:
                for name, original in originals.items():
                    setattr(review, name, original)

            self.assertEqual(
                (temp / "personal_proposals.md").read_text(encoding="utf-8"),
                "# Proposals\n",
            )
            self.assertFalse((temp / "personal_long.md").exists())
            self.assertFalse((temp / "project_long.md").exists())

    def test_proposal_atomic_replace_failure_preserves_bytes_and_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            proposals = temp / "personal_proposals.md"
            original_bytes = b"# Proposals\n\noriginal\n"
            proposals.write_bytes(original_bytes)
            proposals.chmod(0o600)
            originals = {
                name: getattr(review, name)
                for name in (
                    "PERSONAL_PROPOSALS", "PERSONAL_LONG", "PERSONAL_SHORT",
                    "PROJECT_PROPOSALS", "PROJECT_LONG", "PROJECT_QUEUE",
                    "PROJECT_STATE", "CODEX_DIR", "build_queue",
                )
            }
            try:
                review.PERSONAL_PROPOSALS = proposals
                review.PERSONAL_LONG = temp / "personal_long.md"
                review.PERSONAL_SHORT = temp / "personal_short.md"
                review.PROJECT_PROPOSALS = temp / "project_proposals.md"
                review.PROJECT_LONG = temp / "project_long.md"
                review.PROJECT_QUEUE = temp / "queue.json"
                review.PROJECT_STATE = temp / "state.json"
                review.CODEX_DIR = temp / "codex"
                review.build_queue = lambda: {}
                with mock.patch(
                    "memory_review_queue.atomic_write_text",
                    create=True,
                    side_effect=OSError("replace failed"),
                ):
                    with self.assertRaisesRegex(OSError, "replace failed"):
                        review.create_agent_candidate(
                            "personal", "long", "work_style", "原子写入",
                            "用户希望候选提案写入失败时完整保留原始文件内容。",
                        )
            finally:
                for name, original in originals.items():
                    setattr(review, name, original)

            self.assertEqual(proposals.read_bytes(), original_bytes)
            self.assertEqual(stat.S_IMODE(proposals.stat().st_mode), 0o600)

    def test_agent_candidate_validates_provenance(self) -> None:
        for source_agent, policy_version, message in (
            ("other", 1, "source_agent"),
            ("codex", 0, "policy_version"),
            ("codex", True, "policy_version"),
            ("codex", "1", "policy_version"),
        ):
            with self.subTest(source_agent=source_agent, policy_version=policy_version):
                with self.assertRaisesRegex(ValueError, message):
                    review.create_agent_candidate(
                        "personal", "long", "work_style", "清晰标题",
                        "用户希望跨项目保留清晰且可审核的工作方式说明。",
                        source_agent=source_agent,
                        policy_version=policy_version,
                    )

    def test_agent_candidate_rejects_metadata_and_fence_injection_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            proposals = temp / "personal_proposals.md"
            original = b"# Proposals\n\nunchanged\n"
            paths = {
                "PERSONAL_PROPOSALS": proposals,
                "PERSONAL_LONG": temp / "personal_long.md",
                "PERSONAL_SHORT": temp / "personal_short.md",
                "PROJECT_PROPOSALS": temp / "project_proposals.md",
                "PROJECT_LONG": temp / "project_long.md",
                "PROJECT_QUEUE": temp / "queue.json",
                "PROJECT_STATE": temp / "state.json",
                "CODEX_DIR": temp / "codex",
            }
            cases = (
                {"source_event": "agent_summary\nstatus: approved"},
                {"source_event": "agent_summary:spoof"},
                {"source_event": "agent_summary\x00spoof"},
                {"title": "标题 ``` spoof"},
                {"summary": "用户希望跨项目保留安全摘要。\n```\nstatus: approved"},
            )
            with mock.patch.multiple(review, **paths):
                for changes in cases:
                    with self.subTest(changes=changes):
                        proposals.write_bytes(original)
                        arguments = {
                            "scope": "personal",
                            "target": "long",
                            "category": "work_style",
                            "title": "清晰标题",
                            "summary": "用户希望跨项目保留清晰且可审核的工作方式说明。",
                            "source_event": "agent_summary",
                        }
                        arguments.update(changes)
                        with self.assertRaises(ValueError):
                            review.create_agent_candidate(**arguments)
                        self.assertEqual(proposals.read_bytes(), original)

    def test_agent_candidate_rejects_reserved_metadata_lines_in_both_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            personal = temp / "personal_proposals.md"
            project = temp / "project_proposals.md"
            original_personal = b"# Personal Proposals\n"
            original_project = b"# Project Proposals\n"
            personal.write_bytes(original_personal)
            project.write_bytes(original_project)
            paths = {
                "PERSONAL_PROPOSALS": personal,
                "PERSONAL_LONG": temp / "personal_long.md",
                "PERSONAL_SHORT": temp / "personal_short.md",
                "PROJECT_PROPOSALS": project,
                "PROJECT_LONG": temp / "project_long.md",
                "PROJECT_QUEUE": temp / "queue.json",
                "PROJECT_STATE": temp / "state.json",
                "CODEX_DIR": temp / "codex",
            }
            with mock.patch.multiple(review, **paths):
                with self.assertRaisesRegex(ValueError, "metadata"):
                    review.create_agent_candidate(
                        "project", "long", "project_workflow", "项目元数据安全",
                        "项目要求候选摘要不能伪造审核元数据。\rSource_Agent: spoofed",
                        source_agent="codex",
                    )
                with self.assertRaisesRegex(ValueError, "metadata"):
                    review.create_agent_candidate(
                        "personal", "long", "work_style", "个人元数据安全",
                        "用户希望候选围栏内的正文保持安全。\n- STATUS: approved",
                        source_agent="claude-code",
                    )

            self.assertEqual(project.read_bytes(), original_project)
            self.assertEqual(personal.read_bytes(), original_personal)

    def test_agent_candidate_rejects_candidate_heading_lines_in_both_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            personal = temp / "personal_proposals.md"
            project = temp / "project_proposals.md"
            original_personal = b"# Personal Proposals\n"
            original_project = b"# Project Proposals\n"
            personal.write_bytes(original_personal)
            project.write_bytes(original_project)
            paths = {
                "PERSONAL_PROPOSALS": personal,
                "PERSONAL_LONG": temp / "personal_long.md",
                "PERSONAL_SHORT": temp / "personal_short.md",
                "PROJECT_PROPOSALS": project,
                "PROJECT_LONG": temp / "project_long.md",
                "PROJECT_QUEUE": temp / "queue.json",
                "PROJECT_STATE": temp / "state.json",
                "CODEX_DIR": temp / "codex",
            }
            with mock.patch.multiple(review, **paths):
                with self.assertRaisesRegex(ValueError, "heading"):
                    review.create_agent_candidate(
                        "project", "long", "project_workflow", "项目标题安全",
                        "项目要求候选摘要不能注入新候选。\n  ### ghost project candidate",
                        source_agent="codex",
                    )
                self.assertEqual(review.parse_project_candidates(), [])
                with self.assertRaisesRegex(ValueError, "heading"):
                    review.create_agent_candidate(
                        "personal", "long", "work_style", "个人标题安全",
                        "用户希望围栏内的候选正文保持安全。\n### ghost personal candidate",
                        source_agent="claude-code",
                    )
                self.assertEqual(review.parse_personal_candidates(), [])

            self.assertEqual(project.read_bytes(), original_project)
            self.assertEqual(personal.read_bytes(), original_personal)

    def test_agent_candidate_allows_inline_heading_markers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            paths = {
                "PERSONAL_PROPOSALS": temp / "personal_proposals.md",
                "PERSONAL_LONG": temp / "personal_long.md",
                "PERSONAL_SHORT": temp / "personal_short.md",
                "PROJECT_PROPOSALS": temp / "project_proposals.md",
                "PROJECT_LONG": temp / "project_long.md",
                "PROJECT_QUEUE": temp / "queue.json",
                "PROJECT_STATE": temp / "state.json",
                "CODEX_DIR": temp / "codex",
            }
            paths["PROJECT_PROPOSALS"].write_text("# Project Proposals\n", encoding="utf-8")
            with mock.patch.multiple(review, **paths):
                result = review.create_agent_candidate(
                    "project", "long", "project_workflow", "保留行内标记",
                    "项目说明中允许包含 inline ### 普通文本而不创建额外候选。",
                    source_agent="codex",
                )
                items = review.parse_project_candidates()

            self.assertTrue(result["created"])
            self.assertEqual(len(items), 1)

    def test_concurrent_agent_candidates_are_deduplicated_under_one_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            originals = {
                name: getattr(review, name)
                for name in (
                    "PERSONAL_PROPOSALS", "PERSONAL_LONG", "PERSONAL_SHORT",
                    "PROJECT_PROPOSALS", "PROJECT_LONG", "PROJECT_QUEUE",
                    "PROJECT_STATE", "CODEX_DIR", "build_queue", "read_text",
                )
            }
            proposals = temp / "personal_proposals.md"
            proposals.write_text("# Proposals\n", encoding="utf-8")
            proposal_lock = proposals.with_suffix(proposals.suffix + ".lock")
            read_barrier = threading.Barrier(2)
            start_barrier = threading.Barrier(3)
            results: list[tuple[str, dict]] = []
            errors: list[BaseException] = []

            def synchronized_read(path: pathlib.Path) -> str:
                if path == proposals and not proposal_lock.exists():
                    snapshot = originals["read_text"](path)
                    read_barrier.wait(timeout=3)
                    return snapshot
                return originals["read_text"](path)

            def create(source_agent: str, title: str) -> None:
                try:
                    start_barrier.wait(timeout=3)
                    results.append(
                        (title, review.create_agent_candidate(
                            "personal", "long", "work_style", title,
                            "用户希望跨项目候选在并发代理调用时仍然只生成一条。",
                            source_agent=source_agent,
                        ))
                    )
                except BaseException as error:
                    errors.append(error)

            try:
                review.PERSONAL_PROPOSALS = proposals
                review.PERSONAL_LONG = temp / "personal_long.md"
                review.PERSONAL_SHORT = temp / "personal_short.md"
                review.PROJECT_PROPOSALS = temp / "project_proposals.md"
                review.PROJECT_LONG = temp / "project_long.md"
                review.PROJECT_QUEUE = temp / "queue.json"
                review.PROJECT_STATE = temp / "state.json"
                review.CODEX_DIR = temp / "codex"
                review.build_queue = lambda: {}
                review.read_text = synchronized_read
                threads = [
                    threading.Thread(target=create, args=(agent, title))
                    for agent, title in (
                        ("codex", "Codex 首个标题"),
                        ("claude-code", "Claude 备选标题"),
                    )
                ]
                for thread in threads:
                    thread.start()
                start_barrier.wait(timeout=3)
                for thread in threads:
                    thread.join(timeout=5)
                alive = [thread.name for thread in threads if thread.is_alive()]
                review.read_text = originals["read_text"]
                item = review.parse_personal_candidates()[0]
            finally:
                for name, original in originals.items():
                    setattr(review, name, original)

            self.assertEqual(alive, [])
            self.assertEqual(errors, [])
            self.assertEqual(
                sorted(result["created"] for _, result in results), [False, True]
            )
            text = proposals.read_text(encoding="utf-8")
            self.assertEqual(text.count("并发代理调用时仍然只生成一条"), 1)
            winning_title = next(title for title, result in results if result["created"])
            losing_title = next(title for title, result in results if not result["created"])
            self.assertIn(f"**标题：{winning_title}**", text)
            self.assertNotIn(f"**标题：{losing_title}**", text)
            self.assertEqual(item["source_agents"], ["claude-code", "codex"])

    def test_legacy_pending_candidate_is_deduplicated_and_upgraded_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            proposals = temp / "personal_proposals.md"
            summary = "用户希望旧格式候选也参与等价内容去重并合并代理来源。"
            proposals.write_text(
                "# Proposals\n\n### M-legacy\n\n"
                "memory_id: M-legacy\nstatus: pending\ntarget: long\n"
                "created: 2026-07-30T12:00:00+08:00\n"
                "source_event: agent_summary\ncategory: work_style\n\n"
                "candidate:\n\n```text\n"
                f"**标题：旧格式标题**\n\n**分类：工作方式**\n\n{summary}\n"
                "```\n\napproval_rule: Promote only after explicit approval.\n",
                encoding="utf-8",
            )
            originals = {
                name: getattr(review, name)
                for name in (
                    "PERSONAL_PROPOSALS", "PERSONAL_LONG", "PERSONAL_SHORT",
                    "PROJECT_PROPOSALS", "PROJECT_LONG", "PROJECT_QUEUE",
                    "PROJECT_STATE", "CODEX_DIR", "build_queue",
                )
            }
            try:
                review.PERSONAL_PROPOSALS = proposals
                review.PERSONAL_LONG = temp / "personal_long.md"
                review.PERSONAL_SHORT = temp / "personal_short.md"
                review.PROJECT_PROPOSALS = temp / "project_proposals.md"
                review.PROJECT_LONG = temp / "project_long.md"
                review.PROJECT_QUEUE = temp / "queue.json"
                review.PROJECT_STATE = temp / "state.json"
                review.CODEX_DIR = temp / "codex"
                review.build_queue = lambda: {}
                result = review.create_agent_candidate(
                    "personal", "long", "work_style", "新建议标题", summary,
                    source_agent="codex",
                )
                item = review.parse_personal_candidates()[0]
            finally:
                for name, original in originals.items():
                    setattr(review, name, original)

            self.assertFalse(result["created"])
            self.assertEqual(result["id"], "M-legacy")
            text = proposals.read_text(encoding="utf-8")
            self.assertEqual(text.count("### M-legacy"), 1)
            self.assertNotIn("新建议标题", text)
            self.assertIn("source_agent: unknown", text)
            self.assertIn("source_agents: codex,unknown", text)
            self.assertIn("equivalence:", text)
            self.assertEqual(item["source_agents"], ["codex", "unknown"])
            self.assertEqual(
                item["identity"],
                review.candidate_identity(
                    "personal", "long", "work_style", "旧格式标题", summary
                ),
            )

    def _assert_legacy_project_candidate_preserves_id_and_status(self, status: str) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            proposals = temp / "project_proposals.md"
            heading = "### 2026-07-30T12:00:00+08:00 - 旧格式项目候选"
            summary = "项目要求旧格式候选升级时保持原有稳定标识和审核状态。"
            body = f"**标题：旧格式项目候选**\n\n**分类：项目工作流**\n\n{summary}"
            proposals.write_text(
                f"# Project Proposals\n\n{heading}\n\n{body}\n",
                encoding="utf-8",
            )
            state_path = temp / "state.json"
            original_id = review.stable_id("P", proposals, heading, body)
            state_items = {}
            if status != "pending":
                state_items[original_id] = {
                    "status": status,
                    "decided_at": "2026-07-30T12:30:00+08:00",
                }
            state_path.write_text(
                json.dumps({"items": state_items, "last_reminder_at": ""}),
                encoding="utf-8",
            )
            paths = {
                "PERSONAL_PROPOSALS": temp / "personal_proposals.md",
                "PERSONAL_LONG": temp / "personal_long.md",
                "PERSONAL_SHORT": temp / "personal_short.md",
                "PROJECT_PROPOSALS": proposals,
                "PROJECT_LONG": temp / "project_long.md",
                "PROJECT_QUEUE": temp / "queue.json",
                "PROJECT_STATE": state_path,
                "CODEX_DIR": temp / "codex",
            }
            with mock.patch.multiple(review, **paths):
                before = review.build_queue()["items"][0]
                result = review.create_agent_candidate(
                    "project", "long", "project_workflow", "新标题", summary,
                    source_agent="codex",
                )
                queue = review.load_queue(refresh=True)

            self.assertEqual(before["id"], original_id)
            self.assertEqual(before["status"], status)
            self.assertFalse(result["created"])
            self.assertEqual(result["id"], original_id)
            self.assertEqual(queue["items"][0]["id"], original_id)
            self.assertEqual(queue["items"][0]["status"], status)
            self.assertIn(
                f"- candidate_id: `{original_id}`",
                proposals.read_text(encoding="utf-8"),
            )
            self.assertEqual(queue["counts"]["pending"], 1 if status == "pending" else 0)

    def test_legacy_pending_project_candidate_upgrade_preserves_id(self) -> None:
        self._assert_legacy_project_candidate_preserves_id_and_status("pending")

    def test_legacy_approved_project_candidate_upgrade_preserves_status(self) -> None:
        self._assert_legacy_project_candidate_preserves_id_and_status("approved")

    def test_legacy_rejected_project_candidate_upgrade_preserves_status(self) -> None:
        self._assert_legacy_project_candidate_preserves_id_and_status("rejected")

    def _create_isolated_personal_candidate(self, temp: pathlib.Path, title: str) -> dict:
        return review.create_agent_candidate(
            "personal", "long", "work_style", title,
            f"用户希望跨项目保留{title}对应的清晰工作方式说明。",
            source_agent="codex",
        )

    def test_repeated_approval_is_idempotent_and_preserves_official_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            official = temp / "personal_long.md"
            official.write_text("# Long Memory\n", encoding="utf-8")
            official.chmod(0o600)
            paths = {
                "PERSONAL_PROPOSALS": temp / "personal_proposals.md",
                "PERSONAL_LONG": official,
                "PERSONAL_SHORT": temp / "personal_short.md",
                "PROJECT_PROPOSALS": temp / "project_proposals.md",
                "PROJECT_LONG": temp / "project_long.md",
                "PROJECT_QUEUE": temp / "queue.json",
                "PROJECT_STATE": temp / "state.json",
                "CODEX_DIR": temp / "codex",
            }
            paths["PERSONAL_PROPOSALS"].write_text("# Proposals\n", encoding="utf-8")
            with mock.patch.multiple(review, **paths):
                candidate = self._create_isolated_personal_candidate(temp, "重复批准")
                first = review.approve(candidate["id"])
                second = review.approve(candidate["id"])

            text = official.read_text(encoding="utf-8")
            self.assertEqual(text.count("\n### "), 1)
            self.assertEqual(first["decision"], second["decision"])
            self.assertRegex(first["decision"]["content_digest"], r"^[0-9a-f]{64}$")
            self.assertEqual(stat.S_IMODE(official.stat().st_mode), 0o600)

    def test_conflicting_approval_and_status_decisions_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            paths = {
                "PERSONAL_PROPOSALS": temp / "personal_proposals.md",
                "PERSONAL_LONG": temp / "personal_long.md",
                "PERSONAL_SHORT": temp / "personal_short.md",
                "PROJECT_PROPOSALS": temp / "project_proposals.md",
                "PROJECT_LONG": temp / "project_long.md",
                "PROJECT_QUEUE": temp / "queue.json",
                "PROJECT_STATE": temp / "state.json",
                "CODEX_DIR": temp / "codex",
            }
            paths["PERSONAL_PROPOSALS"].write_text("# Proposals\n", encoding="utf-8")
            with mock.patch.multiple(review, **paths):
                candidate = self._create_isolated_personal_candidate(temp, "冲突批准")
                approved = review.approve(candidate["id"])
                with self.assertRaisesRegex(ValueError, "decision conflict"):
                    review.approve(candidate["id"], content="修改后的批准内容")
                with self.assertRaisesRegex(ValueError, "decision conflict"):
                    review.approve(candidate["id"], target="personal_short")
                with self.assertRaisesRegex(ValueError, "decision conflict"):
                    review.reject(candidate["id"])
                final = review.find_item(candidate["id"])

            self.assertEqual(final["decision"], approved["decision"])
            self.assertEqual(
                paths["PERSONAL_LONG"].read_text(encoding="utf-8").count("\n### "), 1
            )
            self.assertFalse(paths["PERSONAL_SHORT"].exists())

    def test_personal_short_approval_marks_only_short_entry_for_retention(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            paths = {
                "PERSONAL_PROPOSALS": temp / "personal_proposals.md",
                "PERSONAL_LONG": temp / "personal_long.md",
                "PERSONAL_SHORT": temp / "personal_short.md",
                "PROJECT_PROPOSALS": temp / "project_proposals.md",
                "PROJECT_LONG": temp / "project_long.md",
                "PROJECT_QUEUE": temp / "queue.json",
                "PROJECT_STATE": temp / "state.json",
                "CODEX_DIR": temp / "codex",
            }
            paths["PERSONAL_PROPOSALS"].write_text("# Proposals\n", encoding="utf-8")
            with mock.patch.multiple(review, **paths):
                candidate = self._create_isolated_personal_candidate(temp, "短期偏好")
                review.approve(candidate["id"], target="personal_short")
            text = paths["PERSONAL_SHORT"].read_text(encoding="utf-8")
            self.assertIn("<!-- vibe-memory:short:begin length=", text)
            self.assertIn("sha256=", text)
            self.assertNotIn("expires_on:", text)
            import vibe_memory_settings
            vibe_memory_settings.prune_personal_short(
                paths["PERSONAL_SHORT"], today=_dt.date(2026, 8, 13), retention_days=14
            )
            text = paths["PERSONAL_SHORT"].read_text(encoding="utf-8")
            self.assertIn("expires_on=2026-08-27", text)
            vibe_memory_settings.prune_personal_short(
                paths["PERSONAL_SHORT"], today=_dt.date(2026, 8, 28), retention_days=14
            )
            self.assertNotIn("vibe-memory:short:begin", paths["PERSONAL_SHORT"].read_text(encoding="utf-8"))

    def test_repeated_reject_is_idempotent_and_conflicting_defer_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            paths = {
                "PERSONAL_PROPOSALS": temp / "personal_proposals.md",
                "PERSONAL_LONG": temp / "personal_long.md",
                "PERSONAL_SHORT": temp / "personal_short.md",
                "PROJECT_PROPOSALS": temp / "project_proposals.md",
                "PROJECT_LONG": temp / "project_long.md",
                "PROJECT_QUEUE": temp / "queue.json",
                "PROJECT_STATE": temp / "state.json",
                "CODEX_DIR": temp / "codex",
            }
            paths["PERSONAL_PROPOSALS"].write_text("# Proposals\n", encoding="utf-8")
            with mock.patch.multiple(review, **paths):
                candidate = self._create_isolated_personal_candidate(temp, "重复拒绝")
                with mock.patch.object(
                    review, "now",
                    side_effect=("first", "queue-1", "second", "queue-2", "third"),
                ):
                    review.reject(candidate["id"])
                    first_state = paths["PROJECT_STATE"].read_bytes()
                    review.reject(candidate["id"])
                    self.assertEqual(paths["PROJECT_STATE"].read_bytes(), first_state)
                    with self.assertRaisesRegex(ValueError, "decision conflict"):
                        review.defer(candidate["id"])

    def test_concurrent_same_candidate_approval_writes_one_entry_without_state_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            paths = {
                "PERSONAL_PROPOSALS": temp / "personal_proposals.md",
                "PERSONAL_LONG": temp / "personal_long.md",
                "PERSONAL_SHORT": temp / "personal_short.md",
                "PROJECT_PROPOSALS": temp / "project_proposals.md",
                "PROJECT_LONG": temp / "project_long.md",
                "PROJECT_QUEUE": temp / "queue.json",
                "PROJECT_STATE": temp / "state.json",
                "CODEX_DIR": temp / "codex",
            }
            paths["PERSONAL_PROPOSALS"].write_text("# Proposals\n", encoding="utf-8")
            errors: list[BaseException] = []
            start = threading.Barrier(3)
            with mock.patch.multiple(review, **paths):
                candidate = self._create_isolated_personal_candidate(temp, "并发批准")
                review.record_decision("unrelated", {"status": "deferred", "decided_at": "seed"})

                def approve_once() -> None:
                    try:
                        start.wait(timeout=3)
                        review.approve(candidate["id"])
                    except BaseException as error:
                        errors.append(error)

                threads = [threading.Thread(target=approve_once) for _ in range(2)]
                for thread in threads:
                    thread.start()
                start.wait(timeout=3)
                for thread in threads:
                    thread.join(timeout=5)
                alive = [thread.name for thread in threads if thread.is_alive()]
                state = json.loads(paths["PROJECT_STATE"].read_text(encoding="utf-8"))

            self.assertEqual(alive, [])
            self.assertEqual(errors, [])
            self.assertEqual(
                paths["PERSONAL_LONG"].read_text(encoding="utf-8").count("\n### "), 1
            )
            self.assertEqual(state["items"][candidate["id"]]["status"], "approved")
            self.assertEqual(state["items"]["unrelated"]["status"], "deferred")

    def test_mark_reminded_and_decision_share_one_state_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            state_path = temp / "state.json"
            state_path.write_text(
                json.dumps({"items": {}, "last_reminder_at": ""}), encoding="utf-8"
            )
            paths = {
                "PROJECT_STATE": state_path,
                "PROJECT_QUEUE": temp / "queue.json",
            }
            original_write_json = review.write_json
            mark_at_write = threading.Event()
            allow_mark_write = threading.Event()
            decision_written = threading.Event()
            errors: list[BaseException] = []

            def synchronized_write(path: pathlib.Path, value: object) -> None:
                if path == state_path and threading.current_thread().name == "reminder":
                    mark_at_write.set()
                    if not allow_mark_write.wait(timeout=3):
                        raise TimeoutError("reminder write was not released")
                original_write_json(path, value)
                if path == state_path and threading.current_thread().name == "decision":
                    decision_written.set()

            def remind() -> None:
                try:
                    review.mark_reminded()
                except BaseException as error:
                    errors.append(error)

            def decide() -> None:
                try:
                    review.record_decision(
                        "candidate-1", {"status": "rejected", "decided_at": "decision"}
                    )
                except BaseException as error:
                    errors.append(error)

            with mock.patch.multiple(review, **paths), mock.patch.object(
                review, "write_json", side_effect=synchronized_write
            ), mock.patch.object(review, "build_queue", return_value={}):
                reminder = threading.Thread(name="reminder", target=remind)
                decision = threading.Thread(name="decision", target=decide)
                reminder.start()
                self.assertTrue(mark_at_write.wait(timeout=3))
                decision.start()
                decision_written.wait(timeout=0.3)
                allow_mark_write.set()
                reminder.join(timeout=5)
                decision.join(timeout=5)
                alive = [
                    thread.name for thread in (reminder, decision) if thread.is_alive()
                ]

            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(alive, [])
            self.assertEqual(errors, [])
            self.assertEqual(state["items"]["candidate-1"]["status"], "rejected")
            self.assertTrue(state["last_reminder_at"])

    def test_reset_then_reapprove_does_not_duplicate_official_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            paths = {
                "PERSONAL_PROPOSALS": temp / "personal_proposals.md",
                "PERSONAL_LONG": temp / "personal_long.md",
                "PERSONAL_SHORT": temp / "personal_short.md",
                "PROJECT_PROPOSALS": temp / "project_proposals.md",
                "PROJECT_LONG": temp / "project_long.md",
                "PROJECT_QUEUE": temp / "queue.json",
                "PROJECT_STATE": temp / "state.json",
                "CODEX_DIR": temp / "codex",
            }
            paths["PERSONAL_PROPOSALS"].write_text("# Proposals\n", encoding="utf-8")
            with mock.patch.multiple(review, **paths):
                candidate = self._create_isolated_personal_candidate(temp, "重置后批准")
                review.approve(candidate["id"])
                review.reset(candidate["id"])
                self.assertEqual(review.find_item(candidate["id"])["status"], "pending")
                review.approve(candidate["id"])
                final = review.find_item(candidate["id"])

            official = paths["PERSONAL_LONG"].read_text(encoding="utf-8")
            self.assertEqual(official.count("\n### "), 1)
            self.assertEqual(final["status"], "approved")
            self.assertRegex(final["decision"]["content_digest"], r"^[0-9a-f]{64}$")

    def test_equivalent_approved_memory_is_not_reproposed_under_a_new_title(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            summary = "用户希望已批准的等价内容不会因为标题变化再次进入候选队列，并保留额外限定。"
            different_summary = "用户希望已批准的等价内容不会因为标题变化再次进入候选队列"
            originals = {
                name: getattr(review, name)
                for name in (
                    "PERSONAL_PROPOSALS", "PERSONAL_LONG", "PERSONAL_SHORT",
                    "PROJECT_PROPOSALS", "PROJECT_LONG", "PROJECT_QUEUE",
                    "PROJECT_STATE", "CODEX_DIR", "build_queue",
                )
            }
            try:
                review.PERSONAL_PROPOSALS = temp / "personal_proposals.md"
                review.PERSONAL_PROPOSALS.write_text("# Proposals\n", encoding="utf-8")
                review.PERSONAL_LONG = temp / "personal_long.md"
                review.PERSONAL_LONG.write_text(
                    "# Long Memory\n\n**标题：已批准标题**\n\n"
                    f"**分类：工作方式**\n\n{summary}\n",
                    encoding="utf-8",
                )
                review.PERSONAL_SHORT = temp / "personal_short.md"
                review.PROJECT_PROPOSALS = temp / "project_proposals.md"
                review.PROJECT_LONG = temp / "project_long.md"
                review.PROJECT_QUEUE = temp / "queue.json"
                review.PROJECT_STATE = temp / "state.json"
                review.CODEX_DIR = temp / "codex"
                review.build_queue = lambda: {}
                result = review.create_agent_candidate(
                    "personal", "long", "work_style", "新的建议标题", summary,
                    source_agent="claude-code",
                )
                different = review.create_agent_candidate(
                    "personal", "long", "work_style", "不同内容标题",
                    different_summary,
                    source_agent="codex",
                )
            finally:
                for name, original in originals.items():
                    setattr(review, name, original)

            self.assertFalse(result["created"])
            self.assertTrue(different["created"])
            proposal_text = (temp / "personal_proposals.md").read_text(encoding="utf-8")
            self.assertEqual(proposal_text.count("memory_id:"), 1)
            self.assertIn(different_summary, proposal_text)

    def test_concurrent_scopes_rebuild_one_atomic_queue_without_lost_items(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            originals = {
                name: getattr(review, name)
                for name in (
                    "PERSONAL_PROPOSALS", "PERSONAL_LONG", "PERSONAL_SHORT",
                    "PROJECT_PROPOSALS", "PROJECT_LONG", "PROJECT_QUEUE",
                    "PROJECT_STATE", "CODEX_DIR", "build_queue", "write_json",
                    "parse_project_candidates",
                )
            }
            personal_parsed_project = threading.Event()
            project_reached_build = threading.Event()
            project_queue_written = threading.Event()
            errors: list[BaseException] = []
            queue_path = temp / "queue.json"
            queue_lock = queue_path.with_suffix(queue_path.suffix + ".lock")

            def synchronized_parse_project() -> list[dict]:
                items = originals["parse_project_candidates"]()
                if threading.current_thread().name == "personal-create":
                    personal_parsed_project.set()
                    if not project_reached_build.wait(timeout=3):
                        raise TimeoutError("project did not reach queue build")
                return items

            def synchronized_build_queue() -> dict:
                if threading.current_thread().name == "project-create":
                    project_reached_build.set()
                return originals["build_queue"]()

            def ordered_write_json(path: pathlib.Path, value: object) -> None:
                if path == queue_path:
                    if (
                        threading.current_thread().name == "personal-create"
                        and not queue_lock.exists()
                        and not project_queue_written.wait(timeout=3)
                    ):
                        raise TimeoutError("project queue write did not finish")
                    originals["write_json"](path, value)
                    if threading.current_thread().name == "project-create":
                        project_queue_written.set()
                    return
                originals["write_json"](path, value)

            def create_personal() -> None:
                try:
                    review.create_agent_candidate(
                        "personal", "long", "work_style", "个人候选",
                        "用户希望并发构建审核队列时保留个人候选记录。",
                        source_agent="codex",
                    )
                except BaseException as error:
                    errors.append(error)

            def create_project() -> None:
                try:
                    review.create_agent_candidate(
                        "project", "long", "project_workflow", "项目候选",
                        "项目要求并发构建审核队列时保留项目候选记录。",
                        source_agent="claude-code",
                    )
                except BaseException as error:
                    errors.append(error)

            try:
                review.PERSONAL_PROPOSALS = temp / "personal_proposals.md"
                review.PERSONAL_PROPOSALS.write_text("# Proposals\n", encoding="utf-8")
                review.PERSONAL_LONG = temp / "personal_long.md"
                review.PERSONAL_SHORT = temp / "personal_short.md"
                review.PROJECT_PROPOSALS = temp / "project_proposals.md"
                review.PROJECT_PROPOSALS.write_text("# Project Proposals\n", encoding="utf-8")
                review.PROJECT_LONG = temp / "project_long.md"
                review.PROJECT_QUEUE = queue_path
                review.PROJECT_STATE = temp / "state.json"
                review.CODEX_DIR = temp / "codex"
                review.build_queue = synchronized_build_queue
                review.write_json = ordered_write_json
                review.parse_project_candidates = synchronized_parse_project
                personal_thread = threading.Thread(
                    name="personal-create", target=create_personal
                )
                project_thread = threading.Thread(
                    name="project-create", target=create_project
                )
                personal_thread.start()
                self.assertTrue(personal_parsed_project.wait(timeout=3))
                project_thread.start()
                personal_thread.join(timeout=5)
                project_thread.join(timeout=5)
                alive = [
                    thread.name
                    for thread in (personal_thread, project_thread)
                    if thread.is_alive()
                ]
            finally:
                for name, original in originals.items():
                    setattr(review, name, original)

            self.assertEqual(alive, [])
            self.assertEqual(errors, [])
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            self.assertEqual({item["scope"] for item in queue["items"]}, {"personal", "project"})
            self.assertEqual(queue["counts"]["pending"], 2)

    def test_propose_cli_accepts_and_forwards_candidate_provenance(self) -> None:
        argv = [
            "memory_review.py", "propose", "--scope", "personal", "--target", "long",
            "--category", "work_style", "--title", "清晰标题",
            "--summary", "用户希望跨项目保留清晰且可审核的工作方式说明。",
            "--source-agent", "claude-code", "--policy-version", "3",
        ]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            review_cli.review,
            "create_agent_candidate",
            return_value={"created": True, "id": "M-test"},
        ) as create, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(review_cli.main(), 0)

        create.assert_called_once_with(
            "personal", "long", "work_style", "清晰标题",
            "用户希望跨项目保留清晰且可审核的工作方式说明。",
            "agent_summary",
            source_agent="claude-code",
            policy_version=3,
        )

    def test_upgrade_memory_hooks_preserves_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            project = pathlib.Path(temp_value)
            old_hook = project / ".codex" / "hooks" / "shared_memory_hook.py"
            old_hook.parent.mkdir(parents=True)
            old_hook.write_text("old managed hook\n", encoding="utf-8")
            result = memory_project.upgrade_memory_hooks(project)
            installed = old_hook.read_text(encoding="utf-8")
            self.assertIn("- source_agent: ", installed)
            self.assertNotIn("- summary: ", installed)
            backups = list(old_hook.parent.glob("shared_memory_hook.py.bak.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "old managed hook\n")
            self.assertTrue(any(item["status"] == "backup" for item in result["changes"]))

    def test_hook_upgrade_merges_ui_gate_entry_and_preserves_unrelated_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            project = pathlib.Path(temp_value)
            settings_path = project / ".codex/hooks.json"
            settings_path.parent.mkdir(parents=True)
            custom = {
                "custom": "keep",
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "CustomTool",
                            "hooks": [{"type": "command", "command": "custom-hook"}],
                        }
                    ],
                    "PostToolUse": [
                        {"hooks": [{"type": "command", "command": "post-hook"}]}
                    ],
                },
            }
            settings_path.write_text(json.dumps(custom), encoding="utf-8")

            first = memory_project.upgrade_memory_hooks(project)
            merged = json.loads(settings_path.read_text(encoding="utf-8"))
            second = memory_project.upgrade_memory_hooks(project)

            self.assertEqual(merged["custom"], "keep")
            self.assertEqual(merged["hooks"]["PostToolUse"], custom["hooks"]["PostToolUse"])
            commands = [
                hook["command"]
                for entry in merged["hooks"]["PreToolUse"]
                for hook in entry["hooks"]
            ]
            self.assertIn("custom-hook", commands)
            self.assertTrue(any("ui_design_gate_hook.py" in item for item in commands))
            self.assertTrue(any(item["status"] == "backup" for item in first["changes"]))
            self.assertFalse(any(item["status"] == "backup" for item in second["changes"]))

    def test_hook_upgrade_reports_malformed_settings_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            project = pathlib.Path(temp_value)
            settings_path = project / ".claude/settings.json"
            settings_path.parent.mkdir(parents=True)
            original = "{not-json\n"
            settings_path.write_text(original, encoding="utf-8")

            result = memory_project.upgrade_memory_hooks(project)

            self.assertEqual(settings_path.read_text(encoding="utf-8"), original)
            self.assertTrue(
                any(
                    item["path"] == str(settings_path.resolve())
                    and item["status"] == "conflict"
                    for item in result["changes"]
                )
            )

    def test_upgrade_managed_rules_preserves_user_text_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            project = pathlib.Path(temp_value)
            agents = project / "AGENTS.md"
            agents.write_text("# User rules\n\nKeep this exact text.\n", encoding="utf-8")
            self.assertTrue(hasattr(memory_project, "upgrade_memory_rules"))

            first = memory_project.upgrade_memory_rules(project)
            updated = agents.read_text(encoding="utf-8")
            second = memory_project.upgrade_memory_rules(project)

            self.assertIn("Keep this exact text.", updated)
            self.assertIn("codex/ui_design/effective-context.json", updated)
            self.assertIn("visible-interface", updated)
            self.assertIn(loop_superpowers.MANAGED_RULE_START, updated)
            self.assertEqual(updated, agents.read_text(encoding="utf-8"))
            self.assertTrue(any(item["status"] == "backup" for item in first["changes"]))
            self.assertFalse(any(item["status"] == "backup" for item in second["changes"]))

    def test_upgrade_managed_rules_reports_unmatched_marker_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            project = pathlib.Path(temp_value)
            agents = project / "AGENTS.md"
            original = f"user text\n{loop_superpowers.MANAGED_RULE_START}\nbroken\n"
            agents.write_text(original, encoding="utf-8")
            self.assertTrue(hasattr(memory_project, "upgrade_memory_rules"))

            result = memory_project.upgrade_memory_rules(project)

            self.assertEqual(agents.read_text(encoding="utf-8"), original)
            self.assertTrue(any(item["status"] == "conflict" for item in result["changes"]))

    def test_quarantine_preserves_source_and_marks_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            source = temp / "proposals.md"
            source.write_text("original candidate source\n", encoding="utf-8")
            review.CODEX_DIR = temp / "codex"
            review.PROJECT_STATE = review.CODEX_DIR / "memory_review_state.json"
            item = {
                "id": "M-20260719-000001",
                "scope": "personal",
                "status": "pending",
                "target": "long",
                "source_path": str(source),
                "content": "审核台候选记忆管理命令",
            }
            original_loader = review.load_queue
            original_record = review.record_decision
            decisions: dict[str, dict] = {}
            try:
                review.load_queue = lambda refresh=True: {"items": [item]}
                review.record_decision = lambda candidate_id, decision: decisions.setdefault(candidate_id, decision)
                ids = review.reject_noise_personal_candidates(dry_run=False)
            finally:
                review.load_queue = original_loader
                review.record_decision = original_record

            self.assertEqual(ids, [item["id"]])
            self.assertEqual(source.read_text(encoding="utf-8"), "original candidate source\n")
            self.assertEqual(decisions[item["id"]]["status"], "rejected")
            archive = review.CODEX_DIR / "memory_review_noise_personal.md"
            self.assertIn(item["id"], archive.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
