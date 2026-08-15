from __future__ import annotations

import http.client
import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
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
import vibe_memory_paths
import vibe_memory_settings


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
        memory_project.register_project(self.project, make_current=False)

    def tearDown(self) -> None:
        memory_project.REGISTRY_PATH = self.original_project_registry
        self.environment.stop()
        self.temporary.cleanup()

    def http_request(
        self,
        method: str,
        path: str,
        host: str | None,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        worker = threading.Thread(target=httpd.serve_forever)
        worker.start()
        try:
            connection = http.client.HTTPConnection(*httpd.server_address, timeout=2)
            connection.putrequest(method, path, skip_host=True)
            if host is not None:
                connection.putheader("Host", host)
            for name, value in (headers or {}).items():
                connection.putheader(name, value)
            if body is not None:
                connection.putheader("Content-Length", str(len(body)))
            connection.endheaders(body)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()
            httpd.shutdown()
            worker.join(timeout=2)
            httpd.server_close()

    def test_health_payload_has_stable_service_identity_and_version(self) -> None:
        payload = server.health_payload()
        self.assertEqual(payload["service"], "vibe-memory")
        self.assertEqual(payload["app_version"], "1.0.0")
        self.assertEqual(payload["data_schema_version"], 1)
        self.assertIs(payload["ok"], True)

    def test_http_boundary_rejects_rebinding_hosts_before_active_memory_read(self) -> None:
        malicious_hosts = (
            "rebind.attacker:8897",
            "localhost.attacker:8897",
            "127.0.0.1@rebind.attacker:8897",
            "rebind.attacker@127.0.0.1:8897",
            "localhost.:8897",
            "127.0.0.1:0",
            "localhost:",
            "127.0.0.1:",
            "[::1]:",
            "localhost:8897#",
            "localhost:8897?",
            "[::1]:8897#",
            "localhost:8897%23",
            "[::1]evil:8897",
            "::1:8897",
            None,
        )
        for host in malicious_hosts:
            with self.subTest(host=host), mock.patch.object(
                server,
                "active_memory_payload",
                return_value={"secret": "personal-memory"},
            ) as active_memory:
                status, payload = self.http_request("GET", "/api/active-memory", host)
                self.assertEqual(status, 400)
                self.assertNotIn(b"personal-memory", payload)
                active_memory.assert_not_called()

    def test_http_boundary_rejects_rebinding_host_for_every_dispatched_method(self) -> None:
        cases = (
            ("GET", "/health", None, None),
            ("POST", "/api/refresh", None, None),
            ("HEAD", "/health", None, None),
            ("OPTIONS", "/health", None, None),
        )
        for method, path, body, headers in cases:
            with self.subTest(method=method):
                status, _payload = self.http_request(
                    method,
                    path,
                    "rebind.attacker:8897",
                    body=body,
                    headers=headers,
                )

            self.assertEqual(status, 400)

    def test_http_boundary_validates_request_target_before_dispatch(self) -> None:
        mismatches = (
            ("http://rebind.attacker:8897/api/active-memory", "localhost:8897"),
            ("http://localhost/api/active-memory", "localhost:8897"),
            ("http://[::1]:8897/api/active-memory", "localhost:8897"),
            ("http://[::1]:8898/api/active-memory", "[::1]:8897"),
            ("http://localhost:8897/api/active-memory#", "localhost:8897"),
            ("http://localhost:8897?/api/active-memory", "localhost:8897"),
            ("http://localhost%3f:8897/api/active-memory", "localhost:8897"),
            ("/api/active-memory#", "localhost:8897"),
        )
        for target, host in mismatches:
            with self.subTest(target=target, host=host), mock.patch.object(
                server,
                "active_memory_payload",
                return_value={"secret": "personal-memory"},
            ) as active_memory:
                status, payload = self.http_request("GET", target, host)
                self.assertEqual(status, 400)
                self.assertNotIn(b"personal-memory", payload)
                active_memory.assert_not_called()

        matches = (
            ("http://localhost:8897/api/active-memory", "localhost:8897"),
            ("http://LOCALHOST:08897/api/active-memory", "localhost:8897"),
            ("http://127.0.0.1:8897/api/active-memory", "127.0.0.1"),
            ("http://[::1]:8897/api/active-memory", "[::1]:8897"),
            ("http://localhost:8897/api/active-memory?scope=personal", "localhost:8897"),
        )
        for target, host in matches:
            with self.subTest(target=target, host=host), mock.patch.object(
                server,
                "active_memory_payload",
                return_value={"ok": True},
            ) as active_memory:
                status, payload = self.http_request("GET", target, host)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(payload), {"ok": True})
                active_memory.assert_called_once_with()

        with mock.patch.object(
            server, "active_memory_payload", return_value={"ok": True}
        ) as active_memory:
            status, payload = self.http_request(
                "GET", "/api/active-memory?scope=personal", "localhost:8897"
            )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload), {"ok": True})
        active_memory.assert_called_once_with()

    def test_http_boundary_preserves_loopback_hosts_and_unsupported_methods(self) -> None:
        for host in (
            "127.0.0.1",
            "localhost",
            "[::1]",
            "127.0.0.1:8897",
            "localhost:8897",
            "[::1]:8897",
        ):
            with self.subTest(host=host), mock.patch.object(
                server,
                "active_memory_payload",
                return_value={"ok": True},
            ) as active_memory:
                status, payload = self.http_request("GET", "/api/active-memory", host)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(payload), {"ok": True})
                active_memory.assert_called_once_with()

        for method in ("HEAD", "OPTIONS"):
            with self.subTest(method=method):
                status, _payload = self.http_request(
                    method, "/health", "127.0.0.1:8897"
                )
            self.assertEqual(status, 501)

    def settings_request(
        self, method: str, path: str, body: dict[str, object] | None = None
    ) -> tuple[int, dict[str, object]]:
        handler = object.__new__(server.Handler)
        handler.path = path
        handler.read_json = mock.Mock(return_value=body or {})
        handler.send_json = mock.Mock()
        if method == "GET":
            handler.do_GET()
        else:
            handler.do_POST()
        payload = handler.send_json.call_args.args[0]
        status = handler.send_json.call_args.kwargs.get("status", 200)
        return status, payload

    def test_settings_api_loads_defaults_and_rejects_disabling_approval(self) -> None:
        paths = vibe_memory_paths.for_home(self.temp / "settings-home")
        with mock.patch.object(
            server.vibe_memory_paths, "for_home", return_value=paths
        ):
            status, payload = self.settings_request("GET", "/api/settings")
            rejected_status, rejected = self.settings_request(
                "POST",
                "/api/settings/first-run",
                {"formal_memory_requires_approval": False},
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload, vibe_memory_settings.default_settings())
        self.assertEqual(rejected_status, 400)
        self.assertIn("formal_memory_requires_approval", rejected["error"])

    def test_first_run_saves_choices_reconciles_and_registers_only_explicit_workspace(self) -> None:
        paths = vibe_memory_paths.for_home(self.temp / "settings-home")
        workspace = self.temp / "chosen-workspace"
        workspace.mkdir()
        body = {
            "codex_hooks": False,
            "claude_hooks": True,
            "automatic_candidate_checks": False,
            "personal_short_retention_days": 14,
            "start_at_login": False,
            "service_port": 9123,
            "workspace": str(workspace),
        }
        with mock.patch.object(
            server.vibe_memory_paths, "for_home", return_value=paths
        ), mock.patch.object(
            server.vibe_memory_settings, "reconcile_hooks"
        ) as hooks, mock.patch.object(
            server.vibe_memory_settings, "reconcile_launch_agent"
        ) as launch, mock.patch.object(
            server.memory_project, "register_project", return_value={"projects": []}
        ) as register:
            status, payload = self.settings_request(
                "POST", "/api/settings/first-run", body
            )

        self.assertEqual(status, 200)
        self.assertTrue(payload["settings"]["first_run_complete"])
        self.assertFalse(payload["settings"]["automatic_candidate_checks"])
        self.assertEqual(payload["registered_project"], {"projects": []})
        register.assert_called_once_with(str(workspace.resolve()))

        persisted = vibe_memory_settings.load_settings(paths)
        self.assertEqual(persisted, payload["settings"])

    def test_first_run_without_workspace_never_registers_application_clone(self) -> None:
        paths = vibe_memory_paths.for_home(self.temp / "settings-home")
        with mock.patch.object(
            server.vibe_memory_paths, "for_home", return_value=paths
        ), mock.patch.object(
            server.vibe_memory_settings, "reconcile_hooks"
        ), mock.patch.object(
            server.vibe_memory_settings, "reconcile_launch_agent"
        ), mock.patch.object(server.memory_project, "register_project") as register:
            status, _payload = self.settings_request(
                "POST", "/api/settings/first-run", {"start_at_login": False}
            )

        self.assertEqual(status, 200)
        register.assert_not_called()

    def test_first_run_page_is_visible_until_setup_completes(self) -> None:
        html = server.first_run_page()
        for expected in (
            'name="codex_hooks"', 'name="claude_hooks"',
            'name="automatic_candidate_checks"', 'value="0"', 'value="14"',
            'value="30"', 'name="start_at_login"', 'name="service_port"',
            'name="workspace"', '/api/settings/first-run',
        ):
            self.assertIn(expected, html)

    def test_first_run_bootout_is_scheduled_only_after_response_is_sent(self) -> None:
        handler = object.__new__(server.Handler)
        handler.path = "/api/settings/first-run"
        handler.read_json = mock.Mock(return_value={})
        order: list[str] = []
        handler.send_json = mock.Mock(side_effect=lambda *_args, **_kwargs: order.append("response"))
        thread = mock.Mock()
        thread.start.side_effect = lambda: order.append("bootout")
        with mock.patch.object(
            server, "save_first_run_settings", return_value={
                "bootout_after_response": True, "service_action_generation": "generation"
            }
        ), mock.patch.object(server.threading, "Thread", return_value=thread) as make_thread:
            handler.do_POST()
        self.assertEqual(order, ["response", "bootout"])
        make_thread.assert_called_once_with(
            target=server.scheduled_bootout_worker,
            args=(mock.ANY, "generation"),
            daemon=False,
        )

    def test_first_run_rejects_unsafe_http_metadata_before_transaction(self) -> None:
        cases = (
            ({"Host": "evil.example", "Content-Type": "application/json", "Content-Length": "2"}, b"{}"),
            ({"Host": "127.0.0.1:8897", "Content-Type": "text/plain", "Content-Length": "2"}, b"{}"),
            ({"Host": "127.0.0.1:8897", "Origin": "https://evil.example", "Content-Type": "application/json", "Content-Length": "2"}, b"{}"),
            ({"Host": "127.0.0.1:8897", "Origin": "http://localhost:8897", "Content-Type": "application/json", "Content-Length": "2"}, b"{}"),
            ({"Host": "127.0.0.1:8898", "Content-Type": "application/json", "Content-Length": "2"}, b"{}"),
            ({"Host": "127.0.0.1:8897", "Content-Type": "application/json", "Content-Length": str(65537)}, b""),
        )
        for headers, raw in cases:
            with self.subTest(headers=headers):
                handler = object.__new__(server.Handler)
                handler.path = "/api/settings/first-run"
                handler.headers = headers
                handler.rfile = __import__("io").BytesIO(raw)
                handler.send_json = mock.Mock()
                with mock.patch.object(server, "save_first_run_settings") as save:
                    handler.do_POST()
                self.assertEqual(handler.send_json.call_args.kwargs["status"], 400)
                save.assert_not_called()

    def test_bootout_worker_persists_failure_and_is_non_daemon(self) -> None:
        paths = vibe_memory_paths.for_home(self.temp / "worker-home")
        settings = vibe_memory_settings.default_settings()
        settings["start_at_login"] = False
        vibe_memory_settings.save_settings(paths, settings)
        with mock.patch.object(server.vibe_memory_paths, "for_home", return_value=paths), mock.patch.object(
            server.vibe_memory_settings.vibe_memory_install, "bootout_launch_agent",
            side_effect=RuntimeError("launchctl failed"),
        ):
            action = server.write_service_action(paths, desired=False)
            server.complete_scheduled_bootout(paths, str(action["generation"]))
        persisted = json.loads(server.service_action_path(paths).read_text(encoding="utf-8"))
        self.assertIn("launchctl failed", persisted["error"])

    def test_stale_bootout_generation_cannot_stop_new_start_request(self) -> None:
        paths = vibe_memory_paths.for_home(self.temp / "generation-home")
        stale = server.write_service_action(paths, desired=False)
        current = server.write_service_action(paths, desired=True)
        with mock.patch.object(
            server.vibe_memory_settings.vibe_memory_install, "bootout_launch_agent"
        ) as bootout:
            server.scheduled_bootout_worker(paths, stale["generation"])
        bootout.assert_not_called()
        self.assertEqual(server.read_service_action(paths)["generation"], current["generation"])
        self.assertTrue(server.read_service_action(paths)["desired_start_at_login"])

    def test_only_latest_false_generation_boots_out(self) -> None:
        paths = vibe_memory_paths.for_home(self.temp / "two-worker-home")
        stale = server.write_service_action(paths, desired=False)
        current = server.write_service_action(paths, desired=False)
        with mock.patch.object(
            server.vibe_memory_settings.vibe_memory_install, "bootout_launch_agent"
        ) as bootout:
            server.scheduled_bootout_worker(paths, stale["generation"])
            server.scheduled_bootout_worker(paths, current["generation"])
        bootout.assert_called_once_with(vibe_memory_paths.for_home())

    def test_bootout_and_true_transaction_serialize_on_shared_lifecycle_lock(self) -> None:
        paths = vibe_memory_paths.for_home(self.temp / "serialized-home")
        action = server.write_service_action(paths, desired=False)
        entered = threading.Event()
        release = threading.Event()
        order: list[str] = []
        def bootout(_paths: vibe_memory_paths.RuntimePaths) -> None:
            order.append("bootout-start")
            entered.set()
            release.wait(2)
            order.append("bootout-end")
        worker = threading.Thread(
            target=server.scheduled_bootout_worker,
            args=(paths, str(action["generation"])),
        )
        with mock.patch.object(
            server.vibe_memory_settings.vibe_memory_install,
            "bootout_launch_agent", side_effect=bootout,
        ):
            worker.start()
            self.assertTrue(entered.wait(1))
            true_finished = threading.Event()
            def true_request() -> None:
                with vibe_memory_settings.lifecycle_lock(paths):
                    server.write_service_action(paths, desired=True)
                    order.append("true")
                true_finished.set()
            true_thread = threading.Thread(target=true_request)
            true_thread.start()
            self.assertFalse(true_finished.wait(0.05))
            release.set()
            worker.join(2)
            true_thread.join(2)
        self.assertEqual(order, ["bootout-start", "bootout-end", "true"])

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

    def test_project_domain_get_rejects_missing_blank_and_unregistered_project(self) -> None:
        unregistered = self.temp / "unregistered"
        unregistered.mkdir()
        with mock.patch.object(server.review, "PROJECT_ROOT", None), mock.patch.object(
            server.ui_design_preferences, "load_project_overrides", return_value={}
        ) as load_project:
            for query in ({}, {"project": [""]}, {"project": str(unregistered)}):
                with self.subTest(query=query), self.assertRaisesRegex(
                    ValueError, "registered project"
                ):
                    server.ui_design_get("/api/ui-design/context", query)
            status, payload = self.settings_request(
                "GET", "/api/ui-design/context?project="
            )

        self.assertEqual(status, 400)
        self.assertIn("registered project", payload["error"])
        load_project.assert_not_called()

    def test_ui_skill_import_maps_all_source_payloads(self) -> None:
        cases = (
            (
                {"type": "editor", "files": {"SKILL.md": "---\nname: demo\n---\n"}},
                {
                    "editor_json": mock.ANY,
                    "local": None,
                    "zip": None,
                    "github": None,
                    "path": None,
                },
            ),
            (
                {"type": "local", "path": "/tmp/demo-skill"},
                {
                    "editor_json": None,
                    "local": "/tmp/demo-skill",
                    "zip": None,
                    "github": None,
                    "path": None,
                },
            ),
            (
                {"type": "zip", "path": "/tmp/demo-skill.zip"},
                {
                    "editor_json": None,
                    "local": None,
                    "zip": "/tmp/demo-skill.zip",
                    "github": None,
                    "path": None,
                },
            ),
            (
                {
                    "type": "github",
                    "repo": "owner/repository",
                    "path": "skills/demo",
                    "revision": "a" * 40,
                },
                {
                    "editor_json": None,
                    "local": None,
                    "zip": None,
                    "github": "owner/repository",
                    "path": "skills/demo",
                },
            ),
        )

        for index, (source, expected) in enumerate(cases, start=1):
            with self.subTest(source=source["type"]):
                with mock.patch.object(
                    server.ui_design_cli,
                    "dispatch",
                    return_value={"status": "validated"},
                ) as dispatch:
                    result = server.ui_design_post(
                        "/api/ui-skills/import",
                        {
                            "source": source,
                            "scope": "project",
                            "project": str(self.project),
                            "targets": ["codex"],
                            "version_label": "2.3.4",
                            "idempotency_key": f"source-map-{index}",
                        },
                    )

                namespace = dispatch.call_args.args[0]
                self.assertEqual(result["status"], "validated")
                self.assertEqual(namespace.scope, "project")
                self.assertEqual(namespace.project, str(self.project))
                self.assertEqual(namespace.targets, "codex")
                self.assertEqual(namespace.version_label, "2.3.4")
                self.assertEqual(namespace.revision, source.get("revision"))
                for key, value in expected.items():
                    if value is mock.ANY:
                        self.assertIsNotNone(getattr(namespace, key))
                    else:
                        self.assertEqual(getattr(namespace, key), value)

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
        self.assertIn("openUISkillImportWizard()", html)
        self.assertIn("uiSkillImportWizard", html)
        self.assertIn("1 选择来源", html)
        self.assertIn("2 配置导入", html)
        self.assertIn("3 确认并校验", html)
        self.assertIn('data-skill-source="${value}"', html)
        for source, label in (
            ("editor", "编辑器"),
            ("local", "本地目录"),
            ("zip", "ZIP"),
            ("github", "GitHub"),
        ):
            self.assertIn(f"['{source}', '{label}'", html)
        self.assertIn("uiSkillTargetCodex", html)
        self.assertIn("uiSkillTargetClaude", html)
        self.assertIn("aria-current", html)
        self.assertIn("不会自动批准、发布或执行包内脚本", html)
        self.assertIn("uiSkillWizard.sourceType ? '' : ' disabled'", html)
        self.assertIn("!fields.codex && !fields.claude", html)
        self.assertIn("请修正以下字段后继续", html)
        self.assertIn("查看校验结果", html)
        self.assertIn("idempotencyKey: idempotencyKey('skill-import')", html)
        self.assertIn("idempotency_key: uiSkillWizard.idempotencyKey", html)
        self.assertIn("clearUISkillWizardLive()", html)
        self.assertNotIn("innerHTML = skill.skill_md", html)
        self.assertNotIn("importEditorSkill()", html)

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

    def test_user_docs_cover_installed_lifecycle_and_model_distilled_memory(self) -> None:
        user_docs = (
            ROOT / "README.md",
            ROOT / "docs/MEMORY_REVIEW_USER_GUIDE.zh-CN.md",
        )
        required_commands = (
            "git clone",
            "cd vibe_coding_manage_platform",
            "./install.sh",
            "./install.sh --with-claude-hooks",
            "vibe-memory open",
            'vibe-memory project register "/path/to/workspace"',
            'vibe-memory project init "/path/to/workspace"',
            'vibe-memory migrate preview --project-root "/path/to/workspace"',
            'vibe-memory migrate apply --approved --project-root "/path/to/workspace"',
            "vibe-memory doctor",
            'vibe-memory update --source-root "/path/to/local/clone"',
            "vibe-memory rollback",
            "vibe-memory repair",
            "vibe-memory start",
            "vibe-memory hooks status",
            "vibe-memory hooks repair",
            "vibe-memory uninstall",
            "--approved-data-deletion",
            '--data-path "$HOME/.codex/memory_review/projects.json"',
        )
        required_contracts = (
            "Python 3.10+",
            "event metadata",
            "active Codex or Claude Code model",
            "registered cwd",
            "unregistered cwd",
            "pending",
            "active",
            "design preferences",
            "UI design approval",
            "UI Skills",
            "Loop",
            "policy",
            "LaunchAgent",
            "fresh Codex or Claude Code session",
        )
        for path in user_docs:
            text = path.read_text(encoding="utf-8")
            for command in required_commands:
                self.assertIn(command, text, f"{path.name} missing {command}")
            for contract in required_contracts:
                self.assertIn(contract, text, f"{path.name} missing {contract}")

        release = (ROOT / "docs/RELEASE_CHECKLIST.md").read_text(encoding="utf-8")
        self.assertIn("13-gate", release)
        self.assertIn("real Darwin installed-runtime E2E", release)
        self.assertIn("Claude Code evaluation", release)
        self.assertIn("release reports", release)
        self.assertIn("preview exit status", release)

        for path in user_docs:
            text = path.read_text(encoding="utf-8")
            self.assertIn("cannot be a directory", text)
            self.assertIn("vibe-memory start && vibe-memory open", text)
            self.assertIn(
                "echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> \"$HOME/.zshrc\"",
                text,
            )

        for path in (*user_docs, ROOT / "docs/RELEASE_CHECKLIST.md"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("bounded prompt summary", text)
            self.assertNotIn("/usr/bin/python", text)


if __name__ == "__main__":
    unittest.main()
