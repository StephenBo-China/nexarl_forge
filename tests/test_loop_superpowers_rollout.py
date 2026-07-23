from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import memory_project


EXPECTED_SKILLS = {
    "brainstorming",
    "dispatching-parallel-agents",
    "executing-plans",
    "finishing-a-development-branch",
    "receiving-code-review",
    "requesting-code-review",
    "subagent-driven-development",
    "systematic-debugging",
    "test-driven-development",
    "using-git-worktrees",
    "using-superpowers",
    "verification-before-completion",
    "writing-plans",
    "writing-skills",
}


class LoopSuperpowersRolloutTest(unittest.TestCase):
    def test_new_loop_contract_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            project = pathlib.Path(value) / "sample"
            project.mkdir()
            config = memory_project.loop_config(project, 8123)

        self.assertEqual(config["schema_version"], 3)
        self.assertIn("methodology", config)
        method = config["methodology"]["superpowers"]
        self.assertEqual(config["methodology"]["provider"], "superpowers")
        self.assertTrue(method["enabled"])
        self.assertEqual(set(method["declared_skills"]), EXPECTED_SKILLS)
        self.assertEqual(method["authority"]["orchestrator"], "loop")
        self.assertEqual(method["authority"]["worktree"], "loop_worktree_flow_only")
        self.assertFalse(method["evaluator"]["may_modify_product_source"])
        self.assertFalse(method["subagents"]["default_enabled"])
        self.assertTrue(method["subagents"]["requires_explicit_user_authorization"])
        self.assertEqual(
            config["worktree"]["finish_validation_commands"],
            ["python3 scripts/validate_loop_methodology.py --phase completion"],
        )


if __name__ == "__main__":
    unittest.main()
