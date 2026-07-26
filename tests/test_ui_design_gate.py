from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import memory_project
import ui_design_gate as gate


class UIProjectStateTest(unittest.TestCase):
    def test_project_entry_reports_ui_design_readiness_without_rewriting(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            (root / ".git").mkdir()
            self.assertEqual(
                memory_project.project_entry(root)["ui_design_status"],
                "not_initialized",
            )
            ui_root = root / "codex/ui_design"
            ui_root.mkdir(parents=True)
            config_path = ui_root / "config.json"
            for name in ("preferences.json", "active-skills.json", "approvals.json"):
                (ui_root / name).write_text("{}\n", encoding="utf-8")
            config = memory_project.ui_design_config(root)
            config["hard_gate_enabled"] = True
            config["formal_frontend_paths"] = ["web/src/**"]
            config_path.write_text(json.dumps(config), encoding="utf-8")

            self.assertEqual(
                memory_project.project_entry(root)["ui_design_status"], "locked"
            )
            config["relocked"] = False
            original = json.dumps(config)
            config_path.write_text(original, encoding="utf-8")

            self.assertEqual(
                memory_project.project_entry(root)["ui_design_status"], "ready"
            )
            self.assertEqual(config_path.read_text(encoding="utf-8"), original)

    def test_reinitialization_preserves_custom_ui_config(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value) / "project"
            root.mkdir()
            (root / ".git").mkdir()
            ui_root = root / "codex/ui_design"
            ui_root.mkdir(parents=True)
            custom = {"schema_version": 99, "custom": "preserve me"}
            config_path = ui_root / "config.json"
            config_path.write_text(json.dumps(custom), encoding="utf-8")
            original_registry = memory_project.REGISTRY_PATH
            try:
                memory_project.REGISTRY_PATH = pathlib.Path(value) / "projects.json"

                memory_project.init_project(root)
            finally:
                memory_project.REGISTRY_PATH = original_registry

            self.assertEqual(json.loads(config_path.read_text(encoding="utf-8")), custom)


class DesignPackageLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.project = pathlib.Path(self.temporary.name) / "project"
        self.project.mkdir()
        ui_root = self.project / "codex/ui_design"
        ui_root.mkdir(parents=True)
        (ui_root / "config.json").write_text(
            json.dumps(memory_project.ui_design_config(self.project)),
            encoding="utf-8",
        )
        (ui_root / "approvals.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "package_approvals": {},
                    "project_global_approval": None,
                }
            ),
            encoding="utf-8",
        )
        self.manifest = {
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

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def enable_gate(self, *, mode: str = "design_package") -> dict:
        config = memory_project.ui_design_config(self.project)
        config.update(
            {
                "hard_gate_enabled": True,
                "gate_mode": mode,
                "formal_frontend_paths": ["web/src/**"],
            }
        )
        (self.project / "codex/ui_design/config.json").write_text(
            json.dumps(config), encoding="utf-8"
        )
        return config

    def test_design_package_change_invalidates_digest_bound_approval(self) -> None:
        package = gate.create_design_package(
            self.project,
            "checkout-redesign",
            self.manifest,
            idempotency_key="create-checkout-001",
        )
        package_root = pathlib.Path(package["root"])
        (package_root / "design-brief.md").write_text("Calm checkout", encoding="utf-8")
        package = gate.get_design_package(self.project, "checkout-redesign")
        approval = gate.approve_design_package(
            self.project,
            "checkout-redesign",
            expected_digest=package["digest"],
            idempotency_key="approve-checkout-001",
        )

        self.assertEqual(
            gate.gate_status(self.project, task_id="checkout-redesign")["decision"],
            "allow_approved_frontend_scope",
        )
        (package_root / "interaction-spec.md").write_text("changed", encoding="utf-8")
        status = gate.gate_status(self.project, task_id="checkout-redesign")
        self.assertEqual(status["decision"], "deny_invalidated_approval")
        self.assertNotEqual(status["current_digest"], approval["digest"])

    def test_scope_revision_reject_and_explicit_invalidation_are_recorded(self) -> None:
        package = gate.create_design_package(
            self.project,
            "checkout-redesign",
            self.manifest,
            idempotency_key="create-checkout-002",
        )
        gate.reject_design_package(
            self.project,
            "checkout-redesign",
            reason="Needs another direction",
            idempotency_key="reject-checkout-001",
        )
        self.assertEqual(
            gate.gate_status(self.project, task_id="checkout-redesign")["status"],
            "rejected",
        )
        revised = dict(self.manifest)
        revised["allowed_file_patterns"] = ["web/src/checkout/**", "web/src/payments/**"]
        revised_package = gate.revise_design_package(
            self.project,
            "checkout-redesign",
            revised,
            idempotency_key="revise-checkout-001",
        )
        self.assertNotEqual(package["digest"], revised_package["digest"])
        approval = gate.approve_design_package(
            self.project,
            "checkout-redesign",
            expected_digest=revised_package["digest"],
            idempotency_key="approve-checkout-002",
        )
        invalidated = gate.invalidate_design_package(
            self.project,
            "checkout-redesign",
            reason="Scope changed",
            idempotency_key="invalidate-checkout-001",
        )
        self.assertEqual(invalidated["status"], "invalidated")
        self.assertEqual(invalidated["superseded_digest"], approval["digest"])

    def test_request_revision_missing_package_and_idempotency(self) -> None:
        self.assertEqual(
            gate.gate_status(self.project, task_id="missing")["decision"],
            "deny_missing_design",
        )
        first = gate.create_design_package(
            self.project,
            "checkout-redesign",
            self.manifest,
            idempotency_key="create-idempotent-001",
        )
        second = gate.create_design_package(
            self.project,
            "checkout-redesign",
            self.manifest,
            idempotency_key="create-idempotent-001",
        )
        self.assertEqual(first, second)
        gate.request_design_revision(
            self.project,
            "checkout-redesign",
            reason="Add mobile error states",
            idempotency_key="request-revision-001",
        )
        self.assertEqual(
            gate.gate_status(self.project, task_id="checkout-redesign")["status"],
            "revision_requested",
        )
        changed = dict(self.manifest, title="Different request")
        with self.assertRaises(gate.IdempotencyConflict):
            gate.create_design_package(
                self.project,
                "checkout-redesign",
                changed,
                idempotency_key="create-idempotent-001",
            )

    def test_manifest_rejects_absolute_traversal_and_undeclared_design_files(self) -> None:
        absolute = dict(self.manifest, allowed_file_patterns=["/web/src/**"])
        with self.assertRaises(gate.GateValidationError):
            gate.create_design_package(
                self.project,
                "checkout-redesign",
                absolute,
                idempotency_key="bad-absolute-001",
            )
        traversal = dict(self.manifest, design_files=["../outside.md"])
        with self.assertRaises(gate.GateValidationError):
            gate.create_design_package(
                self.project,
                "checkout-redesign",
                traversal,
                idempotency_key="bad-traversal-001",
            )

        gate.create_design_package(
            self.project,
            "checkout-redesign",
            self.manifest,
            idempotency_key="create-checkout-003",
        )
        root = self.project / "codex/ui_design/design-packages/checkout-redesign"
        (root / "undeclared.md").write_text("not declared", encoding="utf-8")
        with self.assertRaises(gate.GateValidationError):
            gate.get_design_package(self.project, "checkout-redesign")

    def test_locked_gate_allows_backend_and_design_artifacts_only(self) -> None:
        self.enable_gate()

        backend = gate.decide_tool_use(
            self.project, "Edit", {"file_path": "server/orders.py"}
        )
        design = gate.decide_tool_use(
            self.project,
            "Write",
            {
                "file_path": (
                    "codex/ui_design/design-packages/checkout-redesign/design-brief.md"
                )
            },
        )
        frontend = gate.decide_tool_use(
            self.project, "Edit", {"file_path": "web/src/checkout/Form.tsx"}
        )
        frontend_read = gate.decide_tool_use(
            self.project,
            "mcp__filesystem__read_file",
            {"path": "web/src/checkout/Form.tsx"},
        )

        self.assertEqual(backend["decision"], "allow_non_visual")
        self.assertEqual(design["decision"], "allow_design_artifact")
        self.assertEqual(frontend["decision"], "deny_pending_approval")
        self.assertEqual(frontend_read["decision"], "allow_non_visual")

    def test_design_package_approval_allows_only_declared_current_scope(self) -> None:
        self.enable_gate()
        package = gate.create_design_package(
            self.project,
            "checkout-redesign",
            self.manifest,
            idempotency_key="gate-create-001",
        )
        gate.approve_design_package(
            self.project,
            "checkout-redesign",
            expected_digest=package["digest"],
            idempotency_key="gate-approve-001",
        )
        allowed = gate.decide_tool_use(
            self.project,
            "apply_patch",
            {
                "patch": (
                    "*** Begin Patch\n"
                    "*** Update File: web/src/checkout/Form.tsx\n"
                    "@@\n-old\n+new\n"
                    "*** End Patch"
                )
            },
        )
        outside_scope = gate.decide_tool_use(
            self.project, "Edit", {"file_path": "web/src/profile/Profile.tsx"}
        )
        self.assertEqual(allowed["decision"], "allow_approved_frontend_scope")
        self.assertEqual(outside_scope["decision"], "deny_scope_mismatch")

        package_root = pathlib.Path(package["root"])
        (package_root / "responsive-spec.md").write_text(
            "New breakpoint", encoding="utf-8"
        )
        invalidated = gate.decide_tool_use(
            self.project, "Write", {"file_path": "web/src/checkout/Form.tsx"}
        )
        self.assertEqual(invalidated["decision"], "deny_invalidated_approval")

    def test_project_global_baseline_unlocks_all_frontend_until_relock_or_mode_change(self) -> None:
        config = self.enable_gate(mode="project_global")
        config["project_global_baseline_task"] = "checkout-redesign"
        config_path = self.project / "codex/ui_design/config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        package = gate.create_design_package(
            self.project,
            "checkout-redesign",
            self.manifest,
            idempotency_key="baseline-create-001",
        )

        approval = gate.approve_project_baseline(
            self.project,
            "checkout-redesign",
            expected_digest=package["digest"],
            idempotency_key="baseline-approve-001",
        )
        self.assertEqual(approval["status"], "approved")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertFalse(config["relocked"])
        self.assertEqual(
            gate.decide_tool_use(
                self.project, "Edit", {"file_path": "web/src/profile/Profile.tsx"}
            )["decision"],
            "allow_approved_frontend_scope",
        )

        config["relocked"] = True
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.assertEqual(
            gate.decide_tool_use(
                self.project, "Edit", {"file_path": "web/src/profile/Profile.tsx"}
            )["decision"],
            "deny_pending_approval",
        )
        config.update({"relocked": False, "gate_mode": "design_package"})
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.assertEqual(
            gate.decide_tool_use(
                self.project, "Edit", {"file_path": "web/src/profile/Profile.tsx"}
            )["decision"],
            "deny_pending_approval",
        )

    def test_changed_project_global_baseline_invalidates_unlock(self) -> None:
        config = self.enable_gate(mode="project_global")
        config["project_global_baseline_task"] = "checkout-redesign"
        (self.project / "codex/ui_design/config.json").write_text(
            json.dumps(config), encoding="utf-8"
        )
        package = gate.create_design_package(
            self.project,
            "checkout-redesign",
            self.manifest,
            idempotency_key="baseline-create-002",
        )
        gate.approve_project_baseline(
            self.project,
            "checkout-redesign",
            expected_digest=package["digest"],
            idempotency_key="baseline-approve-002",
        )
        (pathlib.Path(package["root"]) / "design-brief.md").write_text(
            "Changed baseline", encoding="utf-8"
        )

        decision = gate.decide_tool_use(
            self.project, "Write", {"file_path": "web/src/App.tsx"}
        )
        self.assertEqual(decision["decision"], "deny_invalidated_approval")

    def test_corrupt_config_fails_closed_only_for_frontend_and_unresolved_shell(self) -> None:
        config = self.enable_gate()
        config["gate_mode"] = "unsupported"
        (self.project / "codex/ui_design/config.json").write_text(
            json.dumps(config), encoding="utf-8"
        )
        frontend = gate.decide_tool_use(
            self.project, "Write", {"file_path": "web/src/App.tsx"}
        )
        backend = gate.decide_tool_use(
            self.project, "Write", {"file_path": "server/app.py"}
        )
        self.assertEqual(frontend["decision"], "deny_invalid_configuration")
        self.assertEqual(backend["decision"], "allow_non_visual")

        self.enable_gate()
        unresolved = gate.decide_tool_use(
            self.project,
            "Bash",
            {"command": "python3 scripts/rewrite_everything.py"},
        )
        read_only = gate.decide_tool_use(
            self.project, "exec_command", {"cmd": "rg -n checkout web/src"}
        )
        chained = gate.decide_tool_use(
            self.project,
            "exec_command",
            {"cmd": "rg -n checkout web/src && python3 scripts/rewrite_everything.py"},
        )
        self.assertEqual(unresolved["decision"], "deny_pending_approval")
        self.assertIn("apply_patch", unresolved["reason"])
        self.assertEqual(read_only["decision"], "allow_non_visual")
        self.assertEqual(chained["decision"], "deny_pending_approval")

    def test_tool_path_extraction_supports_edit_write_mcp_and_shell(self) -> None:
        self.assertEqual(
            gate.extract_candidate_paths("Edit", {"file_path": "web/src/App.tsx"}),
            ["web/src/App.tsx"],
        )
        self.assertEqual(
            gate.extract_candidate_paths(
                "mcp__filesystem__write_file", {"path": "web/src/App.tsx"}
            ),
            ["web/src/App.tsx"],
        )
        self.assertEqual(
            gate.extract_candidate_paths(
                "Bash", {"command": "touch web/src/New.tsx"}
            ),
            ["web/src/New.tsx"],
        )

    def test_cross_agent_hook_protocol_denies_and_allows_without_third_party_runtime(self) -> None:
        self.enable_gate()
        template = ROOT / "templates/ui_design/ui_design_gate_hook.py"
        hook_paths = {
            "codex": self.project / ".codex/hooks/ui_design_gate_hook.py",
            "claude": self.project / ".claude/hooks/ui_design_gate_hook.py",
        }
        for path in hook_paths.values():
            path.parent.mkdir(parents=True)
            path.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")

        deny_payloads = {
            "codex": {
                "tool_name": "apply_patch",
                "tool_input": {
                    "patch": "*** Update File: web/src/checkout/Form.tsx\n"
                },
            },
            "claude": {
                "tool_name": "Edit",
                "tool_input": {"file_path": "web/src/checkout/Form.tsx"},
            },
        }
        for agent, payload in deny_payloads.items():
            completed = subprocess.run(
                [sys.executable, str(hook_paths[agent])],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            output = json.loads(completed.stdout)
            decision = output["hookSpecificOutput"]
            self.assertEqual(decision["permissionDecision"], "deny")
            self.assertIn("Approve", decision["permissionDecisionReason"])

        design_write = subprocess.run(
            [sys.executable, str(hook_paths["claude"])],
            input=json.dumps(
                {
                    "tool_name": "Write",
                    "tool_input": {
                        "file_path": (
                            "codex/ui_design/design-packages/checkout-redesign/design-brief.md"
                        )
                    },
                }
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(design_write.returncode, 0, design_write.stderr)
        self.assertEqual(design_write.stdout, "")

        bash_deny = subprocess.run(
            [sys.executable, str(hook_paths["codex"])],
            input=json.dumps(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "touch web/src/checkout/New.tsx"},
                }
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            json.loads(bash_deny.stdout)["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )

        package = gate.create_design_package(
            self.project,
            "checkout-redesign",
            self.manifest,
            idempotency_key="hook-create-001",
        )
        gate.approve_design_package(
            self.project,
            "checkout-redesign",
            expected_digest=package["digest"],
            idempotency_key="hook-approve-001",
        )
        allowed = subprocess.run(
            [sys.executable, str(hook_paths["codex"])],
            input=json.dumps(deny_payloads["codex"]),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual(allowed.stdout, "")


if __name__ == "__main__":
    unittest.main()
