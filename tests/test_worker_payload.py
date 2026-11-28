import ast
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from threading import Thread
import time
import unittest
import zipfile

from buzz_server import release, worker_payload
from buzz_server.dependencies import ACP_FLAGS
from buzz_server.identity import public
from buzz_server.runtime import STARTUP_EXIT
from buzz_server.storage import Store, atomic
from fixture_relay import Relay


class WorkerPayloadTests(unittest.TestCase):
    def test_deterministic_explicit_worker_allowlist(self):
        payload = worker_payload.build()
        self.assertEqual(payload, worker_payload.build())
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(set(archive.namelist()), {
                "__main__.py", "buzz_server/__init__.py", "buzz_server/config.py",
                "buzz_server/errors.py", "buzz_server/identity.py", "buzz_server/observations.py", "buzz_server/runtime.py",
                "buzz_server/profile.py", "buzz_server/storage.py", "buzz_server/dependencies.py"})
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

    def test_packaged_managed_worker_uses_only_the_credential_and_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="buzz-managed-prompt-") as temp:
            root = Path(temp)
            payload = root / "worker.pyz"
            payload.write_bytes(worker_payload.build())
            (root / "buzz-acp.toml").touch()
            relay = Relay(root)
            serving = Thread(target=relay.serve_forever, daemon=True)
            serving.start()
            self.addCleanup(relay.server_close)
            self.addCleanup(relay.shutdown)
            for name, script in {
                "buzz": "exit 1",
                "goose": "exit 0",
                "buzz-acp": f"""if [ "$1" = --help ]; then echo '{" ".join(ACP_FLAGS)}'; exit 0; fi
while [ "$#" -gt 1 ]; do
    case "$1" in
        --system-prompt-file) [ "$(cat -- "$2")" = approved ] || exit 1 ;;
        --agent-command) [ "$2" = "{root / 'goose'}" ] || exit 1 ;;
        --config) [ "$2" = "{root / 'buzz-acp.toml'}" ] || exit 1 ;;
    esac
    shift 2
done
exec sleep 10
""",
            }.items():
                path = root / name
                path.write_text("#!/bin/sh\n" + script + "\n")
                path.chmod(0o700)
            store = Store(root / "state")
            agent = store.directory("assistant")
            key = "0" * 63 + "1"
            atomic(agent / "identity.key", key.encode())
            atomic(agent / "substituted.txt", b"substituted")
            atomic(agent / "prompt.txt", b"substituted snapshot")
            store.save({"version": 1, "id": "assistant", "identity": public(key), "desired": "running", "revision": 1,
                        "config": {"relay_url": f"ws://127.0.0.1:{relay.server_port}", "owner": "cd" * 32,
                                   "profile": {"name": "Fixture", "about": "", "avatar": ""},
                                   "prompt_file": str(agent / "substituted.txt"),
                                   "harness": {"kind": "goose", "command": str(agent / "substituted-goose")},
                                   "provider": {"kind": "goose-managed"}}})
            credentials = root / "credentials"
            credential = credentials / "prompt"
            atomic(credential, b"approved")
            credential.chmod(0o400)
            cmd = ["/usr/bin/python3", "-I", str(payload), str(store.root), "assistant", "prompt-0"]
            env = {"PATH": "/usr/bin:/bin", "CREDENTIALS_DIRECTORY": str(credentials)}
            ready = agent / "ready.json"
            with subprocess.Popen(cmd, cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as worker:
                try:
                    deadline = time.monotonic() + 5
                    while not ready.exists() and worker.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(ready.exists())
                    profile = json.loads((root / "profile.json").read_text())
                    self.assertEqual(profile["pubkey"], public(key))
                    self.assertIs(json.loads(profile["content"])["is_agent"], True)
                    self.assertEqual(profile["tags"], [])
                    self.assertEqual((agent / "prompt.txt").read_bytes(), b"substituted snapshot")
                finally:
                    worker.terminate()
                    self.assertEqual(worker.communicate(timeout=6), (b"", b""))
            credential.unlink()
            for substitute in (False, True):
                if substitute:
                    credential.symlink_to(agent / "substituted.txt")
                result = subprocess.run(cmd, cwd=root, env=env, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, STARTUP_EXIT["local_setup_refused"])
                self.assertEqual((result.stdout, result.stderr), (b"", b""))
                self.assertFalse(ready.exists())

    @unittest.skipUnless(os.environ.get("BUZZ_SERVER_TEST_SYSTEM_CREDENTIALS") == "1",
                         "requires a disposable host with a system manager and operator sudo")
    def test_packaged_reader_accepts_real_systemd_credential(self):
        with tempfile.TemporaryDirectory(prefix="buzz-system-credential-") as temp:
            root = Path(temp)
            archive = root / "worker.pyz"
            archive.write_bytes(worker_payload.build())
            source = root / "operator-prompt"
            atomic(source, b"synthetic-approved")
            unit = "buzz-server-credential-" + root.name.rsplit("-", 1)[1]
            program = """import os
import sys
uid = int(sys.argv[2])
assert os.getuid() == uid != 0
sys.path.insert(0, sys.argv[1])
from buzz_server.runtime import credential_prompt
path, prompt = credential_prompt()
assert prompt == b'synthetic-approved'
try:
    path.chmod(0o600)
except OSError:
    pass
else:
    raise AssertionError('credential was writable by the service UID')
"""
            result = subprocess.run(
                ["/usr/bin/sudo", "--non-interactive", "--", "/usr/bin/systemd-run", "--system",
                 "--quiet", "--wait", "--pipe", "--collect", f"--unit={unit}",
                 f"--uid={os.getuid()}", f"--gid={os.getgid()}",
                 "--property=NoNewPrivileges=yes", "--property=RuntimeMaxSec=10",
                 f"--property=LoadCredential=prompt:{source}", "/usr/bin/python3", "-I", "-c", program,
                 str(archive), str(os.getuid())],
                capture_output=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))


if __name__ == "__main__":
    unittest.main()
