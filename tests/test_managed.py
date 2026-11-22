"""Offline Goose-managed regressions; all state and commands are synthetic."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import ANY, Mock, patch

from buzz_server import cli, config, runtime
from buzz_server.errors import Error
from buzz_server.storage import Store, atomic, read_private


class ManagedSchemaTests(unittest.TestCase):
    def test_managed_minimal_schema_allows_unconfigured_create(self):
        c = {"relay_url": "ws://127.0.0.1:19000", "owner": "ab" * 32,
             "profile": {"name": "Synthetic", "about": "", "avatar": ""},
             "prompt_file": "/synthetic/prompt",
             "harness": {"kind": "goose", "command": "/synthetic/goose"},
             "provider": {"kind": "goose-managed"}}
        self.assertEqual(config.validate(c), c)

    def test_obsolete_provider_shapes_rejected_with_static_private_error(self):
        base = {"relay_url": "ws://127.0.0.1:19000", "owner": "ab" * 32,
                "profile": {"name": "Synthetic", "about": "", "avatar": ""},
                "prompt_file": "/SYNTHETIC-PRIVATE/prompt",
                "harness": {"kind": "goose", "command": "/synthetic/goose"}}
        messages = []
        for kind in ("openai", "anthropic", "goose-openai"):
            with self.subTest(kind=kind):
                c = copy.deepcopy(base)
                c["provider"] = {"kind": kind, "model": "SYNTHETIC-PRIVATE-KEY",
                                 "env_file": "/SYNTHETIC-PRIVATE/provider.env"}
                if kind == "goose-openai":
                    del c["harness"]
                    c["provider"]["command"] = "/SYNTHETIC-PRIVATE/goose"
                with patch("buzz_server.storage.read_private") as read:
                    with self.assertRaises(Error) as error:
                        config.validate(c)
                    read.assert_not_called()
                self.assertEqual(error.exception.code, "invalid_config")
                self.assertNotIn("SYNTHETIC-PRIVATE", str(error.exception))
                messages.append(str(error.exception))
        self.assertEqual(len(set(messages)), 1)


class ManagedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="buzz-managed-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "state"
        self.prompt = self.base / "prompt"
        atomic(self.prompt, b"SYNTHETIC-PRIVATE-PROMPT")
        self.c = {"relay_url": "ws://127.0.0.1:19000", "owner": "ab" * 32,
                  "profile": {"name": "Synthetic", "about": "", "avatar": ""},
                  "prompt_file": str(self.prompt),
                  "harness": {"kind": "goose", "command": "/usr/bin/true"},
                  "provider": {"kind": "goose-managed"}}
        self.path = self.base / "config.json"
        self.write_config()
        self.supervisor = Mock()
        self.supervisor.inspect.return_value = {"ActiveState": "inactive", "MainPID": "0", "Result": "success"}
        self.supervisor.name.return_value = "synthetic.service"
        self.supervisor.stopped.return_value = True
        for target, kwargs in (
            ("buzz_server.cli.Systemd", {"return_value": self.supervisor}),
            ("buzz_server.cli.generate", {"return_value": ("1" * 64, "ab" * 32)}),
            ("buzz_server.runtime.public", {"return_value": "ab" * 32}),
            ("buzz_server.runtime.tool", {"side_effect": lambda name, root=None: "/synthetic/" + name}),
            ("buzz_server.runtime.help_check", {}),
        ):
            p = patch(target, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def write_config(self):
        atomic(self.path, json.dumps(self.c).encode())

    def invoke(self, *args, structured=True, tty=True):
        out, err = io.StringIO(), io.StringIO()
        out.isatty = err.isatty = lambda: tty
        stdin = Mock()
        stdin.isatty.return_value = tty
        with patch("buzz_server.cli.sys.stdin", stdin), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main((["--json"] if structured else []) + ["--state-dir", str(self.root), "--unit-dir", str(self.base / "units"), *args])
        output = out.getvalue() + err.getvalue()
        self.assertNotIn("SYNTHETIC-PRIVATE", output)
        return code, output

    def create(self):
        code, output = self.invoke("create", "agent", "--config", str(self.path))
        self.assertEqual(code, 0, output)
        return Store(self.root).load("agent")

    def wizard(self):
        return self.invoke("inference", "configure", "agent", structured=False)

    def test_clean_create_status_and_runtime_without_model_overrides(self):
        doc = self.create()
        self.assertEqual(doc["desired"], "stopped")
        store = Store(self.root)
        with patch.dict(os.environ, {"GOOSE_MODEL": "evil", "GOOSE_PROVIDER": "evil", "OPENAI_API_KEY": "SYNTHETIC-PRIVATE-KEY", "LD_PRELOAD": "evil", "BUZZ_AUTHOR": "evil"}):
            command, _, env, work = runtime.prepare(store, doc)
        self.assertNotIn("--model", command)
        self.assertNotIn("GOOSE_MODEL", env)
        self.assertNotIn("GOOSE_PROVIDER", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("LD_PRELOAD", env)
        self.assertNotIn("BUZZ_AUTHOR", env)
        self.assertEqual(env["GOOSE_DISABLE_KEYRING"], "1")
        code, output = self.invoke("status", "agent")
        self.assertEqual(code, 0)
        status = json.loads(output)["data"]
        self.assertEqual(status["configuration"]["provider"], "goose-managed")
        self.assertIsNone(status["configuration"]["model"])
        self.assertFalse(status["buzz_response_verified"])
        for path in (work, Path(env["HOME"]), Path(env["XDG_CONFIG_HOME"]), Path(env["XDG_DATA_HOME"])):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_wizard_exact_context_terminal_allowlist_umask_and_no_capture(self):
        doc = self.create()
        child = Mock()
        child.wait.return_value = child.poll.return_value = 0
        ambient = {"TERM": "xterm", "COLORTERM": "truecolor", "TERMINFO": "/evil", "LANG": "evil", "LC_ALL": "evil", "GOOSE_MODEL": "evil", "BUZZ_PRIVATE_KEY": "SYNTHETIC-PRIVATE-KEY", "LD_PRELOAD": "evil", "OPENAI_API_KEY": "SYNTHETIC-PRIVATE-KEY"}
        with patch.dict(os.environ, ambient), patch("buzz_server.runtime.subprocess.Popen", return_value=child) as popen:
            code, output = self.wizard()
        self.assertEqual(code, 0, output)
        self.assertIn("setup exited", output)
        self.assertIn("unverified", output)
        args, kwargs = popen.call_args
        self.assertEqual(args, (["/usr/bin/true", "configure"],))
        expected, work = runtime.goose_context(Store(self.root), doc)
        expected.update(TERM="xterm", COLORTERM="truecolor")
        self.assertEqual(kwargs, {"env": expected, "cwd": work, "umask": 0o077})
        self.assertEqual(Store(self.root).load("agent"), doc)
        self.assertFalse((Store(self.root).directory("agent") / "events.jsonl").exists())
        self.assertNotIn(b"SYNTHETIC-PRIVATE", read_private(Store(self.root).directory("agent") / "definition.json"))
        self.assertEqual(self.invoke("start", "agent")[0], 0)
        self.supervisor.start.assert_called_once()

    def test_wizard_refuses_json_and_each_non_terminal_before_goose(self):
        self.create()
        with patch("buzz_server.runtime.subprocess.Popen") as popen:
            code, output = self.invoke("inference", "configure", "agent")
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output)["error"]["code"], "interactive_required")
            code, output = self.invoke("inference", "configure", "agent", structured=False, tty=False)
            self.assertEqual(code, 1)
            self.assertIn("interactive_required", output)
            popen.assert_not_called()

    def test_wizard_requires_stopped_intent_and_empty_supervised_worker(self):
        doc = self.create()
        store = Store(self.root)
        with patch("buzz_server.runtime.subprocess.Popen") as popen:
            doc["desired"] = "running"
            store.save(doc)
            self.assertIn("stop_required", self.wizard()[1])
            doc["desired"] = "stopped"
            store.save(doc)
            self.supervisor.stopped.return_value = False
            self.assertIn("stop_required", self.wizard()[1])
            self.supervisor.stopped.side_effect = Error("supervisor_unavailable", "Synthetic")
            self.assertEqual(self.wizard()[0], 1)
            popen.assert_not_called()
            self.supervisor.stop.assert_not_called()

    def test_nonzero_and_interrupt_preserve_definition_and_existing_setup(self):
        doc = self.create()
        store = Store(self.root)
        env, _ = runtime.goose_context(store, doc)
        settings = Path(env["XDG_CONFIG_HOME"]) / "opaque-goose-settings"
        atomic(settings, b"SYNTHETIC-PRIVATE-SECRET: opaque\n")
        for interrupted in (False, True):
            child = Mock()
            child.poll.return_value = None if interrupted else 2
            child.wait.side_effect = [KeyboardInterrupt(), 0] if interrupted else [2]
            with patch("buzz_server.runtime.subprocess.Popen", return_value=child):
                code, output = self.wizard()
            self.assertEqual(code, 1)
            self.assertNotIn("setup exited.", output)
            self.assertEqual(store.load("agent"), doc)
            self.assertEqual(read_private(settings), b"SYNTHETIC-PRIVATE-SECRET: opaque\n")
            if interrupted:
                child.terminate.assert_called_once()
            self.assertEqual(self.invoke("status", "agent")[0], 0)
            self.assertEqual(self.invoke("logs", "agent")[0], 0)

    def test_running_reconfigure_restarts_without_wizard_and_preserves_private_state(self):
        doc = self.create()
        store = Store(self.root)
        env, _ = runtime.goose_context(store, doc)
        settings = Path(env["XDG_CONFIG_HOME"]) / "opaque-goose-settings"
        atomic(settings, b"SYNTHETIC-PRIVATE-SETTINGS")
        self.assertEqual(self.invoke("start", "agent")[0], 0)
        self.supervisor.reset_mock()
        self.c["profile"]["name"] = "Edited"
        self.write_config()
        with patch("buzz_server.cli.configure_goose") as wizard:
            code, output = self.invoke("configure", "agent", "--config", str(self.path))
            wizard.assert_not_called()
        self.assertEqual(code, 0, output)
        self.supervisor.stop.assert_called_once_with("agent")
        self.supervisor.start.assert_called_once_with("agent", ANY)
        updated = store.load("agent")
        self.assertEqual(updated["identity"], doc["identity"])
        self.assertEqual(updated["desired"], "running")
        self.assertEqual(updated["revision"], doc["revision"] + 1)
        self.assertEqual(read_private(settings), b"SYNTHETIC-PRIVATE-SETTINGS")

    def test_obsolete_config_cli_errors_preserve_existing_definition(self):
        doc = self.create()
        store = Store(self.root)
        before = read_private(store.directory("agent") / "definition.json")
        expected = {"code": "invalid_config", "message":
                    "Invalid configuration. Use the documented v1 schema, absolute file references and credential-free URLs; credentials belong in Goose private state."}
        for kind in ("openai", "anthropic", "goose-openai"):
            with self.subTest(kind=kind):
                c = copy.deepcopy(self.c)
                c["provider"] = {"kind": kind, "model": "SYNTHETIC-PRIVATE-KEY",
                                 "env_file": "/SYNTHETIC-PRIVATE/provider.env"}
                if kind == "goose-openai":
                    del c["harness"]
                    c["provider"]["command"] = "/SYNTHETIC-PRIVATE/goose"
                atomic(self.path, json.dumps(c).encode())
                with patch("buzz_server.cli.prepare") as prepare, patch("buzz_server.cli.generate") as generate:
                    for operation, ident in (("create", "rejected"), ("configure", "agent")):
                        code, output = self.invoke(operation, ident, "--config", str(self.path))
                        self.assertEqual(code, 1)
                        self.assertNotIn(str(self.path), output)
                        self.assertEqual(json.loads(output)["error"], expected)
                    prepare.assert_not_called()
                    generate.assert_not_called()
                self.assertFalse(store.directory("rejected").exists())
                self.assertEqual(read_private(store.directory("agent") / "definition.json"), before)
                self.assertEqual(store.load("agent"), doc)
        self.supervisor.stop.assert_not_called()
        self.supervisor.start.assert_not_called()

    def test_managed_rejects_extra_fields_and_requires_harness(self):
        for field, value in (("model", "fixture"), ("env_file", "/synthetic"), ("secret", "synthetic")):
            c = copy.deepcopy(self.c)
            c["provider"][field] = value
            with self.assertRaises(Error):
                config.validate(c)
        c = copy.deepcopy(self.c)
        del c["harness"]
        with self.assertRaises(Error):
            config.validate(c)

    def test_lock_held_until_interrupted_child_is_reaped(self):
        self.create()
        child = Mock()
        child.poll.return_value = None
        probe = "import fcntl,os,sys; f=os.open(sys.argv[1],os.O_RDWR); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"

        def wait(*args, **kwargs):
            # Separate process: nonblocking flock must fail while cleanup runs.
            result = subprocess.run([os.sys.executable, "-I", "-c", probe, str(self.root / ".lock")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            if not kwargs:
                raise KeyboardInterrupt
            self.assertEqual(signal.getsignal(signal.SIGINT), signal.SIG_IGN)
            return 0

        child.wait.side_effect = wait
        # Patch only runtime's Popen lookup, retaining the real subprocess probe.
        real_run = subprocess.run
        def probe_run(*args, **kwargs):
            with patch("subprocess.Popen", original_popen):
                return real_run(*args, **kwargs)
        original_popen = subprocess.Popen
        with patch("buzz_server.runtime.subprocess.Popen", return_value=child), patch("subprocess.run", side_effect=probe_run):
            self.assertEqual(self.wizard()[0], 1)
        result = real_run([os.sys.executable, "-I", "-c", probe, str(self.root / ".lock")], timeout=5)
        self.assertEqual(result.returncode, 0)

    def test_each_terminal_stream_required_and_root_denied(self):
        self.create()
        for stream in ("stdin", "stdout", "stderr"):
            with contextlib.ExitStack() as stack:
                for name in ("stdin", "stdout", "stderr"):
                    stack.enter_context(patch("buzz_server.cli.sys." + name + ".isatty", return_value=name != stream))
                popen = stack.enter_context(patch("buzz_server.runtime.subprocess.Popen"))
                args = cli.parser().parse_args(["--state-dir", str(self.root), "--unit-dir", str(self.base / "units"), "inference", "configure", "agent"])
                with self.assertRaises(Error) as error:
                    cli.execute(args)
                self.assertEqual(error.exception.code, "interactive_required")
                popen.assert_not_called()
        with patch("buzz_server.cli.os.getuid", return_value=0), patch("buzz_server.runtime.subprocess.Popen") as popen:
            self.assertIn("root_denied", self.wizard()[1])
            popen.assert_not_called()

    def test_sigterm_and_unresponsive_child_cleanup_restore_handlers(self):
        self.create()
        child = Mock()
        child.poll.return_value = None
        old_term = signal.getsignal(signal.SIGTERM)
        old_int = signal.getsignal(signal.SIGINT)
        calls = []

        def wait(*args, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            if len(calls) == 2:
                raise subprocess.TimeoutExpired("synthetic", 5)
            return 0

        child.wait.side_effect = wait
        with patch("buzz_server.runtime.subprocess.Popen", return_value=child):
            code, output = self.wizard()
        self.assertEqual(code, 1)
        self.assertIn("inference_cancelled", output)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertEqual(signal.getsignal(signal.SIGTERM), old_term)
        self.assertEqual(signal.getsignal(signal.SIGINT), old_int)

    def test_real_synthetic_child_private_file_and_clean_environment(self):
        fixture = self.base / "synthetic-goose"
        fixture.write_text("""#!/usr/bin/python3
import os
from pathlib import Path
import sys
assert sys.argv[1:] == ['configure']
assert os.environ['GOOSE_DISABLE_KEYRING'] == '1'
assert not any(k in os.environ for k in ('BUZZ_PRIVATE_KEY', 'BUZZ_AUTHOR', 'OPENAI_API_KEY', 'GOOSE_MODEL', 'GOOSE_PROVIDER', 'LD_PRELOAD', 'TERMINFO', 'LC_ALL'))
assert Path.cwd() == Path(os.environ['HOME']).parent / 'work'
Path(os.environ['XDG_CONFIG_HOME'], 'synthetic-secret').write_text('SYNTHETIC-PRIVATE-SECRET')
""")
        fixture.chmod(0o700)
        self.c['harness']['command'] = str(fixture)
        self.write_config()
        doc = self.create()
        env, _ = runtime.goose_context(Store(self.root), doc)
        # These values must never reach the real child (including loader keys).
        with patch.dict(os.environ, {k: 'SYNTHETIC-PRIVATE' for k in ('BUZZ_PRIVATE_KEY', 'BUZZ_AUTHOR', 'OPENAI_API_KEY', 'GOOSE_MODEL', 'GOOSE_PROVIDER', 'LD_PRELOAD', 'TERMINFO', 'LC_ALL')}):
            code, output = self.wizard()
        self.assertEqual(code, 0, output)
        secret = Path(env['XDG_CONFIG_HOME']) / 'synthetic-secret'
        self.assertEqual(secret.stat().st_mode & 0o777, 0o600)
        self.assertEqual(read_private(secret), b'SYNTHETIC-PRIVATE-SECRET')
