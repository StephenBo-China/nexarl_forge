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
    def test_merge_preserves_custom_groups_and_custom_handler_in_same_group(self) -> None:
        source = {
            "custom": {"keep": True},
            "hooks": {
                "SessionStart": [{
                    "matcher": "startup",
                    "note": "vibe-memory hook --agent is just explanatory text",
                    "hooks": [
                        {"type": "command", "command": "custom-start"},
                        {"type": "command", "command": "old # vibe-memory hook --agent"},
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
                        {"command": "old # vibe-memory hook --agent"},
                    ]},
                    {"hooks": [{"command": "old # vibe-memory hook --agent"}]},
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
            {"command": "custom-stop"}, {"command": "old # vibe-memory hook --agent"},
        ])


class DocumentIOTest(unittest.TestCase):
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
