from __future__ import annotations

import json
import pathlib
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


if __name__ == "__main__":
    unittest.main()
