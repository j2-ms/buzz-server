import ast
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

from buzz_server import release, worker_payload


class WorkerPayloadTests(unittest.TestCase):
    def test_deterministic_explicit_worker_allowlist(self):
        payload = worker_payload.build()
        self.assertEqual(payload, worker_payload.build())
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(set(archive.namelist()), {
                "__main__.py", "buzz_server/__init__.py", "buzz_server/config.py",
                "buzz_server/errors.py", "buzz_server/identity.py", "buzz_server/runtime.py",
                "buzz_server/storage.py", "buzz_server/dependencies.py"})
            for name in archive.namelist():
                compile(archive.read(name), name, "exec")
            tools = archive.read("buzz_server/dependencies.py").decode()
            functions = {n.name for n in ast.parse(tools).body if isinstance(n, ast.FunctionDef)}
            self.assertEqual(functions, set(worker_payload.TOOL_FUNCTIONS))
            self.assertNotIn("urllib", tools)
            self.assertNotIn("download", tools)
            entry = archive.read("__main__.py")
            self.assertNotIn(b"buzz_server.cli", entry)
            self.assertNotIn(b"release", entry)

    def test_entry_refuses_management_commands_without_creating_state(self):
        with tempfile.TemporaryDirectory(prefix="buzz-worker-") as temp:
            root = Path(temp)
            archive = root / "worker.pyz"
            archive.write_bytes(worker_payload.build())
            for args in (["--help"], ["create", "agent", "--config", "never-read"], ["_worker"]):
                p = subprocess.run(["/usr/bin/python3", "-I", str(archive), *args],
                                   env={"PATH": "/usr/bin:/bin"}, cwd=root,
                                   capture_output=True, timeout=5)
                self.assertNotEqual(p.returncode, 0)
                self.assertEqual(p.stdout, b"")
                self.assertEqual(p.stderr, b"")
            self.assertEqual(list(root.iterdir()), [archive])

    def test_worker_can_be_built_from_installed_release_without_source_tree(self):
        with tempfile.TemporaryDirectory(prefix="buzz-worker-release-") as temp:
            root = Path(temp)
            archive = root / "buzz-server.pyz"
            release.build(Path(__file__).resolve().parents[1], archive, "1.0.0")
            program = ("import sys; sys.path.insert(0, sys.argv[1]); "
                       "from buzz_server.worker_payload import build; sys.stdout.buffer.write(build())")
            p = subprocess.run(["/usr/bin/python3", "-I", "-c", program, str(archive)],
                               cwd=root, env={"PATH": "/usr/bin:/bin"}, capture_output=True, timeout=5)
            self.assertEqual(p.returncode, 0, p.stderr)
            with zipfile.ZipFile(io.BytesIO(p.stdout)) as worker:
                self.assertNotIn("buzz_server/cli.py", worker.namelist())
                self.assertIn(b"1.0.0", worker.read("buzz_server/__init__.py"))
            # Execute the real assembled entry, not merely its source/build.
            payload = root / "worker.pyz"
            payload.write_bytes(p.stdout)
            state = root / "state"
            agent = state / "agents/assistant"
            agent.mkdir(mode=0o700, parents=True)
            (state / "agents").chmod(0o700)
            state.chmod(0o700)
            definition = agent / "definition.json"
            definition.write_text(json.dumps({"version": 1, "id": "assistant", "identity": "ab" * 32,
                "desired": "stopped", "revision": 1,
                "config": {"relay_url": "wss://relay.example.test", "owner": "cd" * 32,
                           "profile": {"name": "Fixture", "about": "", "avatar": ""},
                           "prompt_file": str(agent / "prompt"),
                           "harness": {"kind": "goose", "command": str(state / "tools/bin/goose")},
                           "provider": {"kind": "goose-managed"}}}))
            definition.chmod(0o600)
            result = subprocess.run(["/usr/bin/python3", "-I", str(payload), str(state), "assistant"],
                                    cwd=root, env={"PATH": "/usr/bin:/bin"}, capture_output=True, timeout=5)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
            self.assertEqual(json.loads(definition.read_text())["identity"], "ab" * 32)


if __name__ == "__main__":
    unittest.main()
