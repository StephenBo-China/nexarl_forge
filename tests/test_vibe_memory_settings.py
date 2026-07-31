from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import vibe_memory_paths
import vibe_memory_router
import vibe_memory_settings as settings
import vibe_memory_install
from vibe_memory_events import NormalizedEvent


class VibeMemorySettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.temporary.name) / "home"
        self.home.mkdir()
        self.paths = vibe_memory_paths.for_home(self.home)
        self.short_memory = self.paths.personal_memory / "short.md"
        self.short_memory.parent.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_defaults_are_exact_and_keep_safety_invariants(self) -> None:
        self.assertEqual(
            settings.default_settings(),
            {
                "schema_version": 1,
                "first_run_complete": False,
                "codex_hooks_enabled": True,
                "claude_hooks_enabled": False,
                "automatic_candidate_checks": True,
                "personal_short_retention_days": 30,
                "start_at_login": True,
                "formal_memory_requires_approval": True,
                "service_host": "127.0.0.1",
                "service_port": 8897,
            },
        )

    def test_load_accepts_legacy_runtime_config_and_save_preserves_metadata(self) -> None:
        config = self.paths.install_root / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
                    "app_version": "1.0.0",
                    "port": 9123,
                    "schema_version": 1,
                    "service": "vibe-memory",
                }
            ),
            encoding="utf-8",
        )

        loaded = settings.load_settings(self.paths)
        self.assertEqual(loaded["service_port"], 9123)
        loaded["automatic_candidate_checks"] = False
        settings.save_settings(self.paths, loaded)

        persisted = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(persisted["app_version"], "1.0.0")
        self.assertEqual(persisted["service"], "vibe-memory")
        self.assertEqual(persisted["port"], 9123)
        self.assertFalse(persisted["automatic_candidate_checks"])
        runtime = vibe_memory_install.read_runtime_config(self.paths)
        self.assertEqual(runtime["app_version"], "1.0.0")
        self.assertEqual(runtime["port"], 9123)

    def test_save_settings_preserves_persisted_python_metadata(self) -> None:
        vibe_memory_install.install_runtime_config(
            self.paths,
            port=9123,
            app_version="1.0.0",
            python_executable=sys.executable,
        )

        saved = settings.save_settings(self.paths, settings.load_settings(self.paths))
        runtime = vibe_memory_install.read_runtime_config(self.paths)

        self.assertEqual(saved["service_port"], 9123)
        self.assertEqual(runtime["python_executable"], str(pathlib.Path(sys.executable).absolute()))
        self.assertIn("python_version", runtime)

    def test_save_rejects_safety_changes_and_unknown_fields(self) -> None:
        for key, value in (
            ("formal_memory_requires_approval", False),
            ("service_host", "0.0.0.0"),
            ("service_port", 0),
            ("personal_short_retention_days", -1),
        ):
            with self.subTest(key=key):
                candidate = settings.default_settings()
                candidate[key] = value
                with self.assertRaises(ValueError):
                    settings.save_settings(self.paths, candidate)
        candidate = settings.default_settings()
        candidate["unexpected"] = True
        with self.assertRaises(ValueError):
            settings.save_settings(self.paths, candidate)

    def test_runtime_reinstall_preserves_settings_and_updates_shared_port(self) -> None:
        original = settings.default_settings()
        original["first_run_complete"] = True
        original["automatic_candidate_checks"] = False
        original["personal_short_retention_days"] = 14
        settings.save_settings(self.paths, original)

        vibe_memory_install.install_runtime_config(
            self.paths, port=9123, app_version="1.0.0"
        )

        persisted = settings.load_settings(self.paths)
        self.assertTrue(persisted["first_run_complete"])
        self.assertFalse(persisted["automatic_candidate_checks"])
        self.assertEqual(persisted["personal_short_retention_days"], 14)
        self.assertEqual(persisted["service_port"], 9123)

    def test_prune_removes_only_expired_structured_sections_and_backs_up_source(self) -> None:
        source = """# Personal Short Memory

Unstructured notes remain.

## temporary-project-context
expires_on: 2026-07-29

expired temporary context

## active-temporary-context
expires_on: 2026-07-30

active temporary context

## durable-without-expiry

keep this too
"""
        self.short_memory.write_text(source, encoding="utf-8")

        removed = settings.prune_personal_short(
            self.short_memory, today=dt.date(2026, 7, 30)
        )

        self.assertEqual(removed, ["temporary-project-context"])
        current = self.short_memory.read_text(encoding="utf-8")
        self.assertNotIn("expired temporary context", current)
        self.assertIn("active temporary context", current)
        self.assertIn("keep this too", current)
        backups = list(self.short_memory.parent.glob("short.md.bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), source)

    def test_prune_is_noop_for_invalid_expiry_and_creates_no_backup(self) -> None:
        source = "## invalid\nexpires_on: next-week\n\nkeep\n"
        self.short_memory.write_text(source, encoding="utf-8")
        self.assertEqual(
            settings.prune_personal_short(
                self.short_memory, today=dt.date(2026, 7, 30)
            ),
            [],
        )
        self.assertEqual(self.short_memory.read_text(encoding="utf-8"), source)
        self.assertEqual(list(self.short_memory.parent.glob("short.md.bak.*")), [])

    def test_context_omits_candidate_reminder_when_automatic_checks_are_disabled(self) -> None:
        event = NormalizedEvent(
            agent="codex",
            event="UserPromptSubmit",
            cwd=self.home,
            session_id="session",
            timestamp="2026-07-30T00:00:00Z",
            payload_digest="a" * 64,
        )
        context = vibe_memory_router.build_context(
            event,
            project_root=None,
            pending={},
            automatic_candidate_checks=False,
        )
        self.assertIn("personal_memory/long.md", context)
        self.assertIn("personal_memory/short.md", context)
        self.assertNotIn("Candidate CLI", context)
        self.assertNotIn("at most two distilled candidates", context)

    def test_router_prunes_only_on_session_start(self) -> None:
        fake_paths = mock.Mock(
            install_root=self.home / "install",
            personal_memory=self.paths.personal_memory,
        )
        settings_value = settings.default_settings()
        with mock.patch.object(
            vibe_memory_router.vibe_memory_paths, "for_home", return_value=fake_paths
        ), mock.patch.object(
            vibe_memory_router.vibe_memory_settings,
            "load_settings",
            return_value=settings_value,
        ), mock.patch.object(
            vibe_memory_router.vibe_memory_settings, "prune_personal_short"
        ) as prune:
            for event_name in ("UserPromptSubmit", "SessionStart"):
                with self.subTest(event=event_name), mock.patch.object(
                    vibe_memory_router, "_registry_projects", return_value=[]
                ):
                    vibe_memory_router.handle_event(
                        "codex",
                        event_name,
                        {"session_id": event_name},
                        self.home,
                    )

        prune.assert_called_once_with(
            self.paths.personal_memory / "short.md",
            retention_days=30,
        )


if __name__ == "__main__":
    unittest.main()
