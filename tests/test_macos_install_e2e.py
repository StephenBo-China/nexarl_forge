from __future__ import annotations

import hashlib
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import memory_review_server
import vibe_memory_install
import vibe_memory_hooks
import vibe_memory_paths
from tests.test_installed_control_plane import run_installed_doctor
from tests.test_vibe_memory_migration import build_complete_legacy_fixture


def create_legacy_install(base: pathlib.Path):
    return build_complete_legacy_fixture(base)


def _free_port() -> int:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])
    except PermissionError as error:
        raise unittest.SkipTest("sandbox does not allow disposable loopback listeners") from error


def _run_install(home: pathlib.Path, *, with_claude_hooks: bool, port: int) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    command = [
        str(ROOT / "install.sh"),
        "--port",
        str(port),
    ]
    if with_claude_hooks:
        command.append("--with-claude-hooks")
    return subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _digest_tree(root: pathlib.Path, *, skip: set[str]) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if any(relative == prefix or relative.startswith(prefix + "/") for prefix in skip):
            continue
        if not path.is_file() or path.is_symlink():
            continue
        digest.update(relative.encode("utf-8"))
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _business_data_digest(home: pathlib.Path) -> str:
    return _digest_tree(
        home,
        skip={"Library", ".local", ".codex/hooks.json", ".claude/settings.json"},
    )


@unittest.skipUnless(sys.platform == "darwin", "macOS installer contract")
class MacOSInstallE2ETest(unittest.TestCase):
    service = f"gui/{os.getuid()}/com.noema.vibe-memory"

    def setUp(self) -> None:
        existing = subprocess.run(
            ["/bin/launchctl", "print", self.service],
            capture_output=True, text=True, check=False,
        )
        if existing.returncode == 0:
            self.skipTest("fixed product LaunchAgent label is already loaded; refusing to disturb it")

    def tearDown(self) -> None:
        subprocess.run(
            ["/bin/launchctl", "bootout", self.service],
            capture_output=True, text=True, check=False,
        )
        for _attempt in range(20):
            printed = subprocess.run(
                ["/bin/launchctl", "print", self.service],
                capture_output=True, text=True, check=False,
            )
            if printed.returncode != 0:
                break
            time.sleep(0.05)

    def test_clean_install_codex_only(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            home = pathlib.Path(value) / "home"
            home.mkdir()
            port = _free_port()

            result = _run_install(home, with_claude_hooks=False, port=port)

            self.assertEqual(result.returncode, 0, result.stderr)
            paths = vibe_memory_paths.for_home(home)
            runtime = paths.install_root / "current"
            self.assertTrue(runtime.is_symlink())
            self.assertIn("--agent codex", (home / ".codex/hooks.json").read_text(encoding="utf-8"))
            self.assertFalse((home / ".claude/settings.json").exists())
            self.assertEqual(
                vibe_memory_hooks.status(home / ".codex/hooks.json", "codex", paths.launcher)["status"],
                "current",
            )
            self.assertEqual(
                memory_review_server.server_address(
                    {
                        "MEMORY_REVIEW_HOST": "127.0.0.1",
                        "MEMORY_REVIEW_PORT": str(port),
                    }
                ),
                ("127.0.0.1", port),
            )
            doctor = subprocess.run(
                [str(paths.launcher), "doctor", "--json"],
                env={**os.environ, "HOME": str(home)}, capture_output=True,
                text=True, check=False,
            )
            self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
            self.assertTrue(json.loads(doctor.stdout)["service"]["ok"])
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                health = json.loads(response.read().decode("utf-8"))
            release = json.loads((runtime / "release.json").read_text(encoding="utf-8"))
            self.assertIs(health["ok"], True)
            self.assertEqual(health["service"], "vibe-memory")
            self.assertEqual(health["app_version"], release["app_version"])

    def test_legacy_install_preserves_every_control_plane(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            base = pathlib.Path(value)
            fixture = create_legacy_install(base)
            home = fixture.paths.personal_memory.parents[1]
            before = _business_data_digest(home)
            port = _free_port()

            result = _run_install(home, with_claude_hooks=True, port=port)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(_business_data_digest(home), before)
            runtime = fixture.paths.install_root / "current"
            doctor = run_installed_doctor(runtime, home)
            self.assertEqual(doctor["runtime"], "ok")
            self.assertEqual(doctor["memory_review"], "ok")
            self.assertEqual(doctor["projects"], "ok")
            self.assertEqual(doctor["design_preferences"], "ok")
            self.assertEqual(doctor["ui_design_approvals"], "ok")
            self.assertEqual(doctor["ui_skills"], "ok")
            self.assertEqual(doctor["loop"], "ok")


if __name__ == "__main__":
    unittest.main()
