import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import vibe_memory_hooks as hooks

@unittest.skipUnless(os.name == "nt", "native Windows hooks")
class WindowsHookTests(unittest.TestCase):
    def test_command_calls_python_directly(self):
        with tempfile.TemporaryDirectory(prefix="native space ") as directory:
            root = pathlib.Path(directory)
            launcher = root / "bin/nexarl-forge.cmd"
            rendered = hooks.command(launcher, "codex", "SessionStart")
            expected = f'"{sys.executable}" "{root / "current/scripts/vibe_memory_cli.py"}" hook --agent codex --event SessionStart'
            self.assertEqual(rendered, expected)

    def test_repair_idempotent_uninstall_preserves_external_hook(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            target = root / "hooks.json"
            launcher = root / "bin/nexarl-forge.cmd"
            original = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo custom"}]}]}, "custom": True}
            target.write_text(json.dumps(original), encoding="utf-8")
            first = hooks.repair(target, "codex", launcher)
            self.assertTrue(first["changed"])
            self.assertEqual(json.loads(pathlib.Path(first["backup"]).read_text(encoding="utf-8")), original)
            self.assertFalse(hooks.repair(target, "codex", launcher)["changed"])
            self.assertEqual(hooks.status(target, "codex", launcher)["status"], "current")
            hooks.uninstall(target, launcher)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), original)

    def test_transaction_source_check_preserves_new_custom_hook(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            target = root / "hooks.json"
            launcher = root / "bin/nexarl-forge.cmd"
            before = b'{"hooks": {}}'
            target.write_bytes(before)
            changed = b'{"hooks": {"Stop": [{"hooks": [{"type":"command","command":"echo custom"}]}]}}'
            target.write_bytes(changed)
            with self.assertRaises(hooks.ConcurrentConfigChange):
                hooks.repair(target, "codex", launcher, _expected_source=before)
            self.assertEqual(target.read_bytes(), changed)
            with self.assertRaises(hooks.ConcurrentConfigChange):
                hooks.uninstall(target, launcher, _expected_source=before)
            self.assertEqual(target.read_bytes(), changed)

    def test_cmd_executes_hook_path_with_ampersand_without_second_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "native&path"
            script = root / "current/scripts/vibe_memory_cli.py"
            script.parent.mkdir(parents=True)
            script.write_text("import json,sys; print(json.dumps(sys.argv[1:]))", encoding="utf-8")
            command = hooks.command(root / "bin/nexarl-forge.cmd", "codex", "SessionStart")
            result = subprocess.run(command, shell=True, capture_output=True, encoding="utf-8", timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), ["hook", "--agent", "codex", "--event", "SessionStart"])
            with self.assertRaisesRegex(ValueError, "percent"):
                hooks.command(pathlib.Path(directory) / "%TEMP%/bin/nexarl-forge.cmd", "codex", "SessionStart")
