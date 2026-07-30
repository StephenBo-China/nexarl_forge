from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import memory_project
import memory_review as review_cli
import memory_review_queue as review
import loop_superpowers
import ui_design_preferences as preferences


class MemoryReviewQualityTest(unittest.TestCase):
    def test_init_project_creates_safe_ui_design_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value) / "project"
            root.mkdir()
            (root / ".git").mkdir()
            original_registry = memory_project.REGISTRY_PATH
            try:
                memory_project.REGISTRY_PATH = pathlib.Path(value) / "projects.json"

                result = memory_project.init_project(root)
            finally:
                memory_project.REGISTRY_PATH = original_registry

            config = json.loads(
                (root / "codex/ui_design/config.json").read_text(encoding="utf-8")
            )
            self.assertEqual(config["gate_mode"], "design_package")
            self.assertFalse(config["hard_gate_enabled"])
            self.assertEqual(config["schema_version"], 1)
            self.assertTrue((root / "codex/ui_design/active-skills.json").exists())
            self.assertTrue((root / "codex/ui_design/preferences.json").exists())
            self.assertTrue((root / "codex/ui_design/approvals.json").exists())
            self.assertTrue(
                (root / ".codex/hooks/ui_design_gate_hook.py").exists()
            )
            self.assertTrue(
                (root / ".claude/hooks/ui_design_gate_hook.py").exists()
            )
            for settings_path in (
                root / ".codex/hooks.json",
                root / ".claude/settings.json",
            ):
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
                pre_tool = settings["hooks"]["PreToolUse"]
                self.assertTrue(
                    any(
                        "ui_design_gate_hook.py" in hook["command"]
                        for entry in pre_tool
                        for hook in entry["hooks"]
                    )
                )
            context_path = root / "codex/ui_design/effective-context.json"
            self.assertTrue(context_path.exists())
            context = json.loads(context_path.read_text(encoding="utf-8"))
            self.assertEqual(context["gate"]["mode"], "design_package")
            self.assertEqual(context["active_skills"]["skills"], [])
            for instructions in (
                root / "AGENTS.md",
                root / "CLAUDE.md",
                root / ".claude/rules/shared-memory.md",
            ):
                text = instructions.read_text(encoding="utf-8")
                self.assertIn("codex/ui_design/config.json", text)
                self.assertIn("codex/ui_design/effective-context.json", text)
                self.assertIn("codex/ui_design/active-skills.json", text)
                self.assertIn("codex/ui_design/approvals.json", text)
                self.assertIn("visible-interface", text)
            self.assertEqual(
                result["project"]["ui_design_status"], "configuration_required"
            )

    def test_effective_ui_context_merges_preferences_skills_and_gate_state(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            temp = pathlib.Path(value)
            root = temp / "project"
            root.mkdir()
            (root / ".git").mkdir()
            original_registry = memory_project.REGISTRY_PATH
            try:
                memory_project.REGISTRY_PATH = temp / "projects.json"
                with mock.patch.dict(
                    os.environ, {"UI_DESIGN_HOME": str(temp / "ui-design-home")}
                ):
                    memory_project.init_project(root)
                    global_value = preferences.default_global_preferences()
                    global_value["design_principles"] = ["calm hierarchy"]
                    preferences.save_global_preferences(global_value)
                    preferences.save_project_overrides(
                        root,
                        {
                            "design_principles": {
                                "mode": "append",
                                "value": ["clear primary action"],
                            }
                        },
                    )
                    memory_project.write_json(
                        root / "codex/ui_design/active-skills.json",
                        {
                            "schema_version": 1,
                            "execution_order": ["frontend-design", "ui-ux-pro-max"],
                            "skills": [
                                {"name": "frontend-design", "version": "pinned"}
                            ],
                        },
                    )
                    config = memory_project.ui_design_config(root)
                    config.update({"hard_gate_enabled": True, "relocked": False})
                    memory_project.write_json(
                        root / "codex/ui_design/config.json", config
                    )
                    memory_project.write_json(
                        root / "codex/ui_design/approvals.json",
                        {
                            "schema_version": 1,
                            "package_approvals": {"task-1": {"digest": "a" * 64}},
                            "project_global_approval": None,
                        },
                    )

                    snapshot = memory_project.publish_effective_ui_context(root)
            finally:
                memory_project.REGISTRY_PATH = original_registry

            self.assertEqual(
                snapshot["preferences"]["effective"]["value"]["design_principles"],
                ["calm hierarchy", "clear primary action"],
            )
            self.assertEqual(
                snapshot["active_skills"]["execution_order"],
                ["frontend-design", "ui-ux-pro-max"],
            )
            self.assertFalse(snapshot["gate"]["relocked"])
            self.assertIn("task-1", snapshot["gate"]["approvals"]["package_approvals"])

    def test_personal_noise_rejects_project_tasks_and_memory_console_commands(self) -> None:
        base = {
            "scope": "personal",
            "status": "pending",
            "content": "",
        }
        for content in (
            "用户偏好/工作方式：隔离审核台候选记忆并标记为拒绝。",
            "用户偏好/工作方式：更新项目员工开发文档和服务代码。",
        ):
            item = dict(base, content=content)
            self.assertTrue(review.is_noise_personal_candidate(item), content)

        durable = dict(
            base,
            content="**分类：协作偏好**\n\n用户希望跨项目每次修改代码前先确认修改计划。",
        )
        self.assertFalse(review.is_noise_personal_candidate(durable))

    def test_generated_project_hook_records_only_event_metadata(self) -> None:
        hook = memory_project.hook_script(pathlib.Path("/tmp/project"), "codex")
        self.assertNotIn("def find_prompt", hook)
        self.assertNotIn("find_prompt(", hook)
        self.assertNotIn("raw_stdin", hook)
        self.assertNotIn("- summary:", hook)
        self.assertNotIn("compact =", hook)
        self.assertIn('f"- source_agent: {SOURCE}', hook)
        self.assertIn('f"- event: {event}', hook)
        self.assertIn('f"- cwd: `{os.getcwd()}`', hook)
        self.assertIn("session_id", hook)
        self.assertNotIn("append_project_candidate", hook)
        self.assertIn("conversation model reviews memory candidates", hook)

    def test_hook_context_is_conditional_and_safe(self) -> None:
        hook = memory_project.hook_script(pathlib.Path("/tmp/project"), "codex")
        self.assertIn("def loop_context()", hook)
        self.assertIn("Loop × Superpowers", hook)
        self.assertIn("explicit user authorization", hook)
        self.assertIn("Loop configuration is invalid", hook)
        self.assertNotIn("oss_access_key", hook)
        self.assertNotIn("database_password", hook)

    def test_agent_candidate_is_structured_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            originals = {
                name: getattr(review, name)
                for name in (
                    "PERSONAL_PROPOSALS", "PERSONAL_LONG", "PERSONAL_SHORT",
                    "PROJECT_PROPOSALS", "PROJECT_LONG", "PROJECT_QUEUE",
                    "PROJECT_STATE", "CODEX_DIR",
                )
            }
            try:
                review.PERSONAL_PROPOSALS = temp / "personal_proposals.md"
                review.PERSONAL_PROPOSALS.write_text("# Proposals\n", encoding="utf-8")
                review.PERSONAL_LONG = temp / "personal_long.md"
                review.PERSONAL_SHORT = temp / "personal_short.md"
                review.PROJECT_PROPOSALS = temp / "project_proposals.md"
                review.PROJECT_LONG = temp / "project_long.md"
                review.PROJECT_QUEUE = temp / "queue.json"
                review.PROJECT_STATE = temp / "state.json"
                review.CODEX_DIR = temp / "codex"
                first = review.create_agent_candidate(
                    "personal", "long", "collaboration_preference", "修改前确认计划",
                    "用户希望修改代码前先确认修改计划和不确定事项。",
                    source_agent="codex",
                    policy_version=2,
                )
                second = review.create_agent_candidate(
                    "personal", "long", "collaboration_preference", " 修改前确认计划 ",
                    "用户希望修改代码前先确认修改计划和不确定事项。",
                    source_agent="claude-code",
                    policy_version=3,
                )
                personal_item = review.parse_personal_candidates()[0]
                approved_personal = review.approve(first["id"])
                project = review.create_agent_candidate(
                    "project", "long", "project_architecture", "共享路由器",
                    "项目使用共享路由器规范化本地代理事件并生成审核上下文。",
                    source_agent="claude-code",
                    policy_version=4,
                )
                project_item = review.parse_project_candidates()[0]
                approved = review.approve(project["id"])
            finally:
                for name, value in originals.items():
                    setattr(review, name, value)
            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            self.assertTrue(project["created"])
            self.assertEqual(first["id"], personal_item["id"])
            self.assertEqual(project["id"], project_item["id"])
            self.assertEqual(personal_item["source_agent"], "codex")
            self.assertEqual(personal_item["source_agents"], ["claude-code", "codex"])
            self.assertEqual(personal_item["policy_version"], 2)
            self.assertEqual(project_item["source_agent"], "claude-code")
            self.assertEqual(project_item["policy_version"], 4)
            self.assertEqual(approved["decision"]["source_agent"], "claude-code")
            self.assertEqual(approved["decision"]["source_agents"], ["claude-code"])
            self.assertEqual(approved["decision"]["policy_version"], 4)
            self.assertEqual(
                approved_personal["decision"]["source_agents"],
                ["claude-code", "codex"],
            )
            text = (temp / "personal_proposals.md").read_text(encoding="utf-8")
            self.assertIn("status: pending", text)
            self.assertIn("source_agent: codex", text)
            self.assertIn("source_agents: claude-code,codex", text)
            self.assertIn("policy_version: 2", text)
            self.assertIn("**标题：修改前确认计划**", text)
            approved_text = (temp / "project_long.md").read_text(encoding="utf-8")
            self.assertIn("共享路由器", approved_text)
            self.assertIn("项目架构", approved_text)
            self.assertNotIn("source_agent", approved_text)
            self.assertNotIn("policy_version", approved_text)

    def test_candidate_identity_includes_scope_target_category_title_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            originals = {
                name: getattr(review, name)
                for name in (
                    "PERSONAL_PROPOSALS", "PERSONAL_LONG", "PERSONAL_SHORT",
                    "PROJECT_PROPOSALS", "PROJECT_LONG", "PROJECT_QUEUE",
                    "PROJECT_STATE", "CODEX_DIR", "build_queue",
                )
            }
            try:
                review.PERSONAL_PROPOSALS = temp / "personal_proposals.md"
                review.PERSONAL_PROPOSALS.write_text("# Proposals\n", encoding="utf-8")
                review.PERSONAL_LONG = temp / "personal_long.md"
                review.PERSONAL_SHORT = temp / "personal_short.md"
                review.PROJECT_PROPOSALS = temp / "project_proposals.md"
                review.PROJECT_PROPOSALS.write_text("# Project Proposals\n", encoding="utf-8")
                review.PROJECT_LONG = temp / "project_long.md"
                review.PROJECT_QUEUE = temp / "queue.json"
                review.PROJECT_STATE = temp / "state.json"
                review.CODEX_DIR = temp / "codex"
                review.build_queue = lambda: {}
                summary = "用户希望相同摘要在身份字段不同时仍可形成独立候选。"
                results = [
                    review.create_agent_candidate(
                        "personal", "long", "work_style", "标题一", summary
                    ),
                    review.create_agent_candidate(
                        "personal", "long", "work_style", "标题二", summary
                    ),
                    review.create_agent_candidate(
                        "personal", "short", "work_style", "标题一", summary
                    ),
                    review.create_agent_candidate(
                        "personal", "long", "thinking_style", "标题一", summary
                    ),
                    review.create_agent_candidate(
                        "project", "long", "project_workflow", "标题一", summary
                    ),
                ]
            finally:
                for name, original in originals.items():
                    setattr(review, name, original)

            self.assertTrue(all(result["created"] for result in results))

    def test_sensitive_candidate_title_is_rejected_before_write_or_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            originals = {
                name: getattr(review, name)
                for name in (
                    "PERSONAL_PROPOSALS", "PERSONAL_LONG", "PERSONAL_SHORT",
                    "PROJECT_PROPOSALS", "PROJECT_LONG", "PROJECT_QUEUE",
                    "PROJECT_STATE", "CODEX_DIR",
                )
            }
            try:
                review.PERSONAL_PROPOSALS = temp / "personal_proposals.md"
                review.PERSONAL_PROPOSALS.write_text("# Proposals\n", encoding="utf-8")
                review.PERSONAL_LONG = temp / "personal_long.md"
                review.PERSONAL_SHORT = temp / "personal_short.md"
                review.PROJECT_PROPOSALS = temp / "project_proposals.md"
                review.PROJECT_LONG = temp / "project_long.md"
                review.PROJECT_QUEUE = temp / "queue.json"
                review.PROJECT_STATE = temp / "state.json"
                review.CODEX_DIR = temp / "codex"
                with self.assertRaisesRegex(ValueError, "sensitive"):
                    review.create_agent_candidate(
                        "personal", "long", "work_style", "api_key=do-not-store",
                        "用户希望跨项目保留清晰且可审核的工作方式说明。",
                    )
                review.PROJECT_PROPOSALS.write_text(
                    "### 2026-07-30 - api_key=do-not-approve\n\n"
                    "**分类：项目工作流**\n\n这是一条长度足够但标题敏感的候选说明。\n",
                    encoding="utf-8",
                )
                sensitive_id = review.parse_project_candidates()[0]["id"]
                with self.assertRaisesRegex(ValueError, "sensitive"):
                    review.approve(sensitive_id)
            finally:
                for name, original in originals.items():
                    setattr(review, name, original)

            self.assertEqual(
                (temp / "personal_proposals.md").read_text(encoding="utf-8"),
                "# Proposals\n",
            )
            self.assertFalse((temp / "personal_long.md").exists())
            self.assertFalse((temp / "project_long.md").exists())

    def test_proposal_atomic_replace_failure_preserves_bytes_and_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            proposals = temp / "personal_proposals.md"
            original_bytes = b"# Proposals\n\noriginal\n"
            proposals.write_bytes(original_bytes)
            proposals.chmod(0o600)
            originals = {
                name: getattr(review, name)
                for name in (
                    "PERSONAL_PROPOSALS", "PERSONAL_LONG", "PERSONAL_SHORT",
                    "PROJECT_PROPOSALS", "PROJECT_LONG", "PROJECT_QUEUE",
                    "PROJECT_STATE", "CODEX_DIR", "build_queue",
                )
            }
            try:
                review.PERSONAL_PROPOSALS = proposals
                review.PERSONAL_LONG = temp / "personal_long.md"
                review.PERSONAL_SHORT = temp / "personal_short.md"
                review.PROJECT_PROPOSALS = temp / "project_proposals.md"
                review.PROJECT_LONG = temp / "project_long.md"
                review.PROJECT_QUEUE = temp / "queue.json"
                review.PROJECT_STATE = temp / "state.json"
                review.CODEX_DIR = temp / "codex"
                review.build_queue = lambda: {}
                with mock.patch(
                    "memory_review_queue.atomic_write_text",
                    create=True,
                    side_effect=OSError("replace failed"),
                ):
                    with self.assertRaisesRegex(OSError, "replace failed"):
                        review.create_agent_candidate(
                            "personal", "long", "work_style", "原子写入",
                            "用户希望候选提案写入失败时完整保留原始文件内容。",
                        )
            finally:
                for name, original in originals.items():
                    setattr(review, name, original)

            self.assertEqual(proposals.read_bytes(), original_bytes)
            self.assertEqual(stat.S_IMODE(proposals.stat().st_mode), 0o600)

    def test_agent_candidate_validates_provenance(self) -> None:
        for source_agent, policy_version, message in (
            ("other", 1, "source_agent"),
            ("codex", 0, "policy_version"),
            ("codex", True, "policy_version"),
            ("codex", "1", "policy_version"),
        ):
            with self.subTest(source_agent=source_agent, policy_version=policy_version):
                with self.assertRaisesRegex(ValueError, message):
                    review.create_agent_candidate(
                        "personal", "long", "work_style", "清晰标题",
                        "用户希望跨项目保留清晰且可审核的工作方式说明。",
                        source_agent=source_agent,
                        policy_version=policy_version,
                    )

    def test_concurrent_agent_candidates_are_deduplicated_under_one_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            originals = {
                name: getattr(review, name)
                for name in (
                    "PERSONAL_PROPOSALS", "PERSONAL_LONG", "PERSONAL_SHORT",
                    "PROJECT_PROPOSALS", "PROJECT_LONG", "PROJECT_QUEUE",
                    "PROJECT_STATE", "CODEX_DIR", "build_queue", "read_text",
                )
            }
            proposals = temp / "personal_proposals.md"
            proposals.write_text("# Proposals\n", encoding="utf-8")
            proposal_lock = proposals.with_suffix(proposals.suffix + ".lock")
            read_barrier = threading.Barrier(2)
            start_barrier = threading.Barrier(3)
            results: list[dict] = []
            errors: list[BaseException] = []

            def synchronized_read(path: pathlib.Path) -> str:
                if path == proposals and not proposal_lock.exists():
                    snapshot = originals["read_text"](path)
                    read_barrier.wait(timeout=3)
                    return snapshot
                return originals["read_text"](path)

            def create(source_agent: str) -> None:
                try:
                    start_barrier.wait(timeout=3)
                    results.append(
                        review.create_agent_candidate(
                            "personal", "long", "work_style", "并发候选",
                            "用户希望跨项目候选在并发代理调用时仍然只生成一条。",
                            source_agent=source_agent,
                        )
                    )
                except BaseException as error:
                    errors.append(error)

            try:
                review.PERSONAL_PROPOSALS = proposals
                review.PERSONAL_LONG = temp / "personal_long.md"
                review.PERSONAL_SHORT = temp / "personal_short.md"
                review.PROJECT_PROPOSALS = temp / "project_proposals.md"
                review.PROJECT_LONG = temp / "project_long.md"
                review.PROJECT_QUEUE = temp / "queue.json"
                review.PROJECT_STATE = temp / "state.json"
                review.CODEX_DIR = temp / "codex"
                review.build_queue = lambda: {}
                review.read_text = synchronized_read
                threads = [
                    threading.Thread(target=create, args=(agent,))
                    for agent in ("codex", "claude-code")
                ]
                for thread in threads:
                    thread.start()
                start_barrier.wait(timeout=3)
                for thread in threads:
                    thread.join(timeout=5)
                alive = [thread.name for thread in threads if thread.is_alive()]
            finally:
                for name, original in originals.items():
                    setattr(review, name, original)

            self.assertEqual(alive, [])
            self.assertEqual(errors, [])
            self.assertEqual(sorted(result["created"] for result in results), [False, True])
            text = proposals.read_text(encoding="utf-8")
            self.assertEqual(text.count("并发代理调用时仍然只生成一条"), 1)

    def test_concurrent_scopes_rebuild_one_atomic_queue_without_lost_items(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            originals = {
                name: getattr(review, name)
                for name in (
                    "PERSONAL_PROPOSALS", "PERSONAL_LONG", "PERSONAL_SHORT",
                    "PROJECT_PROPOSALS", "PROJECT_LONG", "PROJECT_QUEUE",
                    "PROJECT_STATE", "CODEX_DIR", "build_queue", "write_json",
                    "parse_project_candidates",
                )
            }
            personal_parsed_project = threading.Event()
            project_reached_build = threading.Event()
            project_queue_written = threading.Event()
            errors: list[BaseException] = []
            queue_path = temp / "queue.json"
            queue_lock = queue_path.with_suffix(queue_path.suffix + ".lock")

            def synchronized_parse_project() -> list[dict]:
                items = originals["parse_project_candidates"]()
                if threading.current_thread().name == "personal-create":
                    personal_parsed_project.set()
                    if not project_reached_build.wait(timeout=3):
                        raise TimeoutError("project did not reach queue build")
                return items

            def synchronized_build_queue() -> dict:
                if threading.current_thread().name == "project-create":
                    project_reached_build.set()
                return originals["build_queue"]()

            def ordered_write_json(path: pathlib.Path, value: object) -> None:
                if path == queue_path:
                    if (
                        threading.current_thread().name == "personal-create"
                        and not queue_lock.exists()
                        and not project_queue_written.wait(timeout=3)
                    ):
                        raise TimeoutError("project queue write did not finish")
                    originals["write_json"](path, value)
                    if threading.current_thread().name == "project-create":
                        project_queue_written.set()
                    return
                originals["write_json"](path, value)

            def create_personal() -> None:
                try:
                    review.create_agent_candidate(
                        "personal", "long", "work_style", "个人候选",
                        "用户希望并发构建审核队列时保留个人候选记录。",
                        source_agent="codex",
                    )
                except BaseException as error:
                    errors.append(error)

            def create_project() -> None:
                try:
                    review.create_agent_candidate(
                        "project", "long", "project_workflow", "项目候选",
                        "项目要求并发构建审核队列时保留项目候选记录。",
                        source_agent="claude-code",
                    )
                except BaseException as error:
                    errors.append(error)

            try:
                review.PERSONAL_PROPOSALS = temp / "personal_proposals.md"
                review.PERSONAL_PROPOSALS.write_text("# Proposals\n", encoding="utf-8")
                review.PERSONAL_LONG = temp / "personal_long.md"
                review.PERSONAL_SHORT = temp / "personal_short.md"
                review.PROJECT_PROPOSALS = temp / "project_proposals.md"
                review.PROJECT_PROPOSALS.write_text("# Project Proposals\n", encoding="utf-8")
                review.PROJECT_LONG = temp / "project_long.md"
                review.PROJECT_QUEUE = queue_path
                review.PROJECT_STATE = temp / "state.json"
                review.CODEX_DIR = temp / "codex"
                review.build_queue = synchronized_build_queue
                review.write_json = ordered_write_json
                review.parse_project_candidates = synchronized_parse_project
                personal_thread = threading.Thread(
                    name="personal-create", target=create_personal
                )
                project_thread = threading.Thread(
                    name="project-create", target=create_project
                )
                personal_thread.start()
                self.assertTrue(personal_parsed_project.wait(timeout=3))
                project_thread.start()
                personal_thread.join(timeout=5)
                project_thread.join(timeout=5)
                alive = [
                    thread.name
                    for thread in (personal_thread, project_thread)
                    if thread.is_alive()
                ]
            finally:
                for name, original in originals.items():
                    setattr(review, name, original)

            self.assertEqual(alive, [])
            self.assertEqual(errors, [])
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            self.assertEqual({item["scope"] for item in queue["items"]}, {"personal", "project"})
            self.assertEqual(queue["counts"]["pending"], 2)

    def test_propose_cli_accepts_and_forwards_candidate_provenance(self) -> None:
        argv = [
            "memory_review.py", "propose", "--scope", "personal", "--target", "long",
            "--category", "work_style", "--title", "清晰标题",
            "--summary", "用户希望跨项目保留清晰且可审核的工作方式说明。",
            "--source-agent", "claude-code", "--policy-version", "3",
        ]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            review_cli.review,
            "create_agent_candidate",
            return_value={"created": True, "id": "M-test"},
        ) as create, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(review_cli.main(), 0)

        create.assert_called_once_with(
            "personal", "long", "work_style", "清晰标题",
            "用户希望跨项目保留清晰且可审核的工作方式说明。",
            "agent_summary",
            source_agent="claude-code",
            policy_version=3,
        )

    def test_upgrade_memory_hooks_preserves_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            project = pathlib.Path(temp_value)
            old_hook = project / ".codex" / "hooks" / "shared_memory_hook.py"
            old_hook.parent.mkdir(parents=True)
            old_hook.write_text("old managed hook\n", encoding="utf-8")
            result = memory_project.upgrade_memory_hooks(project)
            installed = old_hook.read_text(encoding="utf-8")
            self.assertIn("- source_agent: ", installed)
            self.assertNotIn("- summary: ", installed)
            backups = list(old_hook.parent.glob("shared_memory_hook.py.bak.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "old managed hook\n")
            self.assertTrue(any(item["status"] == "backup" for item in result["changes"]))

    def test_hook_upgrade_merges_ui_gate_entry_and_preserves_unrelated_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            project = pathlib.Path(temp_value)
            settings_path = project / ".codex/hooks.json"
            settings_path.parent.mkdir(parents=True)
            custom = {
                "custom": "keep",
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "CustomTool",
                            "hooks": [{"type": "command", "command": "custom-hook"}],
                        }
                    ],
                    "PostToolUse": [
                        {"hooks": [{"type": "command", "command": "post-hook"}]}
                    ],
                },
            }
            settings_path.write_text(json.dumps(custom), encoding="utf-8")

            first = memory_project.upgrade_memory_hooks(project)
            merged = json.loads(settings_path.read_text(encoding="utf-8"))
            second = memory_project.upgrade_memory_hooks(project)

            self.assertEqual(merged["custom"], "keep")
            self.assertEqual(merged["hooks"]["PostToolUse"], custom["hooks"]["PostToolUse"])
            commands = [
                hook["command"]
                for entry in merged["hooks"]["PreToolUse"]
                for hook in entry["hooks"]
            ]
            self.assertIn("custom-hook", commands)
            self.assertTrue(any("ui_design_gate_hook.py" in item for item in commands))
            self.assertTrue(any(item["status"] == "backup" for item in first["changes"]))
            self.assertFalse(any(item["status"] == "backup" for item in second["changes"]))

    def test_hook_upgrade_reports_malformed_settings_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            project = pathlib.Path(temp_value)
            settings_path = project / ".claude/settings.json"
            settings_path.parent.mkdir(parents=True)
            original = "{not-json\n"
            settings_path.write_text(original, encoding="utf-8")

            result = memory_project.upgrade_memory_hooks(project)

            self.assertEqual(settings_path.read_text(encoding="utf-8"), original)
            self.assertTrue(
                any(
                    item["path"] == str(settings_path.resolve())
                    and item["status"] == "conflict"
                    for item in result["changes"]
                )
            )

    def test_upgrade_managed_rules_preserves_user_text_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            project = pathlib.Path(temp_value)
            agents = project / "AGENTS.md"
            agents.write_text("# User rules\n\nKeep this exact text.\n", encoding="utf-8")
            self.assertTrue(hasattr(memory_project, "upgrade_memory_rules"))

            first = memory_project.upgrade_memory_rules(project)
            updated = agents.read_text(encoding="utf-8")
            second = memory_project.upgrade_memory_rules(project)

            self.assertIn("Keep this exact text.", updated)
            self.assertIn("codex/ui_design/effective-context.json", updated)
            self.assertIn("visible-interface", updated)
            self.assertIn(loop_superpowers.MANAGED_RULE_START, updated)
            self.assertEqual(updated, agents.read_text(encoding="utf-8"))
            self.assertTrue(any(item["status"] == "backup" for item in first["changes"]))
            self.assertFalse(any(item["status"] == "backup" for item in second["changes"]))

    def test_upgrade_managed_rules_reports_unmatched_marker_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            project = pathlib.Path(temp_value)
            agents = project / "AGENTS.md"
            original = f"user text\n{loop_superpowers.MANAGED_RULE_START}\nbroken\n"
            agents.write_text(original, encoding="utf-8")
            self.assertTrue(hasattr(memory_project, "upgrade_memory_rules"))

            result = memory_project.upgrade_memory_rules(project)

            self.assertEqual(agents.read_text(encoding="utf-8"), original)
            self.assertTrue(any(item["status"] == "conflict" for item in result["changes"]))

    def test_quarantine_preserves_source_and_marks_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = pathlib.Path(temp_value)
            source = temp / "proposals.md"
            source.write_text("original candidate source\n", encoding="utf-8")
            review.CODEX_DIR = temp / "codex"
            review.PROJECT_STATE = review.CODEX_DIR / "memory_review_state.json"
            item = {
                "id": "M-20260719-000001",
                "scope": "personal",
                "status": "pending",
                "target": "long",
                "source_path": str(source),
                "content": "审核台候选记忆管理命令",
            }
            original_loader = review.load_queue
            original_record = review.record_decision
            decisions: dict[str, dict] = {}
            try:
                review.load_queue = lambda refresh=True: {"items": [item]}
                review.record_decision = lambda candidate_id, decision: decisions.setdefault(candidate_id, decision)
                ids = review.reject_noise_personal_candidates(dry_run=False)
            finally:
                review.load_queue = original_loader
                review.record_decision = original_record

            self.assertEqual(ids, [item["id"]])
            self.assertEqual(source.read_text(encoding="utf-8"), "original candidate source\n")
            self.assertEqual(decisions[item["id"]]["status"], "rejected")
            archive = review.CODEX_DIR / "memory_review_noise_personal.md"
            self.assertIn(item["id"], archive.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
