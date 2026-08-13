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

    def test_first_run_normalization_requires_exact_types_retention_and_safe_workspace(self) -> None:
        workspace = pathlib.Path(self.temporary.name) / "workspace"
        workspace.mkdir()
        request = {
            "codex_hooks": True,
            "claude_hooks": False,
            "automatic_candidate_checks": True,
            "personal_short_retention_days": 14,
            "start_at_login": False,
            "service_port": 9123,
            "workspace": str(workspace),
        }
        normalized = settings.normalize_first_run_request(
            self.paths, request, manager_source_root=ROOT
        )
        self.assertEqual(normalized["workspace"], str(workspace.resolve()))
        self.assertEqual(normalized["settings"]["service_port"], 9123)
        self.assertTrue(normalized["settings"]["first_run_complete"])
        for key, value in (
            ("codex_hooks", 1),
            ("personal_short_retention_days", 7),
            ("service_port", True),
        ):
            with self.subTest(key=key):
                invalid = dict(request)
                invalid[key] = value
                with self.assertRaises(ValueError):
                    settings.normalize_first_run_request(
                        self.paths, invalid, manager_source_root=ROOT
                    )
        for forbidden in (
            self.paths.install_root,
            self.paths.install_root / "current",
            ROOT,
        ):
            forbidden.mkdir(parents=True, exist_ok=True)
            invalid = dict(request, workspace=str(forbidden))
            with self.assertRaisesRegex(ValueError, "workspace must not"):
                settings.normalize_first_run_request(
                    self.paths, invalid, manager_source_root=ROOT
                )

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
<!-- vibe-memory:managed-short -->
expires_on: 2026-07-29

expired temporary context

## active-temporary-context
<!-- vibe-memory:managed-short -->
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

    def test_retention_zero_removes_only_manager_marked_sections(self) -> None:
        source = """# Personal Short Memory

## managed
<!-- vibe-memory:managed-short -->

temporary

## user-owned
expires_on: 2020-01-01

keep exactly
"""
        self.short_memory.write_text(source, encoding="utf-8")
        result = settings.prune_personal_short(
            self.short_memory, today=dt.date(2026, 7, 30), retention_days=0
        )
        self.assertEqual(result, ["managed"])
        current = self.short_memory.read_text(encoding="utf-8")
        self.assertNotIn("temporary", current)
        self.assertIn("keep exactly", current)

    def test_retention_rejects_malformed_managed_expiry_and_preserves_unmarked_text(self) -> None:
        source = """# Personal Short Memory

## managed
<!-- vibe-memory:managed-short -->
expires_on: invalid

temporary

## user-owned
expires_on: 2020-01-01

keep exactly
"""
        self.short_memory.write_text(source, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "malformed managed short expiry"):
            settings.prune_personal_short(
                self.short_memory, today=dt.date(2026, 7, 30), retention_days=14
            )
        current = self.short_memory.read_text(encoding="utf-8")
        self.assertIn("expires_on: invalid", current)
        self.assertIn("## user-owned\nexpires_on: 2020-01-01\n\nkeep exactly", current)

    def test_first_run_failure_restores_settings_hooks_registry_plist_and_short_memory(self) -> None:
        config = self.paths.install_root / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({**settings.default_settings(), "port": 8897}), encoding="utf-8")
        codex_hook = self.home / ".codex/hooks.json"
        codex_hook.parent.mkdir(parents=True, exist_ok=True)
        codex_hook.write_text('{"old":"hook"}\n', encoding="utf-8")
        registry = self.paths.project_registry
        registry.parent.mkdir(parents=True)
        registry.write_text('{"current_project":"","projects":[]}\n', encoding="utf-8")
        plist = self.paths.launch_agent
        plist.parent.mkdir(parents=True)
        plist.write_text("old plist\n", encoding="utf-8")
        self.short_memory.write_text("old short\n", encoding="utf-8")
        before = {path: path.read_bytes() for path in (config, codex_hook, registry, plist, self.short_memory)}
        workspace = pathlib.Path(self.temporary.name) / "workspace"
        workspace.mkdir()

        def mutate_hooks(*_args: object) -> dict[str, object]:
            codex_hook.write_text('{"new":"hook"}\n', encoding="utf-8")
            return {}

        def register(_workspace: str) -> dict[str, object]:
            registry.write_text('{"current_project":"new","projects":[]}\n', encoding="utf-8")
            return {}

        with mock.patch.object(settings, "reconcile_hooks", side_effect=mutate_hooks), \
                mock.patch.object(settings, "reconcile_launch_agent", side_effect=RuntimeError("boom")), \
                mock.patch.object(settings.vibe_memory_install, "bootout_launch_agent"):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                settings.apply_first_run(
                    self.paths,
                    {
                        "codex_hooks": True,
                        "claude_hooks": False,
                        "automatic_candidate_checks": True,
                        "personal_short_retention_days": 30,
                        "start_at_login": False,
                        "service_port": 9123,
                        "workspace": str(workspace),
                    },
                    manager_source_root=ROOT,
                    register_workspace=register,
                )

        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content, str(path))

    def test_first_run_updates_install_state_with_exact_enabled_clients_and_port(self) -> None:
        state = {
            "schema_version": 1, "current_version": "1.0.0", "previous_version": None,
            "hook_protocol_version": 1, "data_schema_version": 1, "port": 8897,
            "python_executable": sys.executable, "python_version": "3.14",
            "installed_clients": ["codex", "claude-code"],
        }
        vibe_memory_install.write_install_state(self.paths, state)
        with mock.patch.object(settings, "reconcile_hooks"), mock.patch.object(
            settings, "reconcile_launch_agent", return_value={}
        ), mock.patch.object(settings.vibe_memory_install, "read_runtime_config", return_value={
            "app_version": "1.0.0"
        }), mock.patch.object(settings.vibe_memory_install, "activate_launch_agent"):
            settings.apply_first_run(
                self.paths,
                {"codex_hooks": False, "claude_hooks": True, "service_port": 9123},
                manager_source_root=ROOT,
                register_workspace=mock.Mock(),
            )
        updated = vibe_memory_install.read_install_state(self.paths)
        self.assertEqual(updated["installed_clients"], ["claude-code"])
        self.assertEqual(updated["port"], 9123)
        self.assertEqual(updated["python_executable"], sys.executable)

    def test_start_at_login_requires_valid_runtime_config_and_activation(self) -> None:
        request = {"codex_hooks": True, "start_at_login": True}
        with mock.patch.object(settings, "reconcile_hooks"), mock.patch.object(
            settings, "reconcile_launch_agent", return_value={}
        ), mock.patch.object(
            settings.vibe_memory_install, "read_runtime_config", side_effect=ValueError("bad config")
        ), mock.patch.object(settings.vibe_memory_install, "bootout_launch_agent"):
            with self.assertRaisesRegex(ValueError, "bad config"):
                settings.apply_first_run(
                    self.paths, request, manager_source_root=ROOT,
                    register_workspace=mock.Mock(),
                )
        self.assertFalse(settings.load_settings(self.paths)["first_run_complete"])

    def test_rollback_reports_previous_service_restart_failure_after_restoring_files(self) -> None:
        old = settings.default_settings()
        settings.save_settings(self.paths, old)
        plist = self.paths.launch_agent
        plist.parent.mkdir(parents=True)
        plist.write_text("old plist\n", encoding="utf-8")
        with mock.patch.object(settings, "reconcile_hooks"), mock.patch.object(
            settings, "reconcile_launch_agent", side_effect=RuntimeError("original")
        ), mock.patch.object(settings.vibe_memory_install, "read_runtime_config", return_value={
            "app_version": "1.0.0"
        }), mock.patch.object(
            settings.vibe_memory_install, "activate_launch_agent", side_effect=RuntimeError("restart")
        ), mock.patch.object(settings.vibe_memory_install, "bootout_launch_agent"):
            with self.assertRaisesRegex(Exception, "restart"):
                settings.apply_first_run(
                    self.paths, {"start_at_login": False}, manager_source_root=ROOT,
                    register_workspace=mock.Mock(),
                )
        self.assertEqual(plist.read_text(encoding="utf-8"), "old plist\n")

    def test_managed_expiry_is_stable_across_sessions_and_eventually_expires(self) -> None:
        self.short_memory.write_text(
            "## managed\n<!-- vibe-memory:managed-short -->\n\ncontent\n", encoding="utf-8"
        )
        settings.prune_personal_short(self.short_memory, today=dt.date(2026, 7, 1), retention_days=14)
        first = self.short_memory.read_text(encoding="utf-8")
        self.assertIn("expires_on: 2026-07-15", first)
        settings.prune_personal_short(self.short_memory, today=dt.date(2026, 7, 10), retention_days=14)
        self.assertEqual(self.short_memory.read_text(encoding="utf-8"), first)
        self.assertEqual(
            settings.prune_personal_short(self.short_memory, today=dt.date(2026, 7, 16), retention_days=14),
            ["managed"],
        )

    def test_post_write_failure_preserves_concurrent_config_and_reports_cas_path(self) -> None:
        config = self.paths.install_root / "config.json"
        concurrent = b'{"concurrent":true}\n'
        real_save = settings.save_settings
        def write_replace_raise(paths: object, value: object) -> object:
            result = real_save(paths, value)
            config.write_bytes(concurrent)
            raise RuntimeError("post-write")
        with mock.patch.object(settings, "save_settings", side_effect=write_replace_raise), mock.patch.object(
            settings.vibe_memory_install, "bootout_launch_agent"
        ):
            with self.assertRaisesRegex(settings.FirstRunTransactionError, str(config)):
                settings.apply_first_run(
                    self.paths, {"start_at_login": False}, manager_source_root=ROOT,
                    register_workspace=mock.Mock(),
                )
        self.assertEqual(config.read_bytes(), concurrent)

    def test_managed_envelope_body_markers_and_headings_do_not_escape_record(self) -> None:
        body = "body\n## inside\nexpires_on: 1999-01-01\n<!-- vibe-memory:short:end -->\nend"
        envelope = settings.render_managed_short_envelope("approved", body)
        self.short_memory.write_text("prefix\n" + envelope + "\nsuffix\n", encoding="utf-8")
        removed = settings.prune_personal_short(
            self.short_memory, today=dt.date(2026, 8, 13), retention_days=0
        )
        self.assertEqual(removed, ["approved"])
        self.assertEqual(self.short_memory.read_text(encoding="utf-8"), "prefix\n\nsuffix\n")

    def test_malformed_managed_envelope_fails_closed_without_rewrite(self) -> None:
        malformed = "<!-- vibe-memory:short:begin length=4 sha256=" + "0" * 64 + " -->\nbody\n<!-- vibe-memory:short:end -->\n"
        self.short_memory.write_text(malformed, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "malformed managed short"):
            settings.prune_personal_short(self.short_memory, retention_days=0)
        self.assertEqual(self.short_memory.read_text(encoding="utf-8"), malformed)

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
