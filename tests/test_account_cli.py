"""Operator CLI routing with synthetic NSS, tools, target and system manager."""
import ast
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import struct
import unittest
from unittest.mock import patch
import zipfile

from buzz_server import cli, release
from buzz_server.account_supervisor import AccountSystemd
from buzz_server.account_lifecycle import manifest_path
from buzz_server.errors import Error
from buzz_server.storage import atomic, read_json, write_json
import test_accounts
from test_account_supervisor import FakeCommands


class DedicatedCliTests(unittest.TestCase):
    def setUp(self):
        test_accounts.AccountTests.setUp(self)
        self.units = self.store.root.parent / "units"
        self.prompt = self.store.root.parent / "prompt"
        atomic(self.prompt, b"SYNTHETIC-PRIVATE-PROMPT")
        self.config = self.store.root.parent / "config.json"
        write_json(self.config, {"relay_url": "wss://relay.example.test", "owner": "ab" * 32,
                   "profile": {"name": "Fixture", "about": "", "avatar": ""},
                   "prompt_file": str(self.prompt), "provider": {"kind": "goose-managed"},
                   "harness": {"kind": "goose", "command": "/unused/goose"}})
        self.model = None
        self.target_doc = None
        self.private_state = b"SYNTHETIC-PRIVATE-STATE"
        self.requests = []
        self.installs = []
        self.run.side_effect = self.system_run
        self.packages = ({"name": "fixture", "members": {n: (n, len(n)) for n in ("buzz", "buzz-acp", "goose")}},)
        patch("buzz_server.account_lifecycle.dependencies.PACKAGES", self.packages).start()
        tools = self.store.root / "tools/bin"
        tools.mkdir(mode=0o700, parents=True)
        (self.store.root / "tools").chmod(0o700)
        for name in ("buzz", "buzz-acp", "goose"):
            atomic(tools / name, name.encode())
            (tools / name).chmod(0o700)
        self.acquire = patch("buzz_server.account_lifecycle.dependencies.acquire",
                             side_effect=AssertionError("ordinary create must reuse installed private tools")).start()

        def root_file(supervisor, path, *, exact=True, missing=False):
            if self.model is None:
                self.model = FakeCommands(self.bindings.root)
                self.model.binding = supervisor.binding
                self.model.unit_path = supervisor.commands.unit_path
                self.model.stage_path = supervisor.commands.stage_path
            self.model.supervisor = supervisor
            if path not in self.model.files:
                if missing:
                    return False
                raise Error("unit_conflict", "Synthetic missing unit")
            if exact and self.model.files[path] != supervisor.content():
                raise Error("unit_conflict", "Synthetic wrong unit")
            return True

        patch("buzz_server.account_supervisor.AccountSystemd.root_file", root_file).start()

    def system_run(self, argv, **kwargs):
        from types import SimpleNamespace
        output = b""
        if argv[:2] == ["/usr/bin/systemctl", "--system"]:
            output = self.model.show(AccountSystemd.PROPERTIES) if self.model else b"LoadState=not-found\nDropInPaths=\nFragmentPath=\nActiveState=inactive\nMainPID=0\nControlGroup=\n"
        elif argv[:3] == ["/usr/bin/sudo", "--", "/usr/bin/systemctl"]:
            self.assertEqual(argv[3], "--system")
            if argv[4] == "daemon-reload":
                self.assertEqual(len(argv), 5)
                self.model.reload()
            else:
                self.assertEqual(len(argv), 6)
                self.assertEqual(argv[5], self.model.binding.service)
                self.model.control(argv[4])
        elif argv[:3] == ["/usr/bin/sudo", "--", "/usr/bin/install"]:
            self.assertEqual(argv[3:11], ["-T", "-o", "0", "-g", "0", "-m", "0644", "--"])
            self.assertEqual(argv[-1], str(self.model.stage_path))
            self.model.stage_unit(Path(argv[-2]))
        elif argv[:3] == ["/usr/bin/sudo", "--", "/usr/bin/ln"]:
            self.assertEqual(argv[3:], ["-T", "--", str(self.model.stage_path), str(self.model.unit_path)])
            self.model.publish_unit()
        elif argv[:3] == ["/usr/bin/sudo", "--", "/usr/bin/rm"]:
            self.assertEqual(argv[3], "--")
            self.assertIn(argv[4], [str(self.model.stage_path), str(self.model.unit_path)])
            self.model.unlink_unit(stage=argv[4] == str(self.model.stage_path))
        else:
            self.assertEqual(argv[:4], ["/usr/bin/sudo", "--user=#" + str(self.entry.pw_uid),
                                       "--group=#" + str(self.entry.pw_gid), "--"])
            self.assertEqual(argv[4:11], ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "LANG=C.UTF-8",
                                         "HOME=/home/fixture", "/usr/bin/python3", "-I"])
            self.assertNotIn("SYNTHETIC-PRIVATE", " ".join(argv))
            assignments = {n.targets[0].id: n.value for n in ast.parse(argv[-1]).body
                           if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)}
            operation = ast.literal_eval(assignments["OPERATION"])
            request = json.loads(kwargs["input"]) if kwargs.get("input") else {}
            self.requests.append((operation, request))
            if operation == "install":
                stream = kwargs["stdin"]
                header = json.loads(stream.read(struct.unpack("!I", stream.read(4))[0]))
                self.assertEqual(set(header), {"worker.pyz", "buzz", "buzz-acp", "goose"})
                worker = stream.read(header["worker.pyz"]["size"])
                with zipfile.ZipFile(io.BytesIO(worker)) as archive:
                    self.assertNotIn("buzz_server/cli.py", archive.namelist())
                    self.assertNotIn("buzz_server/release.py", archive.namelist())
                    self.assertIn("__main__.py", archive.namelist())
                self.installs.append(header)
                result = {"installed": True}
            elif operation == "apply":
                identity = self.target_doc["identity"] if self.target_doc else "cd" * 32
                self.target_doc = {"identity": identity, "revision": request["revision"], "desired": "stopped"}
                result = {"identity": identity, "revision": request["revision"]}
            elif operation == "desired":
                self.target_doc["desired"] = request["desired"]
                result = {"applied": True}
            elif operation == "delete":
                self.target_doc = None
                result = {"applied": True}
            elif operation == "inference":
                self.assertFalse(kwargs["capture_output"])
                result = None
            else:
                raise AssertionError(operation)
            output = json.dumps(result).encode()
        return SimpleNamespace(returncode=0, stdout=output)

    def call(self, *args, structured=True, success=True):
        out, err = io.StringIO(), io.StringIO()
        if not structured:
            out.isatty = err.isatty = lambda: True
        argv = ["--state-dir", str(self.store.root), "--unit-dir", str(self.units)]
        if structured:
            argv.insert(0, "--json")
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([*argv, *args])
        self.assertEqual(code, 0 if success else 1, err.getvalue())
        self.assertNotIn("SYNTHETIC-PRIVATE", out.getvalue() + err.getvalue())
        if structured:
            response = json.loads(out.getvalue() if success else err.getvalue())
            self.assertEqual(response["schema_version"], 1)
            self.assertEqual(response["operation"], args[0])
            if success:
                self.assertEqual(err.getvalue(), "")
            else:
                self.assertEqual(out.getvalue(), "")
            return response["data"] if success else response["error"]
        return out.getvalue()

    def create(self):
        return self.call("create", "assistant", "--account", "fixture", "--config", str(self.config))

    def test_guided_dedicated_create_collects_without_mutation_lock_and_paths(self):
        answers = ["", "", "", "ab" * 32, "wss://relay.example.test", "SYNTHETIC-PRIVATE-PROMPT", "yes"]
        def answer(_):
            self.assertFalse((self.store.root / ".lock").exists())
            self.assertFalse(self.bindings.path("assistant").exists())
            return answers.pop(0)
        with patch("builtins.input", side_effect=answer), patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True), patch("sys.stderr.isatty", return_value=True):
            output = self.call("create", "assistant", "--account", "fixture", structured=False)
        self.assertIn("Buzz identity: " + "cd" * 32, output)
        applied = next(r for op, r in self.requests if op == "apply")
        self.assertEqual(applied["prompt"], "SYNTHETIC-PRIVATE-PROMPT")
        self.assertTrue(applied["config"]["harness"]["command"].startswith("/home/fixture/"))
        self.assertFalse(answers)
        self.acquire.assert_not_called()

    def test_guided_account_remap_before_commit_refuses_without_subprocess(self):
        answers = iter(["", "", "", "ab" * 32, "wss://relay.example.test", "prompt", "yes"])
        def answer(_):
            value = next(answers)
            if value == "yes":
                self.entry.pw_uid += 1
            return value
        with patch("builtins.input", side_effect=answer), patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True), patch("sys.stderr.isatty", return_value=True):
            self.call("create", "assistant", "--account", "fixture", structured=False, success=False)
        self.run.assert_not_called()
        self.assertFalse(self.bindings.path("assistant").exists())

    def test_first_operator_lifecycle_and_retention_from_installed_cli(self):
        archive = self.store.root.parent / "app.pyz"
        prefix = self.store.root.parent / "prefix"
        release.build(Path(__file__).resolve().parents[1], archive, "1.0.0")
        release.install(archive, prefix, self.store.root, self.units)
        with patch("buzz_server.release.location", return_value=prefix), patch("buzz_server.release.before_command"):
            created = self.create()
            self.assertEqual(self.call("list")["agents"][0]["identity"], created["identity"])
            started = self.call("start", "assistant")
            self.assertEqual(started["service_state"], "active")
            self.assertIs(started["runtime_started"], False)
            self.assertIs(started["buzz_response_verified"], False)
            self.assertEqual(self.call("status", "assistant")["identity"], created["identity"])
            with self.assertRaises(Error) as failure:
                release.uninstall(prefix)
            self.assertEqual(failure.exception.code, "dedicated_attached")
            self.assertTrue(self.model.active)
            self.assertTrue((prefix / "bin/buzz-server").exists())
            configured = self.call("configure", "assistant", "--config", str(self.config))
            self.assertEqual(configured["identity"], created["identity"])
            self.assertEqual(configured["revision"], 2)
            self.call("stop", "assistant")
            with patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True), patch("sys.stderr.isatty", return_value=True):
                self.call("inference", "configure", "assistant", structured=False)
            self.call("delete", "assistant")
            self.assertEqual(self.private_state, b"SYNTHETIC-PRIVATE-STATE")
            self.assertTrue(self.bindings.path("assistant").exists())
        release.uninstall(prefix)
        self.assertFalse((prefix / "bin/buzz-server").exists())

    def test_incomplete_and_corrupt_bindings_block_uninstall_before_services(self):
        from buzz_server.account_lifecycle import require_detached
        self.bindings.reserve("assistant", "fixture")
        for doc in (None, {"version": True}, {"phase": "deleted"}):
            if doc is not None:
                write_json(manifest_path(self.bindings, "assistant"), doc)
            with self.store.lock(), self.assertRaises(Error) as failure:
                require_detached(self.store)
            self.assertEqual(failure.exception.code, "dedicated_attached")
        self.run.assert_not_called()

    def test_target_report_cannot_select_privileged_operation_or_fake_ack(self):
        self.create()
        self.model.effects.clear()
        original = self.system_run
        for response in ({"applied": 1}, {"applied": True, "unit": "sshd.service", "operation": "start"}):
            def forged(argv, **kwargs):
                result = original(argv, **kwargs)
                if argv[0] == "/usr/bin/sudo" and argv[1].startswith("--user="):
                    result.stdout = json.dumps(response).encode()
                return result
            self.run.side_effect = forged
            self.assertEqual(self.call("start", "assistant", success=False)["code"], "target_report")
            self.assertEqual(self.model.effects, [])
            self.assertFalse(self.model.active)

    def test_dedicated_doctor_needs_no_operator_user_manager_or_sudo(self):
        from types import SimpleNamespace
        with patch("buzz_server.account_process.run", return_value=SimpleNamespace(returncode=0, stdout=b"fixture-systemd\n")) as query, patch("buzz_server.cli.Systemd.check", side_effect=AssertionError("user manager must not be checked")):
            result = self.call("doctor", "--account", "fixture")
        self.assertEqual(query.call_args.args[0], ["/usr/bin/systemctl", "--system", "show", "--property=Version", "--value"])
        self.assertIs(result["buzz_response_verified"], False)
        self.run.assert_not_called()

    def test_uninstall_checks_dedicated_inventory_under_both_exclusive_locks(self):
        from buzz_server.account_lifecycle import require_detached
        archive = self.store.root.parent / "app.pyz"
        prefix = self.store.root.parent / "prefix"
        release.build(Path(__file__).resolve().parents[1], archive, "1.0.0")
        release.install(archive, prefix, self.store.root, self.units)
        self.bindings.reserve("assistant", "fixture")
        def check(store):
            for path, kind in ((prefix / ".release.lock", fcntl.LOCK_SH), (store.root / ".lock", fcntl.LOCK_EX)):
                with path.open("rb") as probe:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(probe.fileno(), kind | fcntl.LOCK_NB)
            return require_detached(store)
        with patch("buzz_server.account_lifecycle.require_detached", side_effect=check) as checked:
            with self.assertRaises(Error) as failure:
                release.uninstall(prefix)
            self.assertEqual(failure.exception.code, "dedicated_attached")
            checked.assert_called_once()
        self.assertTrue((prefix / "bin/buzz-server").exists())
        self.run.assert_not_called()

    def test_corrupt_private_tool_is_not_redownloaded_or_sent_to_target(self):
        atomic(self.store.root / "tools/bin/goose", b"wrong size")
        (self.store.root / "tools/bin/goose").chmod(0o700)
        self.call("create", "assistant", "--account", "fixture", "--config", str(self.config), success=False)
        self.acquire.assert_not_called()
        self.run.assert_not_called()

    def test_missing_tool_acquisition_keeps_json_stdout_stderr_contract(self):
        (self.store.root / "tools/bin/goose").unlink()
        def acquire(package, directory):
            for name, _ in package["members"].values():
                (directory / name).write_bytes(name.encode())
        self.acquire.side_effect = acquire
        created = self.create()
        self.assertEqual(created["tools_acquired"], ["goose"])
        self.acquire.assert_called_once()

    def test_guided_collision_at_commit_refuses_without_target_or_root_effect(self):
        answers = iter(["", "", "", "ab" * 32, "wss://relay.example.test", "prompt", "yes"])
        def answer(_):
            value = next(answers)
            if value == "yes":
                self.bindings.reserve("assistant", "fixture")
                write_json(manifest_path(self.bindings, "assistant"), {
                    "version": 1, "id": "assistant", "phase": "ready", "identity": "ef" * 32,
                    "revision": 1, "desired": "stopped"})
            return value
        with patch("builtins.input", side_effect=answer), patch("sys.stdin.isatty", return_value=True):
            self.call("create", "assistant", "--account", "fixture", structured=False, success=False)
        self.run.assert_not_called()
        self.assertEqual(read_json(manifest_path(self.bindings, "assistant"))["identity"], "ef" * 32)

    def test_interrupted_create_retries_owned_stage_without_redownload(self):
        original = self.system_run
        def fail_publish(argv, **kwargs):
            if argv[:3] == ["/usr/bin/sudo", "--", "/usr/bin/ln"]:
                raise OSError("synthetic interrupted publication")
            return original(argv, **kwargs)
        self.run.side_effect = fail_publish
        self.call("create", "assistant", "--account", "fixture", "--config", str(self.config), success=False)
        self.assertIn(self.model.stage_path, self.model.files)
        self.assertNotIn(self.model.unit_path, self.model.files)
        self.run.side_effect = original
        created = self.create()
        self.assertEqual(created["identity"], "cd" * 32)
        self.assertEqual(set(self.model.files), {self.model.unit_path})
        self.acquire.assert_not_called()


if __name__ == "__main__":
    unittest.main()
