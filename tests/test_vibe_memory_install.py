from __future__ import annotations

import json
import concurrent.futures
import os
import pathlib
import plistlib
import shutil
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import vibe_memory_install
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
                with self.assertRaises(ValueError):
                    vibe_memory_install.install_runtime(source, paths)

            self.assertFalse((paths.install_root / "releases/1.0.0").exists())
            self.assertFalse(os.path.lexists(str(paths.install_root / "current")))

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
                    with self.assertRaises(ValueError):
                        vibe_memory_install.install_runtime(source, paths)

                self.assertEqual(injected, [True])
                self.assertFalse((paths.install_root / "releases/1.0.0").exists())
                self.assertFalse(os.path.lexists(str(paths.install_root / "current")))
                self.assertEqual(
                    list((paths.install_root / "releases").glob(".1.0.0.tmp-*")),
                    [],
                )

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

            text = vibe_memory_install.render_launch_agent(paths, port=8897)
            plist = plistlib.loads(text.encode("utf-8"))
            runtime = str(paths.install_root / "current")

            self.assertEqual(plist["Label"], "com.noema.vibe-memory")
            self.assertEqual(
                plist["ProgramArguments"],
                ["/usr/bin/python3", runtime + "/scripts/memory_review_server.py"],
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


if __name__ == "__main__":
    unittest.main()
