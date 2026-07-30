from __future__ import annotations

import json
import concurrent.futures
import os
import pathlib
import plistlib
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

    def test_failed_copy_cleans_temporary_release_and_preserves_current(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            paths = self.make_paths(root)
            old_release = paths.install_root / "releases/0.9.0"
            old_release.mkdir(parents=True)
            os.symlink("releases/0.9.0", paths.install_root / "current")

            def fail_copy(source_root: pathlib.Path, temporary_release: pathlib.Path) -> None:
                (temporary_release / "partial").write_text("partial", encoding="utf-8")
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

    def test_rejects_symlinks_in_source_tree(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            source = self.make_source(root)
            outside = root / "outside-secret"
            outside.write_text("secret", encoding="utf-8")
            os.symlink(outside, source / "scripts/linked.py")

            with self.assertRaises(ValueError):
                vibe_memory_install.install_runtime(source, self.make_paths(root))

            self.assertFalse((self.make_paths(root).install_root / "current").exists())

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
