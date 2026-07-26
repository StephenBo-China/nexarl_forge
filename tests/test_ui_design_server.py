from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import memory_review_server as server
import memory_project
import memory_review
import ui_design_gate as gate
import ui_design_preferences as preferences
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
        self.original_project_registry = memory_project.REGISTRY_PATH
        memory_project.REGISTRY_PATH = self.temp / "projects.json"

    def tearDown(self) -> None:
        memory_project.REGISTRY_PATH = self.original_project_registry
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
        self.assertIn("UI 设计审批", html)
        self.assertIn("renderUIDesignApproval", html)
        self.assertIn("design_package", html)
        self.assertIn("project_global", html)
        self.assertIn("正式前端路径", html)
        self.assertIn("待审批设计包", html)
        self.assertNotIn("innerHTML = skill.skill_md", html)

    def test_operator_docs_cover_ui_control_recovery_and_forward_tests(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for expected in (
            "UI Design Control Plane",
            "design_package",
            "project_global",
            "request-revision",
            "CODEX_UI_SKILLS_DIR",
            "CLAUDE_UI_SKILLS_DIR",
            "hard_gate_enabled",
            "idempotency",
            "Real-client smoke test",
        ):
            self.assertIn(expected, readme)
        prompts = (
            ROOT
            / "docs/superpowers/forward-tests/2026-07-26-ui-design-control-plane-prompts.md"
        ).read_text(encoding="utf-8")
        for expected in (
            "Web SaaS checkout redesign",
            "React Native onboarding flow",
            "Mini-program appointment booking",
            "Do not modify formal frontend code before approval",
        ):
            self.assertIn(expected, prompts)

    def test_project_config_routes_manage_modes_paths_smoke_enable_and_relock(self) -> None:
        memory_project.init_project(self.project)
        paths = {
            "formal_frontend_paths": ["web/src/**"],
            "design_artifact_paths": ["codex/ui_design/design-packages/**"],
            "generated_paths": ["web/generated/**"],
            "test_artifact_paths": ["web/tests/**"],
        }
        configured = server.ui_design_post(
            "/api/ui-design/project-config/set-paths",
            {
                "project": str(self.project),
                "paths": paths,
                "idempotency_key": "http-paths-001",
            },
        )
        with self.assertRaises(PermissionError):
            server.ui_design_post(
                "/api/ui-design/project-config/set-mode",
                {
                    "project": str(self.project),
                    "mode": "project_global",
                    "idempotency_key": "http-mode-no-confirm",
                },
            )
        mode = server.ui_design_post(
            "/api/ui-design/project-config/set-mode",
            {
                "project": str(self.project),
                "mode": "design_package",
                "confirmed": True,
                "idempotency_key": "http-mode-001",
            },
        )
        enabled = server.ui_design_post(
            "/api/ui-design/project-config/enable-hard-gate",
            {
                "project": str(self.project),
                "confirmed": True,
                "idempotency_key": "http-enable-gate-001",
            },
        )
        relocked = server.ui_design_post(
            "/api/ui-design/project-config/relock",
            {
                "project": str(self.project),
                "confirmed": True,
                "idempotency_key": "http-relock-001",
            },
        )
        shown = server.ui_design_get(
            "/api/ui-design/project-config", {"project": str(self.project)}
        )

        self.assertEqual(configured["formal_frontend_paths"], ["web/src/**"])
        self.assertEqual(mode["gate_mode"], "design_package")
        self.assertTrue(enabled["hard_gate_enabled"])
        self.assertEqual(enabled["hook_smoke_test"]["codex"]["status"], "passed")
        self.assertTrue(relocked["relocked"])
        self.assertEqual(shown["config"], relocked)
        self.assertIn("gate_status", shown)

    def test_design_package_http_lifecycle_and_stale_digest_conflict(self) -> None:
        memory_project.init_project(self.project)
        manifest = {
            "schema_version": 1,
            "task_id": "checkout-redesign",
            "title": "Checkout redesign",
            "classification": "visual_change",
            "pages": ["checkout"],
            "components": ["CheckoutForm"],
            "allowed_file_patterns": ["web/src/checkout/**"],
            "design_files": [
                "design-brief.md",
                "interaction-spec.md",
                "responsive-spec.md",
            ],
            "status": "pending_approval",
        }
        created = server.ui_design_post(
            "/api/ui-design/packages/create",
            {
                "project": str(self.project),
                "manifest": manifest,
                "idempotency_key": "http-package-create-001",
            },
        )
        listed = server.ui_design_get(
            "/api/ui-design/packages", {"project": str(self.project)}
        )
        with self.assertRaises(gate.DigestConflict) as stale:
            server.ui_design_post(
                "/api/ui-design/packages/approve",
                {
                    "project": str(self.project),
                    "task_id": "checkout-redesign",
                    "digest": "0" * 64,
                    "confirmed": True,
                    "idempotency_key": "http-package-approve-stale",
                },
            )
        approved = server.ui_design_post(
            "/api/ui-design/packages/approve",
            {
                "project": str(self.project),
                "task_id": "checkout-redesign",
                "digest": created["digest"],
                "confirmed": True,
                "idempotency_key": "http-package-approve-001",
            },
        )
        revision = server.ui_design_post(
            "/api/ui-design/packages/request-revision",
            {
                "project": str(self.project),
                "task_id": "checkout-redesign",
                "reason": "Add touch error states",
                "idempotency_key": "http-package-request-revision-001",
            },
        )
        invalidated = server.ui_design_post(
            "/api/ui-design/packages/invalidate",
            {
                "project": str(self.project),
                "task_id": "checkout-redesign",
                "reason": "Scope changed",
                "confirmed": True,
                "idempotency_key": "http-package-invalidate-001",
            },
        )

        self.assertEqual(listed["items"][0]["task_id"], "checkout-redesign")
        self.assertEqual(server.ui_design_error_status(stale.exception), 409)
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(revision["status"], "revision_requested")
        self.assertEqual(invalidated["status"], "invalidated")

    def test_project_global_baseline_route_and_cli_parser(self) -> None:
        memory_project.init_project(self.project)
        config = memory_project.ui_design_config(self.project)
        config.update(
            {
                "gate_mode": "project_global",
                "formal_frontend_paths": ["web/src/**"],
                "project_global_baseline_task": "project-ui-baseline",
            }
        )
        memory_project.write_json(
            self.project / "codex/ui_design/config.json", config
        )
        manifest = {
            "schema_version": 1,
            "task_id": "project-ui-baseline",
            "title": "Project UI baseline",
            "classification": "visual_change",
            "pages": ["all"],
            "components": ["DesignSystem"],
            "allowed_file_patterns": ["web/src/**"],
            "design_files": [
                "design-brief.md",
                "interaction-spec.md",
                "responsive-spec.md",
            ],
            "status": "pending_approval",
        }
        package = gate.create_design_package(
            self.project,
            "project-ui-baseline",
            manifest,
            idempotency_key="baseline-domain-create-001",
        )
        approved = server.ui_design_post(
            "/api/ui-design/baseline/approve",
            {
                "project": str(self.project),
                "task_id": "project-ui-baseline",
                "digest": package["digest"],
                "confirmed": True,
                "idempotency_key": "http-baseline-approve-001",
            },
        )
        args = memory_review.build_parser().parse_args(
            [
                "ui-design",
                "project-config",
                "set-mode",
                "--project",
                str(self.project),
                "--mode",
                "design_package",
                "--confirmed",
                "--idempotency-key",
                "cli-mode-001",
            ]
        )

        self.assertEqual(approved["gate_mode"], "project_global")
        self.assertFalse(
            json.loads(
                (self.project / "codex/ui_design/config.json").read_text(
                    encoding="utf-8"
                )
            )["relocked"]
        )
        self.assertEqual(args.ui_design_command, "project-config")
        self.assertEqual(args.project_config_command, "set-mode")

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

    def test_end_to_end_ui_design_control_plane_uses_only_disposable_roots(self) -> None:
        memory_project.init_project(self.project)
        global_preferences = preferences.default_global_preferences()
        global_preferences["visual"]["radius"] = "8px"
        server.ui_design_post(
            "/api/ui-design/preferences/global",
            {
                "value": global_preferences,
                "idempotency_key": "e2e-global-preferences",
            },
        )
        server.ui_design_post(
            "/api/ui-design/preferences/project",
            {
                "project": str(self.project),
                "value": {
                    "visual.radius": {"mode": "replace", "value": "12px"}
                },
                "idempotency_key": "e2e-project-preferences",
            },
        )
        effective = server.ui_design_get(
            "/api/ui-design/context", {"project": str(self.project)}
        )["effective_preferences"]

        draft = server.ui_design_post(
            "/api/ui-skills/import",
            {
                "source": {"type": "local", "path": str(self.fixture)},
                "scope": "global",
                "targets": ["codex", "claude"],
                "idempotency_key": "e2e-skill-import",
            },
        )
        approved_skill = server.ui_design_post(
            "/api/ui-skills/approve",
            {
                "draft_id": draft["id"],
                "digest": draft["digest"],
                "confirmed": True,
                "idempotency_key": "e2e-skill-approve",
            },
        )
        publication = server.ui_design_post(
            "/api/ui-skills/publish",
            {
                "draft_id": draft["id"],
                "digest": draft["digest"],
                "confirmed": True,
                "idempotency_key": "e2e-skill-publish",
            },
        )

        server.ui_design_post(
            "/api/ui-design/project-config/set-paths",
            {
                "project": str(self.project),
                "paths": {
                    "formal_frontend_paths": ["web/src/**"],
                    "design_artifact_paths": [
                        "codex/ui_design/design-packages/**"
                    ],
                    "generated_paths": ["web/generated/**"],
                    "test_artifact_paths": ["web/tests/**"],
                },
                "idempotency_key": "e2e-set-paths",
            },
        )
        server.ui_design_post(
            "/api/ui-design/project-config/enable-hard-gate",
            {
                "project": str(self.project),
                "confirmed": True,
                "idempotency_key": "e2e-enable-gate",
            },
        )
        manifest = {
            "schema_version": 1,
            "task_id": "checkout-redesign",
            "title": "Checkout redesign",
            "classification": "visual_change",
            "pages": ["checkout"],
            "components": ["CheckoutForm"],
            "allowed_file_patterns": ["web/src/checkout/**"],
            "design_files": [
                "design-brief.md",
                "interaction-spec.md",
                "responsive-spec.md",
            ],
            "status": "pending_approval",
        }
        package = server.ui_design_post(
            "/api/ui-design/packages/create",
            {
                "project": str(self.project),
                "manifest": manifest,
                "idempotency_key": "e2e-package-create",
            },
        )
        denied_before = gate.decide_tool_use(
            self.project, "Write", {"file_path": "web/src/checkout/Form.tsx"}
        )
        server.ui_design_post(
            "/api/ui-design/packages/approve",
            {
                "project": str(self.project),
                "task_id": "checkout-redesign",
                "digest": package["digest"],
                "confirmed": True,
                "idempotency_key": "e2e-package-approve",
            },
        )
        allowed_after = gate.decide_tool_use(
            self.project, "Write", {"file_path": "web/src/checkout/Form.tsx"}
        )
        outside_scope = gate.decide_tool_use(
            self.project, "Write", {"file_path": "web/src/profile/Profile.tsx"}
        )
        (pathlib.Path(package["root"]) / "interaction-spec.md").write_text(
            "Changed interaction", encoding="utf-8"
        )
        invalidated = gate.decide_tool_use(
            self.project, "Write", {"file_path": "web/src/checkout/Form.tsx"}
        )

        unmanaged = self.temp / "codex-skills" / "unmanaged-ui"
        shutil.copytree(self.fixture, unmanaged)
        (unmanaged / "SKILL.md").write_text(
            "---\nname: unmanaged-ui\ndescription: Unmanaged fixture\n---\n# Unmanaged\n",
            encoding="utf-8",
        )
        before_digest = registry.package_digest(unmanaged)
        before_mtimes = {
            path.relative_to(unmanaged): path.stat().st_mtime_ns
            for path in unmanaged.rglob("*")
        }
        discovered = server.ui_design_get("/api/ui-skills", {})["discovered"]
        after_mtimes = {
            path.relative_to(unmanaged): path.stat().st_mtime_ns
            for path in unmanaged.rglob("*")
        }

        self.assertEqual(effective["value"]["visual"]["radius"], "12px")
        self.assertEqual(approved_skill["status"], "approved")
        self.assertEqual(publication["status"], "published")
        self.assertTrue((self.temp / "codex-skills/sample-ui").is_dir())
        self.assertTrue((self.temp / "claude-skills/sample-ui").is_dir())
        self.assertEqual(denied_before["decision"], "deny_pending_approval")
        self.assertEqual(allowed_after["decision"], "allow_approved_frontend_scope")
        self.assertEqual(outside_scope["decision"], "deny_scope_mismatch")
        self.assertEqual(invalidated["decision"], "deny_invalidated_approval")
        self.assertIn(
            "unmanaged-ui", {item.get("name") for item in discovered}
        )
        self.assertEqual(registry.package_digest(unmanaged), before_digest)
        self.assertEqual(after_mtimes, before_mtimes)


if __name__ == "__main__":
    unittest.main()
