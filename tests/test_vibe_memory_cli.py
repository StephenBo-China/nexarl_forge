from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import pathlib
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
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

    def test_status_reports_missing_launcher_as_an_actionable_runtime_error(self) -> None:
        release = self.paths.install_root / "releases/1.0.0/scripts"
        release.mkdir(parents=True)
        (release / "vibe_memory_cli.py").write_text("# cli\n", encoding="utf-8")
        (self.paths.install_root / "current").symlink_to("releases/1.0.0")
        vibe_memory_cli.vibe_memory_install.install_runtime_config(
            self.paths,
            port=9123,
            app_version="1.0.0",
            python_executable=sys.executable,
        )

        result = vibe_memory_cli.collect_status(self.paths)

        self.assertFalse(result["runtime"]["ok"])
        self.assertEqual(result["runtime"]["status"], "launcher_missing")
        self.assertEqual(result["runtime"]["action"], "run install")

    def test_status_reports_a_tampered_launcher_as_an_actionable_runtime_error(self) -> None:
        release = self.paths.install_root / "releases/1.0.0/scripts"
        release.mkdir(parents=True)
        (release / "vibe_memory_cli.py").write_text("# cli\n", encoding="utf-8")
        (self.paths.install_root / "current").symlink_to("releases/1.0.0")
        vibe_memory_cli.vibe_memory_install.install_runtime_config(
            self.paths,
            port=9123,
            app_version="1.0.0",
            python_executable=sys.executable,
        )
        self.paths.launcher.parent.mkdir(parents=True)
        self.paths.launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.paths.launcher.chmod(0o700)

        result = vibe_memory_cli.collect_status(self.paths)

        self.assertFalse(result["runtime"]["ok"])
        self.assertEqual(result["runtime"]["status"], "launcher_invalid")
        self.assertEqual(result["runtime"]["action"], "run install")

    def test_doctor_reports_missing_persisted_python_as_python_error(self) -> None:
        release = self.paths.install_root / "releases/1.0.0/scripts"
        release.mkdir(parents=True)
        (release / "vibe_memory_cli.py").write_text("# cli\n", encoding="utf-8")
        (self.paths.install_root / "current").symlink_to("releases/1.0.0")
        vibe_memory_cli.vibe_memory_install.install_runtime_config(
            self.paths,
            port=9123,
            app_version="1.0.0",
            python_executable=sys.executable,
        )
        config_path = self.paths.install_root / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        del config["python_executable"]
        del config["python_version"]
        config_path.write_text(json.dumps(config), encoding="utf-8")
        vibe_memory_cli.vibe_memory_install.install_launcher(
            self.paths, python_executable=sys.executable
        )

        result = vibe_memory_cli.collect_status(self.paths)

        self.assertFalse(result["runtime"]["ok"])
        self.assertEqual(result["runtime"]["status"], "python_error")
        self.assertIn("python", result["runtime"]["error"])

    def test_doctor_reports_low_persisted_python_without_launcher_fallback(self) -> None:
        release = self.paths.install_root / "releases/1.0.0/scripts"
        release.mkdir(parents=True)
        (release / "vibe_memory_cli.py").write_text("# cli\n", encoding="utf-8")
        (self.paths.install_root / "current").symlink_to("releases/1.0.0")
        config_path = self.paths.install_root / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps({
            "app_version": "1.0.0",
            "port": 9123,
            "python_executable": "/usr/bin/python3",
            "python_version": "3.9",
            "schema_version": 1,
            "service": "vibe-memory",
        }), encoding="utf-8")
        self.paths.launcher.parent.mkdir(parents=True)
        self.paths.launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.paths.launcher.chmod(0o700)

        result = vibe_memory_cli.collect_status(self.paths)

        self.assertFalse(result["runtime"]["ok"])
        self.assertEqual(result["runtime"]["status"], "python_error")
        self.assertIn("3.10", result["runtime"]["error"])

    def test_doctor_reports_malformed_persisted_python_version_without_crashing(self) -> None:
        release = self.paths.install_root / "releases/1.0.0/scripts"
        release.mkdir(parents=True)
        (release / "vibe_memory_cli.py").write_text("# cli\n", encoding="utf-8")
        (self.paths.install_root / "current").symlink_to("releases/1.0.0")
        config_path = self.paths.install_root / "config.json"
        config_path.write_text(json.dumps({
            "app_version": "1.0.0",
            "port": 9123,
            "python_executable": sys.executable,
            "python_version": ("9" * 5000) + ".10",
            "schema_version": 1,
            "service": "vibe-memory",
        }), encoding="utf-8")

        result = vibe_memory_cli.collect_status(self.paths)

        self.assertFalse(result["runtime"]["ok"])
        self.assertEqual(result["runtime"]["status"], "python_error")
        self.assertIn("python", result["runtime"]["error"])

    def test_doctor_reports_invalid_persisted_python_executable_without_crashing(self) -> None:
        release = self.paths.install_root / "releases/1.0.0/scripts"
        release.mkdir(parents=True)
        (release / "vibe_memory_cli.py").write_text("# cli\n", encoding="utf-8")
        (self.paths.install_root / "current").symlink_to("releases/1.0.0")
        config_path = self.paths.install_root / "config.json"
        config_path.write_text(json.dumps({
            "app_version": "1.0.0",
            "port": 9123,
            "python_executable": sys.executable + "\x00",
            "python_version": "3.11",
            "schema_version": 1,
            "service": "vibe-memory",
        }), encoding="utf-8")

        result = vibe_memory_cli.collect_status(self.paths)

        self.assertFalse(result["runtime"]["ok"])
        self.assertEqual(result["runtime"]["status"], "python_error")
        self.assertIn("python", result["runtime"]["error"])

    def test_doctor_rejects_symlinked_runtime_config_before_probing_python(self) -> None:
        release = self.paths.install_root / "releases/1.0.0/scripts"
        release.mkdir(parents=True)
        (release / "vibe_memory_cli.py").write_text("# cli\n", encoding="utf-8")
        (self.paths.install_root / "current").symlink_to("releases/1.0.0")
        marker = pathlib.Path(self.temporary.name) / "probed"
        executable = pathlib.Path(self.temporary.name) / "probe-python"
        executable.write_text(
            "#!/bin/sh\n"
            f"printf x > {shlex.quote(str(marker))}\n"
            "printf '3.11\\n'\n",
            encoding="utf-8",
        )
        executable.chmod(0o700)
        target = pathlib.Path(self.temporary.name) / "runtime-config.json"
        target.write_text(json.dumps({
            "app_version": "1.0.0",
            "port": 9123,
            "python_executable": str(executable),
            "python_version": "3.11",
            "schema_version": 1,
            "service": "vibe-memory",
        }), encoding="utf-8")
        config_path = self.paths.install_root / "config.json"
        config_path.symlink_to(target)

        result = vibe_memory_cli.collect_status(self.paths)

        self.assertFalse(result["runtime"]["ok"])
        self.assertEqual(result["runtime"]["status"], "python_error")
        self.assertFalse(marker.exists())

    def test_install_delegates_runtime_plist_and_hooks_without_launchctl(self) -> None:
        runtime = self.paths.install_root / "current"
        launcher = self.paths.launcher
        args = ["install", "--source-root", "/portable/source", "--with-claude-hooks"]
        with mock.patch("vibe_memory_cli.vibe_memory_install.discover_python", return_value="/opt/homebrew/bin/python3") as discover, \
                mock.patch("vibe_memory_cli.vibe_memory_install.validate_runtime_source", return_value={"version": "1.0.0"}) as validate, \
                mock.patch("vibe_memory_cli.vibe_memory_install.install_runtime", return_value={"version": "1.0.0"}) as install, \
                mock.patch("vibe_memory_cli.vibe_memory_install.prepare_data", return_value={"files": []}) as prepare, \
                mock.patch("vibe_memory_cli.vibe_memory_install.render_launch_agent", return_value="<plist/>") as render, \
                mock.patch("vibe_memory_cli.vibe_memory_install.render_runtime_config", return_value="{}\n") as render_config, \
                mock.patch("vibe_memory_cli.vibe_memory_install.install_runtime_config", return_value={"changed": True}) as install_config, \
                mock.patch("vibe_memory_cli.vibe_memory_install._install_state_document", return_value={"state": True}) as state_document, \
                mock.patch("vibe_memory_cli.vibe_memory_install.write_install_state") as write_state, \
                mock.patch("vibe_memory_cli.vibe_memory_install.render_launcher", return_value="#!/bin/sh\n") as render_launcher, \
                mock.patch("vibe_memory_cli.vibe_memory_install.install_launcher", return_value={"changed": True, "path": "launcher"}) as install_launcher, \
                mock.patch("vibe_memory_cli.vibe_memory_install.install_launch_agent", return_value={"changed": True, "path": "agent"}) as write, \
                mock.patch("vibe_memory_cli.vibe_memory_migration.validate_control_plane", return_value={"projects": "ok"}) as validate_control, \
                mock.patch("vibe_memory_cli.vibe_memory_hooks.preview", return_value={"status": "missing"}) as preview, \
                mock.patch("vibe_memory_cli.vibe_memory_hooks.repair", side_effect=[{"status": "created", "changed": True}, {"status": "created", "changed": True}]) as repair, \
                mock.patch("vibe_memory_cli.subprocess.run") as run:
            code, output, _ = self.invoke(args)
        self.assertEqual(code, 0)
        discover.assert_called_once_with()
        validate.assert_called_once_with(pathlib.Path("/portable/source"))
        install.assert_called_once_with(pathlib.Path("/portable/source"), self.paths)
        prepare.assert_called_once_with(self.paths)
        render.assert_called_once_with(
            self.paths, port=8897, python_executable="/opt/homebrew/bin/python3"
        )
        render_config.assert_called_once_with(
            8897, "1.0.0", python_executable="/opt/homebrew/bin/python3"
        )
        render_launcher.assert_called_once_with(
            self.paths, python_executable="/opt/homebrew/bin/python3"
        )
        install_launcher.assert_called_once_with(
            self.paths, python_executable="/opt/homebrew/bin/python3"
        )
        install_config.assert_called_once_with(
            self.paths,
            port=8897,
            app_version="1.0.0",
            python_executable="/opt/homebrew/bin/python3",
        )
        state_document.assert_called_once_with(
            current_version="1.0.0",
            previous_version=None,
            port=8897,
            installed_clients=["codex", "claude-code"],
            python_executable="/opt/homebrew/bin/python3",
        )
        write_state.assert_called_once_with(self.paths, {"state": True})
        write.assert_called_once()
        validate_control.assert_called_once()
        self.assertIs(validate_control.call_args.args[0], self.paths)
        self.assertEqual(repair.call_args_list, [
            mock.call(self.home / ".codex/hooks.json", "codex", launcher),
            mock.call(self.home / ".claude/settings.json", "claude-code", launcher),
        ])
        self.assertEqual(preview.call_count, 2)
        run.assert_not_called()
        self.assertEqual(output["status"], "installed")

    def test_install_error_is_nonzero_and_not_hook_degraded(self) -> None:
        with mock.patch("vibe_memory_cli.vibe_memory_install.validate_runtime_source", side_effect=ValueError("unsafe source")):
            code, output, stderr = self.invoke(["install", "--source-root", "/bad"])
        self.assertEqual(code, 1)
        self.assertEqual(output, {
            "error": "installation preflight failed",
            "phase": "preflight",
            "status": "failed",
        })
        self.assertEqual(stderr, "")
        self.assertNotIn("degraded", stderr)

    def test_install_preflight_failure_does_not_activate_or_write(self) -> None:
        release = self.paths.install_root / "releases/0.9.0"
        release.mkdir(parents=True)
        current = self.paths.install_root / "current"
        current.symlink_to("releases/0.9.0")
        codex = self.home / ".codex/hooks.json"
        codex.parent.mkdir(parents=True)
        codex.write_text("{malformed", encoding="utf-8")
        plist = self.home / "Library/LaunchAgents/com.noema.vibe-memory.plist"
        plist.parent.mkdir(parents=True)
        plist.write_text("sentinel plist\n", encoding="utf-8")

        with mock.patch("vibe_memory_cli.vibe_memory_install.install_runtime") as install:
            code, output, stderr = self.invoke([
                "install", "--source-root", str(ROOT)
            ])

        self.assertEqual(code, 1, stderr)
        self.assertEqual(output["status"], "failed")
        self.assertEqual(output["phase"], "preflight")
        install.assert_not_called()
        self.assertEqual(os.readlink(current), "releases/0.9.0")
        self.assertEqual(codex.read_text(encoding="utf-8"), "{malformed")
        self.assertEqual(plist.read_text(encoding="utf-8"), "sentinel plist\n")

    def test_install_commit_failure_rolls_back_hooks_plist_and_current(self) -> None:
        codex = self.home / ".codex/hooks.json"
        claude = self.home / ".claude/settings.json"
        plist = self.home / "Library/LaunchAgents/com.noema.vibe-memory.plist"
        codex.parent.mkdir(parents=True)
        claude.parent.mkdir(parents=True)
        plist.parent.mkdir(parents=True)
        codex.write_text('{"custom":"codex"}\n', encoding="utf-8")
        claude.write_text('{"custom":"claude"}\n', encoding="utf-8")
        plist.write_text("old plist\n", encoding="utf-8")
        before = {path: path.read_bytes() for path in (codex, claude, plist)}
        real_repair = vibe_memory_cli.vibe_memory_hooks.repair
        calls = []

        def fail_second(*arguments: object, **keywords: object) -> object:
            calls.append(arguments)
            if len(calls) == 2:
                raise OSError("injected repair failure")
            return real_repair(*arguments, **keywords)

        with mock.patch(
            "vibe_memory_cli.vibe_memory_hooks.repair", side_effect=fail_second
        ):
            code, output, stderr = self.invoke([
                "install", "--source-root", str(ROOT), "--with-claude-hooks"
            ])

        self.assertEqual(code, 1, stderr)
        self.assertEqual(output["status"], "failed")
        self.assertEqual(output["phase"], "commit")
        self.assertTrue(output["rollback"]["ok"])
        self.assertTrue(output["rollback"]["data_retained"])
        self.assertFalse(os.path.lexists(self.paths.install_root / "current"))
        self.assertFalse((self.paths.install_root / "config.json").exists())
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertFalse(list(codex.parent.glob("hooks.json.bak.*")))
        self.assertFalse(list(claude.parent.glob("settings.json.bak.*")))

    def test_install_rolls_back_launch_agent_when_replace_then_raise_hides_result(self) -> None:
        plist = self.home / "Library/LaunchAgents/com.noema.vibe-memory.plist"
        plist.parent.mkdir(parents=True)
        plist.write_text("old plist\n", encoding="utf-8")
        real_install = vibe_memory_cli.vibe_memory_install.install_launch_agent

        def replace_then_raise(*arguments: object, **keywords: object) -> object:
            real_install(*arguments, **keywords)
            raise OSError("injected post-replace launch agent failure")

        with mock.patch(
            "vibe_memory_cli.vibe_memory_install.install_launch_agent",
            side_effect=replace_then_raise,
        ):
            code, output, stderr = self.invoke([
                "install", "--source-root", str(ROOT)
            ])

        self.assertEqual(code, 1, stderr)
        self.assertEqual(output["phase"], "commit")
        self.assertTrue(output["rollback"]["ok"])
        self.assertEqual(plist.read_text(encoding="utf-8"), "old plist\n")
        self.assertFalse(os.path.lexists(self.paths.install_root / "current"))
        self.assertFalse((self.paths.install_root / "config.json").exists())

    def test_install_rolls_back_hook_and_backup_when_replace_then_raise_hides_result(self) -> None:
        codex = self.home / ".codex/hooks.json"
        codex.parent.mkdir(parents=True)
        codex.write_text('{"custom":"codex"}\n', encoding="utf-8")
        real_repair = vibe_memory_cli.vibe_memory_hooks.repair

        def replace_then_raise(*arguments: object, **keywords: object) -> object:
            real_repair(*arguments, **keywords)
            raise OSError("injected post-replace hook failure")

        with mock.patch(
            "vibe_memory_cli.vibe_memory_hooks.repair",
            side_effect=replace_then_raise,
        ):
            code, output, stderr = self.invoke([
                "install", "--source-root", str(ROOT)
            ])

        self.assertEqual(code, 1, stderr)
        self.assertEqual(output["phase"], "commit")
        self.assertTrue(output["rollback"]["ok"])
        self.assertEqual(codex.read_text(encoding="utf-8"), '{"custom":"codex"}\n')
        self.assertFalse(list(codex.parent.glob("hooks.json.bak.*")))
        self.assertFalse(os.path.lexists(self.paths.install_root / "current"))
        self.assertFalse((self.paths.install_root / "config.json").exists())

    def test_real_install_keeps_codex_and_claude_hooks_on_current_symlink(self) -> None:
        code, output, stderr = self.invoke([
            "install", "--source-root", str(ROOT), "--with-claude-hooks", "--port", "9123"
        ])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output["status"], "installed")
        install_state = json.loads(
            (self.paths.install_root / "state/install.json").read_text(encoding="utf-8")
        )
        self.assertEqual(install_state["current_version"], "1.0.0")
        self.assertIsNone(install_state["previous_version"])
        self.assertEqual(install_state["port"], 9123)
        self.assertEqual(install_state["installed_clients"], ["codex", "claude-code"])
        persisted_config = json.loads(
            (self.paths.install_root / "config.json").read_text(encoding="utf-8")
        )
        self.assertEqual(install_state["python_executable"], persisted_config["python_executable"])
        self.assertEqual(install_state["python_version"], persisted_config["python_version"])
        runtime_config = self.paths.install_root / "config.json"
        persisted = json.loads(runtime_config.read_text(encoding="utf-8"))
        self.assertEqual(persisted["app_version"], "1.0.0")
        self.assertEqual(persisted["port"], 9123)
        self.assertEqual(persisted["service"], "vibe-memory")
        self.assertTrue(pathlib.Path(persisted["python_executable"]).is_absolute())
        self.assertRegex(persisted["python_version"], r"^3\.(?:1[0-9]|[2-9][0-9])$")
        self.assertEqual(stat.S_IMODE(runtime_config.stat().st_mode), 0o600)
        stable = str(self.paths.launcher)
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
            self.assertTrue(all("/usr/bin/python3" not in command for command in commands))
            self.assertTrue(all("releases/1.0.0" not in command for command in commands))
        before = {
            path: path.read_bytes()
            for path in (
                self.home / ".codex/hooks.json",
                self.home / ".claude/settings.json",
            )
        }
        code, repeated, stderr = self.invoke([
            "install", "--source-root", str(ROOT), "--with-claude-hooks", "--port", "9123"
        ])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(repeated["hooks"]["codex"]["status"], "current")
        self.assertEqual(repeated["hooks"]["claude"]["status"], "current")
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_reinstall_preserves_existing_rollback_state_and_clients(self) -> None:
        code, _output, stderr = self.invoke([
            "install", "--source-root", str(ROOT), "--with-claude-hooks",
        ])
        self.assertEqual(code, 0, stderr)
        state_path = self.paths.install_root / "state/install.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["previous_version"] = "0.9.0"
        state_path.write_text(json.dumps(state), encoding="utf-8")

        code, _output, stderr = self.invoke(["install", "--source-root", str(ROOT)])

        self.assertEqual(code, 0, stderr)
        repeated = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(repeated["previous_version"], "0.9.0")
        self.assertEqual(repeated["installed_clients"], ["codex", "claude-code"])

    def test_installed_launcher_survives_source_tree_removal(self) -> None:
        source = pathlib.Path(self.temporary.name) / "portable-source"
        shutil.copytree(
            ROOT,
            source,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )
        environment = os.environ.copy()
        environment.update({
            "HOME": str(self.home),
            "VIBE_MEMORY_PYTHON": sys.executable,
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        installed = subprocess.run(
            [
                sys.executable,
                str(source / "scripts/vibe_memory_cli.py"),
                "install",
                "--source-root",
                str(source),
            ],
            cwd=source,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        shutil.rmtree(source)

        status = subprocess.run(
            [str(self.paths.launcher), "status"],
            cwd=self.temporary.name,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        doctor = subprocess.run(
            [str(self.paths.launcher), "doctor", "--json"],
            cwd=self.temporary.name,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["runtime"]["status"], "current")
        self.assertEqual(doctor.returncode, 1, doctor.stderr)
        self.assertEqual(json.loads(doctor.stdout)["runtime"]["status"], "current")

    def test_open_only_invokes_usr_bin_open_after_loopback_health(self) -> None:
        vibe_memory_cli.vibe_memory_install.install_runtime_config(
            self.paths, port=9123, app_version="1.0.0"
        )
        healthy = {
            "ok": True,
            "status": "healthy",
            "url": "http://127.0.0.1:9123/",
        }
        with mock.patch("vibe_memory_cli.health_status", return_value=healthy) as health, mock.patch(
            "vibe_memory_cli.subprocess.run", return_value=subprocess.CompletedProcess([], 0)
        ) as run:
            code, output, _ = self.invoke(["open"])
        self.assertEqual(code, 0)
        health.assert_called_once_with(self.paths)
        run.assert_called_once_with(["/usr/bin/open", "http://127.0.0.1:9123/"], check=False)
        self.assertEqual(output["status"], "opened")

        with mock.patch("vibe_memory_cli.health_status", return_value={
            "ok": False, "status": "wrong_service", "url": "http://127.0.0.1:9123/"
        }), mock.patch(
            "vibe_memory_cli.subprocess.run"
        ) as run:
            code, output, stderr = self.invoke(["open"])
        self.assertEqual(code, 1)
        self.assertIsNone(output)
        self.assertIn("health", stderr)
        run.assert_not_called()

    def test_health_rejects_redirect_and_wrong_service_identity(self) -> None:
        vibe_memory_cli.vibe_memory_install.install_runtime_config(
            self.paths, port=9123, app_version="1.0.0"
        )
        redirect = urllib.error.HTTPError(
            "http://127.0.0.1:9123/health", 302, "Found", {}, io.BytesIO()
        )
        opener = mock.Mock()
        opener.open.side_effect = redirect
        with mock.patch("vibe_memory_cli.urllib.request.build_opener", return_value=opener):
            redirected = vibe_memory_cli.health_status(self.paths)
        redirect.close()
        self.assertFalse(redirected["ok"])
        self.assertEqual(redirected["status"], "unreachable")

        response = mock.MagicMock()
        response.status = 200
        response.geturl.return_value = "http://127.0.0.1:9123/health"
        response.read.return_value = json.dumps({
            "ok": True, "service": "something-else", "app_version": "1.0.0"
        }).encode("utf-8")
        response.__enter__.return_value = response
        opener.open.side_effect = None
        opener.open.return_value = response
        with mock.patch("vibe_memory_cli.urllib.request.build_opener", return_value=opener):
            wrong = vibe_memory_cli.health_status(self.paths)
        self.assertFalse(wrong["ok"])
        self.assertEqual(wrong["status"], "wrong_service")

    def test_health_uses_real_persisted_custom_port(self) -> None:
        vibe_memory_cli.vibe_memory_install.install_runtime_config(
            self.paths, port=9123, app_version="1.0.0"
        )
        response = mock.MagicMock()
        response.status = 200
        response.geturl.return_value = "http://127.0.0.1:9123/health"
        response.read.return_value = json.dumps({
            "ok": True,
            "service": "vibe-memory",
            "app_version": "1.0.0",
            "data_schema_version": 1,
        }).encode("utf-8")
        response.__enter__.return_value = response
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch("vibe_memory_cli.urllib.request.build_opener", return_value=opener):
            health = vibe_memory_cli.health_status(self.paths)
        self.assertTrue(health["ok"])
        self.assertEqual(health["url"], "http://127.0.0.1:9123/")
        opener.open.assert_called_once_with("http://127.0.0.1:9123/health", timeout=0.6)

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

    def test_migrate_preview_delegates_to_legacy_hook_migration(self) -> None:
        project = pathlib.Path(self.temporary.name) / "project"
        project.mkdir()
        expected = [{"root": str(project.resolve()), "managed_entries": 5, "custom_entries": 1}]
        with mock.patch(
            "vibe_memory_cli.vibe_memory_migration.preview_legacy_hooks",
            return_value=expected,
        ) as preview:
            code, output, _ = self.invoke(["migrate", "preview", str(project)])

        self.assertEqual(code, 0)
        self.assertEqual(output, expected)
        preview.assert_called_once_with([project.resolve()])

    def test_migrate_apply_requires_explicit_approval_and_then_delegates(self) -> None:
        project = pathlib.Path(self.temporary.name) / "project"
        project.mkdir()

        code, output, stderr = self.invoke(["migrate", "apply", str(project)])

        self.assertEqual(code, 1)
        self.assertIsNone(output)
        self.assertIn("--approved", stderr)

        expected = [{"root": str(project.resolve()), "backups": ["backup"]}]
        with mock.patch(
            "vibe_memory_cli.vibe_memory_migration.apply_legacy_hooks",
            return_value=expected,
        ) as apply:
            code, output, _ = self.invoke([
                "migrate", "apply", "--approved", str(project)
            ])

        self.assertEqual(code, 0)
        self.assertEqual(output, expected)
        apply.assert_called_once_with([project.resolve()])

    def test_runtime_lifecycle_commands_delegate_and_gate_data_deletion(self) -> None:
        with mock.patch(
            "vibe_memory_cli.vibe_memory_install.update",
            return_value={"status": "updated"},
        ) as update:
            code, output, _ = self.invoke(["update", "--source-root", "/next"])
        self.assertEqual(code, 0)
        self.assertEqual(output["status"], "updated")
        update.assert_called_once_with(pathlib.Path("/next"), self.paths)

        with mock.patch(
            "vibe_memory_cli.vibe_memory_install.rollback",
            return_value={"status": "rolled_back"},
        ) as rollback:
            code, output, _ = self.invoke(["rollback"])
        self.assertEqual(code, 0)
        self.assertEqual(output["status"], "rolled_back")
        rollback.assert_called_once_with(self.paths)

        with mock.patch(
            "vibe_memory_cli.vibe_memory_install.repair",
            return_value={"status": "repaired"},
        ) as repair:
            code, output, _ = self.invoke(["repair"])
        self.assertEqual(code, 0)
        self.assertEqual(output["status"], "repaired")
        repair.assert_called_once_with(self.paths)

        with mock.patch("vibe_memory_cli.vibe_memory_install.uninstall") as uninstall:
            code, output, stderr = self.invoke(["uninstall", "--remove-data"])
        self.assertEqual(code, 1)
        self.assertIsNone(output)
        self.assertIn("--approved-data-deletion", stderr)
        uninstall.assert_not_called()

        with mock.patch(
            "vibe_memory_cli.vibe_memory_install.uninstall",
            return_value={"status": "uninstalled"},
        ) as uninstall:
            code, output, _ = self.invoke(["uninstall"])
        self.assertEqual(code, 0)
        self.assertEqual(output["status"], "uninstalled")
        uninstall.assert_called_once_with(
            self.paths,
            remove_data=False,
            approved_data_deletion=False,
            data_paths=[],
        )


class InstallScriptContractTest(unittest.TestCase):
    def test_install_script_discovers_a_supported_python_without_usr_bin_hardcoding(self) -> None:
        script = ROOT / "install.sh"
        text = script.read_text(encoding="utf-8")
        self.assertIn('VIBE_MEMORY_PYTHON', text)
        self.assertIn('command -v python3', text)
        self.assertIn('sys.version_info >= (3, 10)', text)
        self.assertIn('"$PYTHON" "${SOURCE_ROOT}/scripts/vibe_memory_cli.py"', text)
        self.assertIn('"$HOME/.local/bin/vibe-memory" doctor --json', text)
        self.assertNotIn('exec /usr/bin/python3', text)
        self.assertTrue(os.access(script, os.X_OK))
        completed = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)


class ProjectRegistryTransactionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.temporary.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.registry = self.home / ".codex/memory_review/projects.json"
        self.environment = os.environ.copy()
        self.environment.update({
            "HOME": str(self.home),
            "MEMORY_REVIEW_PROJECT_REGISTRY": str(self.registry),
            "PYTHONDONTWRITEBYTECODE": "1",
        })

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *arguments],
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )

    def test_project_list_is_absolutely_read_only(self) -> None:
        project = self.base / "notes"
        project.mkdir()
        registered = self.command("project", "register", str(project))
        self.assertEqual(registered.returncode, 0, registered.stderr)
        before = self.registry.read_bytes()
        before_stat = self.registry.stat()

        listed = self.command("project", "list")

        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(self.registry.stat().st_mtime_ns, before_stat.st_mtime_ns)

    def test_registry_symlink_is_rejected_without_overwriting_sentinel(self) -> None:
        project = self.base / "notes"
        project.mkdir()
        self.registry.parent.mkdir(parents=True)
        sentinel = self.base / "sentinel.json"
        sentinel.write_text('{"sentinel":true}\n', encoding="utf-8")
        self.registry.symlink_to(sentinel)

        completed = self.command("project", "register", str(project))

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), '{"sentinel":true}\n')
        self.assertTrue(self.registry.is_symlink())

    def test_twenty_four_concurrent_registers_do_not_lose_projects(self) -> None:
        projects = [self.base / f"notes-{index:02d}" for index in range(24)]
        for project in projects:
            project.mkdir()
        processes = [
            subprocess.Popen(
                [sys.executable, str(CLI), "project", "register", str(project)],
                env=self.environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for project in projects
        ]
        results = [process.communicate(timeout=30) + (process.returncode,) for process in processes]
        self.assertTrue(all(code == 0 for _stdout, _stderr, code in results), results)

        listed = self.command("project", "list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        roots = {item["root"] for item in json.loads(listed.stdout)["projects"]}
        self.assertEqual(roots, {str(project.resolve()) for project in projects})


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
