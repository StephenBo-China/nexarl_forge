from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ui_design_preferences as preferences


class UIPreferencesTest(unittest.TestCase):
    def test_project_override_can_inherit_replace_append_and_clear(self) -> None:
        global_value = {
            "visual": {"preferred_styles": ["editorial"], "radius": "8px"},
            "interaction": {"motion_intensity": "moderate"},
            "anti_preferences": ["purple AI gradients"],
        }
        override = {
            "visual.preferred_styles": {"mode": "append", "value": ["industrial"]},
            "visual.radius": {"mode": "replace", "value": "4px"},
            "interaction.motion_intensity": {"mode": "inherit"},
            "anti_preferences": {"mode": "clear"},
        }

        effective = preferences.merge_preferences(global_value, override)

        self.assertEqual(
            effective["value"]["visual"]["preferred_styles"],
            ["editorial", "industrial"],
        )
        self.assertEqual(effective["value"]["visual"]["radius"], "4px")
        self.assertEqual(effective["value"]["anti_preferences"], [])
        self.assertEqual(effective["sources"]["visual.radius"], "project")
        self.assertEqual(
            effective["sources"]["interaction.motion_intensity"], "global"
        )

    def test_replace_preserves_explicit_empty_value(self) -> None:
        effective = preferences.merge_preferences(
            {"visual": {"radius": "8px"}},
            {"visual.radius": {"mode": "replace", "value": ""}},
        )

        self.assertEqual(effective["value"]["visual"]["radius"], "")
        self.assertEqual(effective["sources"]["visual.radius"], "project")

    def test_invalid_override_mode_and_append_type_are_rejected(self) -> None:
        with self.assertRaises(preferences.PreferenceValidationError):
            preferences.merge_preferences(
                {"visual": {"preferred_styles": []}},
                {"visual.preferred_styles": {"mode": "merge", "value": []}},
            )
        with self.assertRaises(preferences.PreferenceValidationError):
            preferences.merge_preferences(
                {"visual": {"preferred_styles": []}},
                {"visual.preferred_styles": {"mode": "append", "value": "editorial"}},
            )

    def test_global_and_project_preferences_persist_separately(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            temp = pathlib.Path(value)
            project = temp / "project"
            project.mkdir()
            with mock.patch.dict(os.environ, {"UI_DESIGN_HOME": str(temp / "global")}):
                global_value = preferences.default_global_preferences()
                global_value["visual"]["radius"] = "12px"
                preferences.save_global_preferences(global_value)
                preferences.save_project_overrides(
                    project,
                    {"visual.radius": {"mode": "replace", "value": "2px"}},
                )

                effective = preferences.effective_preferences(project)

            self.assertEqual(effective["value"]["visual"]["radius"], "2px")
            self.assertEqual(effective["sources"]["visual.radius"], "project")
            self.assertEqual(
                preferences.project_preferences_path(project),
                project / "codex/ui_design/preferences.json",
            )

    def test_unknown_global_field_is_rejected(self) -> None:
        value = preferences.default_global_preferences()
        value["unknown"] = True

        with self.assertRaises(preferences.PreferenceValidationError):
            preferences.validate_global_preferences(value)


if __name__ == "__main__":
    unittest.main()
