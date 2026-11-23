"""Execute target file operations only in synthetic private directories."""
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

from buzz_server import worker_payload
from buzz_server.storage import atomic


class TargetFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="buzz-target-files-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir(mode=0o700)
        self.data = self.home / ".local/share/buzz-server-managed/fixture/assistant"
        self.binding = {"version": 1, "id": "assistant", "account": "synthetic",
                        "uid": os.getuid(), "gid": os.getgid(), "home": str(self.home),
                        "data": str(self.data), "service": "buzz-server-account-fixture-assistant.service"}
        script = b"#!/usr/bin/python3\nprint('--system-prompt-file --agent-command --agent-args --model --permission-mode --respond-to --session-title --config')\n"
        self.files = {"worker.pyz": worker_payload.build(), "buzz": script, "buzz-acp": script, "goose": script}
        header = {n: {"size": len(v), "sha256": hashlib.sha256(v).hexdigest()} for n, v in self.files.items()}
        raw = json.dumps(header).encode()
        self.bundle = struct.pack("!I", len(raw)) + raw + b"".join(self.files[n] for n in ("worker.pyz", "buzz", "buzz-acp", "goose"))

    def invoke(self, operation, request=None, raw=None, success=True):
        # Synthetic NSS only, with the current unprivileged test UID. No sudo,
        # account mutation, external binary, relay or provider is involved.
        program = (worker_payload.source("account_target") + "\nBINDING=" + repr(self.binding) +
                   "\nOPERATION=" + repr(operation) +
                   "\nfrom types import SimpleNamespace\npwd.getpwnam=lambda name: SimpleNamespace(pw_uid=BINDING['uid'],pw_gid=BINDING['gid'],pw_dir=BINDING['home'])\n" +
                   "REQUEST=" + ("{}" if raw is not None else "json.loads(sys.stdin.buffer.read(131073))") + "\nmain()\n")
        result = subprocess.run(["/usr/bin/python3", "-I", "-c", program], cwd=self.home,
                                env={"PATH": "/usr/bin:/bin", "HOME": str(self.home)},
                                input=raw if raw is not None else json.dumps(request or {}).encode(),
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0 if success else 1, result.stderr)
        self.assertEqual(result.stderr, b"")
        return json.loads(result.stdout) if success else None

    def apply(self, revision=1, identity=None):
        return self.invoke("apply", {"identity": identity, "revision": revision, "prompt": "Synthetic private prompt",
            "config": {"relay_url": "wss://relay.example.test", "owner": "ab" * 32,
                       "profile": {"name": "Fixture", "about": "", "avatar": ""},
                       "prompt_file": "/unused/operator/prompt", "harness": {"kind": "goose", "command": "/unused/operator/goose"},
                       "provider": {"kind": "goose-managed"}}})

    def test_real_target_install_configure_delete_retains_identity_and_state(self):
        self.invoke("install", raw=self.bundle)
        self.assertEqual((self.data / "worker.pyz").stat().st_mode & 0o777, 0o600)
        for name in ("buzz", "buzz-acp", "goose"):
            self.assertEqual((self.data / "state/tools/bin" / name).stat().st_mode & 0o777, 0o700)
        created = self.apply()
        agent = self.data / "state/agents/assistant"
        key = (agent / "identity.key").read_bytes()
        marker = agent / "home/.config/synthetic-private-state"
        atomic(marker, b"synthetic credentials and state")
        self.assertEqual(self.apply(2, created["identity"])["identity"], created["identity"])
        self.invoke("delete", {"identity": created["identity"]})
        self.invoke("delete", {"identity": created["identity"]})
        self.assertFalse((agent / "definition.json").exists())
        self.assertEqual((agent / "identity.key").read_bytes(), key)
        self.assertEqual(marker.read_bytes(), b"synthetic credentials and state")
        self.assertEqual(marker.stat().st_mode & 0o777, 0o600)

    def test_interrupted_stage_retry_and_hardlink_refusal_preserve_unrelated_file(self):
        self.invoke("install", raw=self.bundle[:-10], success=False)
        self.invoke("install", raw=self.bundle)
        unrelated = self.home / "unrelated"
        atomic(unrelated, b"retain exactly")
        stage = self.data / ".worker.pyz.stage"
        os.link(unrelated, stage)
        self.invoke("install", raw=self.bundle, success=False)
        self.assertEqual(unrelated.read_bytes(), b"retain exactly")


if __name__ == "__main__":
    unittest.main()
