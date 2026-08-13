from __future__ import annotations

import json
import concurrent.futures
import os
import pathlib
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import vibe_memory_install
import vibe_memory_hooks
import vibe_memory_paths


MANIFEST = {
    "app_version": "1.0.0",
    "data_schema_version": 1,
    "hook_protocol_version": 1,
    "minimum_python": "3.10",
    "platform": "macOS",
}


class RuntimeInstallTest(unittest.TestCase):
    def make_source(self, root: pathlib.Path, manifest: dict[str, object] | None = None) -> pathlib.Path:
        source = root / "source"
        (source / "scripts").mkdir(parents=True)
        (source / "templates/macos").mkdir(parents=True)
        (source / "docs").mkdir()
        (source / "scripts/server.py").write_text("print('server')\n", encoding="utf-8")
        (source / "templates/macos/com.noema.vibe-memory.plist").write_text(
            (ROOT / "templates/macos/com.noema.vibe-memory.plist").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (source / "docs/guide.md").write_text("# Guide\n", encoding="utf-8")
        (source / "README.md").write_text("# Runtime\n", encoding="utf-8")
        (source / "release.json").write_text(json.dumps(manifest or MANIFEST), encoding="utf-8")
        (source / "LICENSE").write_text("test license\n", encoding="utf-8")
        return source

    def make_paths(self, root: pathlib.Path) -> vibe_memory_paths.RuntimePaths:
        return vibe_memory_paths.for_home(root / "home")

    def test_installs_required_release_content_and_atomically_points_current(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            paths = self.make_paths(root)

            result = vibe_memory_install.install_runtime(source, paths)

            release = paths.install_root / "releases/1.0.0"
            self.assertEqual(result, {"version": "1.0.0"})
            self.assertTrue((release / "scripts/server.py").is_file())
            self.assertTrue((release / "templates/macos/com.noema.vibe-memory.plist").is_file())
            self.assertTrue((release / "docs/guide.md").is_file())
            for name in ("README.md", "release.json", "LICENSE"):
                self.assertTrue((release / name).is_file())
            self.assertTrue((paths.install_root / "current").is_symlink())
            self.assertEqual((paths.install_root / "current").resolve(), release.resolve())

    def test_runtime_config_persists_validated_interpreter_and_launcher_uses_current(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))

            result = vibe_memory_install.install_runtime_config(
                paths,
                port=18997,
                app_version="1.0.0",
                python_executable=sys.executable,
            )
            runtime = vibe_memory_install.read_runtime_config(paths)
            launcher = vibe_memory_install.render_launcher(paths)

            self.assertEqual(result["python_executable"], os.path.abspath(sys.executable))
            self.assertEqual(runtime["python_executable"], os.path.abspath(sys.executable))
            self.assertRegex(runtime["python_version"], r"^3\.(?:1[0-9]|[2-9][0-9])$")
            self.assertIn(
                f'exec "{os.path.abspath(sys.executable)}" "$RUNTIME/scripts/vibe_memory_cli.py" "$@"',
                launcher,
            )
            self.assertIn('RUNTIME="$HOME/Library/Application Support/VibeMemory/current"', launcher)

    def test_install_launcher_writes_private_executable_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            vibe_memory_install.install_runtime_config(
                paths,
                port=18997,
                app_version="1.0.0",
                python_executable=sys.executable,
            )

            result = vibe_memory_install.install_launcher(paths)

            self.assertTrue(result["changed"])
            self.assertEqual(result["path"], str(paths.launcher))
            self.assertEqual(stat.S_IMODE(paths.launcher.stat().st_mode), 0o700)
            self.assertTrue(os.access(paths.launcher, os.X_OK))
            self.assertIn("Vibe Memory stable launcher", paths.launcher.read_text(encoding="utf-8"))

    def test_install_launcher_preserves_non_manager_owned_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            paths.launcher.parent.mkdir(parents=True)
            paths.launcher.write_text("#!/bin/sh\necho custom\n", encoding="utf-8")

            with self.assertRaises(vibe_memory_install.InstallError):
                vibe_memory_install.install_launcher(paths, python_executable=sys.executable)

            self.assertEqual(
                paths.launcher.read_text(encoding="utf-8"),
                "#!/bin/sh\necho custom\n",
            )

    def test_install_launcher_rejects_marker_without_manager_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            paths.launcher.parent.mkdir(parents=True)
            custom = (
                f"{vibe_memory_install.LAUNCHER_MARKER}\n"
                'RUNTIME="$HOME/Library/Application Support/VibeMemory/current"\n'
                "exec echo custom\n"
            )
            paths.launcher.write_text(custom, encoding="utf-8")

            with self.assertRaises(vibe_memory_install.InstallError):
                vibe_memory_install.install_launcher(paths, python_executable=sys.executable)

            self.assertEqual(paths.launcher.read_text(encoding="utf-8"), custom)

    def test_install_launcher_preserves_custom_replacement_after_initial_ownership_check(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            vibe_memory_install.install_launcher(paths, python_executable=sys.executable)
            paths.launcher.chmod(0o600)
            custom = "#!/bin/sh\necho replaced\n"
            real_fchmod = vibe_memory_install.os.fchmod
            injected: list[bool] = []

            def replace_launcher(descriptor: int, mode: int) -> None:
                real_fchmod(descriptor, mode)
                if not injected:
                    paths.launcher.write_text(custom, encoding="utf-8")
                    paths.launcher.chmod(0o700)
                    injected.append(True)

            with mock.patch.object(vibe_memory_install.os, "fchmod", side_effect=replace_launcher):
                with self.assertRaises(vibe_memory_install.InstallError):
                    vibe_memory_install.install_launcher(paths, python_executable=sys.executable)

            self.assertEqual(injected, [True])
            self.assertEqual(paths.launcher.read_text(encoding="utf-8"), custom)

    def test_install_launcher_rejects_custom_file_created_during_first_install(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            custom = "#!/bin/sh\necho concurrent\n"
            real_fchmod = vibe_memory_install.os.fchmod
            injected: list[bool] = []

            def create_launcher(descriptor: int, mode: int) -> None:
                real_fchmod(descriptor, mode)
                if not injected:
                    paths.launcher.write_text(custom, encoding="utf-8")
                    paths.launcher.chmod(0o700)
                    injected.append(True)

            with mock.patch.object(vibe_memory_install.os, "fchmod", side_effect=create_launcher):
                with self.assertRaises(vibe_memory_install.InstallError):
                    vibe_memory_install.install_launcher(paths, python_executable=sys.executable)

            self.assertEqual(injected, [True])
            self.assertEqual(paths.launcher.read_text(encoding="utf-8"), custom)

    def test_install_launcher_preserves_custom_replacement_after_final_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            vibe_memory_install.install_launcher(paths, python_executable=sys.executable)
            paths.launcher.chmod(0o600)
            custom = "#!/bin/sh\necho final-snapshot replacement\n"
            real_snapshot = vibe_memory_install._launcher_snapshot
            real_renameat = vibe_memory_install._darwin_renameat
            calls: list[int] = []

            def snapshot_then_replace(
                parent_fd: int,
                target: pathlib.Path,
            ) -> tuple[tuple[int, int], bytes, int] | None:
                result = real_snapshot(parent_fd, target)
                calls.append(1)
                if len(calls) == 2:
                    paths.launcher.write_text(custom, encoding="utf-8")
                    paths.launcher.chmod(0o700)
                return result

            def simulated_renameat(
                source_parent_fd: int,
                source_name: str,
                destination_parent_fd: int,
                destination_name: str,
                flags: int,
            ) -> None:
                self.assertEqual(source_parent_fd, destination_parent_fd)
                parent = paths.launcher.parent
                source = parent / source_name
                destination = parent / destination_name
                if flags == getattr(vibe_memory_install, "RENAME_SWAP", 0x2):
                    displaced = parent / f".{destination_name}.displaced"
                    source.rename(displaced)
                    destination.rename(source)
                    displaced.rename(destination)
                    return
                if flags == vibe_memory_install.RENAME_EXCL:
                    if destination.exists() or destination.is_symlink():
                        raise FileExistsError(destination_name)
                    source.rename(destination)
                    return
                real_renameat(
                    source_parent_fd,
                    source_name,
                    destination_parent_fd,
                    destination_name,
                    flags,
                )

            with mock.patch.object(vibe_memory_install.sys, "platform", "darwin"), mock.patch.object(
                vibe_memory_install,
                "_launcher_snapshot",
                side_effect=snapshot_then_replace,
            ), mock.patch.object(
                vibe_memory_install,
                "_darwin_renameat",
                side_effect=simulated_renameat,
            ):
                with self.assertRaises(vibe_memory_install.InstallError):
                    vibe_memory_install.install_launcher(paths, python_executable=sys.executable)

            self.assertGreaterEqual(len(calls), 3)
            self.assertEqual(paths.launcher.read_text(encoding="utf-8"), custom)

    def test_install_launcher_rejects_custom_file_created_after_absent_final_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            custom = "#!/bin/sh\necho absent-final-snapshot replacement\n"
            real_snapshot = vibe_memory_install._launcher_snapshot
            real_renameat = vibe_memory_install._darwin_renameat
            calls: list[int] = []

            def snapshot_then_create(
                parent_fd: int,
                target: pathlib.Path,
            ) -> tuple[tuple[int, int], bytes, int] | None:
                result = real_snapshot(parent_fd, target)
                calls.append(1)
                if len(calls) == 2:
                    paths.launcher.write_text(custom, encoding="utf-8")
                    paths.launcher.chmod(0o700)
                return result

            def exclusive_renameat(
                source_parent_fd: int,
                source_name: str,
                destination_parent_fd: int,
                destination_name: str,
                flags: int,
            ) -> None:
                self.assertEqual(source_parent_fd, destination_parent_fd)
                self.assertEqual(flags, vibe_memory_install.RENAME_EXCL)
                parent = paths.launcher.parent
                source = parent / source_name
                destination = parent / destination_name
                if destination.exists() or destination.is_symlink():
                    raise FileExistsError(destination_name)
                source.rename(destination)

            with mock.patch.object(vibe_memory_install.sys, "platform", "darwin"), mock.patch.object(
                vibe_memory_install,
                "_launcher_snapshot",
                side_effect=snapshot_then_create,
            ), mock.patch.object(
                vibe_memory_install,
                "_darwin_renameat",
                side_effect=exclusive_renameat,
            ):
                with self.assertRaises(vibe_memory_install.InstallError):
                    vibe_memory_install.install_launcher(paths, python_executable=sys.executable)

            self.assertEqual(calls, [1, 1])
            self.assertEqual(paths.launcher.read_text(encoding="utf-8"), custom)

    def test_install_launcher_rejects_replaced_temporary_file_for_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            vibe_memory_install.install_launcher(paths, python_executable=sys.executable)
            paths.launcher.chmod(0o600)
            original = paths.launcher.read_bytes()
            custom = b"#!/bin/sh\necho replaced temporary\n"
            injected: list[bool] = []

            def simulated_renameat(
                source_parent_fd: int,
                source_name: str,
                destination_parent_fd: int,
                destination_name: str,
                flags: int,
            ) -> None:
                self.assertEqual(source_parent_fd, destination_parent_fd)
                parent = paths.launcher.parent
                source = parent / source_name
                destination = parent / destination_name
                if flags == getattr(vibe_memory_install, "RENAME_SWAP", 0x2):
                    if not injected:
                        preserved = root / "preserved-temporary"
                        source.rename(preserved)
                        source.write_bytes(custom)
                        source.chmod(0o700)
                        injected.append(True)
                    displaced = parent / ".launcher-swap-displaced"
                    source.rename(displaced)
                    destination.rename(source)
                    displaced.rename(destination)
                    return
                if flags == vibe_memory_install.RENAME_EXCL:
                    if destination.exists() or destination.is_symlink():
                        raise FileExistsError(destination_name)
                    source.rename(destination)
                    return
                raise AssertionError(f"unexpected rename flags: {flags}")

            with mock.patch.object(vibe_memory_install.sys, "platform", "darwin"), mock.patch.object(
                vibe_memory_install,
                "_darwin_renameat",
                side_effect=simulated_renameat,
            ):
                with self.assertRaises(vibe_memory_install.InstallError):
                    vibe_memory_install.install_launcher(paths, python_executable=sys.executable)

            self.assertEqual(injected, [True])
            self.assertEqual(paths.launcher.read_bytes(), original)
            self.assertTrue(vibe_memory_install._is_manager_launcher(paths.launcher.read_bytes()))
            self.assertTrue(any(item.read_bytes() == custom for item in paths.launcher.parent.glob(".launcher-unknown-*")))

    def test_install_launcher_rejects_replaced_temporary_file_for_absent_target(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            custom = b"#!/bin/sh\necho replaced temporary absent\n"
            injected: list[bool] = []

            def simulated_renameat(
                source_parent_fd: int,
                source_name: str,
                destination_parent_fd: int,
                destination_name: str,
                flags: int,
            ) -> None:
                self.assertEqual(source_parent_fd, destination_parent_fd)
                parent = paths.launcher.parent
                source = parent / source_name
                destination = parent / destination_name
                if flags == vibe_memory_install.RENAME_EXCL:
                    if destination.name == paths.launcher.name and not injected:
                        preserved = root / "preserved-temporary"
                        source.rename(preserved)
                        source.write_bytes(custom)
                        source.chmod(0o700)
                        injected.append(True)
                    if destination.exists() or destination.is_symlink():
                        raise FileExistsError(destination_name)
                    source.rename(destination)
                    return
                raise AssertionError(f"unexpected rename flags: {flags}")

            with mock.patch.object(vibe_memory_install.sys, "platform", "darwin"), mock.patch.object(
                vibe_memory_install,
                "_darwin_renameat",
                side_effect=simulated_renameat,
            ):
                with self.assertRaises(vibe_memory_install.InstallError):
                    vibe_memory_install.install_launcher(paths, python_executable=sys.executable)

            self.assertEqual(injected, [True])
            self.assertFalse(paths.launcher.exists())
            self.assertTrue(any(item.read_bytes() == custom for item in paths.launcher.parent.glob(".launcher-unknown-*")))

    def test_install_launcher_removes_known_temporary_inode_after_absent_promotion_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            rewritten = b"#!/bin/sh\necho rewritten owned temporary\n"
            injected: list[bool] = []

            def simulated_renameat(
                source_parent_fd: int,
                source_name: str,
                destination_parent_fd: int,
                destination_name: str,
                flags: int,
            ) -> None:
                self.assertEqual(source_parent_fd, destination_parent_fd)
                self.assertEqual(flags, vibe_memory_install.RENAME_EXCL)
                parent = paths.launcher.parent
                source = parent / source_name
                destination = parent / destination_name
                if destination.name == paths.launcher.name and not injected:
                    source.write_bytes(rewritten)
                    source.chmod(0o700)
                    injected.append(True)
                if destination.exists() or destination.is_symlink():
                    raise FileExistsError(destination_name)
                source.rename(destination)

            with mock.patch.object(vibe_memory_install.sys, "platform", "darwin"), mock.patch.object(
                vibe_memory_install,
                "_darwin_renameat",
                side_effect=simulated_renameat,
            ):
                with self.assertRaises(vibe_memory_install.InstallError):
                    vibe_memory_install.install_launcher(paths, python_executable=sys.executable)

            self.assertEqual(injected, [True])
            self.assertFalse(paths.launcher.exists())
            self.assertEqual(list(paths.launcher.parent.glob(".launcher-unknown-*")), [])

    def test_atomic_install_state_write_handles_short_os_writes(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            state = vibe_memory_install._install_state_document(
                current_version="1.0.0",
                previous_version=None,
                port=8897,
                installed_clients=["codex"],
                python_executable=sys.executable,
            )
            real_write = vibe_memory_install.os.write

            def short_write(descriptor: int, content: bytes) -> int:
                return real_write(descriptor, content[:1])

            with mock.patch.object(vibe_memory_install.os, "write", side_effect=short_write):
                vibe_memory_install.write_install_state(paths, state)

            self.assertEqual(vibe_memory_install.read_install_state(paths), state)

    def test_read_install_state_rejects_symlinked_file(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            state_path = vibe_memory_install.install_state_path(paths)
            state_path.parent.mkdir(parents=True)
            outside = pathlib.Path(value) / "outside-state.json"
            outside.write_text("{}", encoding="utf-8")
            state_path.symlink_to(outside)

            with self.assertRaises(vibe_memory_install.InstallError):
                vibe_memory_install.read_install_state(paths)

            self.assertEqual(outside.read_text(encoding="utf-8"), "{}")

    def test_read_install_state_rejects_invalid_client_structure(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            state_path = vibe_memory_install.install_state_path(paths)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({
                "schema_version": 1,
                "current_version": "1.0.0",
                "previous_version": None,
                "hook_protocol_version": 1,
                "data_schema_version": 1,
                "port": 8897,
                "installed_clients": "codex",
            }), encoding="utf-8")

            with self.assertRaises(vibe_memory_install.InstallError):
                vibe_memory_install.read_install_state(paths)

    def test_read_install_state_allows_exact_empty_client_selection(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            state = vibe_memory_install._install_state_document(
                current_version="1.0.0",
                previous_version=None,
                port=8897,
                installed_clients=[],
                python_executable=sys.executable,
            )
            vibe_memory_install.write_install_state(paths, state)
            self.assertEqual(
                vibe_memory_install.read_install_state(paths)["installed_clients"], []
            )
            self.assertEqual(vibe_memory_install._installed_clients(paths), [])

    def test_read_install_state_rejects_unhashable_client(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            state_path = vibe_memory_install.install_state_path(paths)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({
                "schema_version": 1,
                "current_version": "1.0.0",
                "previous_version": None,
                "hook_protocol_version": 1,
                "data_schema_version": 1,
                "port": 8897,
                "installed_clients": ["codex", []],
            }), encoding="utf-8")

            with self.assertRaises(vibe_memory_install.InstallError):
                vibe_memory_install.read_install_state(paths)

    def test_read_install_state_rejects_boolean_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            state = vibe_memory_install._install_state_document(
                current_version="1.0.0",
                previous_version=None,
                port=8897,
                installed_clients=["codex"],
                python_executable=sys.executable,
            )
            state["schema_version"] = True
            state_path = vibe_memory_install.install_state_path(paths)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps(state), encoding="utf-8")

            with self.assertRaises(vibe_memory_install.InstallError):
                vibe_memory_install.read_install_state(paths)

    def test_install_does_not_require_optional_license(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            (source / "LICENSE").unlink()

            vibe_memory_install.install_runtime(source, self.make_paths(root))

    def test_installed_directories_and_files_are_private_without_changing_source(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            source_file = source / "scripts/server.py"
            source_file.chmod(0o644)
            paths = self.make_paths(root)

            vibe_memory_install.install_runtime(source, paths)

            release = paths.install_root / "releases/1.0.0"
            for item in [paths.install_root, paths.install_root / "releases", release, *release.rglob("*")]:
                if item.is_symlink():
                    continue
                expected = 0o700 if item.is_dir() else 0o600
                self.assertEqual(stat.S_IMODE(item.stat().st_mode), expected, item)
            self.assertEqual(stat.S_IMODE(source_file.stat().st_mode), 0o644)

    def test_rejects_missing_malformed_and_invalid_manifests(self) -> None:
        cases: list[tuple[str, object]] = [
            ("missing", None),
            ("malformed", "{broken"),
            ("missing-field", {key: value for key, value in MANIFEST.items() if key != "platform"}),
            ("bad-version", {**MANIFEST, "app_version": "../escape"}),
            ("bad-version-leading-zero", {**MANIFEST, "app_version": "01.0.0"}),
            ("bad-version-empty-identifier", {**MANIFEST, "app_version": "1.0.0-alpha..1"}),
            ("bad-version-prerelease-leading-zero", {**MANIFEST, "app_version": "1.0.0-01"}),
            ("bad-platform", {**MANIFEST, "platform": "Linux"}),
            ("bad-schema", {**MANIFEST, "data_schema_version": 0}),
            ("bad-python", {**MANIFEST, "minimum_python": "three.ten"}),
        ]
        for name, content in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as value:
                root = pathlib.Path(value)
                source = self.make_source(root)
                manifest = source / "release.json"
                if content is None:
                    manifest.unlink()
                elif isinstance(content, str):
                    manifest.write_text(content, encoding="utf-8")
                else:
                    manifest.write_text(json.dumps(content), encoding="utf-8")
                with self.assertRaises((OSError, ValueError, json.JSONDecodeError)):
                    vibe_memory_install.install_runtime(source, self.make_paths(root))

    def test_accepts_semver_prerelease_and_build_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            manifest = {**MANIFEST, "app_version": "1.0.0-alpha.1+001"}
            source = self.make_source(root, manifest)

            result = vibe_memory_install.install_runtime(source, self.make_paths(root))

            self.assertEqual(result, {"version": "1.0.0-alpha.1+001"})

    def test_failed_copy_cleans_owned_temporary_release_and_preserves_current(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            paths = self.make_paths(root)
            old_release = paths.install_root / "releases/0.9.0"
            old_release.mkdir(parents=True)
            os.symlink("releases/0.9.0", paths.install_root / "current")
            real_copy = vibe_memory_install._copy_release_content

            def fail_copy(source_root: pathlib.Path, temporary_release: pathlib.Path) -> None:
                real_copy(source_root, temporary_release)
                raise OSError("simulated copy failure")

            with mock.patch.object(vibe_memory_install, "_copy_release_content", side_effect=fail_copy):
                with self.assertRaisesRegex(OSError, "simulated copy failure"):
                    vibe_memory_install.install_runtime(source, paths)

            self.assertEqual((paths.install_root / "current").resolve(), old_release.resolve())
            self.assertFalse((paths.install_root / "releases/1.0.0").exists())
            self.assertEqual(list((paths.install_root / "releases").glob(".1.0.0.tmp-*")), [])

    def test_partial_copy_failure_cleans_exact_created_subset(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            paths = self.make_paths(root)
            real_write_file_at = vibe_memory_install._write_file_at
            created: list[str] = []

            def fail_after_second_created_file(*args: object, **kwargs: object) -> None:
                real_write_file_at(*args, **kwargs)
                relative = kwargs.get("relative")
                if relative is None and len(args) >= 5:
                    relative = args[4]
                created.append(str(relative))
                if len(created) == 2:
                    raise OSError("simulated partial copy failure")

            with mock.patch.object(
                vibe_memory_install,
                "_write_file_at",
                side_effect=fail_after_second_created_file,
            ):
                with self.assertRaisesRegex(OSError, "simulated partial copy failure"):
                    vibe_memory_install.install_runtime(source, paths)

            self.assertEqual(len(created), 2)
            self.assertFalse((paths.install_root / "releases/1.0.0").exists())
            self.assertFalse(os.path.lexists(str(paths.install_root / "current")))
            self.assertEqual(
                list((paths.install_root / "releases").glob(".1.0.0.tmp-*")),
                [],
            )

    def test_incomplete_copy_is_rejected_before_release_rename(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            paths = self.make_paths(root)
            real_copy = vibe_memory_install._copy_release_content

            def incomplete_copy(source_root: pathlib.Path, temporary_release: pathlib.Path) -> None:
                real_copy(source_root, temporary_release)
                (temporary_release / "README.md").unlink()

            with mock.patch.object(vibe_memory_install, "_copy_release_content", side_effect=incomplete_copy):
                with self.assertRaises(vibe_memory_install.TemporaryCleanupConflict):
                    vibe_memory_install.install_runtime(source, paths)

            self.assertFalse((paths.install_root / "releases/1.0.0").exists())
            self.assertFalse(os.path.lexists(str(paths.install_root / "current")))
            temporary = list((paths.install_root / "releases").glob(".1.0.0.tmp-*"))
            self.assertEqual(len(temporary), 1)
            self.assertFalse((temporary[0] / "README.md").exists())
            self.assertTrue((temporary[0] / "release.json").is_file())

    def test_same_release_is_idempotent_but_modified_release_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            paths = self.make_paths(root)
            vibe_memory_install.install_runtime(source, paths)

            self.assertEqual(vibe_memory_install.install_runtime(source, paths), {"version": "1.0.0"})
            installed_readme = paths.install_root / "releases/1.0.0/README.md"
            installed_readme.write_text("unknown content\n", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                vibe_memory_install.install_runtime(source, paths)
            self.assertEqual(installed_readme.read_text(encoding="utf-8"), "unknown content\n")

    def test_concurrent_identical_installs_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            paths = self.make_paths(root)
            (paths.install_root / "releases").mkdir(parents=True)
            real_copy = vibe_memory_install._copy_release_content
            copied = threading.Barrier(2)

            def synchronized_copy(source_root: pathlib.Path, temporary_release: pathlib.Path) -> None:
                real_copy(source_root, temporary_release)
                copied.wait(timeout=5)

            with mock.patch.object(vibe_memory_install, "_copy_release_content", side_effect=synchronized_copy):
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [executor.submit(vibe_memory_install.install_runtime, source, paths) for _ in range(2)]
                    results = [future.result(timeout=10) for future in futures]

            self.assertEqual(results, [{"version": "1.0.0"}, {"version": "1.0.0"}])
            self.assertEqual(
                (paths.install_root / "current").resolve(),
                (paths.install_root / "releases/1.0.0").resolve(),
            )

    def test_destination_race_preserves_unknown_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            paths = self.make_paths(root)
            unknown_identity: list[tuple[int, int]] = []

            def race_destination(temporary: pathlib.Path, destination: pathlib.Path) -> None:
                destination_path = pathlib.Path(os.fspath(destination))
                destination_path.mkdir()
                metadata = destination_path.stat()
                unknown_identity.append((metadata.st_dev, metadata.st_ino))
                raise FileExistsError("destination race")

            with mock.patch.object(
                vibe_memory_install,
                "_atomic_rename_exclusive",
                create=True,
                side_effect=race_destination,
            ):
                with self.assertRaises(FileExistsError):
                    vibe_memory_install.install_runtime(source, paths)

            destination = paths.install_root / "releases/1.0.0"
            metadata = destination.stat()
            self.assertEqual((metadata.st_dev, metadata.st_ino), unknown_identity[0])
            self.assertEqual(list(destination.iterdir()), [])
            self.assertFalse(os.path.lexists(str(paths.install_root / "current")))
            self.assertEqual(list((paths.install_root / "releases").glob(".1.0.0.tmp-*")), [])

    def test_destination_race_accepts_identical_complete_winner(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            paths = self.make_paths(root)
            injected: list[str] = []

            def race_destination(temporary: pathlib.Path, destination: pathlib.Path) -> None:
                if destination.name == "current":
                    os.rename(temporary, destination)
                    return
                injected.append(destination.name)
                shutil.copytree(temporary, destination)
                raise FileExistsError("destination race")

            with mock.patch.object(
                vibe_memory_install,
                "_atomic_rename_exclusive",
                create=True,
                side_effect=race_destination,
            ):
                result = vibe_memory_install.install_runtime(source, paths)

            self.assertEqual(result, {"version": "1.0.0"})
            self.assertEqual(injected, ["1.0.0"])
            self.assertEqual(list((paths.install_root / "releases").glob(".1.0.0.tmp-*")), [])
            self.assertEqual(
                (paths.install_root / "current").resolve(),
                (paths.install_root / "releases/1.0.0").resolve(),
            )

    def test_current_race_preserves_unknown_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            paths = self.make_paths(root)
            unknown = b"unknown current\n"

            real_symlink = vibe_memory_install.os.symlink

            def race_current(
                target: pathlib.Path | str,
                link_name: pathlib.Path | str,
                *args: object,
                **kwargs: object,
            ) -> None:
                if link_name != "current":
                    real_symlink(target, link_name, *args, **kwargs)
                    return
                (paths.install_root / "current").write_bytes(unknown)
                raise FileExistsError("current race")

            with mock.patch.object(
                vibe_memory_install.os,
                "symlink",
                side_effect=race_current,
            ):
                with self.assertRaises(FileExistsError):
                    vibe_memory_install.install_runtime(source, paths)

            self.assertEqual((paths.install_root / "current").read_bytes(), unknown)
            self.assertTrue((paths.install_root / "releases/1.0.0").is_dir())
            self.assertEqual(list(paths.install_root.glob(".current.tmp-*")), [])

    def test_current_race_accepts_identical_managed_winner(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            paths = self.make_paths(root)
            injected: list[str] = []

            real_symlink = vibe_memory_install.os.symlink

            def race_current(
                target: pathlib.Path | str,
                link_name: pathlib.Path | str,
                *args: object,
                **kwargs: object,
            ) -> None:
                if link_name != "current":
                    real_symlink(target, link_name, *args, **kwargs)
                    return
                injected.append(str(link_name))
                real_symlink(target, link_name, *args, **kwargs)
                raise FileExistsError("current race")

            with mock.patch.object(
                vibe_memory_install.os,
                "symlink",
                side_effect=race_current,
            ):
                result = vibe_memory_install.install_runtime(source, paths)

            self.assertEqual(result, {"version": "1.0.0"})
            self.assertEqual(injected, ["current"])
            self.assertEqual(os.readlink(paths.install_root / "current"), "releases/1.0.0")
            self.assertEqual(list(paths.install_root.glob(".current.tmp-*")), [])

    def test_existing_different_managed_current_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            paths = self.make_paths(root)
            old_release = paths.install_root / "releases/0.9.0"
            old_release.mkdir(parents=True)
            current = paths.install_root / "current"
            os.symlink("releases/0.9.0", current)

            with self.assertRaises(FileExistsError):
                vibe_memory_install.install_runtime(source, paths)

            self.assertEqual(os.readlink(current), "releases/0.9.0")

    @unittest.skipUnless(sys.platform == "darwin", "Darwin rename flags only")
    def test_darwin_exclusive_rename_never_clobbers_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            destination_identity = destination.stat().st_ino

            with self.assertRaises(FileExistsError):
                vibe_memory_install._darwin_rename(
                    source,
                    destination,
                    vibe_memory_install.RENAME_EXCL,
                )

            self.assertTrue(source.is_dir())
            self.assertEqual(destination.stat().st_ino, destination_identity)

    def test_rejects_unsafe_source_entry_types(self) -> None:
        for kind in ("live-symlink", "broken-symlink", "fifo", "socket", "device"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as value:
                root = pathlib.Path(value)
                source = self.make_source(root)
                unsafe = source / "scripts/unsafe"
                outside = root / "outside-secret"
                outside.write_text("secret", encoding="utf-8")
                if kind == "live-symlink":
                    os.symlink(outside, unsafe)
                elif kind == "broken-symlink":
                    os.symlink(root / "missing", unsafe)
                elif kind == "fifo":
                    os.mkfifo(unsafe)
                else:
                    unsafe.write_bytes(b"type injection\n")

                def assert_rejected() -> None:
                    with self.assertRaises(ValueError):
                        vibe_memory_install.install_runtime(source, self.make_paths(root))

                if kind in ("socket", "device"):
                    real_stat = vibe_memory_install.os.stat
                    scripts_identity = (source / "scripts").stat().st_dev, (source / "scripts").stat().st_ino

                    def report_unsafe_type(
                        path: pathlib.Path | str,
                        *args: object,
                        **kwargs: object,
                    ) -> os.stat_result:
                        metadata = real_stat(path, *args, **kwargs)
                        directory_fd = kwargs.get("dir_fd")
                        if path != "unsafe" or not isinstance(directory_fd, int):
                            return metadata
                        parent = os.fstat(directory_fd)
                        if (parent.st_dev, parent.st_ino) != scripts_identity:
                            return metadata
                        fields = list(metadata)
                        fields[0] = (stat.S_IFSOCK if kind == "socket" else stat.S_IFCHR) | 0o600
                        return os.stat_result(fields)

                    with mock.patch.object(
                        vibe_memory_install.os,
                        "stat",
                        side_effect=report_unsafe_type,
                    ):
                        assert_rejected()
                else:
                    assert_rejected()

                self.assertFalse(os.path.lexists(str(self.make_paths(root).install_root / "current")))

    def test_source_file_path_swap_after_open_copies_opened_inode(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            paths = self.make_paths(root)
            readme = source / "README.md"
            original = source / "README.original"
            outside = root / "outside-secret"
            secret = "OUTSIDE-ROOT-SECRET\n"
            outside.write_text(secret, encoding="utf-8")
            opened: list[bool] = []
            real_open = vibe_memory_install.os.open

            def open_then_swap(path: pathlib.Path | str, flags: int, *args: object, **kwargs: object) -> int:
                descriptor = real_open(path, flags, *args, **kwargs)
                if path == "README.md" and kwargs.get("dir_fd") is not None and not opened:
                    os.rename(readme, original)
                    os.symlink(outside, readme)
                    opened.append(True)
                return descriptor

            with mock.patch.object(vibe_memory_install.os, "open", side_effect=open_then_swap):
                result = vibe_memory_install.install_runtime(source, paths)

            installed = paths.install_root / "releases/1.0.0/README.md"
            self.assertEqual(result, {"version": "1.0.0"})
            self.assertEqual(opened, [True])
            self.assertEqual(installed.read_text(encoding="utf-8"), "# Runtime\n")
            self.assertNotEqual(installed.read_text(encoding="utf-8"), secret)
            self.assertEqual(outside.read_text(encoding="utf-8"), secret)

    def test_manifest_path_swap_after_open_uses_opened_manifest_inode(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            paths = self.make_paths(root)
            release_json = source / "release.json"
            original = source / "release.original"
            outside = root / "outside-release.json"
            outside.write_text(json.dumps({**MANIFEST, "app_version": "9.9.9"}), encoding="utf-8")
            opened: list[bool] = []
            real_open = vibe_memory_install.os.open

            def open_then_swap(path: pathlib.Path | str, flags: int, *args: object, **kwargs: object) -> int:
                descriptor = real_open(path, flags, *args, **kwargs)
                if path == "release.json" and kwargs.get("dir_fd") is not None and not opened:
                    os.rename(release_json, original)
                    os.symlink(outside, release_json)
                    opened.append(True)
                return descriptor

            with mock.patch.object(vibe_memory_install.os, "open", side_effect=open_then_swap):
                result = vibe_memory_install.install_runtime(source, paths)

            self.assertEqual(result, {"version": "1.0.0"})
            self.assertEqual(opened, [True])
            self.assertTrue((paths.install_root / "releases/1.0.0/release.json").is_file())
            self.assertFalse((paths.install_root / "releases/9.9.9").exists())

    def test_release_temp_replacement_preserves_unknown_entry(self) -> None:
        for kind in ("directory", "file", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as value:
                root = pathlib.Path(value)
                source = self.make_source(root)
                paths = self.make_paths(root)
                outside = root / "outside"
                outside.write_bytes(b"outside\n")
                real_copy = vibe_memory_install._copy_release_content
                unknown_identity: list[tuple[int, int]] = []
                unknown_path: list[pathlib.Path] = []

                def replace_then_fail(source_snapshot: object, temporary_release: pathlib.Path) -> None:
                    real_copy(source_snapshot, temporary_release)
                    temporary = pathlib.Path(os.fspath(temporary_release))
                    os.rename(temporary, temporary.parent / "attacker-moved-owned-temp")
                    if kind == "directory":
                        temporary.mkdir()
                        (temporary / "marker").write_bytes(b"unknown directory\n")
                    elif kind == "file":
                        temporary.write_bytes(b"unknown file\n")
                    else:
                        os.symlink(outside, temporary)
                    metadata = os.lstat(temporary)
                    unknown_identity.append((metadata.st_dev, metadata.st_ino))
                    unknown_path.append(temporary)
                    raise OSError("simulated copy failure")

                with mock.patch.object(
                    vibe_memory_install,
                    "_copy_release_content",
                    side_effect=replace_then_fail,
                ):
                    with self.assertRaisesRegex(
                        vibe_memory_install.TemporaryCleanupConflict,
                        "temporary cleanup conflict",
                    ):
                        vibe_memory_install.install_runtime(source, paths)

                metadata = os.lstat(unknown_path[0])
                self.assertEqual((metadata.st_dev, metadata.st_ino), unknown_identity[0])
                if kind == "directory":
                    self.assertEqual((unknown_path[0] / "marker").read_bytes(), b"unknown directory\n")
                elif kind == "file":
                    self.assertEqual(unknown_path[0].read_bytes(), b"unknown file\n")
                else:
                    self.assertEqual(os.readlink(unknown_path[0]), str(outside))
                    self.assertEqual(outside.read_bytes(), b"outside\n")

    def test_release_temp_replaced_before_open_preserves_unknown_directory(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            paths = self.make_paths(root)
            real_open = vibe_memory_install.os.open
            unknown_identity: list[tuple[int, int]] = []
            unknown_name: list[str] = []

            def replace_before_open(
                path: pathlib.Path | str,
                flags: int,
                *args: object,
                **kwargs: object,
            ) -> int:
                directory_fd = kwargs.get("dir_fd")
                if (
                    isinstance(path, str)
                    and path.startswith(".1.0.0.tmp-")
                    and isinstance(directory_fd, int)
                    and not unknown_name
                ):
                    os.rename(
                        path,
                        "attacker-moved-before-open",
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                    )
                    os.mkdir(path, 0o755, dir_fd=directory_fd)
                    metadata = os.stat(path, dir_fd=directory_fd, follow_symlinks=False)
                    unknown_identity.append((metadata.st_dev, metadata.st_ino))
                    unknown_name.append(path)
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(vibe_memory_install.os, "open", side_effect=replace_before_open):
                with self.assertRaisesRegex(
                    vibe_memory_install.TemporaryCleanupConflict,
                    "temporary cleanup conflict",
                ):
                    vibe_memory_install.install_runtime(source, paths)

            unknown = paths.install_root / "releases" / unknown_name[0]
            metadata = os.lstat(unknown)
            self.assertEqual((metadata.st_dev, metadata.st_ino), unknown_identity[0])
            self.assertTrue(unknown.is_dir())

    def test_release_temp_replaced_before_first_stat_preserves_unknown_entry(self) -> None:
        for kind in ("directory", "file", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as value:
                root = pathlib.Path(value)
                source = self.make_source(root)
                paths = self.make_paths(root)
                outside = root / "outside"
                outside.write_bytes(b"outside\n")
                real_stat = vibe_memory_install.os.stat
                real_open = vibe_memory_install.os.open
                unknown_identity: list[tuple[int, int]] = []
                unknown_name: list[str] = []

                def replace_before_first_stat(
                    path: pathlib.Path | str,
                    *args: object,
                    **kwargs: object,
                ) -> os.stat_result:
                    directory_fd = kwargs.get("dir_fd")
                    if (
                        isinstance(path, str)
                        and path.startswith(".1.0.0.tmp-")
                        and isinstance(directory_fd, int)
                        and not unknown_name
                    ):
                        os.rename(
                            path,
                            "attacker-moved-before-first-stat",
                            src_dir_fd=directory_fd,
                            dst_dir_fd=directory_fd,
                        )
                        if kind == "directory":
                            os.mkdir(path, 0o755, dir_fd=directory_fd)
                        elif kind == "file":
                            descriptor = real_open(
                                path,
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                0o644,
                                dir_fd=directory_fd,
                            )
                            try:
                                os.write(descriptor, b"unknown file\n")
                            finally:
                                os.close(descriptor)
                        else:
                            os.symlink(outside, path, dir_fd=directory_fd)
                        metadata = real_stat(path, dir_fd=directory_fd, follow_symlinks=False)
                        unknown_identity.append((metadata.st_dev, metadata.st_ino))
                        unknown_name.append(path)
                    return real_stat(path, *args, **kwargs)

                with mock.patch.object(
                    vibe_memory_install.os,
                    "stat",
                    side_effect=replace_before_first_stat,
                ):
                    with self.assertRaises(Exception):
                        vibe_memory_install.install_runtime(source, paths)

                unknown = paths.install_root / "releases" / unknown_name[0]
                metadata = os.lstat(unknown)
                self.assertEqual((metadata.st_dev, metadata.st_ino), unknown_identity[0])
                if kind == "directory":
                    self.assertTrue(unknown.is_dir())
                elif kind == "file":
                    self.assertEqual(unknown.read_bytes(), b"unknown file\n")
                else:
                    self.assertEqual(os.readlink(unknown), str(outside))
                    self.assertEqual(outside.read_bytes(), b"outside\n")

    def test_current_creation_race_preserves_unknown_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            paths = self.make_paths(root)
            real_symlink = vibe_memory_install.os.symlink

            def race_current(
                target: pathlib.Path | str,
                link_name: pathlib.Path | str,
                *args: object,
                **kwargs: object,
            ) -> None:
                real_symlink("releases/unknown", link_name, *args, **kwargs)
                raise FileExistsError("current race")

            with mock.patch.object(
                vibe_memory_install.os,
                "symlink",
                side_effect=race_current,
            ):
                with self.assertRaises(FileExistsError):
                    vibe_memory_install.install_runtime(source, paths)

            current = paths.install_root / "current"
            self.assertTrue(current.is_symlink())
            self.assertEqual(os.readlink(current), "releases/unknown")
            self.assertEqual(list(paths.install_root.glob(".current.tmp-*")), [])

    def test_current_activation_has_no_replaceable_named_temp(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            paths = self.make_paths(root)
            unknown = b"UNKNOWN CURRENT\n"
            injected: list[bool] = []
            real_stat = vibe_memory_install.os.stat

            def replace_before_current_temp_stat(
                path: pathlib.Path | str,
                *args: object,
                **kwargs: object,
            ) -> os.stat_result:
                directory_fd = kwargs.get("dir_fd")
                if (
                    isinstance(path, str)
                    and path.startswith(".current.tmp-")
                    and isinstance(directory_fd, int)
                    and not injected
                ):
                    os.unlink(path, dir_fd=directory_fd)
                    descriptor = os.open(
                        path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o644,
                        dir_fd=directory_fd,
                    )
                    try:
                        os.write(descriptor, unknown)
                    finally:
                        os.close(descriptor)
                    injected.append(True)
                return real_stat(path, *args, **kwargs)

            with mock.patch.object(
                vibe_memory_install.os,
                "stat",
                side_effect=replace_before_current_temp_stat,
            ):
                result = vibe_memory_install.install_runtime(source, paths)

            current = paths.install_root / "current"
            self.assertEqual(result, {"version": "1.0.0"})
            self.assertEqual(injected, [])
            self.assertTrue(current.is_symlink())
            self.assertEqual(os.readlink(current), "releases/1.0.0")
            self.assertEqual(list(paths.install_root.glob(".current.tmp-*")), [])

    def test_rejects_unsafe_install_layout_components(self) -> None:
        scenarios = (
            "install-parent-symlink",
            "install-root-symlink",
            "releases-symlink",
            "current-directory",
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as value:
                root = pathlib.Path(value)
                source = self.make_source(root)
                paths = self.make_paths(root)
                outside = root / "outside"
                outside.mkdir()
                if scenario == "install-parent-symlink":
                    paths.install_root.parent.parent.mkdir(parents=True)
                    os.symlink(outside, paths.install_root.parent)
                else:
                    paths.install_root.parent.mkdir(parents=True)
                if scenario == "install-root-symlink":
                    os.symlink(outside, paths.install_root)
                elif scenario not in ("install-parent-symlink",):
                    paths.install_root.mkdir()
                    if scenario == "releases-symlink":
                        os.symlink(outside, paths.install_root / "releases")
                    else:
                        (paths.install_root / "releases").mkdir()
                        (paths.install_root / "current").mkdir()

                with self.assertRaises((OSError, ValueError)):
                    vibe_memory_install.install_runtime(source, paths)
                self.assertEqual(list(outside.iterdir()), [])

    def test_rejects_unsafe_components_high_in_install_ancestor_chain(self) -> None:
        scenarios = ("live-symlink", "broken-symlink", "regular-file")
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as value:
                root = pathlib.Path(value)
                source = self.make_source(root)
                base = root / "managed-base"
                attack_component = base / "outer"
                home = attack_component / "inner/home"
                paths = vibe_memory_paths.for_home(home)
                base.mkdir()
                outside = root / "outside"
                outside.mkdir()
                if scenario == "live-symlink":
                    os.symlink(outside, attack_component)
                elif scenario == "broken-symlink":
                    os.symlink(root / "missing-target", attack_component)
                else:
                    attack_component.write_text("unknown ancestor\n", encoding="utf-8")

                with self.assertRaises((OSError, ValueError)):
                    vibe_memory_install.install_runtime(source, paths)

                self.assertEqual(list(outside.iterdir()), [])
                if scenario == "regular-file":
                    self.assertEqual(
                        attack_component.read_text(encoding="utf-8"),
                        "unknown ancestor\n",
                    )

    def test_rejects_unlisted_root_owned_symlink_alias(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            outside = root / "outside"
            outside.mkdir()
            alias = root / "unlisted-root-alias"
            os.symlink(outside, alias)
            paths = vibe_memory_paths.for_home(alias / "home")
            real_lstat = os.lstat

            def report_alias_as_root(path: pathlib.Path | str) -> os.stat_result:
                metadata = real_lstat(path)
                if pathlib.Path(path) != alias:
                    return metadata
                fields = list(metadata)
                fields[4] = 0
                return os.stat_result(fields)

            with mock.patch.object(vibe_memory_install.os, "lstat", side_effect=report_alias_as_root):
                with self.assertRaises(ValueError):
                    vibe_memory_install.install_runtime(source, paths)

            self.assertEqual(list(outside.iterdir()), [])

    def test_ancestor_inserted_after_validation_cannot_redirect_creation(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            base = root / "managed-base"
            base.mkdir()
            missing = base / "inserted"
            paths = vibe_memory_paths.for_home(missing / "home")
            outside = root / "outside"
            outside.mkdir()
            real_validate = vibe_memory_install._validate_install_ancestor_chain

            def validate_then_race(path: pathlib.Path) -> None:
                real_validate(path)
                os.symlink(outside, missing)

            with mock.patch.object(
                vibe_memory_install,
                "_validate_install_ancestor_chain",
                side_effect=validate_then_race,
            ):
                with self.assertRaises((OSError, ValueError)):
                    vibe_memory_install.install_runtime(source, paths)

            self.assertEqual(list(outside.iterdir()), [])

    def test_non_darwin_install_fails_closed_even_for_identical_release(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            paths = self.make_paths(root)
            vibe_memory_install.install_runtime(source, paths)

            with mock.patch.object(vibe_memory_install.sys, "platform", "linux"):
                with self.assertRaises(NotImplementedError):
                    vibe_memory_install.install_runtime(source, paths)

            self.assertEqual(os.readlink(paths.install_root / "current"), "releases/1.0.0")

    def test_identical_destination_winner_permissions_are_made_private(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            paths = self.make_paths(root)

            def permissive_winner(temporary: pathlib.Path, destination: pathlib.Path) -> None:
                if destination.name == "current":
                    os.rename(temporary, destination)
                    return
                shutil.copytree(temporary, destination)
                destination_path = pathlib.Path(os.fspath(destination))
                for item in [destination_path, *destination_path.rglob("*")]:
                    item.chmod(0o777 if item.is_dir() else 0o666)
                raise FileExistsError("destination race")

            with mock.patch.object(
                vibe_memory_install,
                "_atomic_rename_exclusive",
                side_effect=permissive_winner,
            ):
                vibe_memory_install.install_runtime(source, paths)

            release = paths.install_root / "releases/1.0.0"
            for item in [release, *release.rglob("*")]:
                expected = 0o700 if item.is_dir() else 0o600
                self.assertEqual(stat.S_IMODE(item.stat().st_mode), expected, item)
            self.assertEqual(list((paths.install_root / "releases").glob(".1.0.0.tmp-*")), [])

    def test_existing_release_file_swap_before_chmod_fails_without_touching_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            paths = self.make_paths(root)
            vibe_memory_install.install_runtime(source, paths)
            (paths.install_root / "current").unlink()
            readme = paths.install_root / "releases/1.0.0/README.md"
            preserved = readme.with_name("README.preserved")
            readme_identity = readme.stat().st_dev, readme.stat().st_ino
            unknown = b"UNKNOWN README\n"
            injected: list[bool] = []
            real_fchmod = vibe_memory_install.os.fchmod
            real_fstat = vibe_memory_install.os.fstat

            def swap_before_chmod(descriptor: int, mode: int) -> None:
                metadata = real_fstat(descriptor)
                if (metadata.st_dev, metadata.st_ino) == readme_identity and not injected:
                    os.rename(readme, preserved)
                    readme.write_bytes(unknown)
                    readme.chmod(0o644)
                    injected.append(True)
                real_fchmod(descriptor, mode)

            with mock.patch.object(vibe_memory_install.os, "fchmod", side_effect=swap_before_chmod):
                with self.assertRaises(FileExistsError):
                    vibe_memory_install.install_runtime(source, paths)

            self.assertEqual(injected, [True])
            self.assertEqual(readme.read_bytes(), unknown)
            self.assertEqual(stat.S_IMODE(readme.stat().st_mode), 0o644)
            self.assertFalse(os.path.lexists(str(paths.install_root / "current")))

    def test_existing_release_directory_swap_after_open_fails_without_touching_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            paths = self.make_paths(root)
            vibe_memory_install.install_runtime(source, paths)
            (paths.install_root / "current").unlink()
            release = paths.install_root / "releases/1.0.0"
            scripts = release / "scripts"
            preserved = release / "scripts.preserved"
            release_identity = release.stat().st_dev, release.stat().st_ino
            unknown = b"UNKNOWN DIRECTORY\n"
            injected: list[bool] = []
            real_open = vibe_memory_install.os.open
            real_fstat = vibe_memory_install.os.fstat

            def open_then_swap(
                path: pathlib.Path | str,
                flags: int,
                *args: object,
                **kwargs: object,
            ) -> int:
                descriptor = real_open(path, flags, *args, **kwargs)
                parent_fd = kwargs.get("dir_fd")
                if path == "scripts" and isinstance(parent_fd, int) and not injected:
                    parent = real_fstat(parent_fd)
                    if (parent.st_dev, parent.st_ino) == release_identity:
                        os.rename(scripts, preserved)
                        scripts.mkdir(mode=0o755)
                        (scripts / "unknown").write_bytes(unknown)
                        (scripts / "unknown").chmod(0o644)
                        injected.append(True)
                return descriptor

            with mock.patch.object(vibe_memory_install.os, "open", side_effect=open_then_swap):
                with self.assertRaises(FileExistsError):
                    vibe_memory_install.install_runtime(source, paths)

            self.assertEqual(injected, [True])
            self.assertEqual((scripts / "unknown").read_bytes(), unknown)
            self.assertEqual(stat.S_IMODE(scripts.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE((scripts / "unknown").stat().st_mode), 0o644)
            self.assertFalse(os.path.lexists(str(paths.install_root / "current")))

    def test_existing_release_mutation_during_chmod_fails_final_verification(self) -> None:
        for kind in ("extra-entry", "same-inode-rewrite"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as value:
                root = pathlib.Path(value)
                source = self.make_source(root)
                paths = self.make_paths(root)
                vibe_memory_install.install_runtime(source, paths)
                (paths.install_root / "current").unlink()
                release = paths.install_root / "releases/1.0.0"
                readme = release / "README.md"
                release_identity = release.stat().st_dev, release.stat().st_ino
                readme_identity = readme.stat().st_dev, readme.stat().st_ino
                injected: list[bool] = []
                real_fchmod = vibe_memory_install.os.fchmod
                real_fstat = vibe_memory_install.os.fstat

                def mutate_during_chmod(descriptor: int, mode: int) -> None:
                    metadata = real_fstat(descriptor)
                    identity = metadata.st_dev, metadata.st_ino
                    if kind == "extra-entry" and identity == release_identity and not injected:
                        (release / "extra").write_bytes(b"UNKNOWN EXTRA\n")
                        (release / "extra").chmod(0o644)
                        injected.append(True)
                    elif kind == "same-inode-rewrite" and identity == readme_identity and not injected:
                        before = readme.stat().st_dev, readme.stat().st_ino
                        readme.write_bytes(b"UNKNOWN REWRITE\n")
                        self.assertEqual((readme.stat().st_dev, readme.stat().st_ino), before)
                        injected.append(True)
                    real_fchmod(descriptor, mode)

                with mock.patch.object(
                    vibe_memory_install.os,
                    "fchmod",
                    side_effect=mutate_during_chmod,
                ):
                    with self.assertRaises(FileExistsError):
                        vibe_memory_install.install_runtime(source, paths)

                self.assertEqual(injected, [True])
                self.assertFalse(os.path.lexists(str(paths.install_root / "current")))
                if kind == "extra-entry":
                    self.assertEqual((release / "extra").read_bytes(), b"UNKNOWN EXTRA\n")
                else:
                    self.assertEqual(readme.read_bytes(), b"UNKNOWN REWRITE\n")

    def test_temporary_release_mutation_during_chmod_is_not_published(self) -> None:
        for kind in ("extra-entry", "same-inode-rewrite"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as value:
                root = pathlib.Path(value)
                source = self.make_source(root)
                paths = self.make_paths(root)
                injected: list[bool] = []
                first_comparison_ready: list[bool] = []
                real_fchmod = vibe_memory_install.os.fchmod
                real_fstat = vibe_memory_install.os.fstat
                real_pin_release_entries_fd = vibe_memory_install._pin_release_entries_fd

                def pin_then_mark(*args: object, **kwargs: object) -> object:
                    entries = real_pin_release_entries_fd(*args, **kwargs)
                    if len(args) < 3 or args[2] == pathlib.Path():
                        first_comparison_ready.append(True)
                    return entries

                def mutate_during_chmod(descriptor: int, mode: int) -> None:
                    metadata = real_fstat(descriptor)
                    temporary = list(
                        (paths.install_root / "releases").glob(".1.0.0.tmp-*")
                    )
                    if temporary and first_comparison_ready and not injected:
                        temporary_root = temporary[0]
                        target = temporary_root if kind == "extra-entry" else temporary_root / "README.md"
                        target_metadata = target.stat()
                        if (metadata.st_dev, metadata.st_ino) == (
                            target_metadata.st_dev,
                            target_metadata.st_ino,
                        ):
                            if kind == "extra-entry":
                                (temporary_root / "extra").write_bytes(b"UNKNOWN EXTRA\n")
                            else:
                                before = target_metadata.st_dev, target_metadata.st_ino
                                target.write_bytes(b"UNKNOWN REWRITE\n")
                                self.assertEqual((target.stat().st_dev, target.stat().st_ino), before)
                            injected.append(True)
                    real_fchmod(descriptor, mode)

                with mock.patch.object(
                    vibe_memory_install.os,
                    "fchmod",
                    side_effect=mutate_during_chmod,
                ), mock.patch.object(
                    vibe_memory_install,
                    "_pin_release_entries_fd",
                    side_effect=pin_then_mark,
                ):
                    expected_error = (
                        vibe_memory_install.TemporaryCleanupConflict
                        if kind == "extra-entry"
                        else ValueError
                    )
                    with self.assertRaises(expected_error):
                        vibe_memory_install.install_runtime(source, paths)

                self.assertEqual(injected, [True])
                self.assertFalse((paths.install_root / "releases/1.0.0").exists())
                self.assertFalse(os.path.lexists(str(paths.install_root / "current")))
                temporary = list(
                    (paths.install_root / "releases").glob(".1.0.0.tmp-*")
                )
                if kind == "extra-entry":
                    self.assertEqual(len(temporary), 1)
                    self.assertEqual(
                        (temporary[0] / "extra").read_bytes(),
                        b"UNKNOWN EXTRA\n",
                    )
                else:
                    self.assertEqual(temporary, [])

    def test_existing_release_mode_drift_after_chmod_fails_final_verification(self) -> None:
        for kind in ("file", "directory"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as value:
                root = pathlib.Path(value)
                source = self.make_source(root)
                paths = self.make_paths(root)
                vibe_memory_install.install_runtime(source, paths)
                (paths.install_root / "current").unlink()
                release = paths.install_root / "releases/1.0.0"
                target = release / "README.md" if kind == "file" else release / "scripts"
                drift_mode = 0o666 if kind == "file" else 0o777
                injected: list[bool] = []
                real_final_verification = vibe_memory_install._release_entries_from_pinned_fds

                def drift_mode_before_final_verification(*args: object, **kwargs: object) -> object:
                    if not injected:
                        target.chmod(drift_mode)
                        injected.append(True)
                    return real_final_verification(*args, **kwargs)

                with mock.patch.object(
                    vibe_memory_install,
                    "_release_entries_from_pinned_fds",
                    side_effect=drift_mode_before_final_verification,
                ):
                    with self.assertRaises(FileExistsError):
                        vibe_memory_install.install_runtime(source, paths)

                self.assertEqual(injected, [True])
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), drift_mode)
                self.assertFalse(os.path.lexists(str(paths.install_root / "current")))

    def test_temporary_release_mode_drift_after_chmod_is_not_published(self) -> None:
        for kind in ("file", "directory"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as value:
                root = pathlib.Path(value)
                source = self.make_source(root)
                paths = self.make_paths(root)
                drift_mode = 0o666 if kind == "file" else 0o777
                injected: list[bool] = []
                real_final_verification = vibe_memory_install._release_entries_from_pinned_fds

                def drift_mode_before_final_verification(*args: object, **kwargs: object) -> object:
                    temporary = list(
                        (paths.install_root / "releases").glob(".1.0.0.tmp-*")
                    )
                    if temporary and not injected:
                        target = (
                            temporary[0] / "README.md"
                            if kind == "file"
                            else temporary[0] / "scripts"
                        )
                        target.chmod(drift_mode)
                        injected.append(True)
                    return real_final_verification(*args, **kwargs)

                with mock.patch.object(
                    vibe_memory_install,
                    "_release_entries_from_pinned_fds",
                    side_effect=drift_mode_before_final_verification,
                ):
                    with self.assertRaises(ValueError):
                        vibe_memory_install.install_runtime(source, paths)

                self.assertEqual(injected, [True])
                self.assertFalse((paths.install_root / "releases/1.0.0").exists())
                self.assertFalse(os.path.lexists(str(paths.install_root / "current")))
                self.assertEqual(
                    list((paths.install_root / "releases").glob(".1.0.0.tmp-*")),
                    [],
                )

    def test_temporary_initial_listdir_failure_closes_fd_and_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            paths = self.make_paths(root)
            real_open = vibe_memory_install.os.open
            real_listdir = vibe_memory_install.os.listdir
            baseline = len(os.listdir("/dev/fd"))

            for _ in range(8):
                temporary_fds: set[int] = set()
                injected: list[bool] = []

                def record_temporary_open(
                    path: pathlib.Path | str,
                    flags: int,
                    *args: object,
                    **kwargs: object,
                ) -> int:
                    descriptor = real_open(path, flags, *args, **kwargs)
                    if isinstance(path, str) and path.startswith(".1.0.0.tmp-"):
                        temporary_fds.add(descriptor)
                    return descriptor

                def fail_initial_listdir(path: pathlib.Path | str | int) -> list[str]:
                    if isinstance(path, int) and path in temporary_fds and not injected:
                        injected.append(True)
                        raise OSError("injected temporary listdir failure")
                    return real_listdir(path)

                with mock.patch.object(
                    vibe_memory_install.os,
                    "open",
                    side_effect=record_temporary_open,
                ), mock.patch.object(
                    vibe_memory_install.os,
                    "listdir",
                    side_effect=fail_initial_listdir,
                ):
                    with self.assertRaisesRegex(OSError, "injected temporary listdir failure"):
                        vibe_memory_install.install_runtime(source, paths)

                self.assertEqual(injected, [True])
                self.assertEqual(
                    list((paths.install_root / "releases").glob(".1.0.0.tmp-*")),
                    [],
                )

            self.assertEqual(len(os.listdir("/dev/fd")), baseline)

    def test_cleanup_preserves_owned_temp_when_unknown_child_was_inserted(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            paths = self.make_paths(root)
            real_copy = vibe_memory_install._copy_release_content
            unknown_bytes = b"UNKNOWN CHILD\n"
            unknown_identity: list[tuple[int, int]] = []
            temporary_path: list[pathlib.Path] = []

            def insert_unknown_then_fail(
                source_snapshot: object,
                temporary_release: pathlib.Path,
            ) -> None:
                real_copy(source_snapshot, temporary_release)
                temporary = pathlib.Path(os.fspath(temporary_release))
                unknown = temporary / "unknown-child"
                unknown.write_bytes(unknown_bytes)
                metadata = unknown.stat()
                unknown_identity.append((metadata.st_dev, metadata.st_ino))
                temporary_path.append(temporary)
                raise OSError("simulated copy failure after unknown insertion")

            with mock.patch.object(
                vibe_memory_install,
                "_copy_release_content",
                side_effect=insert_unknown_then_fail,
            ):
                with self.assertRaisesRegex(
                    vibe_memory_install.TemporaryCleanupConflict,
                    "temporary cleanup conflict",
                ):
                    vibe_memory_install.install_runtime(source, paths)

            unknown = temporary_path[0] / "unknown-child"
            metadata = unknown.stat()
            self.assertEqual((metadata.st_dev, metadata.st_ino), unknown_identity[0])
            self.assertEqual(unknown.read_bytes(), unknown_bytes)
            self.assertTrue(temporary_path[0].is_dir())
            self.assertTrue((temporary_path[0] / "README.md").is_file())
            self.assertFalse((paths.install_root / "releases/1.0.0").exists())
            self.assertFalse(os.path.lexists(str(paths.install_root / "current")))

    def test_cleanup_never_deletes_unknown_child_inserted_after_inventory_check(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            paths = self.make_paths(root)
            real_copy = vibe_memory_install._copy_release_content
            real_delete = vibe_memory_install._delete_tree_contents_fd
            unknown_bytes = b"UNKNOWN LATE CHILD\n"
            unknown_identity: list[tuple[int, int]] = []
            temporary_path: list[pathlib.Path] = []

            def copy_then_fail(source_snapshot: object, temporary_release: pathlib.Path) -> None:
                real_copy(source_snapshot, temporary_release)
                temporary_path.append(pathlib.Path(os.fspath(temporary_release)))
                raise OSError("simulated copy failure before late insertion")

            def insert_unknown_at_delete(
                directory_fd: int,
                creation_ledger: dict[str, tuple[int, int, int]],
                prefix: pathlib.Path = pathlib.Path(),
            ) -> None:
                if not unknown_identity:
                    descriptor = os.open(
                        "unknown-late",
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=directory_fd,
                    )
                    try:
                        with os.fdopen(descriptor, "wb", closefd=False) as handle:
                            handle.write(unknown_bytes)
                        metadata = os.fstat(descriptor)
                        unknown_identity.append((metadata.st_dev, metadata.st_ino))
                    finally:
                        os.close(descriptor)
                real_delete(directory_fd, creation_ledger, prefix)

            with mock.patch.object(
                vibe_memory_install,
                "_copy_release_content",
                side_effect=copy_then_fail,
            ), mock.patch.object(
                vibe_memory_install,
                "_delete_tree_contents_fd",
                side_effect=insert_unknown_at_delete,
            ):
                with self.assertRaises(vibe_memory_install.TemporaryCleanupConflict):
                    vibe_memory_install.install_runtime(source, paths)

            unknown = temporary_path[0] / "unknown-late"
            metadata = unknown.stat()
            self.assertEqual((metadata.st_dev, metadata.st_ino), unknown_identity[0])
            self.assertEqual(unknown.read_bytes(), unknown_bytes)
            self.assertTrue(temporary_path[0].is_dir())
            self.assertFalse((paths.install_root / "releases/1.0.0").exists())
            self.assertFalse(os.path.lexists(str(paths.install_root / "current")))

    def test_cleanup_child_claim_preserves_replacement_after_final_identity_check(self) -> None:
        for kind in ("file", "directory"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as value:
                root = pathlib.Path(value)
                source = self.make_source(root)
                if kind == "directory":
                    (source / "docs/empty").mkdir()
                paths = self.make_paths(root)
                real_copy = vibe_memory_install._copy_release_content
                real_renameat = vibe_memory_install._darwin_renameat
                target_relative = pathlib.Path("README.md" if kind == "file" else "docs/empty")
                target_name = target_relative.name
                owned_name = f"{target_name}.owned"
                unknown_bytes = b"UNKNOWN CLAIM REPLACEMENT\n"
                unknown_identity: list[tuple[int, int]] = []
                injected: list[bool] = []
                temporary_path: list[pathlib.Path] = []

                def copy_then_fail(source_snapshot: object, temporary_release: pathlib.Path) -> None:
                    real_copy(source_snapshot, temporary_release)
                    temporary_path.append(pathlib.Path(os.fspath(temporary_release)))
                    raise OSError("simulated copy failure before child claim")

                def replace_before_child_claim(
                    source_parent_fd: int,
                    source_name: str,
                    destination_parent_fd: int,
                    destination_name: str,
                    flags: int,
                ) -> None:
                    if (
                        source_name == target_name
                        and destination_name.startswith(".cleanup-child-")
                        and not injected
                    ):
                        os.rename(
                            source_name,
                            owned_name,
                            src_dir_fd=source_parent_fd,
                            dst_dir_fd=source_parent_fd,
                        )
                        if kind == "file":
                            descriptor = os.open(
                                source_name,
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                0o600,
                                dir_fd=source_parent_fd,
                            )
                            try:
                                with os.fdopen(descriptor, "wb", closefd=False) as handle:
                                    handle.write(unknown_bytes)
                                metadata = os.fstat(descriptor)
                            finally:
                                os.close(descriptor)
                        else:
                            os.mkdir(source_name, 0o700, dir_fd=source_parent_fd)
                            descriptor = os.open(
                                source_name,
                                vibe_memory_install._DIRECTORY_OPEN_FLAGS,
                                dir_fd=source_parent_fd,
                            )
                            try:
                                marker = os.open(
                                    "marker",
                                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                    0o600,
                                    dir_fd=descriptor,
                                )
                                try:
                                    with os.fdopen(marker, "wb", closefd=False) as handle:
                                        handle.write(unknown_bytes)
                                finally:
                                    os.close(marker)
                                metadata = os.fstat(descriptor)
                            finally:
                                os.close(descriptor)
                        unknown_identity.append((metadata.st_dev, metadata.st_ino))
                        injected.append(True)
                    real_renameat(
                        source_parent_fd,
                        source_name,
                        destination_parent_fd,
                        destination_name,
                        flags,
                    )

                with mock.patch.object(
                    vibe_memory_install,
                    "_copy_release_content",
                    side_effect=copy_then_fail,
                ), mock.patch.object(
                    vibe_memory_install,
                    "_darwin_renameat",
                    side_effect=replace_before_child_claim,
                ):
                    with self.assertRaises(vibe_memory_install.TemporaryCleanupConflict):
                        vibe_memory_install.install_runtime(source, paths)

                target = temporary_path[0] / target_relative
                metadata = target.stat()
                self.assertEqual(injected, [True])
                self.assertEqual((metadata.st_dev, metadata.st_ino), unknown_identity[0])
                self.assertTrue((target.parent / owned_name).exists())
                if kind == "file":
                    self.assertEqual(target.read_bytes(), unknown_bytes)
                else:
                    self.assertEqual((target / "marker").read_bytes(), unknown_bytes)
                self.assertFalse((paths.install_root / "releases/1.0.0").exists())
                self.assertFalse(os.path.lexists(str(paths.install_root / "current")))

    def test_created_directory_open_failure_cleans_partial_temp_without_fd_leak(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            paths = self.make_paths(root)
            real_mkdir = vibe_memory_install.os.mkdir
            real_open = vibe_memory_install.os.open
            baseline = len(os.listdir("/dev/fd"))

            for _ in range(8):
                docs_parents: set[int] = set()
                injected: list[bool] = []

                def record_docs_mkdir(
                    path: pathlib.Path | str,
                    mode: int = 0o777,
                    *args: object,
                    **kwargs: object,
                ) -> None:
                    real_mkdir(path, mode, *args, **kwargs)
                    parent_fd = kwargs.get("dir_fd")
                    if path == "docs" and isinstance(parent_fd, int):
                        docs_parents.add(parent_fd)

                def fail_docs_open(
                    path: pathlib.Path | str,
                    flags: int,
                    *args: object,
                    **kwargs: object,
                ) -> int:
                    if (
                        path == "docs"
                        and kwargs.get("dir_fd") in docs_parents
                        and not injected
                    ):
                        injected.append(True)
                        raise OSError("injected created directory open failure")
                    return real_open(path, flags, *args, **kwargs)

                with mock.patch.object(
                    vibe_memory_install.os,
                    "mkdir",
                    side_effect=record_docs_mkdir,
                ), mock.patch.object(
                    vibe_memory_install.os,
                    "open",
                    side_effect=fail_docs_open,
                ):
                    with self.assertRaisesRegex(
                        OSError,
                        "injected created directory open failure",
                    ):
                        vibe_memory_install.install_runtime(source, paths)

                self.assertEqual(injected, [True])
                self.assertEqual(
                    list((paths.install_root / "releases").glob(".1.0.0.tmp-*")),
                    [],
                )

            self.assertEqual(len(os.listdir("/dev/fd")), baseline)

    def test_created_directory_replacement_between_stat_and_open_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            paths = self.make_paths(root)
            real_open = vibe_memory_install.os.open
            unknown_bytes = b"UNKNOWN DIRECTORY BETWEEN STAT AND OPEN\n"
            unknown_identity: list[tuple[int, int]] = []
            temporary_path: list[pathlib.Path] = []

            def replace_docs_before_open(
                path: pathlib.Path | str,
                flags: int,
                *args: object,
                **kwargs: object,
            ) -> int:
                parent_fd = kwargs.get("dir_fd")
                temporary = list(
                    (paths.install_root / "releases").glob(".1.0.0.tmp-*")
                )
                if (
                    path == "docs"
                    and isinstance(parent_fd, int)
                    and temporary
                    and not unknown_identity
                ):
                    os.rename(
                        "docs",
                        "docs.owned",
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
                    os.mkdir("docs", 0o700, dir_fd=parent_fd)
                    unknown_fd = real_open(
                        "docs",
                        vibe_memory_install._DIRECTORY_OPEN_FLAGS,
                        dir_fd=parent_fd,
                    )
                    try:
                        marker = os.open(
                            "marker",
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                            dir_fd=unknown_fd,
                        )
                        try:
                            with os.fdopen(marker, "wb", closefd=False) as handle:
                                handle.write(unknown_bytes)
                        finally:
                            os.close(marker)
                        metadata = os.fstat(unknown_fd)
                    finally:
                        os.close(unknown_fd)
                    unknown_identity.append((metadata.st_dev, metadata.st_ino))
                    temporary_path.append(temporary[0])
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(
                vibe_memory_install.os,
                "open",
                side_effect=replace_docs_before_open,
            ):
                with self.assertRaises(vibe_memory_install.TemporaryCleanupConflict):
                    vibe_memory_install.install_runtime(source, paths)

            docs = temporary_path[0] / "docs"
            metadata = docs.stat()
            self.assertEqual((metadata.st_dev, metadata.st_ino), unknown_identity[0])
            self.assertEqual((docs / "marker").read_bytes(), unknown_bytes)
            self.assertTrue((temporary_path[0] / "docs.owned").is_dir())
            self.assertFalse((paths.install_root / "releases/1.0.0").exists())
            self.assertFalse(os.path.lexists(str(paths.install_root / "current")))

    def test_descriptor_validation_failures_do_not_leak_install_fds(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            paths = self.make_paths(root)
            scripts_metadata = (source / "scripts").stat()
            scripts_identity = scripts_metadata.st_dev, scripts_metadata.st_ino
            real_fstat = vibe_memory_install.os.fstat
            real_open = vibe_memory_install.os.open
            real_fchmod = vibe_memory_install.os.fchmod

            source_baseline = len(os.listdir("/dev/fd"))
            for _ in range(8):
                injected: list[bool] = []

                def fail_source_fstat(descriptor: int) -> os.stat_result:
                    metadata = real_fstat(descriptor)
                    if (metadata.st_dev, metadata.st_ino) == scripts_identity and not injected:
                        injected.append(True)
                        raise OSError("injected source fstat failure")
                    return metadata

                with mock.patch.object(
                    vibe_memory_install.os,
                    "fstat",
                    side_effect=fail_source_fstat,
                ):
                    with self.assertRaisesRegex(OSError, "injected source fstat failure"):
                        vibe_memory_install.install_runtime(source, paths)
                self.assertEqual(injected, [True])
            self.assertEqual(len(os.listdir("/dev/fd")), source_baseline)

            temporary_baseline = len(os.listdir("/dev/fd"))
            for _ in range(8):
                temporary_fds: set[int] = set()

                def record_temporary_open(
                    path: pathlib.Path | str,
                    flags: int,
                    *args: object,
                    **kwargs: object,
                ) -> int:
                    descriptor = real_open(path, flags, *args, **kwargs)
                    if isinstance(path, str) and path.startswith(".1.0.0.tmp-"):
                        temporary_fds.add(descriptor)
                    return descriptor

                def fail_temporary_fstat(descriptor: int) -> os.stat_result:
                    if descriptor in temporary_fds:
                        temporary_fds.remove(descriptor)
                        raise OSError("injected temporary fstat failure")
                    return real_fstat(descriptor)

                with mock.patch.object(
                    vibe_memory_install.os,
                    "open",
                    side_effect=record_temporary_open,
                ), mock.patch.object(
                    vibe_memory_install.os,
                    "fstat",
                    side_effect=fail_temporary_fstat,
                ):
                    with self.assertRaisesRegex(
                        vibe_memory_install.TemporaryCleanupConflict,
                        "ownership could not be established",
                    ):
                        vibe_memory_install.install_runtime(source, paths)
            self.assertEqual(len(os.listdir("/dev/fd")), temporary_baseline)

            releases_baseline = len(os.listdir("/dev/fd"))
            for _ in range(8):
                releases_fds: set[int] = set()

                def record_releases_open(
                    path: pathlib.Path | str,
                    flags: int,
                    *args: object,
                    **kwargs: object,
                ) -> int:
                    descriptor = real_open(path, flags, *args, **kwargs)
                    if path == "releases":
                        releases_fds.add(descriptor)
                    return descriptor

                def fail_releases_fchmod(descriptor: int, mode: int) -> None:
                    if descriptor in releases_fds:
                        releases_fds.remove(descriptor)
                        raise OSError("injected releases fchmod failure")
                    real_fchmod(descriptor, mode)

                with mock.patch.object(
                    vibe_memory_install.os,
                    "open",
                    side_effect=record_releases_open,
                ), mock.patch.object(
                    vibe_memory_install.os,
                    "fchmod",
                    side_effect=fail_releases_fchmod,
                ):
                    with self.assertRaisesRegex(OSError, "injected releases fchmod failure"):
                        vibe_memory_install.install_runtime(source, paths)
            self.assertEqual(len(os.listdir("/dev/fd")), releases_baseline)

            install_root_baseline = len(os.listdir("/dev/fd"))
            for _ in range(8):
                install_root_fds: set[int] = set()

                def record_install_root_open(
                    path: pathlib.Path | str,
                    flags: int,
                    *args: object,
                    **kwargs: object,
                ) -> int:
                    descriptor = real_open(path, flags, *args, **kwargs)
                    if path == "VibeMemory":
                        install_root_fds.add(descriptor)
                    return descriptor

                def fail_install_root_fchmod(descriptor: int, mode: int) -> None:
                    if descriptor in install_root_fds:
                        install_root_fds.remove(descriptor)
                        raise OSError("injected install root fchmod failure")
                    real_fchmod(descriptor, mode)

                with mock.patch.object(
                    vibe_memory_install.os,
                    "open",
                    side_effect=record_install_root_open,
                ), mock.patch.object(
                    vibe_memory_install.os,
                    "fchmod",
                    side_effect=fail_install_root_fchmod,
                ):
                    with self.assertRaisesRegex(OSError, "injected install root fchmod failure"):
                        vibe_memory_install.install_runtime(source, paths)
            self.assertEqual(len(os.listdir("/dev/fd")), install_root_baseline)

    def test_verified_destination_swapped_before_chmod_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            paths = self.make_paths(root)
            destination = paths.install_root / "releases/1.0.0"
            preserved = paths.install_root / "releases/verified-winner"
            unknown = b"unknown replacement\n"
            real_verify_and_make_private_fd = vibe_memory_install._verify_and_make_private_fd
            injected: list[bool] = []

            def identical_winner(temporary: pathlib.Path, target: pathlib.Path) -> None:
                if target.name == "current":
                    os.rename(temporary, target)
                    return
                shutil.copytree(temporary, target)
                raise FileExistsError("destination race")

            def swap_after_destination_open(
                directory_fd: int,
                expected_entries: dict[str, tuple[str, bytes | None]],
            ) -> None:
                if destination.exists() and not injected:
                    os.rename(destination, preserved)
                    destination.mkdir(mode=0o755)
                    (destination / "unknown").write_bytes(unknown)
                    injected.append(True)
                real_verify_and_make_private_fd(directory_fd, expected_entries)

            with mock.patch.object(
                vibe_memory_install,
                "_atomic_rename_exclusive",
                side_effect=identical_winner,
            ), mock.patch.object(
                vibe_memory_install,
                "_verify_and_make_private_fd",
                side_effect=swap_after_destination_open,
            ):
                with self.assertRaises(FileExistsError):
                    vibe_memory_install.install_runtime(source, paths)

            self.assertEqual(injected, [True])
            self.assertEqual((destination / "unknown").read_bytes(), unknown)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o755)
            self.assertFalse(os.path.lexists(str(paths.install_root / "current")))

    def test_render_launch_agent_uses_template_contract_and_valid_plist(self) -> None:
        with tempfile.TemporaryDirectory(prefix="Vibe & Memory ") as value:
            paths = self.make_paths(pathlib.Path(value))

            text = vibe_memory_install.render_launch_agent(
                paths,
                port=8897,
                python_executable=sys.executable,
            )
            plist = plistlib.loads(text.encode("utf-8"))
            runtime = str(paths.install_root / "current")

            self.assertEqual(plist["Label"], "com.noema.vibe-memory")
            self.assertEqual(
                plist["ProgramArguments"],
                [os.path.abspath(sys.executable), runtime + "/scripts/memory_review_server.py"],
            )
            self.assertEqual(plist["EnvironmentVariables"], {
                "MEMORY_REVIEW_HOST": "127.0.0.1",
                "MEMORY_REVIEW_PORT": "8897",
            })
            self.assertIs(plist["KeepAlive"], True)
            self.assertIs(plist["RunAtLoad"], True)
            self.assertIn("Application Support/VibeMemory/current", text)
            self.assertIn("&amp;", text)
            self.assertNotIn("/Users/stephenbo", text)

    def test_template_retains_runtime_and_port_variables(self) -> None:
        template = (ROOT / "templates/macos/com.noema.vibe-memory.plist").read_text(encoding="utf-8")
        self.assertIn("${PYTHON}", template)
        self.assertIn("${RUNTIME}", template)
        self.assertIn("${PORT}", template)

    def test_render_launch_agent_rejects_invalid_ports(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            for port in (True, 0, -1, 65536, "8897"):
                with self.subTest(port=port), self.assertRaises(ValueError):
                    vibe_memory_install.render_launch_agent(paths, port=port)
            for port in (1, 65535):
                plistlib.loads(vibe_memory_install.render_launch_agent(paths, port=port).encode("utf-8"))

    def test_render_fails_closed_for_unknown_or_invalid_template_content(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            bad_templates = ("${UNKNOWN}", "<plist><broken></plist>")
            for template in bad_templates:
                with self.subTest(template=template), mock.patch.object(
                    vibe_memory_install, "_read_launch_agent_template", return_value=template
                ):
                    with self.assertRaises((KeyError, ValueError, plistlib.InvalidFileException)):
                        vibe_memory_install.render_launch_agent(paths)

    def test_install_launch_agent_is_atomic_and_rejects_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            target = pathlib.Path(value) / "home/Library/LaunchAgents/com.noema.vibe-memory.plist"

            result = vibe_memory_install.install_launch_agent(paths, "first\n")
            self.assertEqual(result, {"changed": True, "path": str(target)})
            self.assertEqual(target.read_text(encoding="utf-8"), "first\n")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(
                vibe_memory_install.install_launch_agent(paths, "first\n"),
                {"changed": False, "path": str(target)},
            )

            target.unlink()
            outside = pathlib.Path(value) / "outside"
            outside.write_text("preserve", encoding="utf-8")
            target.symlink_to(outside)
            with self.assertRaises(ValueError):
                vibe_memory_install.install_launch_agent(paths, "replacement\n")
            self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")

    def test_prepare_data_creates_private_defaults_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            first = vibe_memory_install.prepare_data(paths)
            self.assertTrue(all(item["status"] == "created" for item in first["files"]))
            long_memory = paths.personal_memory / "long.md"
            long_memory.write_text("approved content\n", encoding="utf-8")

            second = vibe_memory_install.prepare_data(paths)
            self.assertTrue(all(item["status"] == "existing" for item in second["files"]))
            self.assertEqual(long_memory.read_text(encoding="utf-8"), "approved content\n")
            registry = json.loads(paths.project_registry.read_text(encoding="utf-8"))
            self.assertEqual(registry, {"current_project": "", "projects": []})
            for path in (
                paths.personal_memory / "long.md",
                paths.personal_memory / "short.md",
                paths.personal_memory / "proposals.md",
                paths.project_registry,
            ):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_failed_update_keeps_previous_current_release(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            old_source = self.make_source(root / "old", {**MANIFEST, "app_version": "1.0.0"})
            new_source = self.make_source(root / "new", {**MANIFEST, "app_version": "1.1.0"})
            vibe_memory_install.install_runtime(old_source, paths)

            with self.assertRaises(vibe_memory_install.InstallError):
                vibe_memory_install.update(
                    new_source,
                    paths,
                    validation={"control_plane": "error"},
                )

            self.assertEqual(
                (paths.install_root / "current").resolve(),
                (paths.install_root / "releases" / "1.0.0").resolve(),
            )

    def test_rollback_switches_runtime_without_reverting_memory(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            for version in ("1.0.0", "1.1.0"):
                (paths.install_root / "releases" / version).mkdir(parents=True)
            (paths.install_root / "current").symlink_to("releases/1.1.0")
            state = paths.install_root / "state" / "install.json"
            state.parent.mkdir(parents=True)
            state.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "current_version": "1.1.0",
                        "previous_version": "1.0.0",
                        "hook_protocol_version": 1,
                        "data_schema_version": 1,
                        "port": 8897,
                        "installed_clients": ["codex"],
                    }
                ),
                encoding="utf-8",
            )
            memory = paths.personal_memory / "long.md"
            memory.parent.mkdir(parents=True)
            memory.write_text("approved after upgrade\n", encoding="utf-8")

            with mock.patch("vibe_memory_install.activate_launch_agent", return_value={"status": "healthy"}), \
                    mock.patch("vibe_memory_install.smoke_managed_hooks", return_value={"codex": {"ok": True}}):
                result = vibe_memory_install.rollback(paths)

            self.assertEqual(result["current_version"], "1.0.0")
            self.assertTrue(result["data_retained"])
            self.assertEqual(
                (paths.install_root / "current").resolve(),
                (paths.install_root / "releases" / "1.0.0").resolve(),
            )
            self.assertEqual(memory.read_text(encoding="utf-8"), "approved after upgrade\n")

    def test_uninstall_removes_only_managed_assets_and_requires_data_deletion_approval(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            home = paths.personal_memory.parents[1]
            release = paths.install_root / "releases" / "1.0.0"
            release.mkdir(parents=True)
            (paths.install_root / "current").symlink_to("releases/1.0.0")
            memory = paths.personal_memory / "long.md"
            memory.parent.mkdir(parents=True)
            memory.write_text("approved memory\n", encoding="utf-8")
            codex_hooks = home / ".codex" / "hooks.json"
            codex_hooks.parent.mkdir(parents=True, exist_ok=True)
            source = {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "custom-hook"}]}
                    ]
                }
            }
            codex_hooks.write_text(
                json.dumps(vibe_memory_hooks.merge_document(source, "codex", paths.install_root / "current")),
                encoding="utf-8",
            )
            plist = home / "Library" / "LaunchAgents" / "com.noema.vibe-memory.plist"
            plist.parent.mkdir(parents=True)
            plist.write_text(vibe_memory_install.render_launch_agent(paths), encoding="utf-8")

            with self.assertRaises(vibe_memory_install.InstallError):
                vibe_memory_install.uninstall(paths, remove_data=True)

            result = vibe_memory_install.uninstall(paths, remove_data=False)

            self.assertTrue(result["data_retained"])
            self.assertTrue(memory.exists())
            self.assertFalse(os.path.lexists(paths.install_root / "current"))
            self.assertFalse(plist.exists())
            text = codex_hooks.read_text(encoding="utf-8")
            self.assertIn("custom-hook", text)
            self.assertNotIn("vibe-memory hook", text)

    def test_installed_launcher_runs_without_a_source_cli_path(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            vibe_memory_install.install_runtime(ROOT, paths)
            vibe_memory_install.install_runtime_config(
                paths,
                port=8897,
                app_version="1.0.0",
                python_executable=sys.executable,
            )
            vibe_memory_install.install_launcher(paths)
            vibe_memory_install.prepare_data(paths)
            environment = os.environ.copy()
            environment.update({"HOME": str(root / "home"), "PYTHONDONTWRITEBYTECODE": "1"})
            completed = subprocess.run(
                [str(paths.launcher), "status"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                set(json.loads(completed.stdout)),
                {"runtime", "codex_hooks", "claude_hooks", "service", "data"},
            )
            self.assertNotIn("/Users/stephenbo", completed.stdout)


class FakeLaunchctlRunner:
    def __init__(self, results: list[subprocess.CompletedProcess[str]] | None = None) -> None:
        self.results = list(results or [])
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if self.results:
            return self.results.pop(0)
        return subprocess.CompletedProcess(command, 0, "", "")


class LaunchAgentLifecycleTest(unittest.TestCase):
    def make_paths(self, root: pathlib.Path) -> vibe_memory_paths.RuntimePaths:
        return vibe_memory_paths.for_home(root / "home")

    def test_launchctl_domain_uses_explicit_or_current_uid(self) -> None:
        self.assertEqual(vibe_memory_install.launchctl_domain(501), "gui/501")
        with mock.patch("vibe_memory_install.os.getuid", return_value=777):
            self.assertEqual(vibe_memory_install.launchctl_domain(), "gui/777")

    def test_activate_boots_out_absent_service_then_bootstraps_kickstarts_and_checks_identity(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            runner = FakeLaunchctlRunner([
                subprocess.CompletedProcess([], 3, "", "Boot-out failed: 3: No such process"),
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, "", ""),
            ])
            health = mock.Mock(return_value={
                "ok": True, "service": "vibe-memory", "app_version": "1.0.0"
            })

            result = vibe_memory_install.activate_launch_agent(
                paths, runner=runner, health=health, expected_version="1.0.0", uid=501,
                attempts=1, sleeper=lambda _delay: None,
            )

            self.assertEqual(result["status"], "healthy")
            self.assertEqual(runner.commands, [
                ["/bin/launchctl", "bootout", "gui/501/com.noema.vibe-memory"],
                ["/bin/launchctl", "bootstrap", "gui/501", str(paths.launch_agent)],
                ["/bin/launchctl", "kickstart", "-k", "gui/501/com.noema.vibe-memory"],
            ])

    def test_bootout_rejects_non_absent_failure(self) -> None:
        runner = FakeLaunchctlRunner([
            subprocess.CompletedProcess([], 5, "", "Boot-out failed: 5: Input/output error")
        ])
        with self.assertRaisesRegex(vibe_memory_install.InstallError, "bootout"):
            vibe_memory_install.bootout_launch_agent(runner=runner, uid=501)

    def test_activate_health_failure_boots_out_new_service(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            runner = FakeLaunchctlRunner()
            with self.assertRaisesRegex(vibe_memory_install.InstallError, "health"):
                vibe_memory_install.activate_launch_agent(
                    paths, runner=runner,
                    health=lambda: {"ok": True, "service": "other", "app_version": "1.0.0"},
                    expected_version="1.0.0", uid=501, attempts=2,
                    sleeper=lambda _delay: None,
                )
            self.assertEqual(runner.commands[-1], [
                "/bin/launchctl", "bootout", "gui/501/com.noema.vibe-memory"
            ])

    def test_smoke_managed_hooks_passes_harmless_json_without_shell(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, '{"status":"ok"}\n', ""))
            result = vibe_memory_install.smoke_managed_hooks(
                paths, ["codex", "claude-code"], runner=runner
            )
            self.assertTrue(result["codex"]["ok"])
            self.assertTrue(result["claude"]["ok"])
            self.assertEqual([call.args[0][-5:] for call in runner.call_args_list], [
                ["hook", "--agent", "codex", "--event", "SessionStart"],
                ["hook", "--agent", "claude-code", "--event", "SessionStart"],
            ])
            self.assertTrue(all(call.kwargs["input"] == "{}" for call in runner.call_args_list))

    def test_smoke_managed_hooks_rejects_fail_open_degraded_and_unknown_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            for status in ("degraded", "error", "handled", None):
                payload = {} if status is None else {"status": status}
                runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), ""))
                with self.subTest(status=status), self.assertRaisesRegex(
                    vibe_memory_install.InstallError, "unsuccessful status"
                ):
                    vibe_memory_install.smoke_managed_hooks(paths, ["codex"], runner=runner)

    def test_smoke_managed_hooks_accepts_router_success_and_duplicate_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            for status in ("ok", "duplicate"):
                runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({"status": status}), ""))
                with self.subTest(status=status):
                    result = vibe_memory_install.smoke_managed_hooks(paths, ["codex"], runner=runner)
                self.assertEqual(result["codex"]["status"], status)

    def test_smoke_managed_hooks_rejects_oversized_stdout_and_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            cases = (
                ("x" * (vibe_memory_install.HOOK_SMOKE_OUTPUT_LIMIT + 1), ""),
                (json.dumps({"status": "ok"}), "x" * (vibe_memory_install.HOOK_SMOKE_OUTPUT_LIMIT + 1)),
            )
            for stdout, stderr in cases:
                runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout, stderr))
                with self.subTest(stderr=bool(stderr)), self.assertRaisesRegex(
                    vibe_memory_install.InstallError, "output limit"
                ):
                    vibe_memory_install.smoke_managed_hooks(paths, ["codex"], runner=runner)

    def test_update_renders_matching_assets_restarts_and_smokes_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            source = RuntimeInstallTest().make_source(root, {**MANIFEST, "app_version": "1.1.0"})
            (paths.install_root / "releases/1.0.0").mkdir(parents=True)
            (paths.install_root / "current").symlink_to("releases/1.0.0")
            vibe_memory_install.install_runtime_config(paths, port=9123, app_version="1.0.0", python_executable=sys.executable)
            vibe_memory_install.write_install_state(paths, vibe_memory_install._install_state_document(
                current_version="1.0.0", previous_version=None, port=9123,
                installed_clients=["codex"], python_executable=sys.executable,
            ))
            with mock.patch("vibe_memory_install.activate_launch_agent", return_value={"status": "healthy"}) as activate, \
                    mock.patch("vibe_memory_install.smoke_managed_hooks", return_value={"codex": {"ok": True}}) as smoke, \
                    mock.patch("vibe_memory_hooks.repair", return_value={"status": "created"}):
                result = vibe_memory_install.update(source, paths, validation={"control": "ok"})
            self.assertEqual(result["current_version"], "1.1.0")
            activate.assert_called_once_with(paths, expected_version="1.1.0")
            smoke.assert_called_once_with(paths, ["codex"])
            self.assertEqual(vibe_memory_install.read_runtime_config(paths)["app_version"], "1.1.0")
            self.assertIn("/current/scripts/memory_review_server.py", paths.launch_agent.read_text(encoding="utf-8"))

    def test_update_preserves_explicit_empty_clients_without_repairing_codex(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            source = RuntimeInstallTest().make_source(
                root, {**MANIFEST, "app_version": "1.1.0"}
            )
            (paths.install_root / "releases/1.0.0").mkdir(parents=True)
            (paths.install_root / "current").symlink_to("releases/1.0.0")
            vibe_memory_install.install_runtime_config(
                paths, port=9123, app_version="1.0.0", python_executable=sys.executable
            )
            vibe_memory_install.write_install_state(
                paths,
                vibe_memory_install._install_state_document(
                    current_version="1.0.0", previous_version=None, port=9123,
                    installed_clients=[], python_executable=sys.executable,
                ),
            )
            with mock.patch(
                "vibe_memory_install.activate_launch_agent", return_value={"status": "healthy"}
            ), mock.patch("vibe_memory_install.smoke_managed_hooks", return_value={}) as smoke, \
                    mock.patch("vibe_memory_hooks.repair") as repair:
                vibe_memory_install.update(
                    source, paths, installed_clients=[], validation={"control": "ok"}
                )
            self.assertEqual(
                vibe_memory_install.read_install_state(paths)["installed_clients"], []
            )
            repair.assert_not_called()
            smoke.assert_called_once_with(paths, [])

    def test_update_health_failure_removes_new_release_and_restores_old_service(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            source = RuntimeInstallTest().make_source(root, {**MANIFEST, "app_version": "1.1.0"})
            (paths.install_root / "releases/1.0.0").mkdir(parents=True)
            (paths.install_root / "current").symlink_to("releases/1.0.0")
            vibe_memory_install.install_runtime_config(paths, port=9123, app_version="1.0.0", python_executable=sys.executable)
            vibe_memory_install.write_install_state(paths, vibe_memory_install._install_state_document(current_version="1.0.0", previous_version=None, port=9123, installed_clients=["codex"], python_executable=sys.executable))
            with mock.patch("vibe_memory_install.activate_launch_agent", side_effect=[vibe_memory_install.InstallError("health"), {"status": "healthy"}]) as activate, mock.patch("vibe_memory_hooks.repair", return_value={"status": "created"}):
                with self.assertRaises(vibe_memory_install.InstallError):
                    vibe_memory_install.update(source, paths, validation={"control": "ok"})
            self.assertEqual(os.readlink(paths.install_root / "current"), "releases/1.0.0")
            self.assertFalse((paths.install_root / "releases/1.1.0").exists())
            self.assertEqual(activate.call_args_list[-1], mock.call(paths, expected_version="1.0.0"))

    def test_uninstall_boots_out_before_removing_owned_releases_and_runtime_home_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            home = paths.personal_memory.parents[1]
            vibe_memory_install.install_runtime(RuntimeInstallTest().make_source(root), paths)
            release = paths.install_root / "releases/1.0.0"
            vibe_memory_install.write_install_state(paths, vibe_memory_install._install_state_document(
                current_version="1.0.0", previous_version=None, port=9123,
                installed_clients=["codex"], python_executable=sys.executable,
            ))
            hook = home / ".codex/hooks.json"
            hook.parent.mkdir(parents=True)
            hook.write_text(json.dumps(vibe_memory_hooks.merge_document({"hooks": {}}, "codex", paths.launcher)), encoding="utf-8")
            order: list[str] = []
            with mock.patch("vibe_memory_install.bootout_launch_agent", side_effect=lambda: order.append("bootout")), \
                    mock.patch("vibe_memory_hooks.uninstall", side_effect=lambda path, _runtime: order.append(str(path)) or {"status": "removed"}):
                result = vibe_memory_install.uninstall(paths)
            self.assertEqual(order[0], "bootout")
            self.assertIn(str(hook), order)
            self.assertFalse(release.exists())
            self.assertTrue(result["data_retained"])

    def test_uninstall_without_valid_state_removes_all_owned_releases_and_exact_home_hook(self) -> None:
        for state_mode in ("missing", "malformed"):
            with self.subTest(state=state_mode), tempfile.TemporaryDirectory() as value:
                root = pathlib.Path(value)
                paths = self.make_paths(root)
                source = RuntimeInstallTest().make_source(root)
                vibe_memory_install.install_runtime(source, paths)
                for version in ("1.1.0", "2.0.0"):
                    release = paths.install_root / "releases" / version
                    shutil.copytree(paths.install_root / "releases/1.0.0", release)
                    manifest = json.loads((release / "release.json").read_text(encoding="utf-8"))
                    manifest["app_version"] = version
                    (release / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
                state = vibe_memory_install.install_state_path(paths)
                if state_mode == "malformed":
                    state.parent.mkdir(parents=True, exist_ok=True)
                    state.write_text("{bad", encoding="utf-8")
                hook = paths.personal_memory.parents[1] / ".codex/hooks.json"
                hook.parent.mkdir(parents=True)
                hook.write_text(json.dumps(vibe_memory_hooks.merge_document({"hooks": {}}, "codex", paths.launcher)), encoding="utf-8")
                with mock.patch("vibe_memory_install.bootout_launch_agent", return_value={"status": "booted_out"}):
                    result = vibe_memory_install.uninstall(paths)
                self.assertEqual(list((paths.install_root / "releases").glob("*")), [])
                self.assertNotIn(str(paths.launcher), hook.read_text(encoding="utf-8"))
                self.assertEqual(result["preserved_releases"], [])

    def test_uninstall_preserves_and_reports_unknown_release_without_deleting_it(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            unknown = paths.install_root / "releases/custom"
            unknown.mkdir(parents=True)
            (unknown / "sentinel").write_text("keep", encoding="utf-8")
            with mock.patch("vibe_memory_install.bootout_launch_agent", return_value={"status": "booted_out"}):
                result = vibe_memory_install.uninstall(paths)
            self.assertTrue((unknown / "sentinel").exists())
            self.assertEqual(result["preserved_releases"], [str(unknown)])

    def test_uninstall_never_deletes_semver_release_with_valid_manifest_and_user_extra(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            source = RuntimeInstallTest().make_source(root)
            vibe_memory_install.install_runtime(source, paths)
            release = paths.install_root / "releases/1.0.0"
            sentinel = release / "USER_SENTINEL"
            sentinel.write_text("keep", encoding="utf-8")
            sentinel.chmod(0o600)
            with mock.patch("vibe_memory_install.bootout_launch_agent", return_value={"status": "booted_out"}):
                result = vibe_memory_install.uninstall(paths)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            self.assertIn(str(release), result["preserved_releases"])

    def test_repair_health_failure_restores_all_managed_files_and_restarts_old_service(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            vibe_memory_install.install_runtime(RuntimeInstallTest().make_source(root), paths)
            vibe_memory_install.install_runtime_config(paths, port=9123, app_version="1.0.0", python_executable=sys.executable)
            vibe_memory_install.install_launcher(paths, python_executable=sys.executable)
            vibe_memory_install.install_launch_agent(paths, vibe_memory_install.render_launch_agent(paths, port=9123, python_executable=sys.executable))
            vibe_memory_install.write_install_state(paths, vibe_memory_install._install_state_document(
                current_version="1.0.0", previous_version=None, port=9123,
                installed_clients=["codex"], python_executable=sys.executable,
            ))
            hook = paths.personal_memory.parents[1] / ".codex/hooks.json"
            hook.parent.mkdir(parents=True)
            hook.write_text('{"custom":true}\n', encoding="utf-8")
            managed = [paths.install_root / "config.json", paths.launcher, paths.launch_agent, hook, vibe_memory_install.install_state_path(paths)]
            before = {path: path.read_bytes() for path in managed}
            with mock.patch("vibe_memory_install.activate_launch_agent", side_effect=[
                vibe_memory_install.InstallError("health failed"), {"status": "healthy"}
            ]) as activate:
                with self.assertRaisesRegex(vibe_memory_install.InstallError, "repair failed"):
                    vibe_memory_install.repair(paths)
            self.assertEqual(before, {path: path.read_bytes() for path in managed})
            self.assertEqual(activate.call_args_list[-1], mock.call(paths, expected_version="1.0.0"))

    def test_repair_smoke_failure_preserves_concurrent_config_and_reports_rollback_failure(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            vibe_memory_install.install_runtime(RuntimeInstallTest().make_source(root), paths)
            vibe_memory_install.install_runtime_config(paths, port=9123, app_version="1.0.0", python_executable=sys.executable)
            vibe_memory_install.write_install_state(paths, vibe_memory_install._install_state_document(current_version="1.0.0", previous_version=None, port=9123, installed_clients=["codex"], python_executable=sys.executable))
            config = paths.install_root / "config.json"
            concurrent = b'{"user":"concurrent"}\n'
            def concurrent_smoke(*_args: object, **_kwargs: object) -> object:
                config.write_bytes(concurrent)
                raise vibe_memory_install.InstallError("smoke failed")
            with mock.patch("vibe_memory_install.activate_launch_agent", return_value={"status": "healthy"}), mock.patch("vibe_memory_install.smoke_managed_hooks", side_effect=concurrent_smoke), mock.patch("vibe_memory_hooks.repair", return_value={"status": "created"}):
                with self.assertRaisesRegex(vibe_memory_install.InstallError, "rollback failed"):
                    vibe_memory_install.repair(paths)
            self.assertEqual(config.read_bytes(), concurrent)

    def test_repair_rebuilds_missing_or_malformed_config_and_state_from_current_release(self) -> None:
        for config_mode, state_mode in (("missing", "missing"), ("malformed", "missing"), ("missing", "malformed"), ("malformed", "malformed")):
            with self.subTest(config=config_mode, state=state_mode), tempfile.TemporaryDirectory() as value:
                root = pathlib.Path(value)
                paths = self.make_paths(root)
                source = RuntimeInstallTest().make_source(root)
                vibe_memory_install.install_runtime(source, paths)
                config = paths.install_root / "config.json"
                state = vibe_memory_install.install_state_path(paths)
                if config_mode == "malformed":
                    config.write_text("{bad", encoding="utf-8")
                if state_mode == "malformed":
                    state.parent.mkdir(parents=True, exist_ok=True)
                    state.write_text("{bad", encoding="utf-8")
                with mock.patch("vibe_memory_install.activate_launch_agent", return_value={"status": "healthy"}), mock.patch("vibe_memory_install.smoke_managed_hooks", return_value={"codex": {"ok": True}}), mock.patch("vibe_memory_hooks.repair", return_value={"status": "created"}):
                    result = vibe_memory_install.repair(paths)
                self.assertEqual(result["status"], "repaired")
                rebuilt_config = vibe_memory_install.read_runtime_config(paths)
                rebuilt_state = vibe_memory_install.read_install_state(paths)
                self.assertEqual(rebuilt_config["app_version"], "1.0.0")
                self.assertEqual(rebuilt_state["current_version"], "1.0.0")
                self.assertEqual(rebuilt_state["installed_clients"], ["codex"])

    def test_rollback_health_failure_restores_assets_state_current_and_restarts_original(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            for version in ("1.0.0", "1.1.0"):
                (paths.install_root / "releases" / version).mkdir(parents=True)
            current = paths.install_root / "current"
            current.symlink_to("releases/1.1.0")
            vibe_memory_install.install_runtime_config(paths, port=9123, app_version="1.1.0", python_executable=sys.executable)
            vibe_memory_install.install_launcher(paths, python_executable=sys.executable)
            vibe_memory_install.install_launch_agent(paths, vibe_memory_install.render_launch_agent(paths, port=9123, python_executable=sys.executable))
            vibe_memory_install.write_install_state(paths, vibe_memory_install._install_state_document(current_version="1.1.0", previous_version="1.0.0", port=9123, installed_clients=["codex"], python_executable=sys.executable))
            hook = paths.personal_memory.parents[1] / ".codex/hooks.json"
            hook.parent.mkdir(parents=True)
            hook.write_text('{"custom":true}\n', encoding="utf-8")
            managed = [paths.install_root / "config.json", paths.launcher, paths.launch_agent, hook, vibe_memory_install.install_state_path(paths)]
            before = {path: path.read_bytes() for path in managed}
            with mock.patch("vibe_memory_install.activate_launch_agent", side_effect=[vibe_memory_install.InstallError("health failed"), {"status": "healthy"}]) as activate:
                with self.assertRaisesRegex(vibe_memory_install.InstallError, "rollback failed"):
                    vibe_memory_install.rollback(paths)
            self.assertEqual(os.readlink(current), "releases/1.1.0")
            self.assertEqual(before, {path: path.read_bytes() for path in managed})
            self.assertEqual(activate.call_args_list[-1], mock.call(paths, expected_version="1.1.0"))


if __name__ == "__main__":
    unittest.main()
