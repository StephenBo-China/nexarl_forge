from __future__ import annotations

import json
import os
import pathlib
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import vibe_memory_hooks as hooks


class ManagedCommandTest(unittest.TestCase):
    def test_generated_command_executes_in_bin_sh_without_marker_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            runtime = pathlib.Path(value) / 'runtime with "quotes" and \'apostrophe\''
            script = runtime / "scripts" / "vibe_memory_cli.py"
            script.parent.mkdir(parents=True)
            script.write_text(
                "import json, sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8"
            )

            result = subprocess.run(
                ["/bin/sh", "-c", hooks.command(runtime, "claude-code", "PostCompact")],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(result.stdout),
                ["hook", "--agent", "claude-code", "--event", "PostCompact"],
            )

    def test_command_covers_both_clients_and_shell_safe_runtime_paths(self) -> None:
        runtime = '/tmp/Vibe Runtime/with "quotes" and \'apostrophe\''

        for agent in ("codex", "claude-code"):
            for event in hooks.EVENTS:
                value = hooks.command(runtime, agent, event)
                executable, *arguments = shlex.split(value.split(" # ", 1)[0])
                self.assertEqual(executable, "/usr/bin/python3")
                self.assertEqual(arguments, [
                    str(pathlib.Path(os.path.abspath(runtime)) / "scripts" / "vibe_memory_cli.py"),
                    "hook", "--agent", agent,
                    "--event", event,
                ])
                self.assertIn(hooks.MANAGED_SIGNATURE, value)

    def test_command_rejects_unknown_agent_or_event(self) -> None:
        with self.assertRaisesRegex(ValueError, "agent"):
            hooks.command("/runtime", "claude", "Stop")
        with self.assertRaisesRegex(ValueError, "event"):
            hooks.command("/runtime", "codex", "PreToolUse")

    def test_relative_runtime_is_resolved_and_repeated_merge_is_idempotent(self) -> None:
        first = hooks.merge_document({"hooks": {}}, "codex", ".")
        second = hooks.merge_document(first, "codex", ".")

        self.assertEqual(first, second)
        generated = first["hooks"]["Stop"][0]["hooks"][0]["command"]
        script = shlex.split(generated.split(" # ", 1)[0])[1]
        self.assertTrue(pathlib.Path(script).is_absolute())
        self.assertEqual(
            pathlib.Path(script),
            pathlib.Path.cwd().resolve() / "scripts" / "vibe_memory_cli.py",
        )

    def test_command_keeps_current_symlink_lexically_stable(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            install_root = pathlib.Path(value) / "Vibe Memory"
            release = install_root / "releases/1.0.0"
            release.mkdir(parents=True)
            current = install_root / "current"
            current.symlink_to("releases/1.0.0")

            generated = hooks.command(current, "codex", "Stop")
            script = shlex.split(generated.split(" # ", 1)[0])[1]

            self.assertEqual(
                script,
                str(current / "scripts/vibe_memory_cli.py"),
            )
            self.assertNotIn("releases/1.0.0", script)


class MergeDocumentTest(unittest.TestCase):
    def test_prompt_handler_with_managed_command_shape_survives(self) -> None:
        prompt = {
            "type": "prompt",
            "command": hooks.command("/old", "codex", "Stop"),
        }
        source = {"hooks": {"Stop": [{"hooks": [prompt]}]}}

        cleaned = hooks.remove_managed_entries(source)

        self.assertEqual(cleaned["hooks"]["Stop"], [{"hooks": [prompt]}])

    def test_ownership_requires_exact_managed_command_tokens(self) -> None:
        managed = hooks.command("/old runtime", "codex", "Stop")
        command_prefix, command_comment = managed.split(" # ", 1)
        custom_commands = [
            f"printf '%s' '{hooks.MANAGED_SIGNATURE}'",
            command_prefix,
            managed + " ",
            f"{command_prefix} extra-token # {command_comment}",
            "/usr/bin/python3 /tmp/not-vibe.py hook --agent codex --event Stop",
            "/usr/bin/python3 /runtime/scripts/vibe_memory_cli.py hook --agent other --event Stop",
            "/usr/bin/python3 /runtime/scripts/vibe_memory_cli.py hook --agent codex --event Other",
        ]
        source = {
            "hooks": {
                "Stop": [{
                    "hooks": [
                        {"type": "command", "command": managed},
                        *[{"command": command_value} for command_value in custom_commands],
                    ]
                }]
            }
        }

        cleaned = hooks.remove_managed_entries(source)

        self.assertEqual(
            [item["command"] for item in cleaned["hooks"]["Stop"][0]["hooks"]],
            custom_commands,
        )

    def test_empty_custom_group_survives_remove_and_merge(self) -> None:
        custom_group = {"matcher": "custom", "note": "keep", "hooks": []}
        source = {"hooks": {"Stop": [custom_group]}}

        cleaned = hooks.remove_managed_entries(source)
        merged = hooks.merge_document(source, "codex", "/runtime")

        self.assertEqual(cleaned["hooks"]["Stop"], [custom_group])
        self.assertEqual(merged["hooks"]["Stop"][0], custom_group)

    def test_merge_installs_into_object_without_hooks_and_preserves_unknown_fields(self) -> None:
        source = {"custom": {"keep": True}}

        merged = hooks.merge_document(source, "codex", "/runtime")

        self.assertEqual(source, {"custom": {"keep": True}})
        self.assertEqual(merged["custom"], source["custom"])
        self.assertEqual(set(merged["hooks"]), set(hooks.EVENTS))

    def test_merge_preserves_custom_groups_and_custom_handler_in_same_group(self) -> None:
        source = {
            "custom": {"keep": True},
            "hooks": {
                "SessionStart": [{
                    "matcher": "startup",
                    "note": "vibe-memory hook --agent is just explanatory text",
                    "hooks": [
                        {"type": "command", "command": "custom-start"},
                        {"type": "command", "command": hooks.command("/old", "codex", "SessionStart")},
                    ],
                }],
                "OtherEvent": [{"hooks": [{"command": "other-command"}]}],
            },
        }
        original = json.loads(json.dumps(source))

        merged = hooks.merge_document(source, "codex", "/runtime")

        self.assertEqual(source, original)
        self.assertEqual(merged["custom"], source["custom"])
        self.assertEqual(merged["hooks"]["OtherEvent"], source["hooks"]["OtherEvent"])
        session = merged["hooks"]["SessionStart"]
        self.assertEqual(session[0]["matcher"], "startup")
        self.assertEqual(session[0]["note"], source["hooks"]["SessionStart"][0]["note"])
        self.assertEqual(session[0]["hooks"], [{"type": "command", "command": "custom-start"}])
        self.assertEqual(len(session), 2)

    def test_merge_installs_exactly_one_current_handler_for_each_event_and_is_idempotent(self) -> None:
        first = hooks.merge_document({"unknown": 4, "hooks": {}}, "claude-code", "/tmp/runtime")
        second = hooks.merge_document(first, "claude-code", "/tmp/runtime")

        self.assertEqual(first, second)
        self.assertEqual(first["unknown"], 4)
        for event in hooks.EVENTS:
            managed = [
                handler["command"]
                for group in first["hooks"][event]
                for handler in group.get("hooks", [])
                if hooks.MANAGED_SIGNATURE in handler.get("command", "")
            ]
            self.assertEqual(managed, [hooks.command("/tmp/runtime", "claude-code", event)])

    def test_merge_rejects_invalid_root_or_hooks_without_mutation(self) -> None:
        for source, expected in (([], "root"), ({"hooks": []}, "hooks")):
            original = json.loads(json.dumps(source))
            with self.assertRaisesRegex(ValueError, expected):
                hooks.merge_document(source, "codex", "/runtime")
            self.assertEqual(source, original)

    def test_remove_managed_entries_leaves_unrelated_handlers_and_cleans_empty_groups(self) -> None:
        source = {
            "hooks": {
                "Stop": [
                    {"matcher": "all", "hooks": [
                        {"command": "custom-stop"},
                        {"type": "command", "command": hooks.command("/old", "codex", "Stop")},
                    ]},
                    {"hooks": [{
                        "type": "command",
                        "command": hooks.command("/old", "codex", "Stop"),
                    }]},
                ],
                "SessionStart": [{"command": "vibe-memory hook --agent in data, not handler"}],
            },
        }

        cleaned = hooks.remove_managed_entries(source)

        self.assertEqual(cleaned["hooks"]["Stop"], [{
            "matcher": "all", "hooks": [{"command": "custom-stop"}],
        }])
        self.assertEqual(cleaned["hooks"]["SessionStart"], source["hooks"]["SessionStart"])
        self.assertEqual(source["hooks"]["Stop"][0]["hooks"], [
            {"command": "custom-stop"},
            {"type": "command", "command": hooks.command("/old", "codex", "Stop")},
        ])


class DocumentIOTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "Darwin rename flags only")
    def test_darwin_rename_swap_exchanges_two_files(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            first = root / "first"
            second = root / "second"
            first.write_bytes(b"first")
            second.write_bytes(b"second")

            hooks._darwin_rename(first, second, hooks.RENAME_SWAP)

            self.assertEqual(first.read_bytes(), b"second")
            self.assertEqual(second.read_bytes(), b"first")

    def test_load_rejects_bom_utf16_and_utf32_without_repair_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            payloads = (
                b"\xef\xbb\xbf" + b'{"hooks": {}}',
                '{"hooks": {}}'.encode("utf-16"),
                '{"hooks": {}}'.encode("utf-32"),
            )
            for index, original in enumerate(payloads):
                path = root / f"encoded-{index}.json"
                path.write_bytes(original)
                before_mtime = path.stat().st_mtime_ns

                with self.assertRaisesRegex(ValueError, "UTF-8"):
                    hooks.load_document(path)
                self.assertEqual(hooks.status(path, "codex", "/runtime")["status"], "malformed")
                with self.assertRaisesRegex(ValueError, "UTF-8"):
                    hooks.repair(path, "codex", "/runtime")

                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(path.stat().st_mtime_ns, before_mtime)
                self.assertEqual(list(root.glob(f"{path.name}.bak.*")), [])

    def test_symlink_configs_are_rejected_without_changing_live_or_broken_targets(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            live_target = root / "live-target.json"
            live_target.write_text('{"custom": true}\n', encoding="utf-8")
            broken_target = root / "missing-target.json"
            for name, target in (("live", live_target), ("broken", broken_target)):
                link = root / f"{name}.json"
                link.symlink_to(target)
                before = target.read_bytes() if target.exists() else None

                with self.assertRaisesRegex(ValueError, "symlink"):
                    hooks.load_document(link)
                self.assertEqual(hooks.status(link, "codex", "/runtime")["status"], "malformed")
                with self.assertRaisesRegex(ValueError, "symlink"):
                    hooks.repair(link, "codex", "/runtime")
                with self.assertRaisesRegex(ValueError, "symlink"):
                    hooks.write_with_backup(link, {"hooks": {}})

                self.assertTrue(link.is_symlink())
                self.assertEqual(target.read_bytes() if target.exists() else None, before)
                self.assertEqual(list(root.glob(f"{name}.json.bak.*")), [])

    def test_load_rejects_non_finite_json_and_repair_preserves_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            for index, constant in enumerate(("NaN", "Infinity", "-Infinity")):
                path = root / f"non-finite-{index}.json"
                original = f'{{"value": {constant}}}\n'.encode()
                path.write_bytes(original)

                with self.assertRaisesRegex(ValueError, "Invalid JSON"):
                    hooks.load_document(path)
                self.assertEqual(hooks.status(path, "codex", "/runtime")["status"], "malformed")
                with self.assertRaisesRegex(ValueError, "Invalid JSON"):
                    hooks.repair(path, "codex", "/runtime")
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(list(root.glob(f"{path.name}.bak.*")), [])

    def test_write_fsyncs_temp_backup_and_directory_entries(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = pathlib.Path(value) / "settings.json"
            path.write_text('{"hooks": {}}\n', encoding="utf-8")
            real_fsync = os.fsync
            fsync_calls: list[int] = []

            with mock.patch(
                "vibe_memory_hooks.os.fsync",
                side_effect=lambda descriptor: fsync_calls.append(descriptor) or real_fsync(descriptor),
            ):
                hooks.write_with_backup(path, {"hooks": {"Stop": []}})

            self.assertEqual(len(fsync_calls), 4)

    def test_temp_mode_is_applied_before_temp_fsync(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = pathlib.Path(value) / "settings.json"
            path.write_text('{"hooks": {}}\n', encoding="utf-8")
            path.chmod(0o640)
            events: list[str] = []
            real_fchmod = os.fchmod
            real_fsync = os.fsync

            def track_fchmod(descriptor: int, mode: int) -> None:
                events.append(f"fchmod:{mode:o}")
                real_fchmod(descriptor, mode)

            def track_fsync(descriptor: int) -> None:
                events.append("fsync")
                real_fsync(descriptor)

            with mock.patch("vibe_memory_hooks.os.fchmod", side_effect=track_fchmod), mock.patch(
                "vibe_memory_hooks.os.fsync", side_effect=track_fsync
            ):
                hooks.write_with_backup(path, {"hooks": {"Stop": []}})

            self.assertEqual(events[:2], ["fchmod:640", "fsync"])

    def test_failure_before_replace_preserves_original_and_removes_incomplete_backup(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = pathlib.Path(value) / "settings.json"
            original = b'{"hooks": {}}\n'
            path.write_bytes(original)
            real_fsync = os.fsync
            call_count = 0

            def fail_directory_sync(descriptor: int) -> None:
                nonlocal call_count
                call_count += 1
                if call_count == 3:
                    raise OSError("directory sync failed")
                real_fsync(descriptor)

            with mock.patch("vibe_memory_hooks.os.fsync", side_effect=fail_directory_sync):
                with self.assertRaisesRegex(OSError, "directory sync failed"):
                    hooks.write_with_backup(path, {"hooks": {"Stop": []}})

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.glob("settings.json.bak.*")), [])

    def test_failure_after_replace_preserves_and_reports_backup(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = pathlib.Path(value) / "settings.json"
            original = b'{"hooks": {}}\n'
            path.write_bytes(original)
            real_fsync = os.fsync
            call_count = 0

            def fail_final_sync(descriptor: int) -> None:
                nonlocal call_count
                call_count += 1
                if call_count == 4:
                    raise OSError("final directory sync failed")
                real_fsync(descriptor)

            with mock.patch("vibe_memory_hooks.os.fsync", side_effect=fail_final_sync):
                with self.assertRaisesRegex(hooks.ConfigWriteError, "backup") as raised:
                    hooks.write_with_backup(path, {"hooks": {"Stop": []}})

            self.assertIsNotNone(raised.exception.backup)
            backup = pathlib.Path(raised.exception.backup)
            self.assertEqual(backup.read_bytes(), original)
            self.assertNotEqual(path.read_bytes(), original)

    def test_load_document_missing_and_malformed_content_do_not_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = pathlib.Path(value) / "settings.json"
            self.assertEqual(hooks.load_document(path), {})

            for text, expected in (("{ bad", "Invalid JSON"), ("[]", "root must be an object")):
                path.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, expected):
                    hooks.load_document(path)
                self.assertEqual(path.read_text(encoding="utf-8"), text)

    def test_write_with_backup_is_atomic_preserves_permissions_and_skips_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = pathlib.Path(value) / "private" / "settings.json"
            first = hooks.write_with_backup(path, {"hooks": {}})
            self.assertTrue(first["changed"])
            self.assertIsNone(first["backup"])
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

            path.chmod(0o640)
            original = path.read_bytes()
            second = hooks.write_with_backup(path, {"hooks": {"Stop": []}})
            backup = pathlib.Path(second["backup"])
            self.assertTrue(second["changed"])
            self.assertEqual(backup.read_bytes(), original)
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o640)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

            third = hooks.write_with_backup(path, {"hooks": {"Stop": []}})
            self.assertFalse(third["changed"])
            self.assertIsNone(third["backup"])
            self.assertEqual(len(list(path.parent.glob("settings.json.bak.*"))), 1)

    def test_write_does_not_change_permissions_of_an_existing_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            parent = pathlib.Path(value) / "existing"
            parent.mkdir()
            parent.chmod(0o755)

            hooks.write_with_backup(parent / "settings.json", {"hooks": {}})

            self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o755)

    def test_write_rejects_nonserializable_or_nonobject_without_rewrite_and_atomic_failure_preserves_original(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = pathlib.Path(value) / "settings.json"
            original = b'{"hooks": {}}\n'
            path.write_bytes(original)
            path.chmod(0o600)

            with self.assertRaisesRegex(ValueError, "object"):
                hooks.write_with_backup(path, [])
            with self.assertRaisesRegex(ValueError, "serializable"):
                hooks.write_with_backup(path, {"bad": {1}})
            self.assertEqual(path.read_bytes(), original)

            with mock.patch("vibe_memory_hooks.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    hooks.write_with_backup(path, {"hooks": {"Stop": []}})
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(list(path.parent.glob("settings.json.bak.*")), [])


class StatusAndRepairTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "Darwin atomic exchange only")
    def test_repair_converges_e3_active_and_preserves_e1_e2(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            path = root / "hooks.json"
            versions = {
                1: b'{"external": "E1", "hooks": {}}\n',
                2: b'{"external": "E2", "hooks": {}}\n',
                3: b'{"external": "E3", "hooks": {}}\n',
            }
            path.write_bytes(versions[1])
            real_rename = hooks._darwin_rename
            swap_count = 0

            def race_each_correction(source, destination, flags):
                nonlocal swap_count
                if flags == hooks.RENAME_SWAP:
                    swap_count += 1
                    if swap_count <= 3:
                        replacement = root / f"external-{swap_count}.json"
                        replacement.write_bytes(versions[swap_count])
                        os.replace(replacement, destination)
                return real_rename(source, destination, flags)

            with mock.patch(
                "vibe_memory_hooks._darwin_rename", side_effect=race_each_correction
            ):
                with self.assertRaisesRegex(
                    hooks.ConcurrentConfigChange, "changed concurrently"
                ) as raised:
                    hooks.repair(path, "codex", "/runtime")

            self.assertGreaterEqual(swap_count, 4)
            self.assertEqual(path.read_bytes(), versions[3])
            conflict_contents = {
                artifact.read_bytes() for artifact in root.glob("hooks.json.conflict.*")
            }
            self.assertIn(versions[1], conflict_contents)
            self.assertIn(versions[2], conflict_contents)
            self.assertIsNotNone(raised.exception.attempt)
            self.assertIn(
                hooks.MANAGED_SIGNATURE.encode(),
                pathlib.Path(raised.exception.attempt).read_bytes(),
            )

    @unittest.skipUnless(sys.platform == "darwin", "Darwin atomic exchange only")
    def test_repair_continuous_writer_stops_at_bound_without_losing_versions(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            path = root / "hooks.json"
            initial = b'{"external": "E0", "hooks": {}}\n'
            path.write_bytes(initial)
            real_rename = hooks._darwin_rename
            injected: list[bytes] = []
            swap_count = 0

            def continuous_writer(source, destination, flags):
                nonlocal swap_count
                if flags == hooks.RENAME_SWAP:
                    swap_count += 1
                    version = (
                        initial
                        if swap_count == 1
                        else json.dumps({"external": f"E{swap_count}", "hooks": {}}).encode()
                        + b"\n"
                    )
                    replacement = root / f"external-{swap_count}.json"
                    replacement.write_bytes(version)
                    os.replace(replacement, destination)
                    injected.append(version)
                return real_rename(source, destination, flags)

            with mock.patch(
                "vibe_memory_hooks._darwin_rename", side_effect=continuous_writer
            ):
                with self.assertRaisesRegex(
                    hooks.ContinuousConfigChange, "continuous concurrent writes"
                ) as raised:
                    hooks.repair(path, "codex", "/runtime")

            self.assertEqual(
                swap_count,
                hooks.DARWIN_RECOVERY_SWAP_LIMIT + 1,
            )
            retained_paths = [
                path,
                *root.glob("hooks.json.conflict.*"),
                *root.glob("hooks.json.attempt.*"),
            ]
            retained_contents = {retained.read_bytes() for retained in retained_paths}
            self.assertTrue(set(injected).issubset(retained_contents))
            self.assertIn("continuous", str(raised.exception))
            self.assertGreaterEqual(len(raised.exception.recovery_paths), len(injected))

    @unittest.skipUnless(sys.platform == "darwin", "Darwin atomic exchange only")
    def test_repair_rollback_race_keeps_e2_active_and_preserves_e1(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            path = root / "hooks.json"
            e1 = b'{"external": "E1", "hooks": {}}\n'
            e2 = b'{"external": "E2", "hooks": {}}\n'
            path.write_bytes(e1)
            path.chmod(0o640)
            real_rename = hooks._darwin_rename
            swap_count = 0

            def race_rollback(source, destination, flags):
                nonlocal swap_count
                if flags == hooks.RENAME_SWAP:
                    swap_count += 1
                    replacement = root / f"external-{swap_count}.json"
                    if swap_count == 1:
                        replacement.write_bytes(e1)
                        replacement.chmod(0o640)
                        os.replace(replacement, destination)
                    elif swap_count == 2:
                        replacement.write_bytes(e2)
                        replacement.chmod(0o600)
                        os.replace(replacement, destination)
                return real_rename(source, destination, flags)

            with mock.patch("vibe_memory_hooks._darwin_rename", side_effect=race_rollback):
                with self.assertRaisesRegex(
                    hooks.ConcurrentConfigChange, "changed concurrently"
                ) as raised:
                    hooks.repair(path, "codex", "/runtime")

            self.assertGreaterEqual(swap_count, 3)
            self.assertEqual(path.read_bytes(), e2)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertIsNotNone(raised.exception.backup)
            self.assertEqual(pathlib.Path(raised.exception.backup).read_bytes(), e1)
            self.assertEqual(
                stat.S_IMODE(pathlib.Path(raised.exception.backup).stat().st_mode),
                0o640,
            )
            self.assertIsNotNone(raised.exception.attempt)
            self.assertIn(
                hooks.MANAGED_SIGNATURE.encode(),
                pathlib.Path(raised.exception.attempt).read_bytes(),
            )

    @unittest.skipUnless(sys.platform == "darwin", "Darwin atomic exchange only")
    def test_repair_post_swap_active_change_preserves_original_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            path = root / "hooks.json"
            original = b'{"external": "original", "hooks": {}}\n'
            newer = b'{"external": "newer", "hooks": {}}\n'
            path.write_bytes(original)
            path.chmod(0o640)
            real_rename = hooks._darwin_rename
            injected = False

            def race_after_swap(source, destination, flags):
                nonlocal injected
                result = real_rename(source, destination, flags)
                if not injected and flags == hooks.RENAME_SWAP:
                    replacement = root / "newer.json"
                    replacement.write_bytes(newer)
                    replacement.chmod(0o600)
                    os.replace(replacement, destination)
                    injected = True
                return result

            with mock.patch("vibe_memory_hooks._darwin_rename", side_effect=race_after_swap):
                with self.assertRaisesRegex(
                    hooks.ConcurrentConfigChange, "changed concurrently"
                ) as raised:
                    hooks.repair(path, "codex", "/runtime")

            self.assertTrue(injected)
            self.assertEqual(path.read_bytes(), newer)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertIsNotNone(raised.exception.backup)
            self.assertEqual(pathlib.Path(raised.exception.backup).read_bytes(), original)
            self.assertEqual(
                stat.S_IMODE(pathlib.Path(raised.exception.backup).stat().st_mode),
                0o640,
            )

    @unittest.skipUnless(sys.platform == "darwin", "Darwin atomic exchange only")
    def test_repair_recovery_artifact_failure_reports_unknown_temp(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            path = root / "hooks.json"
            original = b'{"external": "original", "hooks": {}}\n'
            newer = b'{"external": "newer", "hooks": {}}\n'
            path.write_bytes(original)
            real_rename = hooks._darwin_rename
            injected = False

            def race_after_swap(source, destination, flags):
                nonlocal injected
                result = real_rename(source, destination, flags)
                if not injected and flags == hooks.RENAME_SWAP:
                    replacement = root / "newer.json"
                    replacement.write_bytes(newer)
                    os.replace(replacement, destination)
                    injected = True
                return result

            with mock.patch(
                "vibe_memory_hooks._darwin_rename", side_effect=race_after_swap
            ), mock.patch(
                "vibe_memory_hooks._promote_recovery_path",
                side_effect=OSError("artifact fsync failed"),
                create=True,
            ):
                with self.assertRaisesRegex(
                    hooks.ConcurrentConfigChange, "changed concurrently"
                ) as raised:
                    hooks.repair(path, "codex", "/runtime")

            self.assertTrue(injected)
            self.assertEqual(path.read_bytes(), newer)
            self.assertIsNotNone(raised.exception.backup)
            recovery_path = pathlib.Path(raised.exception.backup)
            self.assertTrue(recovery_path.exists())
            self.assertEqual(recovery_path.read_bytes(), original)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin atomic exchange only")
    def test_repair_recovery_fsync_failure_reports_promoted_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            path = root / "hooks.json"
            original = b'{"external": "original", "hooks": {}}\n'
            newer = b'{"external": "newer", "hooks": {}}\n'
            path.write_bytes(original)
            original_identity = (path.stat().st_dev, path.stat().st_ino)
            real_rename = hooks._darwin_rename
            real_fsync = os.fsync
            injected = False

            def race_after_swap(source, destination, flags):
                nonlocal injected
                result = real_rename(source, destination, flags)
                if not injected and flags == hooks.RENAME_SWAP:
                    replacement = root / "newer.json"
                    replacement.write_bytes(newer)
                    os.replace(replacement, destination)
                    injected = True
                return result

            def fail_original_fsync(descriptor):
                metadata = os.fstat(descriptor)
                if (metadata.st_dev, metadata.st_ino) == original_identity:
                    raise OSError("artifact fsync failed")
                return real_fsync(descriptor)

            with mock.patch(
                "vibe_memory_hooks._darwin_rename", side_effect=race_after_swap
            ), mock.patch("vibe_memory_hooks.os.fsync", side_effect=fail_original_fsync):
                with self.assertRaisesRegex(
                    hooks.ConcurrentConfigChange, "changed concurrently"
                ) as raised:
                    hooks.repair(path, "codex", "/runtime")

            self.assertTrue(injected)
            self.assertEqual(path.read_bytes(), newer)
            self.assertIsNotNone(raised.exception.backup)
            recovery_path = pathlib.Path(raised.exception.backup)
            self.assertTrue(recovery_path.exists())
            self.assertEqual(recovery_path.read_bytes(), original)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin atomic exchange only")
    def test_repair_backup_failure_swaps_original_back(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            path = root / "hooks.json"
            original = b'{"hooks": {}}\n'
            path.write_bytes(original)
            real_promote = hooks._promote_recovery_path

            def fail_backup(target, source, label="conflict"):
                if label == "bak":
                    raise OSError("backup failed")
                return real_promote(target, source, label)

            with mock.patch(
                "vibe_memory_hooks._promote_recovery_path",
                side_effect=fail_backup,
            ):
                with self.assertRaises(OSError):
                    hooks.repair(path, "codex", "/runtime")

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(root.glob("hooks.json.bak.*")), [])

    @unittest.skipUnless(sys.platform == "darwin", "Darwin atomic exchange only")
    def test_repair_atomic_exchange_restores_external_atomic_save(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            path = root / "hooks.json"
            original = b'{"hooks": {}}\n'
            external = b'{"external": "atomic-save", "hooks": {}}\n'
            path.write_bytes(original)
            real_rename = hooks._darwin_rename
            injected = False

            def race_exchange(source, destination, flags):
                nonlocal injected
                if not injected and flags == hooks.RENAME_SWAP:
                    replacement = root / "external.json"
                    replacement.write_bytes(external)
                    os.replace(replacement, destination)
                    injected = True
                return real_rename(source, destination, flags)

            with mock.patch("vibe_memory_hooks._darwin_rename", side_effect=race_exchange):
                with self.assertRaisesRegex(
                    hooks.ConcurrentConfigChange, "changed concurrently"
                ) as raised:
                    hooks.repair(path, "codex", "/runtime")

            self.assertTrue(injected)
            self.assertEqual(path.read_bytes(), external)
            self.assertIsNotNone(raised.exception.backup)
            self.assertEqual(pathlib.Path(raised.exception.backup).read_bytes(), external)
            self.assertIsNotNone(raised.exception.attempt)
            self.assertIn(
                hooks.MANAGED_SIGNATURE.encode(),
                pathlib.Path(raised.exception.attempt).read_bytes(),
            )

    @unittest.skipUnless(sys.platform == "darwin", "Darwin atomic exchange only")
    def test_repair_restores_external_before_attempt_artifact_failure(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            path = root / "hooks.json"
            external = b'{"external": "must-survive", "hooks": {}}\n'
            path.write_bytes(b'{"hooks": {}}\n')
            real_rename = hooks._darwin_rename
            injected = False

            def race_exchange(source, destination, flags):
                nonlocal injected
                if not injected and flags == hooks.RENAME_SWAP:
                    replacement = root / "external.json"
                    replacement.write_bytes(external)
                    os.replace(replacement, destination)
                    injected = True
                return real_rename(source, destination, flags)

            with mock.patch(
                "vibe_memory_hooks._darwin_rename", side_effect=race_exchange
            ), mock.patch(
                "vibe_memory_hooks._create_attempt_exclusive",
                side_effect=OSError("attempt fsync failed"),
            ):
                with self.assertRaisesRegex(
                    hooks.ConcurrentConfigChange, "changed concurrently"
                ) as raised:
                    hooks.repair(path, "codex", "/runtime")

            self.assertTrue(injected)
            self.assertEqual(path.read_bytes(), external)
            self.assertIsNotNone(raised.exception.attempt)
            self.assertIn(
                hooks.MANAGED_SIGNATURE.encode(),
                pathlib.Path(raised.exception.attempt).read_bytes(),
            )

    @unittest.skipUnless(sys.platform == "darwin", "Darwin exclusive rename only")
    def test_repair_new_file_race_does_not_overwrite_external_creation(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            path = root / "hooks.json"
            external = b'{"external": "created-first", "hooks": {}}\n'
            real_rename = hooks._darwin_rename
            injected = False

            def race_exclusive(source, destination, flags):
                nonlocal injected
                if not injected and flags == hooks.RENAME_EXCL:
                    pathlib.Path(destination).write_bytes(external)
                    injected = True
                return real_rename(source, destination, flags)

            with mock.patch("vibe_memory_hooks._darwin_rename", side_effect=race_exclusive):
                with self.assertRaisesRegex(
                    hooks.ConcurrentConfigChange, "changed concurrently"
                ):
                    hooks.repair(path, "codex", "/runtime")

            self.assertTrue(injected)
            self.assertEqual(path.read_bytes(), external)
            self.assertEqual(list(root.glob("hooks.json.bak.*")), [])

    def test_status_validates_agent_before_reporting_missing(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = pathlib.Path(value) / "missing.json"

            with self.assertRaisesRegex(ValueError, "unsupported agent"):
                hooks.status(path, "unsupported", "/runtime")

    def test_repair_rejects_same_byte_external_inode_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = pathlib.Path(value) / "hooks.json"
            original = b'{"hooks": {}}\n'
            path.write_bytes(original)
            original_inode = path.stat().st_ino
            real_write = hooks.write_with_backup

            def replace_inode(target, document, **kwargs):
                replacement = pathlib.Path(value) / "external-same-bytes.json"
                replacement.write_bytes(original)
                os.replace(replacement, target)
                return real_write(target, document, **kwargs)

            with mock.patch("vibe_memory_hooks.write_with_backup", side_effect=replace_inode):
                with self.assertRaisesRegex(hooks.ConcurrentConfigChange, "changed concurrently"):
                    hooks.repair(path, "codex", "/runtime")

            self.assertEqual(path.read_bytes(), original)
            self.assertNotEqual(path.stat().st_ino, original_inode)
            self.assertEqual(list(path.parent.glob("hooks.json.bak.*")), [])

    def test_repair_restores_external_write_to_held_inode_during_replace(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = pathlib.Path(value) / "hooks.json"
            original = b'{"hooks": {}}\n'
            external = b'{"external": "final-window", "hooks": {}}\n'
            path.write_bytes(original)
            injected = False

            if sys.platform == "darwin":
                real_rename = hooks._darwin_rename

                def race_replace(source, destination, flags):
                    nonlocal injected
                    if not injected and flags == hooks.RENAME_SWAP:
                        path.write_bytes(external)
                        injected = True
                    return real_rename(source, destination, flags)

                patcher = mock.patch(
                    "vibe_memory_hooks._darwin_rename", side_effect=race_replace
                )
            else:
                real_replace = os.replace

                def race_replace(source, destination):
                    nonlocal injected
                    if not injected and pathlib.Path(destination) == path:
                        path.write_bytes(external)
                        injected = True
                    return real_replace(source, destination)

                patcher = mock.patch(
                    "vibe_memory_hooks.os.replace", side_effect=race_replace
                )

            with patcher:
                with self.assertRaisesRegex(hooks.ConcurrentConfigChange, "changed concurrently") as raised:
                    hooks.repair(path, "codex", "/runtime")

            self.assertTrue(injected)
            self.assertEqual(path.read_bytes(), external)
            if sys.platform == "darwin":
                self.assertIsNotNone(raised.exception.backup)
                self.assertEqual(
                    pathlib.Path(raised.exception.backup).read_bytes(), external
                )
            else:
                self.assertIsNotNone(raised.exception.backup)
                self.assertEqual(pathlib.Path(raised.exception.backup).read_bytes(), original)
            self.assertIsNotNone(raised.exception.attempt)
            attempt = pathlib.Path(raised.exception.attempt)
            self.assertIn(hooks.MANAGED_SIGNATURE.encode(), attempt.read_bytes())

    def test_repair_compare_and_swap_rejects_noncooperating_external_edit(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = pathlib.Path(value) / "hooks.json"
            path.write_text('{"hooks": {}}\n', encoding="utf-8")
            external = b'{"external": true, "hooks": {}}\n'
            real_write = hooks.write_with_backup
            lock_path = path.with_name(f".{path.name}.vibe-memory.lock")

            def race_write(target, document, **kwargs):
                self.assertTrue(lock_path.exists())
                pathlib.Path(target).write_bytes(external)
                return real_write(target, document, **kwargs)

            with mock.patch("vibe_memory_hooks.write_with_backup", side_effect=race_write):
                with self.assertRaisesRegex(hooks.ConcurrentConfigChange, "changed concurrently"):
                    hooks.repair(path, "codex", "/runtime")

            self.assertEqual(path.read_bytes(), external)
            self.assertEqual(list(path.parent.glob("hooks.json.bak.*")), [])

    def test_repair_compare_and_swap_rechecks_after_backup_before_replace(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = pathlib.Path(value) / "hooks.json"
            path.write_text('{"hooks": {}}\n', encoding="utf-8")
            external = b'{"external": "during-backup", "hooks": {}}\n'
            original = path.read_bytes()
            real_promote = hooks._promote_recovery_path

            def race_backup(target, source, label="conflict"):
                artifact = real_promote(target, source, label)
                if label == "bak":
                    pathlib.Path(target).write_bytes(external)
                return artifact

            with mock.patch(
                "vibe_memory_hooks._promote_recovery_path", side_effect=race_backup
            ):
                with self.assertRaisesRegex(
                    hooks.ConcurrentConfigChange, "changed concurrently"
                ) as raised:
                    hooks.repair(path, "codex", "/runtime")

            self.assertEqual(path.read_bytes(), external)
            self.assertIsNotNone(raised.exception.backup)
            self.assertEqual(pathlib.Path(raised.exception.backup).read_bytes(), original)

    def test_missing_hooks_object_is_drifted_without_mutation_then_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            for index, document in enumerate(({}, {"custom": {"keep": True}})):
                path = root / f"hooks-{index}.json"
                path.write_text(json.dumps(document), encoding="utf-8")
                before = path.read_bytes()
                before_mtime = path.stat().st_mtime_ns

                self.assertEqual(hooks.status(path, "codex", "/runtime")["status"], "drifted")
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(path.stat().st_mtime_ns, before_mtime)

                repaired = hooks.repair(path, "codex", "/runtime")
                self.assertTrue(repaired["changed"])
                self.assertEqual(repaired["status"], "updated")
                self.assertEqual(hooks.status(path, "codex", "/runtime")["status"], "current")
                merged = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(merged.get("custom"), document.get("custom"))
                self.assertEqual(set(merged["hooks"]), set(hooks.EVENTS))

    def test_repair_does_not_rewrite_structurally_current_document_with_different_formatting(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = pathlib.Path(value) / "hooks.json"
            current = hooks.merge_document({"hooks": {}}, "codex", "/runtime")
            path.write_text(
                json.dumps(current, separators=(",", ":"), sort_keys=True), encoding="utf-8"
            )
            before = path.read_bytes()
            before_mtime = path.stat().st_mtime_ns

            result = hooks.repair(path, "codex", "/runtime")

            self.assertFalse(result["changed"])
            self.assertEqual(result["status"], "current")
            self.assertIsNone(result["backup"])
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(path.stat().st_mtime_ns, before_mtime)

    def test_status_and_repair_report_missing_current_drifted_and_malformed_without_unwanted_writes(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            path = pathlib.Path(value) / "nested" / "hooks.json"
            self.assertEqual(hooks.status(path, "codex", "/runtime")["status"], "missing")

            created = hooks.repair(path, "codex", "/runtime")
            self.assertTrue(created["changed"])
            self.assertEqual(created["status"], "created")
            self.assertEqual(hooks.status(path, "codex", "/runtime")["status"], "current")

            document = json.loads(path.read_text(encoding="utf-8"))
            document["hooks"]["Stop"].append({"hooks": [{"command": "custom"}]})
            path.write_text(json.dumps(document), encoding="utf-8")
            self.assertEqual(hooks.status(path, "codex", "/runtime")["status"], "drifted")
            repaired = hooks.repair(path, "codex", "/runtime")
            self.assertTrue(repaired["changed"])
            self.assertIsNotNone(repaired["backup"])
            self.assertEqual(hooks.status(path, "codex", "/runtime")["status"], "current")

            malformed = "{ no"
            path.write_text(malformed, encoding="utf-8")
            before = path.read_bytes()
            self.assertEqual(hooks.status(path, "codex", "/runtime")["status"], "malformed")
            with self.assertRaisesRegex(ValueError, "Invalid JSON"):
                hooks.repair(path, "codex", "/runtime")
            self.assertEqual(path.read_bytes(), before)
