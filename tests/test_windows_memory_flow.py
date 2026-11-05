import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/vibe_memory_cli.py"

@unittest.skipUnless(os.name == "nt", "Windows memory end-to-end")
class WindowsMemoryFlowTests(unittest.TestCase):
    def test_registered_candidate_requires_explicit_approval_and_unregistered_rejected(self):
        with tempfile.TemporaryDirectory(prefix="native memory ") as directory:
            home = pathlib.Path(directory) / "home"
            project = pathlib.Path(directory) / "project"
            other = pathlib.Path(directory) / "unregistered"
            for path in (home, project, other):
                path.mkdir()
            environment = os.environ.copy()
            environment.update(USERPROFILE=str(home), HOME=str(home), PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
            def run(*arguments, good=True):
                result = subprocess.run([sys.executable, "-B", str(CLI), *arguments], env=environment, capture_output=True, text=True, encoding="utf-8", timeout=20)
                if good:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    return json.loads(result.stdout)
                self.assertNotEqual(result.returncode, 0)
            run("project", "register", str(project))
            run("project", "init", str(project))
            formal = project / "codex/codex_long_memory.md"
            before = formal.read_bytes()
            for agent in ("codex", "claude-code"):
                with self.subTest(agent=agent):
                    result = subprocess.run([sys.executable, "-B", str(CLI), "hook", "--agent", agent, "--event", "SessionStart"], input=json.dumps({"cwd": str(project), "session_id": "native-" + agent}), cwd=project, env=environment, capture_output=True, encoding="utf-8", timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    routed = json.loads(result.stdout)
                    self.assertNotEqual(routed.get("status"), "degraded", routed)
                    self.assertIn("Shared Memory Context", result.stdout)
                    result = subprocess.run([sys.executable, "-B", str(CLI), "hook", "--agent", agent, "--event", "SessionStart"], input=json.dumps({"cwd": str(other), "session_id": "outside-" + agent}), cwd=other, env=environment, capture_output=True, encoding="utf-8", timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse((other / "codex").exists())
            self.assertEqual(formal.read_bytes(), before)
            args = ["memory", "propose", "--scope", "project", "--target", "long", "--category", "project_architecture", "--title", "Native test", "--summary", "The project uses explicit approval for durable architecture decisions."]
            created = run("--project-root", str(project), *args)
            self.assertEqual(formal.read_bytes(), before)
            items = run("--project-root", str(project), "memory", "list")["items"]
            self.assertEqual(len(items), 1)
            candidate = items[0]["id"]
            run("--project-root", str(project), "memory", "approve", candidate)
            self.assertIn("explicit approval", formal.read_text(encoding="utf-8"))
            run("--project-root", str(other), *args, good=False)
            self.assertFalse((other / "codex").exists())
