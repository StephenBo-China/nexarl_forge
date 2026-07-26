from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import memory_review_server as server
import ui_skill_registry as registry


class UIDesignServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.temp = pathlib.Path(self.temporary.name)
        self.project = self.temp / "project"
        self.project.mkdir()
        self.fixture = ROOT / "tests/fixtures/ui_skills/minimal"
        self.environment = mock.patch.dict(
            os.environ,
            {
                "UI_DESIGN_HOME": str(self.temp / "ui-design-home"),
                "CODEX_UI_SKILLS_DIR": str(self.temp / "codex-skills"),
                "CLAUDE_UI_SKILLS_DIR": str(self.temp / "claude-skills"),
            },
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def test_context_and_skill_routes_use_shared_domain_operations(self) -> None:
        context = server.ui_design_get(
            "/api/ui-design/context", {"project": str(self.project)}
        )
        imported = server.ui_design_post(
            "/api/ui-skills/import",
            {
                "source": {"type": "local", "path": str(self.fixture)},
                "scope": "global",
                "targets": ["codex", "claude"],
                "idempotency_key": "http-import-001",
            },
        )
        listed = server.ui_design_get("/api/ui-skills", {})

        self.assertEqual(context["project"], str(self.project))
        self.assertIn("effective_preferences", context)
        self.assertEqual(imported["status"], "validated")
        self.assertEqual(listed["items"][0]["id"], imported["id"])

    def test_mutating_routes_require_idempotency_and_dangerous_confirmation(self) -> None:
        with self.assertRaises(ValueError) as missing_key:
            server.ui_design_post(
                "/api/ui-design/preferences/project",
                {"project": str(self.project), "value": {}},
            )
        self.assertEqual(server.ui_design_error_status(missing_key.exception), 400)

        with self.assertRaises(PermissionError) as missing_confirmation:
            server.ui_design_post(
                "/api/ui-skills/approve",
                {
                    "draft_id": "draft-1",
                    "digest": "0" * 64,
                    "idempotency_key": "approve-001",
                },
            )
        self.assertEqual(server.ui_design_error_status(missing_confirmation.exception), 403)

    def test_exact_http_retry_returns_same_preference_result(self) -> None:
        body = {
            "project": str(self.project),
            "value": {"visual.radius": {"mode": "replace", "value": "5px"}},
            "idempotency_key": "preference-http-001",
        }

        first = server.ui_design_post("/api/ui-design/preferences/project", body)
        second = server.ui_design_post("/api/ui-design/preferences/project", body)

        self.assertEqual(first, second)
        context = server.ui_design_get(
            "/api/ui-design/context", {"project": str(self.project)}
        )
        self.assertEqual(
            context["effective_preferences"]["value"]["visual"]["radius"],
            "5px",
        )

    def test_digest_and_idempotency_conflicts_map_to_http_409(self) -> None:
        self.assertEqual(server.ui_design_error_status(registry.DigestConflict("stale")), 409)
        self.assertEqual(server.ui_design_error_status(registry.InvalidTransition("bad")), 409)

    def test_console_exposes_design_preferences_and_ui_skill_review_safely(self) -> None:
        html = server.page()

        self.assertIn("设计偏好", html)
        self.assertIn("UI Skills", html)
        self.assertIn("renderDesignPreferences", html)
        self.assertIn("renderUISkills", html)
        self.assertIn("esc(skill.skill_md", html)
        self.assertIn("批准并发布", html)
        self.assertNotIn("innerHTML = skill.skill_md", html)

    def test_http_skill_lifecycle_routes_share_cli_transactions(self) -> None:
        imported = server.ui_design_post(
            "/api/ui-skills/import",
            {
                "source": {"type": "local", "path": str(self.fixture)},
                "scope": "global",
                "targets": ["codex", "claude"],
                "idempotency_key": "lifecycle-import",
            },
        )
        validated = server.ui_design_post(
            "/api/ui-skills/validate",
            {"draft_id": imported["id"], "idempotency_key": "lifecycle-validate"},
        )
        approved = server.ui_design_post(
            "/api/ui-skills/approve",
            {
                "draft_id": imported["id"],
                "digest": imported["digest"],
                "confirmed": True,
                "idempotency_key": "lifecycle-approve",
            },
        )
        published = server.ui_design_post(
            "/api/ui-skills/publish",
            {
                "draft_id": imported["id"],
                "digest": imported["digest"],
                "confirmed": True,
                "idempotency_key": "lifecycle-publish",
            },
        )
        disabled = server.ui_design_post(
            "/api/ui-skills/disable",
            {
                "name": imported["name"],
                "confirmed": True,
                "idempotency_key": "lifecycle-disable",
            },
        )
        disabled_view = server.ui_design_get("/api/ui-skills", {})["items"][0]
        rolled_back = server.ui_design_post(
            "/api/ui-skills/rollback",
            {
                "name": imported["name"],
                "version": approved["version_id"],
                "confirmed": True,
                "idempotency_key": "lifecycle-rollback",
            },
        )

        self.assertEqual(validated["status"], "validated")
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(published["status"], "published")
        self.assertEqual(disabled["status"], "disabled")
        self.assertEqual(disabled_view["deployment_status"], "disabled")
        self.assertEqual(rolled_back["status"], "published")
        self.assertTrue((self.temp / "codex-skills" / imported["name"]).is_dir())
        self.assertTrue((self.temp / "claude-skills" / imported["name"]).is_dir())

    def test_scan_exact_retry_returns_stored_result(self) -> None:
        body = {"idempotency_key": "scan-http-001"}
        first = server.ui_design_post("/api/ui-skills/scan", body)
        unmanaged = self.temp / "codex-skills" / "later-skill"
        unmanaged.mkdir(parents=True)
        (unmanaged / "SKILL.md").write_text(
            "---\nname: later-skill\ndescription: Later\n---\n# Later\n",
            encoding="utf-8",
        )

        retry = server.ui_design_post("/api/ui-skills/scan", body)
        refreshed = server.ui_design_post(
            "/api/ui-skills/scan", {"idempotency_key": "scan-http-002"}
        )

        self.assertEqual(retry, first)
        self.assertEqual(refreshed["items"][0]["name"], "later-skill")


if __name__ == "__main__":
    unittest.main()
