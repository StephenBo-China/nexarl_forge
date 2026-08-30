from __future__ import annotations

import json
import concurrent.futures
import contextlib
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

    def test_install_excludes_python_bytecode_caches_from_release(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            cache = source / "scripts/__pycache__"
            cache.mkdir()
            (cache / "server.cpython-314.pyc").write_bytes(b"derived bytecode")
            (source / "scripts/ignored.pyc").write_bytes(b"derived bytecode")
            paths = self.make_paths(root)

            vibe_memory_install.install_runtime(source, paths)

            release = paths.install_root / "releases/1.0.0/scripts"
            self.assertFalse((release / "__pycache__").exists())
            self.assertFalse((release / "ignored.pyc").exists())

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
            self.assertIn(
                "export PYTHONDONTWRITEBYTECODE=1",
                paths.launcher.read_text(encoding="utf-8"),
            )

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

    def test_read_install_state_rejects_empty_document(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            state_path = vibe_memory_install.install_state_path(paths)
            state_path.parent.mkdir(parents=True)
            state_path.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(vibe_memory_install.InstallError):
                vibe_memory_install.read_install_state(paths)

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
                "HOME": str(pathlib.Path(paths.personal_memory).parents[1]),
                "MEMORY_REVIEW_HOST": "127.0.0.1",
                "MEMORY_REVIEW_PORT": "8897",
                "PYTHONDONTWRITEBYTECODE": "1",
            })
            self.assertIs(plist["KeepAlive"], True)
            self.assertIs(plist["RunAtLoad"], True)
            self.assertIn("Application Support/VibeMemory/current", text)
            self.assertIn("&amp;", text)
            self.assertNotIn("/Users/stephenbo", text)

    def test_manual_launch_agent_has_no_next_login_or_keepalive_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            manual = plistlib.loads(
                vibe_memory_install.render_launch_agent(
                    paths,
                    port=9123,
                    python_executable=sys.executable,
                    run_at_load=False,
                ).encode("utf-8")
            )

        self.assertFalse(manual["RunAtLoad"])
        self.assertFalse(manual["KeepAlive"])
        self.assertFalse(manual["RunAtLoad"] or manual["KeepAlive"])

    def test_template_retains_runtime_and_port_variables(self) -> None:
        template = (ROOT / "templates/macos/com.noema.vibe-memory.plist").read_text(encoding="utf-8")
        self.assertIn("${PYTHON}", template)
        self.assertIn("${RUNTIME}", template)
        self.assertIn("${PORT}", template)
        self.assertIn("${HOME}", template)
        self.assertIn("PYTHONDONTWRITEBYTECODE", template)

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

    def test_uninstall_rejects_directory_data_target_before_any_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            memory = paths.personal_memory / "long.md"
            memory.parent.mkdir(parents=True)
            memory.write_text("keep\n", encoding="utf-8")
            current = paths.install_root / "current"
            current.parent.mkdir(parents=True)
            current.write_text("runtime sentinel\n", encoding="utf-8")
            with mock.patch(
                "vibe_memory_install.bootout_launch_agent"
            ) as bootout, mock.patch("vibe_memory_hooks.uninstall") as hooks:
                with self.assertRaisesRegex(
                    vibe_memory_install.InstallError, "regular file"
                ):
                    vibe_memory_install.uninstall(
                        paths,
                        remove_data=True,
                        approved_data_deletion=True,
                        data_paths=[paths.personal_memory],
                    )

            bootout.assert_not_called()
            hooks.assert_not_called()
            self.assertEqual(memory.read_text(encoding="utf-8"), "keep\n")
            self.assertEqual(current.read_text(encoding="utf-8"), "runtime sentinel\n")

    def test_uninstall_rejects_symlink_data_target_before_any_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            target = paths.project_registry
            target.parent.mkdir(parents=True)
            outside = pathlib.Path(value) / "outside.json"
            outside.write_text("keep\n", encoding="utf-8")
            target.symlink_to(outside)
            with mock.patch(
                "vibe_memory_install.bootout_launch_agent"
            ) as bootout, mock.patch("vibe_memory_hooks.uninstall") as hooks:
                with self.assertRaisesRegex(
                    vibe_memory_install.InstallError, "symlink"
                ):
                    vibe_memory_install.uninstall(
                        paths,
                        remove_data=True,
                        approved_data_deletion=True,
                        data_paths=[target],
                    )

            bootout.assert_not_called()
            hooks.assert_not_called()
            self.assertEqual(outside.read_text(encoding="utf-8"), "keep\n")

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
    def __init__(
        self,
        results: list[subprocess.CompletedProcess[str]] | None = None,
        *,
        by_kind: dict[str, list[subprocess.CompletedProcess[str]]] | None = None,
    ) -> None:
        self.results = list(results or [])
        self.by_kind = {kind: list(items) for kind, items in (by_kind or {}).items()}
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        kind = command[1] if len(command) > 1 else ""
        if self.by_kind.get(kind):
            return self.by_kind[kind].pop(0)
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

    def launchctl_print(self, paths: vibe_memory_paths.RuntimePaths, *, managed: bool) -> subprocess.CompletedProcess[str]:
        path = paths.launch_agent if managed else pathlib.Path("/tmp/foreign.plist")
        return subprocess.CompletedProcess([], 0, f"path = {path}\n", "")

    def absent_print(self) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 113, "", "Could not find service")

    def test_bootout_prints_identity_and_refuses_foreign_without_bootout(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            runner = FakeLaunchctlRunner([self.launchctl_print(paths, managed=False)])
            with self.assertRaisesRegex(vibe_memory_install.InstallError, "foreign"):
                vibe_memory_install.bootout_launch_agent(paths, runner=runner, uid=501)
            self.assertEqual(runner.commands, [[
                "/bin/launchctl", "print", "gui/501/com.noema.vibe-memory"
            ]])

    def test_bootout_refuses_path_prefix_collision_without_bootout(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            runner = FakeLaunchctlRunner([subprocess.CompletedProcess(
                [], 0, f"path = {paths.launch_agent}.foreign\n", ""
            )])
            with self.assertRaisesRegex(vibe_memory_install.InstallError, "foreign"):
                vibe_memory_install.bootout_launch_agent(paths, runner=runner, uid=501)
            self.assertEqual([command[1] for command in runner.commands], ["print"])

    def test_bootout_managed_print_then_bootout_and_absent_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            managed = FakeLaunchctlRunner(by_kind={
                "print": [self.launchctl_print(paths, managed=True)] * 2,
                "bootout": [subprocess.CompletedProcess([], 0, "", "")],
            })
            self.assertEqual(vibe_memory_install.bootout_launch_agent(paths, runner=managed, uid=501)["status"], "booted_out")
            self.assertEqual([command[1] for command in managed.commands], ["print", "print", "bootout"])
            absent = FakeLaunchctlRunner([self.absent_print()])
            self.assertTrue(vibe_memory_install.bootout_launch_agent(paths, runner=absent, uid=501)["absent"])
            self.assertEqual(len(absent.commands), 1)

    def test_bootout_refuses_identity_replacement_after_print_before_bootout(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            runner = FakeLaunchctlRunner(by_kind={
                "print": [
                    self.launchctl_print(paths, managed=True),
                    self.launchctl_print(paths, managed=False),
                ],
            })
            with self.assertRaisesRegex(vibe_memory_install.InstallError, "foreign"):
                vibe_memory_install.bootout_launch_agent(paths, runner=runner, uid=501)
            self.assertEqual([command[1] for command in runner.commands], ["print", "print"])

    def test_bootout_fails_closed_for_ambiguous_or_failed_identity_print(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            cases = (
                ("ambiguous", subprocess.CompletedProcess([], 0, "", ""), "foreign"),
                ("unexpected return code", subprocess.CompletedProcess([], 5, "", "Input/output error"), "print failed"),
            )
            for name, result, message in cases:
                runner = FakeLaunchctlRunner([result])
                with self.subTest(name=name), self.assertRaisesRegex(
                    vibe_memory_install.InstallError, message
                ):
                    vibe_memory_install.bootout_launch_agent(paths, runner=runner, uid=501)
                self.assertEqual([command[1] for command in runner.commands], ["print"])

    def test_bootout_fails_closed_when_identity_print_times_out(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            runner = mock.Mock(side_effect=subprocess.TimeoutExpired("launchctl print", 15))
            with self.assertRaisesRegex(vibe_memory_install.InstallError, "launchctl print failed"):
                vibe_memory_install.bootout_launch_agent(paths, runner=runner, uid=501)
            self.assertEqual(runner.call_count, 1)

    def test_activate_absent_precheck_then_foreign_race_never_boots_out(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            runner = FakeLaunchctlRunner([
                self.absent_print(),
                self.launchctl_print(paths, managed=False),
            ])
            with self.assertRaisesRegex(vibe_memory_install.InstallError, "foreign"):
                vibe_memory_install.activate_launch_agent(
                    paths, runner=runner, expected_version="1.0.0", uid=501,
                    attempts=1, sleeper=lambda _delay: None,
                )
            self.assertEqual([command[1] for command in runner.commands], ["print", "print"])

    def test_uninstall_foreign_service_refuses_before_removing_assets(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            vibe_memory_install.install_runtime(RuntimeInstallTest().make_source(root), paths)
            release = paths.install_root / "releases/1.0.0/release.json"
            runner = FakeLaunchctlRunner([self.launchctl_print(paths, managed=False)])
            with self.assertRaisesRegex(vibe_memory_install.InstallError, "foreign"):
                vibe_memory_install.uninstall(paths, runner=runner)
            self.assertTrue(release.exists())
            self.assertEqual([command[1] for command in runner.commands], ["print"])

    def test_activate_boots_out_absent_service_then_bootstraps_kickstarts_and_checks_identity(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            runner = FakeLaunchctlRunner([
                self.absent_print(),
                self.absent_print(),
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
                ["/bin/launchctl", "print", "gui/501/com.noema.vibe-memory"],
                ["/bin/launchctl", "print", "gui/501/com.noema.vibe-memory"],
                ["/bin/launchctl", "bootstrap", "gui/501", str(paths.launch_agent)],
                ["/bin/launchctl", "kickstart", "-k", "gui/501/com.noema.vibe-memory"],
            ])

    def test_bootout_rejects_non_absent_failure(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            runner = FakeLaunchctlRunner(by_kind={
                "print": [self.launchctl_print(paths, managed=True)] * 2,
                "bootout": [subprocess.CompletedProcess([], 5, "", "Boot-out failed: 5: Input/output error")],
            })
            with self.assertRaisesRegex(vibe_memory_install.InstallError, "bootout"):
                vibe_memory_install.bootout_launch_agent(paths, runner=runner, uid=501)

    def test_activate_health_failure_boots_out_new_service(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            runner = FakeLaunchctlRunner(by_kind={
                "print": [
                    self.absent_print(),
                    self.absent_print(),
                    self.launchctl_print(paths, managed=True),
                    self.launchctl_print(paths, managed=True),
                ],
                "bootstrap": [subprocess.CompletedProcess([], 0, "", "")],
                "kickstart": [subprocess.CompletedProcess([], 0, "", "")],
                "bootout": [subprocess.CompletedProcess([], 0, "", "")],
            })
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

    def test_update_refuses_to_replace_current_rebound_after_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = RuntimeInstallTest().make_paths(root)
            source = RuntimeInstallTest().make_source(root, {**MANIFEST, "app_version": "1.1.0"})
            (paths.install_root / "releases/1.0.0").mkdir(parents=True)
            (paths.install_root / "releases/2.0.0").mkdir(parents=True)
            current = paths.install_root / "current"
            current.symlink_to("releases/1.0.0")
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
            real_runtime_port = vibe_memory_install._runtime_port

            def replace_current_before_activation(*args: object, **kwargs: object) -> int:
                current.unlink()
                current.symlink_to("releases/2.0.0")
                return real_runtime_port(*args, **kwargs)

            with mock.patch(
                "vibe_memory_install._runtime_port",
                side_effect=replace_current_before_activation,
            ), mock.patch(
                "vibe_memory_install.activate_launch_agent",
            ) as activate:
                with self.assertRaisesRegex(
                    vibe_memory_install.InstallError, "concurrently"
                ):
                    vibe_memory_install.update(source, paths, validation={"control": "ok"})

            self.assertEqual(os.readlink(current), "releases/2.0.0")
            activate.assert_not_called()

    def test_update_removes_committed_new_current_when_original_current_was_absent(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = RuntimeInstallTest().make_paths(root)
            source = RuntimeInstallTest().make_source(root, {**MANIFEST, "app_version": "1.1.0"})
            real_activate = vibe_memory_install._activate_managed_version

            def activate_current(*args: object, **kwargs: object) -> object:
                return real_activate(*args, **kwargs)

            with mock.patch(
                "vibe_memory_install._activate_managed_version",
                side_effect=activate_current,
            ), mock.patch(
                "vibe_memory_install.install_launch_agent",
                side_effect=vibe_memory_install.InstallError("plist"),
            ), mock.patch("vibe_memory_install.bootout_launch_agent"):
                with self.assertRaises(vibe_memory_install.InstallError):
                    vibe_memory_install.update(
                        source, paths, installed_clients=[], validation={"control": "ok"}
                    )

            self.assertFalse(os.path.lexists(paths.install_root / "current"))

    def test_update_restarts_old_service_when_activation_stops_it_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = RuntimeInstallTest().make_paths(root)
            source = RuntimeInstallTest().make_source(root, {**MANIFEST, "app_version": "1.1.0"})
            (paths.install_root / "releases/1.0.0").mkdir(parents=True)
            (paths.install_root / "current").symlink_to("releases/1.0.0")
            vibe_memory_install.install_runtime_config(
                paths, port=9123, app_version="1.0.0", python_executable=sys.executable
            )
            with mock.patch(
                "vibe_memory_install._activate_managed_version",
                side_effect=vibe_memory_install.InstallError("current CAS"),
            ), mock.patch(
                "vibe_memory_install.bootout_launch_agent",
                return_value={"status": "booted_out"},
            ), mock.patch(
                "vibe_memory_install.activate_launch_agent",
                return_value={"status": "healthy"},
            ) as activate:
                with self.assertRaises(vibe_memory_install.InstallError):
                    vibe_memory_install.update(
                        source, paths, installed_clients=[], validation={"control": "ok"}
                    )

            self.assertEqual(
                activate.call_args_list,
                [mock.call(paths, expected_version="1.0.0")],
            )

    def test_update_does_not_start_absent_old_service_during_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = RuntimeInstallTest().make_paths(root)
            source = RuntimeInstallTest().make_source(root, {**MANIFEST, "app_version": "1.1.0"})
            (paths.install_root / "releases/1.0.0").mkdir(parents=True)
            (paths.install_root / "current").symlink_to("releases/1.0.0")
            vibe_memory_install.install_runtime_config(
                paths, port=9123, app_version="1.0.0", python_executable=sys.executable
            )
            with mock.patch(
                "vibe_memory_install._activate_managed_version",
                side_effect=vibe_memory_install.InstallError("current CAS"),
            ), mock.patch(
                "vibe_memory_install.bootout_launch_agent",
                return_value={"status": "absent"},
            ), mock.patch("vibe_memory_install.activate_launch_agent") as activate:
                with self.assertRaises(vibe_memory_install.InstallError):
                    vibe_memory_install.update(
                        source, paths, installed_clients=[], validation={"control": "ok"}
                    )

            activate.assert_not_called()

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
            with mock.patch("vibe_memory_install.bootout_launch_agent", side_effect=lambda _paths, **_kwargs: order.append("bootout")), \
                    mock.patch("vibe_memory_hooks.uninstall", side_effect=lambda path, _runtime, **_kwargs: order.append(str(path)) or {"status": "removed"}):
                result = vibe_memory_install.uninstall(paths)
            self.assertEqual(order[0], "bootout")
            self.assertIn(str(hook), order)
            self.assertFalse(release.exists())
            self.assertTrue(result["data_retained"])

    def test_uninstall_without_state_removes_all_owned_releases_and_exact_home_hook(self) -> None:
        for state_mode in ("missing",):
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

    def test_uninstall_does_not_touch_release_added_after_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            source = RuntimeInstallTest().make_source(root)
            vibe_memory_install.install_runtime(source, paths)
            original = paths.install_root / "releases/1.0.0"
            external = paths.install_root / "releases/2.0.0"
            real_snapshot = vibe_memory_install._snapshot_owned_release
            injected = False

            def snapshot_with_external_release(path: pathlib.Path, *_args: object) -> object:
                nonlocal injected
                if not injected:
                    injected = True
                    shutil.copytree(original, external)
                    manifest = json.loads((external / "release.json").read_text(encoding="utf-8"))
                    manifest["app_version"] = "2.0.0"
                    (external / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
                    (external / "EXTERNAL_SENTINEL").write_text("keep\n", encoding="utf-8")
                    (external / "EXTERNAL_SENTINEL").chmod(0o600)
                return real_snapshot(path)

            with mock.patch("vibe_memory_install.bootout_launch_agent", return_value={"status": "booted_out"}), mock.patch(
                "vibe_memory_install._snapshot_owned_release", side_effect=snapshot_with_external_release
            ):
                vibe_memory_install.uninstall(paths)

            self.assertTrue(injected)
            self.assertEqual((external / "EXTERNAL_SENTINEL").read_text(encoding="utf-8"), "keep\n")

    def test_uninstall_rejects_owned_release_replaced_between_inventory_and_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            source = RuntimeInstallTest().make_source(root)
            vibe_memory_install.install_runtime(source, paths)
            release = paths.install_root / "releases/1.0.0"
            displaced = paths.install_root / "releases/1.0.0.foreign"
            real_snapshot = vibe_memory_install._snapshot_owned_release
            replaced = False

            def replace_before_snapshot(path: pathlib.Path, expected: object = None) -> object:
                nonlocal replaced
                if not replaced:
                    replaced = True
                    os.replace(release, displaced)
                    shutil.copytree(source, release)
                    for entry in release.rglob("*"):
                        if entry.is_file():
                            entry.chmod(0o600)
                        elif entry.is_dir():
                            entry.chmod(0o700)
                return real_snapshot(path, expected)

            with mock.patch(
                "vibe_memory_install._snapshot_owned_release",
                side_effect=replace_before_snapshot,
            ), mock.patch("vibe_memory_install.bootout_launch_agent") as bootout, self.assertRaises(
                vibe_memory_install.InstallError
            ):
                vibe_memory_install.uninstall(paths)

            self.assertTrue(replaced)
            bootout.assert_not_called()
            self.assertTrue(release.is_dir())
            self.assertTrue(displaced.is_dir())

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

    def test_uninstall_preflights_later_malformed_hook_before_any_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            vibe_memory_install.install_runtime(RuntimeInstallTest().make_source(root), paths)
            codex, _agent, _label = vibe_memory_install._hook_target_for_client(paths, "codex")
            claude, _agent, _label = vibe_memory_install._hook_target_for_client(paths, "claude-code")
            codex.parent.mkdir(parents=True)
            claude.parent.mkdir(parents=True)
            codex.write_text(
                json.dumps(vibe_memory_hooks.merge_document({"hooks": {}}, "codex", paths.launcher)),
                encoding="utf-8",
            )
            claude.write_text("{bad", encoding="utf-8")
            before = codex.read_bytes()

            with mock.patch("vibe_memory_install.bootout_launch_agent") as bootout, self.assertRaisesRegex(
                ValueError, "Invalid JSON"
            ):
                vibe_memory_install.uninstall(paths)

            bootout.assert_not_called()
            self.assertEqual(codex.read_bytes(), before)
            self.assertTrue((paths.install_root / "current").is_symlink())

    def test_uninstall_hook_failure_restores_prior_hook_and_service(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            vibe_memory_install.install_runtime(RuntimeInstallTest().make_source(root), paths)
            codex, _agent, _label = vibe_memory_install._hook_target_for_client(paths, "codex")
            codex.parent.mkdir(parents=True)
            codex.write_text(
                json.dumps(vibe_memory_hooks.merge_document({"hooks": {}}, "codex", paths.launcher)),
                encoding="utf-8",
            )
            before = codex.read_bytes()
            calls = 0

            def uninstall_hook(path: pathlib.Path, _runtime: pathlib.Path, **kwargs: object) -> dict[str, object]:
                nonlocal calls
                calls += 1
                self.assertIs(kwargs.get("_include_commit_snapshot"), True)
                if calls == 2:
                    raise RuntimeError("later hook write failed")
                pathlib.Path(path).write_text('{"hooks": {}}\n', encoding="utf-8")
                return {
                    "status": "updated",
                    "changed": True,
                    "_commit_snapshot": vibe_memory_install._snapshot_regular_file(pathlib.Path(path)),
                }

            with mock.patch("vibe_memory_install.bootout_launch_agent", return_value={"status": "booted_out"}), mock.patch(
                "vibe_memory_install.activate_launch_agent", return_value={"status": "healthy"}
            ) as activate, mock.patch("vibe_memory_hooks.uninstall", side_effect=uninstall_hook), self.assertRaisesRegex(
                vibe_memory_install.InstallError, "later hook write failed"
            ):
                vibe_memory_install.uninstall(paths)

            self.assertEqual(codex.read_bytes(), before)
            activate.assert_called_once_with(paths, expected_version="1.0.0")

    def test_uninstall_system_exit_preserves_concurrent_hook_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            vibe_memory_install.install_runtime(RuntimeInstallTest().make_source(root), paths)
            codex, _agent, _label = vibe_memory_install._hook_target_for_client(paths, "codex")
            codex.parent.mkdir(parents=True)
            codex.write_text(
                json.dumps(vibe_memory_hooks.merge_document({"hooks": {}}, "codex", paths.launcher)),
                encoding="utf-8",
            )
            external = b'{"third_party": true}\n'
            interruption = SystemExit(47)
            calls = 0

            def uninstall_hook(path: pathlib.Path, _runtime: pathlib.Path, **kwargs: object) -> dict[str, object]:
                nonlocal calls
                calls += 1
                self.assertIs(kwargs.get("_include_commit_snapshot"), True)
                if calls == 2:
                    codex.write_bytes(external)
                    raise interruption
                pathlib.Path(path).write_text('{"hooks": {}}\n', encoding="utf-8")
                return {
                    "status": "updated",
                    "changed": True,
                    "_commit_snapshot": vibe_memory_install._snapshot_regular_file(pathlib.Path(path)),
                }

            with mock.patch("vibe_memory_install.bootout_launch_agent", return_value={"status": "booted_out"}), mock.patch(
                "vibe_memory_install.activate_launch_agent", return_value={"status": "healthy"}
            ), mock.patch("vibe_memory_hooks.uninstall", side_effect=uninstall_hook), self.assertRaises(SystemExit) as raised:
                vibe_memory_install.uninstall(paths)

            self.assertIs(raised.exception, interruption)
            self.assertEqual(codex.read_bytes(), external)
            self.assertEqual(getattr(interruption, "_rollback_conflicts", []), [str(codex)])

    def test_uninstall_asset_failure_restores_hooks_assets_and_service(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            vibe_memory_install.install_runtime(RuntimeInstallTest().make_source(root), paths)
            vibe_memory_install.install_runtime_config(
                paths, port=9123, app_version="1.0.0", python_executable=sys.executable
            )
            vibe_memory_install.install_launcher(paths, python_executable=sys.executable)
            vibe_memory_install.install_launch_agent(
                paths,
                vibe_memory_install.render_launch_agent(
                    paths, port=9123, python_executable=sys.executable
                ),
            )
            codex, _agent, _label = vibe_memory_install._hook_target_for_client(paths, "codex")
            codex.parent.mkdir(parents=True)
            codex.write_text(
                json.dumps(vibe_memory_hooks.merge_document({"hooks": {}}, "codex", paths.launcher)),
                encoding="utf-8",
            )
            managed = [paths.launch_agent, paths.launcher, paths.install_root / "config.json", codex]
            before = {path: path.read_bytes() for path in managed}

            with mock.patch("vibe_memory_install.bootout_launch_agent", return_value={"status": "booted_out"}), mock.patch(
                "vibe_memory_install.activate_launch_agent", return_value={"status": "healthy"}
            ) as activate, mock.patch(
                "vibe_memory_install._unlink_regular_file", side_effect=PermissionError("asset unlink denied")
            ), self.assertRaisesRegex(vibe_memory_install.InstallError, "asset unlink denied"):
                vibe_memory_install.uninstall(paths)

            self.assertEqual(before, {path: path.read_bytes() for path in managed})
            self.assertTrue((paths.install_root / "current").is_symlink())
            self.assertTrue((paths.install_root / "releases/1.0.0").is_dir())
            activate.assert_called_once_with(paths, expected_version="1.0.0")

    def test_uninstall_rejects_foreign_fixed_json_assets_before_mutation(self) -> None:
        for asset_name in ("config", "install-state", "service-action"):
            with self.subTest(asset=asset_name), tempfile.TemporaryDirectory() as value:
                root = pathlib.Path(value)
                paths = self.make_paths(root)
                vibe_memory_install.install_runtime(RuntimeInstallTest().make_source(root), paths)
                assets = {
                    "config": paths.install_root / "config.json",
                    "install-state": vibe_memory_install.install_state_path(paths),
                    "service-action": paths.install_root / "state" / "service-action.json",
                }
                target = assets[asset_name]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('{"foreign": true}\n', encoding="utf-8")

                with mock.patch("vibe_memory_install.bootout_launch_agent") as bootout, self.assertRaises(
                    vibe_memory_install.InstallError
                ):
                    vibe_memory_install.uninstall(paths)

                bootout.assert_not_called()
                self.assertEqual(target.read_text(encoding="utf-8"), '{"foreign": true}\n')
                self.assertTrue((paths.install_root / "current").is_symlink())

    def test_uninstall_accepts_current_session_active_service_action(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            vibe_memory_install.install_runtime(RuntimeInstallTest().make_source(root), paths)
            action = paths.install_root / "state" / "service-action.json"
            action.parent.mkdir(parents=True, exist_ok=True)
            action.write_text(json.dumps({"generation": "a" * 32, "desired_start_at_login": False, "status": "current_session_active"}), encoding="utf-8")
            with mock.patch("vibe_memory_install.bootout_launch_agent", return_value={"status": "booted_out"}):
                vibe_memory_install.uninstall(paths)
            self.assertFalse(action.exists())

    def test_uninstall_rejects_malformed_install_state_before_bootout(self) -> None:
        for payload in (b"{bad", b"\xff\xfe not json"):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as value:
                root = pathlib.Path(value)
                paths = self.make_paths(root)
                vibe_memory_install.install_runtime(RuntimeInstallTest().make_source(root), paths)
                state = vibe_memory_install.install_state_path(paths)
                state.parent.mkdir(parents=True, exist_ok=True)
                state.write_bytes(payload)
                with mock.patch("vibe_memory_install.bootout_launch_agent") as bootout, self.assertRaises(vibe_memory_install.InstallError):
                    vibe_memory_install.uninstall(paths)
                bootout.assert_not_called()
                self.assertEqual(state.read_bytes(), payload)

    def test_uninstall_rejects_fixed_asset_replaced_between_validation_and_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            vibe_memory_install.install_runtime(RuntimeInstallTest().make_source(root), paths)
            vibe_memory_install.write_install_state(paths, vibe_memory_install._install_state_document(
                current_version="1.0.0", previous_version=None, port=8897,
                installed_clients=[], python_executable=sys.executable,
            ))
            state = vibe_memory_install.install_state_path(paths)
            foreign = b'{"foreign": true}\n'
            real_snapshot = vibe_memory_install._snapshot_regular_file
            replaced = False

            def replace_before_snapshot(path: pathlib.Path) -> object:
                nonlocal replaced
                if path == state and not replaced:
                    replaced = True
                    replacement = state.with_name("foreign-state.json")
                    replacement.write_bytes(foreign)
                    os.replace(replacement, state)
                return real_snapshot(path)

            with mock.patch("vibe_memory_install._snapshot_regular_file", side_effect=replace_before_snapshot), mock.patch(
                "vibe_memory_install.bootout_launch_agent"
            ) as bootout, self.assertRaises(vibe_memory_install.InstallError):
                vibe_memory_install.uninstall(paths)

            bootout.assert_not_called()
            self.assertEqual(state.read_bytes(), foreign)
            self.assertTrue((paths.install_root / "current").is_symlink())

    def test_uninstall_rejects_launch_asset_replaced_after_marker_check(self) -> None:
        for asset in ("launch_agent", "launcher"):
            with self.subTest(asset=asset), tempfile.TemporaryDirectory() as value:
                root = pathlib.Path(value)
                paths = self.make_paths(root)
                vibe_memory_install.install_runtime(RuntimeInstallTest().make_source(root), paths)
                launch_agent = paths.launch_agent
                launcher = paths.launcher
                launch_agent.parent.mkdir(parents=True, exist_ok=True)
                launcher.parent.mkdir(parents=True, exist_ok=True)
                launch_agent.write_text(vibe_memory_install.render_launch_agent(paths), encoding="utf-8")
                launcher.write_text(vibe_memory_install.render_launcher(paths), encoding="utf-8")
                target = launch_agent if asset == "launch_agent" else launcher
                foreign = b"#!/bin/sh\necho foreign\n"
                real_snapshot = vibe_memory_install._snapshot_regular_file
                replaced = False

                def replace_after_marker(path: pathlib.Path) -> object:
                    nonlocal replaced
                    if path == target and not replaced:
                        replaced = True
                        target.write_bytes(foreign)
                    return real_snapshot(path)

                with mock.patch("vibe_memory_install._snapshot_regular_file", side_effect=replace_after_marker), mock.patch(
                    "vibe_memory_install.bootout_launch_agent"
                ) as bootout, self.assertRaises(vibe_memory_install.InstallError):
                    vibe_memory_install.uninstall(paths)

                bootout.assert_not_called()
                self.assertEqual(target.read_bytes(), foreign)

    def test_uninstall_rejects_crafted_launch_agent_with_manager_markers_only(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            vibe_memory_install.install_runtime(RuntimeInstallTest().make_source(root), paths)
            paths.launch_agent.parent.mkdir(parents=True, exist_ok=True)
            crafted = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.noema.vibe-memory</string>
<key>ProgramArguments</key><array><string>/bin/echo</string><string>memory_review_server.py</string></array>
</dict></plist>
"""
            paths.launch_agent.write_text(crafted, encoding="utf-8")
            with mock.patch("vibe_memory_install.bootout_launch_agent") as bootout, self.assertRaises(
                vibe_memory_install.InstallError
            ):
                vibe_memory_install.uninstall(paths)
            bootout.assert_not_called()
            self.assertEqual(paths.launch_agent.read_text(encoding="utf-8"), crafted)

    def test_uninstall_rejects_launcher_with_marker_only(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            vibe_memory_install.install_runtime(RuntimeInstallTest().make_source(root), paths)
            paths.launcher.parent.mkdir(parents=True, exist_ok=True)
            crafted = f"{vibe_memory_install.LAUNCHER_MARKER}\n"
            paths.launcher.write_text(crafted, encoding="utf-8")
            with mock.patch("vibe_memory_install.bootout_launch_agent") as bootout, self.assertRaises(
                vibe_memory_install.InstallError
            ):
                vibe_memory_install.uninstall(paths)
            bootout.assert_not_called()
            self.assertEqual(paths.launcher.read_text(encoding="utf-8"), crafted)

    def test_uninstall_rejects_empty_install_state_as_foreign(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            vibe_memory_install.install_runtime(RuntimeInstallTest().make_source(root), paths)
            state = vibe_memory_install.install_state_path(paths)
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text("{}\n", encoding="utf-8")
            with mock.patch("vibe_memory_install.bootout_launch_agent") as bootout, self.assertRaises(
                vibe_memory_install.InstallError
            ):
                vibe_memory_install.uninstall(paths)
            bootout.assert_not_called()
            self.assertEqual(state.read_text(encoding="utf-8"), "{}\n")

    def test_uninstall_rejects_fixed_asset_added_after_initial_presence_check(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            vibe_memory_install.install_runtime(RuntimeInstallTest().make_source(root), paths)
            target = paths.install_root / "config.json"
            original_validate = vibe_memory_install._validate_owned_fixed_asset
            injected = False

            def validate_then_add(path: pathlib.Path, kind: str, runtime_paths: object) -> None:
                nonlocal injected
                original_validate(path, kind, runtime_paths)
                if kind == "config" and not injected:
                    injected = True
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(
                        vibe_memory_install.render_runtime_config(
                            8897, "1.0.0", python_executable=sys.executable
                        ),
                        encoding="utf-8",
                    )

            with mock.patch(
                "vibe_memory_install._validate_owned_fixed_asset",
                side_effect=validate_then_add,
            ), mock.patch("vibe_memory_install.bootout_launch_agent") as bootout, self.assertRaises(
                vibe_memory_install.InstallError
            ):
                vibe_memory_install.uninstall(paths)

            self.assertTrue(injected)
            bootout.assert_not_called()
            self.assertTrue(target.exists())

    def test_uninstall_rejects_approved_data_added_after_initial_presence_check(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            vibe_memory_install.install_runtime(RuntimeInstallTest().make_source(root), paths)
            target = paths.project_registry
            original_validate = vibe_memory_install._validate_owned_fixed_asset
            injected = False

            def validate_then_add(path: pathlib.Path, kind: str, runtime_paths: object) -> None:
                nonlocal injected
                original_validate(path, kind, runtime_paths)
                if kind == "config" and not injected:
                    injected = True
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("{\"external\":true}\n", encoding="utf-8")

            with mock.patch(
                "vibe_memory_install._validate_owned_fixed_asset",
                side_effect=validate_then_add,
            ), mock.patch("vibe_memory_install.bootout_launch_agent") as bootout, self.assertRaises(
                vibe_memory_install.InstallError
            ):
                vibe_memory_install.uninstall(
                    paths,
                    remove_data=True,
                    approved_data_deletion=True,
                    data_paths=[target],
                )

            self.assertTrue(injected)
            bootout.assert_not_called()
            self.assertEqual(target.read_text(encoding="utf-8"), "{\"external\":true}\n")

    def test_uninstall_preserves_fixed_asset_replaced_after_hook_commit(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            paths = self.make_paths(root)
            vibe_memory_install.install_runtime(RuntimeInstallTest().make_source(root), paths)
            vibe_memory_install.install_runtime_config(
                paths, port=9123, app_version="1.0.0", python_executable=sys.executable
            )
            config = paths.install_root / "config.json"
            external = b'{"external": true}\n'
            real_uninstall_hook = vibe_memory_install._uninstall_lifecycle_hook
            calls = 0

            def replace_after_hooks(*args: object, **kwargs: object) -> dict[str, object]:
                nonlocal calls
                result = real_uninstall_hook(*args, **kwargs)
                calls += 1
                if calls == 2:
                    replacement = config.with_name("replacement.json")
                    replacement.write_bytes(external)
                    os.replace(replacement, config)
                return result

            with mock.patch("vibe_memory_install.bootout_launch_agent", return_value={"status": "booted_out"}), mock.patch(
                "vibe_memory_install.activate_launch_agent", return_value={"status": "healthy"}
            ), mock.patch(
                "vibe_memory_install._uninstall_lifecycle_hook", side_effect=replace_after_hooks
            ), self.assertRaisesRegex(vibe_memory_install.InstallError, "concurrent|changed"):
                vibe_memory_install.uninstall(paths)

            self.assertEqual(config.read_bytes(), external)

    def test_uninstall_release_cleanup_preserves_concurrent_quarantine_changes(self) -> None:
        for race in ("sentinel", "rebind"):
            with self.subTest(race=race), tempfile.TemporaryDirectory() as value:
                root = pathlib.Path(value)
                paths = self.make_paths(root)
                vibe_memory_install.install_runtime(RuntimeInstallTest().make_source(root), paths)
                release = paths.install_root / "releases/1.0.0"
                real_quarantine = vibe_memory_install._quarantine_owned_release
                captured: list[pathlib.Path] = []

                def inject(path: pathlib.Path, snapshot: object) -> pathlib.Path:
                    quarantine = real_quarantine(path, snapshot)
                    captured.append(quarantine)
                    if race == "sentinel":
                        (quarantine / "FOREIGN_SENTINEL").write_text("keep\n", encoding="utf-8")
                    else:
                        displaced = quarantine.with_name(quarantine.name + ".manager")
                        os.replace(quarantine, displaced)
                        quarantine.mkdir(mode=0o700)
                        (quarantine / "FOREIGN_SENTINEL").write_text("keep\n", encoding="utf-8")
                    return quarantine

                with mock.patch("vibe_memory_install.bootout_launch_agent", return_value={"status": "booted_out"}), mock.patch(
                    "vibe_memory_install.activate_launch_agent", return_value={"status": "healthy"}
                ), mock.patch(
                    "vibe_memory_install._quarantine_owned_release", side_effect=inject
                ), self.assertRaisesRegex(vibe_memory_install.InstallError, "release|cleanup|changed"):
                    vibe_memory_install.uninstall(paths)

                self.assertTrue(captured[0].exists())
                self.assertEqual((captured[0] / "FOREIGN_SENTINEL").read_text(encoding="utf-8"), "keep\n")

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

    def test_rollback_restores_original_lifecycle_after_current_replace_fsync_failure(self) -> None:
        failures = (
            OSError("durability sync failed after replacement"),
            KeyboardInterrupt(),
            SystemExit(47),
        )
        real_fsync = os.fsync

        for failure in failures:
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as value:
                paths = self.make_paths(pathlib.Path(value))
                releases = paths.install_root / "releases"
                for version in ("0.9.0", "1.0.0"):
                    (releases / version).mkdir(parents=True)
                current = paths.install_root / "current"
                current.symlink_to("releases/1.0.0")
                vibe_memory_install.install_runtime_config(
                    paths,
                    port=9123,
                    app_version="1.0.0",
                    python_executable=sys.executable,
                )
                vibe_memory_install.install_launcher(paths, python_executable=sys.executable)
                vibe_memory_install.install_launch_agent(
                    paths,
                    vibe_memory_install.render_launch_agent(
                        paths,
                        port=9123,
                        python_executable=sys.executable,
                    ),
                )
                vibe_memory_install.write_install_state(
                    paths,
                    vibe_memory_install._install_state_document(
                        current_version="1.0.0",
                        previous_version="0.9.0",
                        port=9123,
                        installed_clients=[],
                        python_executable=sys.executable,
                    ),
                )
                managed = [
                    paths.install_root / "config.json",
                    paths.launcher,
                    paths.launch_agent,
                    vibe_memory_install.install_state_path(paths),
                ]
                before = {path: path.read_bytes() for path in managed}
                fsync_calls = 0

                def fail_first_fsync(descriptor: int) -> None:
                    nonlocal fsync_calls
                    fsync_calls += 1
                    if fsync_calls == 1:
                        raise failure
                    real_fsync(descriptor)

                with mock.patch(
                    "vibe_memory_install.os.fsync", side_effect=fail_first_fsync
                ), mock.patch(
                    "vibe_memory_install.bootout_launch_agent"
                ), mock.patch(
                    "vibe_memory_install.activate_launch_agent",
                    return_value={"status": "healthy"},
                ) as activate:
                    if isinstance(failure, Exception):
                        with self.assertRaises(vibe_memory_install.InstallError) as raised:
                            vibe_memory_install.rollback(paths)
                        self.assertIs(raised.exception.__cause__, failure)
                    else:
                        with self.assertRaises(type(failure)) as raised:
                            vibe_memory_install.rollback(paths)
                        self.assertIs(raised.exception, failure)

                self.assertEqual(os.readlink(current), "releases/1.0.0")
                self.assertEqual(before, {path: path.read_bytes() for path in managed})
                self.assertEqual(
                    activate.call_args_list,
                    [mock.call(paths, expected_version="1.0.0")],
                )
                self.assertTrue((releases / "0.9.0").is_dir())
                self.assertTrue((releases / "1.0.0").is_dir())

    def test_rollback_cleanup_preserves_concurrent_current_after_replace_fsync_interrupt(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            releases = paths.install_root / "releases"
            for version in ("0.9.0", "1.0.0", "2.0.0"):
                (releases / version).mkdir(parents=True)
            current = paths.install_root / "current"
            current.symlink_to("releases/1.0.0")
            vibe_memory_install.write_install_state(
                paths,
                vibe_memory_install._install_state_document(
                    current_version="1.0.0",
                    previous_version="0.9.0",
                    port=9123,
                    installed_clients=[],
                    python_executable=sys.executable,
                ),
            )
            state_path = vibe_memory_install.install_state_path(paths)
            original_state = state_path.read_bytes()
            interruption = KeyboardInterrupt()
            real_fsync = os.fsync
            fsync_calls = 0

            def replace_current_then_interrupt(descriptor: int) -> None:
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls == 1:
                    current.unlink()
                    current.symlink_to("releases/2.0.0")
                    raise interruption
                real_fsync(descriptor)

            with mock.patch(
                "vibe_memory_install.os.fsync",
                side_effect=replace_current_then_interrupt,
            ), mock.patch(
                "vibe_memory_install.bootout_launch_agent"
            ), mock.patch(
                "vibe_memory_install.activate_launch_agent",
                return_value={"status": "healthy"},
            ):
                with self.assertRaises(KeyboardInterrupt) as raised:
                    vibe_memory_install.rollback(paths)

            self.assertIs(raised.exception, interruption)
            self.assertEqual(os.readlink(current), "releases/2.0.0")
            self.assertEqual(state_path.read_bytes(), original_state)
            self.assertEqual(
                getattr(interruption, "_rollback_conflicts", []),
                ["version"],
            )
            self.assertIn(
                "changed concurrently",
                getattr(interruption, "_cleanup_failures", [""])[0],
            )

    @unittest.skipUnless(sys.platform == "darwin", "Darwin atomic exchange only")
    def test_rollback_current_cas_preserves_latest_writer_across_exchange_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            releases = paths.install_root / "releases"
            for version in ("0.9.0", "1.0.0", "2.0.0", "3.0.0"):
                (releases / version).mkdir(parents=True)
            current = paths.install_root / "current"
            current.symlink_to("releases/1.0.0")
            vibe_memory_install.write_install_state(
                paths,
                vibe_memory_install._install_state_document(
                    current_version="1.0.0",
                    previous_version="0.9.0",
                    port=9123,
                    installed_clients=[],
                    python_executable=sys.executable,
                ),
            )
            state_path = vibe_memory_install.install_state_path(paths)
            original_state = state_path.read_bytes()
            real_snapshot = vibe_memory_install._managed_current_snapshot_at
            real_renameat = vibe_memory_install._darwin_renameat
            snapshot_calls = 0
            swap_calls = 0
            first_writer_injected = False

            def install_external(version: str) -> None:
                replacement = paths.install_root / f".external-{version}"
                replacement.symlink_to(f"releases/{version}")
                os.replace(replacement, current)

            def race_after_compare(install_fd: int):
                nonlocal snapshot_calls, first_writer_injected
                result = real_snapshot(install_fd)
                snapshot_calls += 1
                if snapshot_calls == 2 and not first_writer_injected:
                    install_external("2.0.0")
                    first_writer_injected = True
                return result

            def race_atomic_exchange(
                source_parent_fd: int,
                source_name: str,
                destination_parent_fd: int,
                destination_name: str,
                flags: int,
            ) -> None:
                nonlocal first_writer_injected, swap_calls
                if flags == vibe_memory_install.RENAME_SWAP:
                    swap_calls += 1
                    if swap_calls == 1 and not first_writer_injected:
                        install_external("2.0.0")
                        first_writer_injected = True
                    elif swap_calls == 2:
                        install_external("3.0.0")
                real_renameat(
                    source_parent_fd,
                    source_name,
                    destination_parent_fd,
                    destination_name,
                    flags,
                )

            with mock.patch(
                "vibe_memory_install._managed_current_snapshot_at",
                side_effect=race_after_compare,
            ), mock.patch(
                "vibe_memory_install._darwin_renameat",
                side_effect=race_atomic_exchange,
            ), mock.patch(
                "vibe_memory_install.bootout_launch_agent"
            ), mock.patch(
                "vibe_memory_install.activate_launch_agent",
                return_value={"status": "healthy"},
            ):
                with self.assertRaisesRegex(
                    vibe_memory_install.InstallError,
                    "changed concurrently",
                ):
                    vibe_memory_install.rollback(paths)

            self.assertTrue(first_writer_injected)
            self.assertEqual(os.readlink(current), "releases/3.0.0")
            self.assertEqual(state_path.read_bytes(), original_state)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin exclusive rename only")
    def test_rollback_absent_current_cleanup_does_not_unlink_concurrent_writer(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            releases = paths.install_root / "releases"
            for version in ("0.9.0", "1.0.0", "2.0.0"):
                (releases / version).mkdir(parents=True)
            vibe_memory_install.write_install_state(
                paths,
                vibe_memory_install._install_state_document(
                    current_version="1.0.0",
                    previous_version="0.9.0",
                    port=9123,
                    installed_clients=[],
                    python_executable=sys.executable,
                ),
            )
            current = paths.install_root / "current"
            state_path = vibe_memory_install.install_state_path(paths)
            original_state = state_path.read_bytes()
            real_snapshot = vibe_memory_install._managed_current_snapshot_at
            real_renameat = vibe_memory_install._darwin_renameat
            snapshot_calls = 0
            injected = False

            def install_external() -> None:
                replacement = paths.install_root / ".external-current"
                replacement.symlink_to("releases/2.0.0")
                os.replace(replacement, current)

            def race_after_cleanup_compare(install_fd: int):
                nonlocal snapshot_calls, injected
                result = real_snapshot(install_fd)
                snapshot_calls += 1
                if snapshot_calls == 5 and not injected:
                    install_external()
                    injected = True
                return result

            def race_atomic_remove(
                source_parent_fd: int,
                source_name: str,
                destination_parent_fd: int,
                destination_name: str,
                flags: int,
            ) -> None:
                nonlocal injected
                if source_name == "current" and not injected:
                    install_external()
                    injected = True
                real_renameat(
                    source_parent_fd,
                    source_name,
                    destination_parent_fd,
                    destination_name,
                    flags,
                )

            with mock.patch(
                "vibe_memory_install._managed_current_snapshot_at",
                side_effect=race_after_cleanup_compare,
            ), mock.patch(
                "vibe_memory_install._darwin_renameat",
                side_effect=race_atomic_remove,
            ), mock.patch(
                "vibe_memory_install.install_runtime_config",
                side_effect=RuntimeError("later failure"),
            ), mock.patch(
                "vibe_memory_install.bootout_launch_agent"
            ), mock.patch(
                "vibe_memory_install.activate_launch_agent",
                return_value={"status": "healthy"},
            ):
                with self.assertRaisesRegex(
                    vibe_memory_install.InstallError,
                    "later failure",
                ):
                    vibe_memory_install.rollback(paths)

            self.assertTrue(injected)
            self.assertEqual(os.readlink(current), "releases/2.0.0")
            self.assertEqual(state_path.read_bytes(), original_state)

    def test_rollback_restores_current_when_displaced_entry_cleanup_is_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            releases = paths.install_root / "releases"
            for version in ("0.9.0", "1.0.0"):
                (releases / version).mkdir(parents=True)
            current = paths.install_root / "current"
            current.symlink_to("releases/1.0.0")
            vibe_memory_install.write_install_state(
                paths,
                vibe_memory_install._install_state_document(
                    current_version="1.0.0",
                    previous_version="0.9.0",
                    port=9123,
                    installed_clients=[],
                    python_executable=sys.executable,
                ),
            )
            interruption = KeyboardInterrupt()
            real_remove = vibe_memory_install._remove_current_entry_exact_at
            remove_calls = 0

            def interrupt_first_remove(*args: object, **kwargs: object) -> None:
                nonlocal remove_calls
                remove_calls += 1
                real_remove(*args, **kwargs)
                if remove_calls == 1:
                    raise interruption

            with mock.patch(
                "vibe_memory_install._remove_current_entry_exact_at",
                side_effect=interrupt_first_remove,
            ), mock.patch(
                "vibe_memory_install.bootout_launch_agent"
            ), mock.patch(
                "vibe_memory_install.activate_launch_agent",
                return_value={"status": "healthy"},
            ):
                with self.assertRaises(KeyboardInterrupt) as raised:
                    vibe_memory_install.rollback(paths)

            self.assertIs(raised.exception, interruption)
            self.assertEqual(os.readlink(current), "releases/1.0.0")
            self.assertEqual(getattr(interruption, "_rollback_conflicts", []), [])

    def test_current_activation_preserves_primary_exception_and_closes_fd_when_finally_cleanup_fails(self) -> None:
        cleanup_modes = ("remove", "snapshot")

        for cleanup_mode in cleanup_modes:
            with self.subTest(cleanup=cleanup_mode), tempfile.TemporaryDirectory() as value:
                paths = self.make_paths(pathlib.Path(value))
                releases = paths.install_root / "releases"
                for version in ("0.9.0", "1.0.0"):
                    (releases / version).mkdir(parents=True)
                current = paths.install_root / "current"
                current.symlink_to("releases/1.0.0")
                expected = vibe_memory_install._snapshot_managed_current(paths)
                self.assertIsNotNone(expected)
                primary: BaseException = (
                    KeyboardInterrupt()
                    if cleanup_mode == "remove"
                    else SystemExit(47)
                )
                cleanup = RuntimeError(f"{cleanup_mode} cleanup failed")
                real_open_chain = vibe_memory_install._open_or_create_directory_chain
                real_entry_snapshot = vibe_memory_install._current_entry_snapshot_at
                captured_fd: int | None = None
                snapshot_calls = 0

                def capture_fd(path: pathlib.Path) -> int:
                    nonlocal captured_fd
                    captured_fd = real_open_chain(path)
                    return captured_fd

                def fail_finally_snapshot(install_fd: int, name: str):
                    nonlocal snapshot_calls
                    snapshot_calls += 1
                    if cleanup_mode == "snapshot" and snapshot_calls == 2:
                        raise cleanup
                    return real_entry_snapshot(install_fd, name)

                remove_effect: object = (
                    cleanup
                    if cleanup_mode == "remove"
                    else mock.DEFAULT
                )
                caught: BaseException | None = None
                with mock.patch(
                    "vibe_memory_install._open_or_create_directory_chain",
                    side_effect=capture_fd,
                ), mock.patch(
                    "vibe_memory_install._darwin_renameat",
                    side_effect=primary,
                ), mock.patch(
                    "vibe_memory_install._current_entry_snapshot_at",
                    side_effect=fail_finally_snapshot,
                ), mock.patch(
                    "vibe_memory_install._remove_current_entry_exact_at",
                    side_effect=remove_effect,
                    wraps=vibe_memory_install._remove_current_entry_exact_at,
                ):
                    try:
                        vibe_memory_install._activate_managed_version(
                            paths,
                            "0.9.0",
                            expected_current=expected,
                        )
                    except BaseException as error:
                        caught = error
                    else:
                        self.fail("activation unexpectedly succeeded")

                self.assertIs(caught, primary)
                self.assertEqual(
                    getattr(primary, "_rollback_conflicts", []),
                    ["current temporary"],
                )
                self.assertIn(
                    str(cleanup),
                    getattr(primary, "_cleanup_failures", [""])[0],
                )
                self.assertIsNotNone(captured_fd)
                with self.assertRaises(OSError):
                    os.fstat(captured_fd)

    def test_remove_current_entry_preserves_foreign_replacement_after_claim(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            paths = self.make_paths(pathlib.Path(value))
            install_root = paths.install_root
            install_root.mkdir(parents=True, exist_ok=True)
            current = install_root / "current"
            current.symlink_to("releases/1.0.0")
            install_fd = os.open(install_root, vibe_memory_install._DIRECTORY_OPEN_FLAGS)
            expected = vibe_memory_install._current_entry_snapshot_at(install_fd, "current")
            assert expected is not None
            real_renameat = vibe_memory_install._darwin_renameat
            injected = False

            def replace_after_claim(
                source_parent_fd: int,
                source_name: str,
                destination_parent_fd: int,
                destination_name: str,
                flags: int,
            ) -> None:
                nonlocal injected
                real_renameat(
                    source_parent_fd,
                    source_name,
                    destination_parent_fd,
                    destination_name,
                    flags,
                )
                if source_name == "current" and not injected:
                    injected = True
                    displaced = install_root / ".expected-current"
                    os.replace(install_root / destination_name, displaced)
                    foreign = install_root / ".foreign-current"
                    foreign.symlink_to("releases/2.0.0")
                    os.replace(foreign, install_root / destination_name)

            try:
                with mock.patch("vibe_memory_install._darwin_renameat", side_effect=replace_after_claim), self.assertRaises(
                    vibe_memory_install.InstallError
                ):
                    vibe_memory_install._remove_current_entry_exact_at(install_fd, "current", expected)
            finally:
                os.close(install_fd)

            self.assertTrue(injected)
            self.assertTrue(current.is_symlink())
            self.assertEqual(os.readlink(current), "releases/2.0.0")

    def test_remove_quarantined_release_preserves_foreign_rebind_before_rmdir(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            quarantine = root / "quarantine"
            quarantine.mkdir(mode=0o700)
            for directory in ("scripts", "templates", "docs"):
                (quarantine / directory).mkdir(mode=0o700)
            (quarantine / "README.md").write_bytes(b"# Runtime\n")
            (quarantine / "release.json").write_bytes(json.dumps(MANIFEST).encode())
            (quarantine / "scripts" / "server.py").write_bytes(b"print('server')\n")
            (quarantine / "templates" / "template.txt").write_bytes(b"template\n")
            (quarantine / "docs" / "guide.md").write_bytes(b"# Guide\n")
            for file in quarantine.rglob("*"):
                if file.is_file():
                    file.chmod(0o600)
            snapshot = vibe_memory_install._snapshot_owned_release(quarantine)
            real_claim = vibe_memory_install._claim_entry_for_removal
            injected = False

            def rebind_after_claim(parent_fd: int, name: str, stem: str) -> str:
                nonlocal injected
                claimed_name = real_claim(parent_fd, name, stem)
                if name == quarantine.name and not injected:
                    injected = True
                    quarantine.mkdir(mode=0o700)
                    (quarantine / "FOREIGN").write_bytes(b"keep")
                    (quarantine / "FOREIGN").chmod(0o600)
                return claimed_name

            with mock.patch("vibe_memory_install._claim_entry_for_removal", side_effect=rebind_after_claim):
                vibe_memory_install._remove_quarantined_release(quarantine, snapshot)

            self.assertTrue(injected)
            self.assertTrue(quarantine.is_dir())
            self.assertEqual((quarantine / "FOREIGN").read_bytes(), b"keep")

    def test_remove_quarantined_release_rejects_in_place_file_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            quarantine = root / "quarantine"
            quarantine.mkdir(mode=0o700)
            for directory in ("scripts", "templates", "docs"):
                (quarantine / directory).mkdir(mode=0o700)
            (quarantine / "README.md").write_bytes(b"# Runtime\n")
            (quarantine / "release.json").write_bytes(json.dumps(MANIFEST).encode())
            (quarantine / "scripts" / "server.py").write_bytes(b"print('server')\n")
            (quarantine / "templates" / "template.txt").write_bytes(b"template\n")
            (quarantine / "docs" / "guide.md").write_bytes(b"# Guide\n")
            for file in quarantine.rglob("*"):
                if file.is_file():
                    file.chmod(0o600)
            snapshot = vibe_memory_install._snapshot_owned_release(quarantine)
            real_inventory = vibe_memory_install._temporary_tree_inventory_fd
            inventory_calls = 0

            def mutate_after_inventory(fd: int, prefix: pathlib.Path = pathlib.Path()) -> object:
                nonlocal inventory_calls
                result = real_inventory(fd, prefix)
                inventory_calls += 1
                if inventory_calls == 2:
                    target = quarantine / "README.md"
                    target.write_bytes(b"foreign\n")
                    target.chmod(0o600)
                return result

            with mock.patch(
                "vibe_memory_install._temporary_tree_inventory_fd",
                side_effect=mutate_after_inventory,
            ), self.assertRaises(vibe_memory_install.InstallError):
                vibe_memory_install._remove_quarantined_release(quarantine, snapshot)

            self.assertEqual((quarantine / "README.md").read_bytes(), b"foreign\n")

    def test_unlink_regular_file_rejects_in_place_mutation_before_final_unlink(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            target = pathlib.Path(value) / "managed"
            target.write_bytes(b"manager\n")
            target.chmod(0o600)
            expected = vibe_memory_install._snapshot_regular_file(target)
            real_snapshot = vibe_memory_install._snapshot_regular_file_at
            snapshot_calls = 0

            def mutate_after_first_snapshot(fd: int, name: str, display: pathlib.Path) -> object:
                nonlocal snapshot_calls
                result = real_snapshot(fd, name, display)
                snapshot_calls += 1
                if snapshot_calls == 3:
                    private_target = target.parent / name
                    private_target.write_bytes(b"foreign\n")
                    private_target.chmod(0o600)
                return result

            with mock.patch(
                "vibe_memory_install._snapshot_regular_file_at",
                side_effect=mutate_after_first_snapshot,
            ), self.assertRaises(vibe_memory_install.InstallError):
                vibe_memory_install._unlink_regular_file(target, expected=expected)

            self.assertEqual(target.read_bytes(), b"foreign\n")

    def test_unlink_regular_file_preserves_keyboard_interrupt_when_cleanup_check_fails(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            target = pathlib.Path(value) / "managed"
            target.write_bytes(b"manager\n")
            target.chmod(0o600)
            expected = vibe_memory_install._snapshot_regular_file(target)
            interruption = KeyboardInterrupt()
            with mock.patch(
                "vibe_memory_install._unlink_claimed_entry_exact",
                side_effect=interruption,
            ), mock.patch(
                "vibe_memory_install._entry_exists",
                side_effect=OSError("cleanup check failed"),
            ):
                with self.assertRaises(KeyboardInterrupt) as raised:
                    vibe_memory_install._unlink_regular_file(target, expected=expected)
            self.assertIs(raised.exception, interruption)
            self.assertEqual(type(raised.exception), KeyboardInterrupt)

    def test_portable_claim_restore_does_not_replace_occupied_destination(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            claimed = root / "claimed"
            original = root / "original"
            claimed.write_bytes(b"manager\n")
            original.write_bytes(b"foreign\n")
            parent_fd = os.open(root, vibe_memory_install._DIRECTORY_OPEN_FLAGS)
            try:
                with mock.patch.object(vibe_memory_install.sys, "platform", "linux"), self.assertRaises(
                    vibe_memory_install.InstallError
                ):
                    vibe_memory_install._restore_claimed_entry(parent_fd, "claimed", "original")
            finally:
                os.close(parent_fd)
            self.assertEqual(original.read_bytes(), b"foreign\n")
            self.assertEqual(claimed.read_bytes(), b"manager\n")

    def test_portable_directory_restore_does_not_replace_occupied_destination(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            claimed = root / "claimed"
            original = root / "original"
            claimed.mkdir(mode=0o700)
            (claimed / "manager").write_bytes(b"manager\n")
            parent_fd = os.open(root, vibe_memory_install._DIRECTORY_OPEN_FLAGS)

            real_rename = os.rename
            real_noreplace = vibe_memory_install._rename_noreplace_at

            def race_rename(source: object, destination: object, **kwargs: object) -> object:
                destination_path = root / os.fspath(destination)
                if not destination_path.exists():
                    destination_path.mkdir(mode=0o700)
                return real_rename(source, destination, **kwargs)

            def race_noreplace(
                source_parent_fd: int,
                source_name: str,
                destination_parent_fd: int,
                destination_name: str,
            ) -> None:
                destination_path = root / destination_name
                if not destination_path.exists():
                    destination_path.mkdir(mode=0o700)
                return real_noreplace(
                    source_parent_fd,
                    source_name,
                    destination_parent_fd,
                    destination_name,
                )

            try:
                with mock.patch.object(vibe_memory_install.sys, "platform", "linux"), mock.patch(
                    "vibe_memory_install.os.rename", side_effect=race_rename
                ), mock.patch(
                    "vibe_memory_install._rename_noreplace_at", side_effect=race_noreplace
                ), self.assertRaises(vibe_memory_install.InstallError):
                    vibe_memory_install._restore_claimed_entry(parent_fd, "claimed", "original")
            finally:
                os.close(parent_fd)
            self.assertTrue(original.is_dir())
            self.assertEqual(list(original.iterdir()), [])
            self.assertEqual((claimed / "manager").read_bytes(), b"manager\n")

    def test_portable_claim_does_not_replace_occupied_private_name(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = root / "source"
            source.write_bytes(b"manager\n")
            parent_fd = os.open(root, vibe_memory_install._DIRECTORY_OPEN_FLAGS)
            fixed_uuid = mock.Mock(hex="fixed")
            next_uuid = mock.Mock(hex="next")
            try:
                (root / ".source.remove-fixed").write_bytes(b"foreign\n")
                with mock.patch.object(vibe_memory_install.sys, "platform", "linux"), mock.patch(
                    "vibe_memory_install.uuid.uuid4", side_effect=[fixed_uuid, next_uuid]
                ):
                    claimed_name = vibe_memory_install._claim_entry_for_removal(parent_fd, "source", "source")
            finally:
                os.close(parent_fd)
            self.assertEqual(claimed_name, ".source.remove-next")
            self.assertFalse(source.exists())
            self.assertEqual((root / ".source.remove-fixed").read_bytes(), b"foreign\n")

    def test_portable_directory_claim_does_not_replace_occupied_private_name(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = root / "source"
            source.mkdir(mode=0o700)
            (source / "manager").write_bytes(b"manager\n")
            parent_fd = os.open(root, vibe_memory_install._DIRECTORY_OPEN_FLAGS)
            fixed_uuid = mock.Mock(hex="fixed")
            next_uuid = mock.Mock(hex="next")

            real_rename = os.rename
            real_noreplace = vibe_memory_install._rename_noreplace_at
            raced = False

            def race_rename(source: object, destination: object, **kwargs: object) -> object:
                destination_path = root / os.fspath(destination)
                if not destination_path.exists():
                    destination_path.mkdir(mode=0o700)
                return real_rename(source, destination, **kwargs)

            def race_noreplace(
                source_parent_fd: int,
                source_name: str,
                destination_parent_fd: int,
                destination_name: str,
            ) -> None:
                nonlocal raced
                destination_path = root / destination_name
                if not raced:
                    raced = True
                    destination_path.mkdir(mode=0o700)
                return real_noreplace(
                    source_parent_fd,
                    source_name,
                    destination_parent_fd,
                    destination_name,
                )

            try:
                with mock.patch.object(vibe_memory_install.sys, "platform", "linux"), mock.patch(
                    "vibe_memory_install.uuid.uuid4", side_effect=[fixed_uuid, next_uuid]
                ), mock.patch(
                    "vibe_memory_install.os.rename", side_effect=race_rename
                ), mock.patch(
                    "vibe_memory_install._rename_noreplace_at", side_effect=race_noreplace
                ):
                    claimed_name = vibe_memory_install._claim_entry_for_removal(parent_fd, "source", "source")
            finally:
                os.close(parent_fd)
            occupied = root / ".source.remove-fixed"
            claimed = root / claimed_name
            self.assertFalse(source.exists())
            self.assertTrue(occupied.is_dir())
            self.assertEqual(list(occupied.iterdir()), [])
            self.assertEqual(claimed_name, ".source.remove-next")
            self.assertEqual((claimed / "manager").read_bytes(), b"manager\n")

    def test_remove_quarantined_release_preserves_keyboard_interrupt_when_cleanup_check_fails(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            quarantine = root / "quarantine"
            quarantine.mkdir(mode=0o700)
            for directory in ("scripts", "templates", "docs"):
                (quarantine / directory).mkdir(mode=0o700)
            (quarantine / "README.md").write_bytes(b"# Runtime\n")
            (quarantine / "release.json").write_bytes(json.dumps(MANIFEST).encode())
            (quarantine / "scripts" / "server.py").write_bytes(b"print('server')\n")
            (quarantine / "templates" / "template.txt").write_bytes(b"template\n")
            (quarantine / "docs" / "guide.md").write_bytes(b"# Guide\n")
            for file in quarantine.rglob("*"):
                if file.is_file():
                    file.chmod(0o600)
            snapshot = vibe_memory_install._snapshot_owned_release(quarantine)
            real_inventory = vibe_memory_install._temporary_tree_inventory_fd
            calls = 0
            interruption = KeyboardInterrupt()

            def interrupt_on_claimed_inventory(fd: int, prefix: pathlib.Path = pathlib.Path()) -> object:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise interruption
                return real_inventory(fd, prefix)

            with mock.patch(
                "vibe_memory_install._temporary_tree_inventory_fd",
                side_effect=interrupt_on_claimed_inventory,
            ), mock.patch(
                "vibe_memory_install._entry_exists",
                side_effect=OSError("cleanup check failed"),
            ):
                with self.assertRaises(KeyboardInterrupt) as raised:
                    vibe_memory_install._remove_quarantined_release(quarantine, snapshot)
            self.assertIs(raised.exception, interruption)

    def test_restore_quarantined_release_preserves_keyboard_interrupt(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            quarantine = root / "quarantine"
            quarantine.mkdir(mode=0o700)
            for directory in ("scripts", "templates", "docs"):
                (quarantine / directory).mkdir(mode=0o700)
            (quarantine / "README.md").write_bytes(b"# Runtime\n")
            (quarantine / "release.json").write_bytes(json.dumps(MANIFEST).encode())
            (quarantine / "scripts" / "server.py").write_bytes(b"print('server')\n")
            (quarantine / "templates" / "template.txt").write_bytes(b"template\n")
            (quarantine / "docs" / "guide.md").write_bytes(b"# Guide\n")
            for file in quarantine.rglob("*"):
                if file.is_file():
                    file.chmod(0o600)
            snapshot = vibe_memory_install._snapshot_owned_release(quarantine)
            interruption = KeyboardInterrupt()
            with mock.patch(
                "vibe_memory_install._snapshot_directory_fd",
                side_effect=interruption,
            ):
                with self.assertRaises(KeyboardInterrupt) as raised:
                    vibe_memory_install._restore_quarantined_release(
                        root / "restored", quarantine, snapshot
                    )
            self.assertIs(raised.exception, interruption)
            self.assertTrue(quarantine.is_dir())

    def test_materialize_release_snapshot_does_not_replace_concurrent_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            target = root / "restored"
            outside = root / "outside"
            outside.write_bytes(b"keep\n")
            entries = {
                "scripts": ("directory", None),
                "templates": ("directory", None),
                "docs": ("directory", None),
                "README.md": ("file", b"manager\n"),
                "release.json": ("file", json.dumps(MANIFEST).encode()),
            }
            real_noreplace = vibe_memory_install._rename_noreplace_at

            def race_noreplace(
                parent_fd: int,
                source_name: str,
                destination_parent_fd: int,
                destination_name: str,
            ) -> None:
                target.symlink_to(outside)
                return real_noreplace(parent_fd, source_name, destination_parent_fd, destination_name)

            with mock.patch(
                "vibe_memory_install._rename_noreplace_at", side_effect=race_noreplace
            ), self.assertRaises(vibe_memory_install.InstallError):
                vibe_memory_install._materialize_release_snapshot(target, entries)
            self.assertEqual(outside.read_bytes(), b"keep\n")
            self.assertTrue(target.is_symlink())

    def test_restore_regular_file_absent_expected_rejects_late_foreign_addition(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            target = root / "managed"
            calls = 0
            real_snapshot = vibe_memory_install._snapshot_regular_file

            def add_foreign_after_snapshot(path: pathlib.Path) -> object:
                nonlocal calls
                result = real_snapshot(path)
                calls += 1
                if calls == 1:
                    path.write_bytes(b"foreign\n")
                return result

            with mock.patch(
                "vibe_memory_install._snapshot_regular_file", side_effect=add_foreign_after_snapshot
            ), self.assertRaises(vibe_memory_install.InstallError):
                vibe_memory_install._restore_regular_file(
                    target, None, expected_current=None
                )
            self.assertEqual(target.read_bytes(), b"foreign\n")

    def test_restore_regular_file_existing_expected_rejects_foreign_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            target = root / "managed"
            target.write_bytes(b"manager\n")
            target.chmod(0o600)
            expected = vibe_memory_install._snapshot_regular_file(target)
            assert expected is not None
            calls = 0
            real_snapshot = vibe_memory_install._snapshot_regular_file

            def replace_after_check(path: pathlib.Path) -> object:
                nonlocal calls
                result = real_snapshot(path)
                calls += 1
                if calls == 1:
                    path.write_bytes(b"foreign\n")
                    path.chmod(0o600)
                return result

            with mock.patch(
                "vibe_memory_install._snapshot_regular_file", side_effect=replace_after_check
            ), self.assertRaises(vibe_memory_install.InstallError):
                vibe_memory_install._restore_regular_file(
                    target,
                    (expected[0], b"restored\n", expected[2]),
                    expected_current=expected,
                )
            self.assertEqual(target.read_bytes(), b"foreign\n")

    def test_restore_regular_file_rejects_ancestor_symlink_rebind(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            parent = root / "safe" / "nested"
            parent.mkdir(parents=True)
            outside = root / "outside" / "nested"
            outside.mkdir(parents=True)
            target = parent / "managed"
            outside_target = outside / "managed"
            outside_target.write_bytes(b"outside\n")
            real_validate = vibe_memory_install._validate_install_ancestor_chain
            rebound = False

            def validate_then_rebind(path: pathlib.Path) -> None:
                nonlocal rebound
                real_validate(path)
                if not rebound and path == parent:
                    rebound = True
                    moved = root / "safe-original"
                    (root / "safe").rename(moved)
                    (root / "safe").symlink_to(root / "outside", target_is_directory=True)

            with mock.patch(
                "vibe_memory_install._validate_install_ancestor_chain",
                side_effect=validate_then_rebind,
            ), self.assertRaises((OSError, ValueError, vibe_memory_install.InstallError)):
                vibe_memory_install._restore_regular_file(
                    target,
                    ((1, 1), b"manager\n", 0o600),
                    expected_current=None,
                )
            self.assertTrue(rebound)
            self.assertEqual(outside_target.read_bytes(), b"outside\n")

    def test_unlink_regular_file_rejects_ancestor_symlink_rebind(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            parent = root / "safe" / "nested"
            parent.mkdir(parents=True)
            target = parent / "managed"
            target.write_bytes(b"manager\n")
            target.chmod(0o600)
            outside = root / "outside" / "nested"
            outside.mkdir(parents=True)
            outside_target = outside / "managed"
            outside_target.write_bytes(b"outside\n")
            real_validate = vibe_memory_install._validate_install_ancestor_chain
            rebound = False

            def validate_then_rebind(path: pathlib.Path) -> None:
                nonlocal rebound
                real_validate(path)
                if not rebound and path == parent:
                    rebound = True
                    moved = root / "safe-original"
                    (root / "safe").rename(moved)
                    (root / "safe").symlink_to(root / "outside", target_is_directory=True)

            expected = vibe_memory_install._snapshot_regular_file(target)
            with mock.patch(
                "vibe_memory_install._validate_install_ancestor_chain",
                side_effect=validate_then_rebind,
            ), self.assertRaises((OSError, ValueError, vibe_memory_install.InstallError)):
                vibe_memory_install._unlink_regular_file(target, expected=expected)
            self.assertTrue(rebound)
            self.assertEqual(outside_target.read_bytes(), b"outside\n")

    def test_verify_private_release_closes_directory_fd(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = RuntimeInstallTest().make_source(root)
            paths = self.make_paths(root)
            vibe_memory_install.install_runtime(source, paths)
            release = paths.install_root / "releases" / "1.0.0"
            entries = vibe_memory_install._snapshot_source_release(source)
            releases_fd = os.open(paths.install_root / "releases", vibe_memory_install._DIRECTORY_OPEN_FLAGS)
            anchored = vibe_memory_install._AnchoredPath(releases_fd, "1.0.0", release)
            child_fd: int | None = None
            real_open = vibe_memory_install.os.open

            def capture_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
                nonlocal child_fd
                descriptor = real_open(path, flags, *args, **kwargs)
                if path == "1.0.0":
                    child_fd = descriptor
                return descriptor

            with mock.patch("vibe_memory_install.os.open", side_effect=capture_open):
                vibe_memory_install._verify_and_make_private_release(anchored, entries)
            os.close(releases_fd)
            assert child_fd is not None
            with self.assertRaises(OSError):
                os.fstat(child_fd)

    def test_lifecycle_hook_rollback_uses_immediate_commit_snapshots(self) -> None:
        operations = ("update", "rollback", "repair")
        failure_modes = (
            "failure",
            "failure_conflict_restart_failure",
            "keyboard_interrupt",
            "system_exit_conflict",
            "system_exit_post_commit",
            "system_exit_restore_io_failure",
            "keyboard_interrupt_bootout_failure",
            "system_exit_restart_failure",
            "late_external_then_failure",
        )

        for operation in operations:
            operation_failure_modes = failure_modes + (
                ("system_exit_release_cleanup_failure",)
                if operation == "update"
                else ()
            )
            for failure_mode in operation_failure_modes:
                with self.subTest(operation=operation, failure=failure_mode), tempfile.TemporaryDirectory() as value:
                    root = pathlib.Path(value)
                    paths = self.make_paths(root)
                    releases = paths.install_root / "releases"
                    for version in ("0.9.0", "1.0.0"):
                        (releases / version).mkdir(parents=True, exist_ok=True)
                    current = paths.install_root / "current"
                    current.symlink_to("releases/1.0.0")
                    clients = ["codex", "claude-code"]
                    codex, _agent, _label = vibe_memory_install._hook_target_for_client(paths, "codex")
                    claude, _agent, _label = vibe_memory_install._hook_target_for_client(paths, "claude-code")
                    codex.parent.mkdir(parents=True)
                    claude.parent.mkdir(parents=True)
                    codex.write_text('{"before":"codex"}\n', encoding="utf-8")
                    claude.write_text('{"before":"claude"}\n', encoding="utf-8")
                    original = codex.read_bytes()
                    external = b'{"third_party":true}\n'
                    interruption: BaseException
                    if failure_mode.startswith("keyboard_interrupt"):
                        interruption = KeyboardInterrupt()
                    elif failure_mode.startswith("system_exit"):
                        interruption = SystemExit(47)
                    else:
                        interruption = RuntimeError("later hook failed")
                    commit_snapshot_flags: list[object] = []

                    def repair_hook(target: pathlib.Path, *_args: object, **kwargs: object) -> dict[str, object]:
                        commit_snapshot_flags.append(kwargs.get("_include_commit_snapshot"))
                        if pathlib.Path(target) == claude:
                            if failure_mode == "late_external_then_failure":
                                claude.write_text('{"managed":true}\n', encoding="utf-8")
                                committed = vibe_memory_install._snapshot_regular_file(claude)
                                return {
                                    "status": "updated",
                                    "changed": True,
                                    "_commit_snapshot": committed,
                                }
                            if failure_mode == "system_exit_post_commit":
                                claude.write_text('{"managed":true}\n', encoding="utf-8")
                                interruption._commit_snapshot = (
                                    vibe_memory_install._snapshot_regular_file(claude)
                                )
                            elif failure_mode == "system_exit_restore_io_failure":
                                codex.unlink()
                                codex.parent.rmdir()
                                codex.parent.write_bytes(b"blocks hook restore")
                            elif failure_mode == "system_exit_release_cleanup_failure":
                                shutil.rmtree(releases / "1.1.0")
                                (releases / "1.1.0").write_bytes(b"foreign release")
                            elif failure_mode not in {
                                "keyboard_interrupt",
                                "keyboard_interrupt_bootout_failure",
                                "system_exit_restart_failure",
                            }:
                                codex.write_bytes(external)
                            raise interruption
                        codex.write_text('{"managed":true}\n', encoding="utf-8")
                        committed = vibe_memory_install._snapshot_regular_file(codex)
                        return {
                            "status": "updated",
                            "changed": True,
                            "_commit_snapshot": committed,
                        }

                    state = {
                        "current_version": "1.0.0",
                        "previous_version": "0.9.0",
                        "port": 9123,
                        "python_executable": sys.executable,
                        "installed_clients": clients,
                    }
                    service_state: dict[str, str | None] = {"version": None}
                    activation_attempts = 0

                    def install_runtime(*_args: object, **_kwargs: object) -> dict[str, str]:
                        (releases / "1.1.0").mkdir(parents=True, exist_ok=True)
                        return {"version": "1.1.0"}

                    def activate_service(
                        _paths: vibe_memory_paths.RuntimePaths,
                        *,
                        expected_version: str,
                    ) -> dict[str, str]:
                        nonlocal activation_attempts
                        activation_attempts += 1
                        if failure_mode == "late_external_then_failure" and activation_attempts == 1:
                            raise vibe_memory_install.InstallError("health failed")
                        if failure_mode in {
                            "failure_conflict_restart_failure",
                            "system_exit_restart_failure",
                        }:
                            raise PermissionError("restart denied")
                        service_state["version"] = expected_version
                        return {"status": "healthy"}

                    with contextlib.ExitStack() as stack:
                        stack.enter_context(mock.patch("vibe_memory_install._current_version", return_value="1.0.0"))
                        stack.enter_context(mock.patch("vibe_memory_install._installed_clients", return_value=clients))
                        stack.enter_context(mock.patch("vibe_memory_install.read_install_state", return_value=state))
                        stack.enter_context(mock.patch("vibe_memory_install._repair_metadata", return_value=(9123, sys.executable, clients, "0.9.0")))
                        stack.enter_context(mock.patch(
                            "vibe_memory_install._managed_release_version",
                            side_effect=lambda path: pathlib.Path(path).name,
                        ))
                        stack.enter_context(mock.patch("vibe_memory_install.validate_runtime_source", return_value={"version": "1.1.0"}))
                        stack.enter_context(mock.patch("vibe_memory_install.install_runtime", side_effect=install_runtime))
                        stack.enter_context(mock.patch("vibe_memory_install.install_runtime_config"))
                        stack.enter_context(mock.patch("vibe_memory_install.install_launcher", return_value={"status": "current"}))
                        stack.enter_context(mock.patch("vibe_memory_install.install_launch_agent", return_value={"status": "current"}))
                        stack.enter_context(mock.patch("vibe_memory_install._validate_control_plane", return_value={"control": "ok"}))
                        stack.enter_context(mock.patch(
                            "vibe_memory_install.write_install_state",
                            side_effect=(
                                lambda *_args, **_kwargs: codex.write_bytes(external)
                                if failure_mode == "late_external_then_failure"
                                else None
                            ),
                        ))
                        stack.enter_context(mock.patch(
                            "vibe_memory_install.activate_launch_agent",
                            side_effect=activate_service,
                        ))
                        stack.enter_context(mock.patch(
                            "vibe_memory_install.bootout_launch_agent",
                            side_effect=(
                                PermissionError("bootout denied")
                                if failure_mode == "keyboard_interrupt_bootout_failure"
                                else None
                            ),
                        ))
                        stack.enter_context(mock.patch("vibe_memory_hooks.repair", side_effect=repair_hook))

                        if operation == "update":
                            invoke = lambda: vibe_memory_install.update(root / "source", paths, validation={"control": "ok"})
                        elif operation == "rollback":
                            invoke = lambda: vibe_memory_install.rollback(paths)
                        else:
                            invoke = lambda: vibe_memory_install.repair(paths)

                        if failure_mode in {
                            "failure",
                            "failure_conflict_restart_failure",
                            "late_external_then_failure",
                        }:
                            with self.assertRaises(vibe_memory_install.InstallError) as raised:
                                invoke()
                            self.assertIn(str(codex), str(raised.exception))
                            expected_original = (
                                "health failed"
                                if failure_mode == "late_external_then_failure"
                                else "later hook failed"
                            )
                            self.assertIn(expected_original, str(raised.exception))
                            if failure_mode == "failure_conflict_restart_failure":
                                self.assertIn("restart denied", str(raised.exception))
                        else:
                            with self.assertRaises(type(interruption)) as raised:
                                invoke()
                            self.assertIs(raised.exception, interruption)
                            if isinstance(interruption, SystemExit):
                                self.assertEqual(interruption.code, 47)

                    self.assertEqual(commit_snapshot_flags, [True, True])
                    self.assertEqual(os.readlink(current), "releases/1.0.0")
                    if operation == "update":
                        if failure_mode == "system_exit_release_cleanup_failure":
                            self.assertEqual(
                                (releases / "1.1.0").read_bytes(),
                                b"foreign release",
                            )
                        else:
                            self.assertFalse((releases / "1.1.0").exists())
                    if failure_mode not in {
                        "failure_conflict_restart_failure",
                        "system_exit_restart_failure",
                    }:
                        self.assertEqual(service_state["version"], "1.0.0")

                    if failure_mode in {
                        "keyboard_interrupt",
                        "system_exit_post_commit",
                        "keyboard_interrupt_bootout_failure",
                        "system_exit_restart_failure",
                        "system_exit_release_cleanup_failure",
                    }:
                        self.assertEqual(codex.read_bytes(), original)
                        self.assertEqual(
                            claude.read_text(encoding="utf-8"),
                            '{"before":"claude"}\n',
                        )
                        expected_conflicts = {
                            "keyboard_interrupt_bootout_failure": ["bootout"],
                            "system_exit_restart_failure": ["service"],
                            "system_exit_release_cleanup_failure": [
                                str(releases / "1.1.0")
                            ],
                        }.get(failure_mode, [])
                        self.assertEqual(
                            getattr(interruption, "_rollback_conflicts", []),
                            expected_conflicts,
                        )
                        self.assertEqual(
                            len(getattr(interruption, "_cleanup_failures", [])),
                            len(expected_conflicts),
                        )
                    elif failure_mode == "system_exit_restore_io_failure":
                        self.assertTrue(codex.parent.is_file())
                        self.assertEqual(
                            getattr(interruption, "_rollback_conflicts", []),
                            [str(codex)],
                        )
                        self.assertIn(
                            "NotADirectoryError",
                            getattr(interruption, "_cleanup_failures", [""])[0],
                        )
                    else:
                        self.assertEqual(codex.read_bytes(), external)
                        if failure_mode == "system_exit_conflict":
                            self.assertIn(
                                str(codex),
                                getattr(interruption, "_rollback_conflicts", []),
                            )


if __name__ == "__main__":
    unittest.main()
