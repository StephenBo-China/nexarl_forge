from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import memory_review_server
import vibe_memory_install
import vibe_memory_migration as migration
import vibe_memory_paths
from scripts import verify_release
from tests.test_vibe_memory_migration import build_complete_legacy_fixture


def run_installed_doctor(
    runtime: pathlib.Path,
    fixture_home: pathlib.Path,
) -> dict[str, str]:
    paths = vibe_memory_paths.for_home(fixture_home)
    registry = json.loads(paths.project_registry.read_text(encoding="utf-8"))
    result = migration.validate_control_plane(paths, registry)
    result["runtime"] = (
        "ok"
        if (runtime / "scripts" / "memory_review_server.py").is_file()
        else "error"
    )
    return result


class InstalledControlPlaneTest(unittest.TestCase):
    def install_complete_fixture(self, root: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
        fixture = build_complete_legacy_fixture(root)
        home = fixture.paths.personal_memory.parents[1]
        vibe_memory_install.install_runtime(ROOT, fixture.paths)
        vibe_memory_install.install_runtime_config(
            fixture.paths,
            port=19097,
            app_version="1.0.0",
        )
        return fixture.paths.install_root / "current", home

    def test_installed_runtime_reads_all_existing_control_plane_data(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            runtime, fixture_home = self.install_complete_fixture(pathlib.Path(value))

            result = run_installed_doctor(runtime, fixture_home)

        self.assertEqual(result["runtime"], "ok")
        self.assertEqual(result["memory_review"], "ok")
        self.assertEqual(result["projects"], "ok")
        self.assertEqual(result["design_preferences"], "ok")
        self.assertEqual(result["ui_design_approvals"], "ok")
        self.assertEqual(result["ui_skills"], "ok")
        self.assertEqual(result["loop"], "ok")

    def test_server_binds_configured_loopback_port(self) -> None:
        self.assertEqual(
            memory_review_server.server_address(
                {
                    "MEMORY_REVIEW_HOST": "127.0.0.1",
                    "MEMORY_REVIEW_PORT": "19097",
                }
            ),
            ("127.0.0.1", 19097),
        )
        with self.assertRaises(ValueError):
            memory_review_server.server_address(
                {
                    "MEMORY_REVIEW_HOST": "0.0.0.0",
                    "MEMORY_REVIEW_PORT": "19097",
                }
            )

    def test_installed_release_e2e_reports_non_darwin_as_distinct_skip(self) -> None:
        with mock.patch.object(verify_release.sys, "platform", "linux"):
            result = verify_release._run_installed_release_e2e(ROOT)

        self.assertEqual(result, "skipped: macOS installed-runtime E2E")

    def test_release_gate_wires_real_installed_release_harness(self) -> None:
        patches = {
            "_command_status": mock.Mock(return_value="ok"),
            "_compile_python": mock.Mock(return_value="ok"),
            "_permissions_check": mock.Mock(return_value="ok"),
            "_hook_check": mock.Mock(return_value="ok"),
            "_control_plane_check": mock.Mock(return_value="ok"),
            "_rollback_check": mock.Mock(return_value="ok"),
            "_uninstall_check": mock.Mock(return_value="ok"),
            "_public_tree_status": mock.Mock(return_value="ok"),
            "_run_installed_release_e2e": mock.Mock(return_value="ok"),
        }
        with mock.patch.multiple(verify_release, **patches):
            result = verify_release.evaluate_checks(ROOT)

        self.assertEqual(result["install_e2e"], "ok")
        patches["_run_installed_release_e2e"].assert_called_once_with(ROOT.resolve())
