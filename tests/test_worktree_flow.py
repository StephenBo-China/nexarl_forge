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
import worktree_flow as workflow


def command(*args: str, cwd: pathlib.Path | None = None) -> str:
    result = subprocess.run(
        list(args),
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout.strip()


class WorktreeFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.temp.name)
        self.remote = self.base / "remote.git"
        self.repo = self.base / "canonical"
        self.worktrees = self.base / "worktrees"
        command("git", "init", "--bare", str(self.remote))
        command("git", "init", "-b", "master", str(self.repo))
        command("git", "config", "user.name", "Test User", cwd=self.repo)
        command("git", "config", "user.email", "test@example.com", cwd=self.repo)
        (self.repo / "app.txt").write_text("base\n", encoding="utf-8")
        command("git", "add", "app.txt", cwd=self.repo)
        command("git", "commit", "-m", "initial", cwd=self.repo)
        command("git", "remote", "add", "origin", str(self.remote), cwd=self.repo)
        command("git", "push", "-u", "origin", "master", cwd=self.repo)
        config = {
            "schema_version": 2,
            "repository": {"canonical_root": str(self.repo), "main_branch": "master", "remote": "origin"},
            "worktree": {"root": str(self.worktrees)},
            "branch": {"name_format": "loop/<project>-<date>-<slug>"},
            "verification": {"commands": []},
        }
        (self.repo / ".loop").mkdir()
        (self.repo / ".loop" / "config.json").write_text(json.dumps(config), encoding="utf-8")
        self.manager = self.base / "manager"
        workflow.MANAGER_ROOT = self.manager
        workflow.REGISTRY_PATH = self.manager / "tasks.json"
        workflow.LOCK_ROOT = self.manager / "locks"
        workflow.DEFAULT_WORKTREE_ROOT = self.worktrees

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_generated_loop_config_has_safe_multi_conversation_defaults(self) -> None:
        value = memory_project.loop_config(self.repo, 8088)
        self.assertEqual(value["schema_version"], 2)
        self.assertEqual(pathlib.Path(value["repository"]["canonical_root"]), self.repo.resolve())
        self.assertFalse(value["worktree"]["allow_inside_canonical_root"])
        self.assertTrue(value["release"]["serialized"])
        self.assertEqual(value["canonical_sync"]["mode"], "ff-only")
        self.assertFalse(value["canonical_sync"]["allow_auto_stash"])
        self.assertTrue(value["verification"]["require_remote_canonical_deploy_commit_match"])

    def test_upgrade_loop_config_preserves_project_resource_values(self) -> None:
        config_path = self.repo / ".loop" / "config.json"
        current = json.loads(config_path.read_text(encoding="utf-8"))
        current["schema_version"] = 1
        current["staging"] = {
            "port": 9191,
            "database": "project_offline_database",
            "oss_bucket": "project-controlled-bucket",
        }
        config_path.write_text(json.dumps(current), encoding="utf-8")

        upgraded, status = memory_project.upgrade_loop_config(self.repo, 8088)
        self.assertEqual(status, "upgraded")
        self.assertEqual(upgraded["schema_version"], 2)
        self.assertEqual(upgraded["staging"]["port"], 9191)
        self.assertEqual(upgraded["staging"]["database"], "project_offline_database")
        self.assertEqual(upgraded["staging"]["oss_bucket"], "project-controlled-bucket")
        self.assertTrue(upgraded["release"]["serialized"])
        self.assertEqual(upgraded["canonical_sync"]["mode"], "ff-only")

    def test_start_finish_release_sync_and_verify(self) -> None:
        entry = workflow.start(str(self.repo), "preview-chat", "conversation-1")
        feature = pathlib.Path(entry["worktree"])
        self.assertFalse(str(feature).startswith(str(self.repo) + "/"))
        (feature / "feature.txt").write_text("feature\n", encoding="utf-8")
        command("git", "add", "feature.txt", cwd=feature)
        command("git", "commit", "-m", "feature", cwd=feature)
        command("git", "push", "-u", "origin", entry["branch"], cwd=feature)

        finished = workflow.finish(str(self.repo), "preview-chat")
        self.assertEqual(finished["status"], "ready_for_user_acceptance")
        with self.assertRaises(workflow.WorkflowError):
            workflow.release(str(self.repo), "preview-chat", approved=False, test_commands=[])

        released = workflow.release(
            str(self.repo),
            "preview-chat",
            approved=True,
            test_commands=["git diff --check"],
        )
        self.assertEqual(released["status"], "canonical_synced")
        checked = workflow.verify(str(self.repo), "preview-chat")
        self.assertTrue(checked["ok"])
        self.assertTrue(checked["feature_is_ancestor"])
        self.assertTrue(checked["canonical_matches_remote"])

    def test_finish_runs_configured_feature_worktree_validation(self) -> None:
        config_path = self.repo / ".loop" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["worktree"]["finish_validation_commands"] = [
            "test -f finish-ready.txt"
        ]
        config_path.write_text(json.dumps(config), encoding="utf-8")

        entry = workflow.start(str(self.repo), "validated-finish", "conversation-2")
        feature = pathlib.Path(entry["worktree"])
        (feature / "feature.txt").write_text("feature\n", encoding="utf-8")
        command("git", "add", "feature.txt", cwd=feature)
        command("git", "commit", "-m", "feature", cwd=feature)
        command("git", "push", "-u", "origin", entry["branch"], cwd=feature)

        with self.assertRaises(workflow.WorkflowError):
            workflow.finish(str(self.repo), "validated-finish")

        (feature / "finish-ready.txt").write_text("ready\n", encoding="utf-8")
        command("git", "add", "finish-ready.txt", cwd=feature)
        command("git", "commit", "-m", "finish evidence", cwd=feature)
        command("git", "push", cwd=feature)

        finished = workflow.finish(str(self.repo), "validated-finish")
        self.assertEqual(finished["status"], "ready_for_user_acceptance")

    def test_canonical_sync_blocks_overlapping_dirty_file(self) -> None:
        updater = self.base / "updater"
        command("git", "clone", "--branch", "master", str(self.remote), str(updater))
        command("git", "config", "user.name", "Update User", cwd=updater)
        command("git", "config", "user.email", "update@example.com", cwd=updater)
        (updater / "app.txt").write_text("remote update\n", encoding="utf-8")
        command("git", "add", "app.txt", cwd=updater)
        command("git", "commit", "-m", "remote update", cwd=updater)
        command("git", "push", "origin", "master", cwd=updater)
        (self.repo / "app.txt").write_text("local dirty update\n", encoding="utf-8")

        result = workflow.sync_canonical(str(self.repo), fetch=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked_by_dirty_overlap")
        self.assertEqual(result["overlap"], ["app.txt"])
        self.assertEqual((self.repo / "app.txt").read_text(encoding="utf-8"), "local dirty update\n")


if __name__ == "__main__":
    unittest.main()
