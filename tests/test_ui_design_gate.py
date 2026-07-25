from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import memory_project


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


if __name__ == "__main__":
    unittest.main()
