from __future__ import annotations

import json
import io
import pathlib
import subprocess
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

    def test_installed_e2e_cleanup_uninstalls_before_temporary_home_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            home = pathlib.Path(value) / "home"
            paths = vibe_memory_paths.for_home(home)
            for path in (paths.launcher, paths.launch_agent):
                path.parent.mkdir(parents=True, exist_ok=True)
            paths.launcher.write_text(
                vibe_memory_install.render_launcher(
                    paths, python_executable=sys.executable
                ),
                encoding="utf-8",
            )
            paths.launch_agent.write_text("managed\n", encoding="utf-8")
            current = paths.install_root / "current"
            current.parent.mkdir(parents=True, exist_ok=True)
            current.symlink_to("releases/1.0.0")

            def uninstall_while_home_exists(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
                self.assertTrue(home.is_dir())
                self.assertTrue(paths.launcher.is_file())
                paths.launcher.unlink()
                paths.launch_agent.unlink()
                current.unlink()
                return subprocess.CompletedProcess([], 0, '{"status":"uninstalled"}\n', "")

            absent = subprocess.CompletedProcess([], 113, "", "Could not find service")
            booted = subprocess.CompletedProcess([], 0, "", "")
            with mock.patch.object(
                verify_release, "_installed_e2e_process", side_effect=uninstall_while_home_exists
            ) as uninstall, mock.patch.object(
                verify_release, "_installed_e2e_service_state",
                side_effect=[("owned", None), ("absent", None)],
            ), mock.patch.object(verify_release, "_launchctl") as launchctl:
                errors = verify_release._cleanup_installed_release_e2e(
                    ROOT, home, paths, owned_service=True
                )

        self.assertEqual(errors, [])
        uninstall.assert_called_once_with(
            [paths.launcher, "uninstall"], home=home, cwd=ROOT
        )
        launchctl.assert_not_called()

    def test_installed_e2e_cleanup_reports_uninstall_and_residual_service_failures(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            home = pathlib.Path(value) / "home"
            paths = vibe_memory_paths.for_home(home)
            paths.launcher.parent.mkdir(parents=True, exist_ok=True)
            paths.launcher.write_text("managed\n", encoding="utf-8")
            failed_uninstall = subprocess.CompletedProcess([], 1, "", "uninstall failed")
            residual = subprocess.CompletedProcess([], 0, "loaded", "")
            with mock.patch.object(
                verify_release, "_installed_e2e_process", return_value=failed_uninstall
            ), mock.patch.object(
                verify_release, "_installed_e2e_service_state",
                side_effect=[("owned", None), ("owned", None)] * 20,
            ), mock.patch.object(
                verify_release.subprocess, "run", return_value=residual
            ), mock.patch.object(verify_release.time, "sleep"):
                errors = verify_release._cleanup_installed_release_e2e(
                    ROOT, home, paths, owned_service=True
                )

        self.assertTrue(any("uninstall" in error for error in errors))
        self.assertTrue(any("still loaded" in error for error in errors))

    def test_installed_e2e_cleanup_accepts_label_disappearing_after_poll(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            home = pathlib.Path(value) / "home"
            paths = vibe_memory_paths.for_home(home)
            absent = subprocess.CompletedProcess([], 1, "", "not found")
            loaded = subprocess.CompletedProcess([], 0, "loaded", "")
            with mock.patch.object(
                verify_release, "_installed_e2e_service_state",
                side_effect=[("owned", None), ("absent", None)],
            ), mock.patch.object(
                verify_release.subprocess, "run", side_effect=[loaded]
            ), mock.patch.object(verify_release.time, "sleep"):
                errors = verify_release._cleanup_installed_release_e2e(
                    ROOT, home, paths, owned_service=True
                )

        self.assertEqual(errors, [])

    def test_cleanup_without_owned_install_never_boots_out(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            home = pathlib.Path(value) / "home"
            paths = vibe_memory_paths.for_home(home)
            with mock.patch.object(verify_release.subprocess, "run") as run:
                errors = verify_release._cleanup_installed_release_e2e(
                    ROOT, home, paths, owned_service=False
                )

        self.assertEqual(errors, [])
        run.assert_not_called()

    def test_foreign_service_is_reported_without_bootout(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            home = pathlib.Path(value) / "home"
            paths = vibe_memory_paths.for_home(home)
            with mock.patch.object(
                verify_release, "_installed_e2e_service_state",
                return_value=("foreign", "service identity mismatch"),
            ), mock.patch.object(verify_release.subprocess, "run") as run:
                errors = verify_release._cleanup_installed_release_e2e(
                    ROOT, home, paths, owned_service=True
                )

        self.assertIn("foreign service remains", "; ".join(errors))
        run.assert_not_called()

    def test_launchctl_absence_is_narrow_and_errors_are_not_absence(self) -> None:
        absent = subprocess.CompletedProcess(
            [], 113, "", 'Could not find service "com.noema.vibe-memory" in domain for user gui: 501'
        )
        denied = subprocess.CompletedProcess([], 1, "", "Operation not permitted")
        self.assertTrue(verify_release._launchctl_service_absent(absent))
        self.assertFalse(verify_release._launchctl_service_absent(denied))

    def test_http_probe_rejects_redirect_oversize_and_wrong_content_type(self) -> None:
        class Response(io.BytesIO):
            status = 200
            headers = mock.Mock()
            def __enter__(self):
                return self
            def __exit__(self, *_args: object) -> None:
                return None
            def geturl(self) -> str:
                return "http://127.0.0.1:19097/redirected"

        redirected = Response(b"{}")
        redirected.headers.get_content_type.return_value = "application/json"
        opener = mock.Mock()
        opener.open.return_value = redirected
        with mock.patch.object(verify_release.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(RuntimeError, "redirect"):
                verify_release._installed_e2e_http(19097, "/health")

        oversized = Response(b"x" * (64 * 1024 + 1))
        oversized.geturl = lambda: "http://127.0.0.1:19097/api/queue"
        oversized.headers.get_content_type.return_value = "application/json"
        opener.open.return_value = oversized
        with mock.patch.object(verify_release.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(RuntimeError, "too large"):
                verify_release._installed_e2e_http(19097, "/api/queue")

        wrong_type = Response(b"{}")
        wrong_type.geturl = lambda: "http://127.0.0.1:19097/health"
        wrong_type.headers.get_content_type.return_value = "text/plain"
        opener.open.return_value = wrong_type
        with mock.patch.object(verify_release.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(RuntimeError, "Content-Type"):
                verify_release._installed_e2e_http(19097, "/health")

    def test_http_probe_rejects_a_stream_that_finishes_after_the_deadline(self) -> None:
        clock = [0.0]

        class SlowResponse(io.BytesIO):
            status = 200
            headers = mock.Mock()

            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def geturl(self) -> str:
                return "http://127.0.0.1:19097/health"

            def read(self, size: int = -1) -> bytes:
                clock[0] = 6.0
                return super().read(size)

        response = SlowResponse(b"")
        response.headers.get_content_type.return_value = "application/json"
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(
            verify_release.urllib.request, "build_opener", return_value=opener
        ), mock.patch.object(
            verify_release.time, "monotonic", side_effect=lambda: clock[0]
        ):
            with self.assertRaisesRegex(RuntimeError, "deadline"):
                verify_release._installed_e2e_http(19097, "/health")

    def test_rollback_check_removes_all_temporary_install_sources(self) -> None:
        with tempfile.TemporaryDirectory() as value, mock.patch.object(
            tempfile, "tempdir", value
        ):
            result = verify_release._rollback_check(ROOT)
            leftovers = sorted(pathlib.Path(value).glob("verify-release-source*"))

        self.assertEqual(result, "ok")
        self.assertEqual(leftovers, [])

    def test_installed_e2e_result_combines_primary_and_cleanup_failures(self) -> None:
        result = verify_release._installed_e2e_result(
            "health probe failed",
            ["installed uninstall exited 1", "test LaunchAgent is still loaded"],
        )

        self.assertEqual(
            result,
            "failed: health probe failed | cleanup: installed uninstall exited 1; "
            "test LaunchAgent is still loaded",
        )
