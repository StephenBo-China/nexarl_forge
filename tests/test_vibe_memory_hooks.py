from __future__ import annotations

import json
import os
import pathlib
import shlex
import stat
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import vibe_memory_hooks as hooks


class ManagedCommandTest(unittest.TestCase):
    def test_command_covers_both_clients_and_shell_safe_runtime_paths(self) -> None:
        runtime = '/tmp/Vibe Runtime/with "quotes" and \'apostrophe\''

        for agent in ("codex", "claude-code"):
            for event in hooks.EVENTS:
                value = hooks.command(runtime, agent, event)
                executable, *arguments = shlex.split(value.split(" # ", 1)[0])
                self.assertEqual(executable, "/usr/bin/python3")
                self.assertEqual(arguments, [
                    f"{runtime}/scripts/vibe_memory_cli.py", "hook", "--agent", agent,
                    "--event", event,
                ])
                self.assertIn(hooks.MANAGED_SIGNATURE, value)

    def test_command_rejects_unknown_agent_or_event(self) -> None:
        with self.assertRaisesRegex(ValueError, "agent"):
            hooks.command("/runtime", "claude", "Stop")
        with self.assertRaisesRegex(ValueError, "event"):
            hooks.command("/runtime", "codex", "PreToolUse")


class MergeDocumentTest(unittest.TestCase):
    def test_ownership_requires_exact_managed_command_tokens(self) -> None:
        managed = hooks.command("/old runtime", "codex", "Stop")
        command_prefix, command_comment = managed.split(" # ", 1)
        custom_commands = [
            f"printf '%s' '{hooks.MANAGED_SIGNATURE}'",
            f"{command_prefix} extra-token # {command_comment}",
            "/usr/bin/python3 /tmp/not-vibe.py hook --agent codex --event Stop",
            "/usr/bin/python3 /runtime/scripts/vibe_memory_cli.py hook --agent other --event Stop",
            "/usr/bin/python3 /runtime/scripts/vibe_memory_cli.py hook --agent codex --event Other",
        ]
        source = {
            "hooks": {
                "Stop": [{
                    "hooks": [
                        {"command": command_value} for command_value in [managed, *custom_commands]
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
                        {"command": hooks.command("/old", "codex", "Stop")},
                    ]},
                    {"hooks": [{"command": hooks.command("/old", "codex", "Stop")}]},
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
            {"command": hooks.command("/old", "codex", "Stop")},
        ])


class DocumentIOTest(unittest.TestCase):
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
            real_backup = hooks._create_backup_exclusive

            def race_backup(target, content, mode):
                backup = real_backup(target, content, mode)
                pathlib.Path(target).write_bytes(external)
                return backup

            with mock.patch(
                "vibe_memory_hooks._create_backup_exclusive", side_effect=race_backup
            ):
                with self.assertRaisesRegex(hooks.ConcurrentConfigChange, "changed concurrently"):
                    hooks.repair(path, "codex", "/runtime")

            self.assertEqual(path.read_bytes(), external)
            self.assertEqual(list(path.parent.glob("hooks.json.bak.*")), [])

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
