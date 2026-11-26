"""Operator CLI routing with synthetic NSS, tools, target and system manager."""
import ast
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import stat
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from buzz_server import cli, dependencies, release
from buzz_server.account_commands import Commands
from buzz_server.account_supervisor import AccountSystemd
from buzz_server.account_lifecycle import manifest_path
from buzz_server.errors import Error
from buzz_server.storage import atomic, read_json, write_json
import test_accounts
from test_account_supervisor import FakeCommands


OPERATOR_NPUB = "npub14w46h2at4w46h2at4w46h2at4w46h2at4w46h2at4w46h2at4w4scf6zts"


class OperatorPublicKeyTests(unittest.TestCase):
    def test_guided_key_formats(self):
        self.assertEqual(cli.operator_public_key(OPERATOR_NPUB.upper()), "ab" * 32)
        for value in (OPERATOR_NPUB[:-1], OPERATOR_NPUB.upper().replace("N", "n", 1),
                      OPERATOR_NPUB.upper().replace("S", "ſ", 1), "AB" * 32, "g0" * 32, "ab" * 31):
            with self.subTest(value=value):
                self.assertIsNone(cli.operator_public_key(value))


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
        self.startup_status = b""
        self.private_state = b"SYNTHETIC-PRIVATE-STATE"
        self.requests = []
        self.installs = []
        self.deployment_files = {}
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
            if path not in self.model.files:
                if missing:
                    return False
                raise Error("unit_conflict", "Synthetic missing unit")
            if exact and self.model.files[path] != supervisor.content():
                raise Error("unit_conflict", "Synthetic wrong unit")
            return "notify" if exact else True

        patch("buzz_server.account_supervisor.AccountSystemd.root_file", root_file).start()
        patch.object(AccountSystemd, "replacement_installed", lambda unit: self.model.files[unit.commands.unit_path] == unit.content()).start()
        patch.object(Commands, "stage_deployment", self.stage_deployment).start()
        patch.object(Commands, "deployment_file", self.verify_deployment_file).start()

    def stage_deployment(self, slot, files, *, prompt=False):
        base = Path("/var/lib/buzz-server") / self.model.binding.service
        directory = base / (f"prompt-{slot}" if prompt else f"worker-{slot}")
        for name, path in files.items():
            self.deployment_files[directory / name] = path.read_bytes()

    def verify_deployment_file(self, path, *_):
        self.assertIn(path, self.deployment_files)

    def system_run(self, argv, **kwargs):
        from types import SimpleNamespace
        output = b""
        if argv == ["/usr/bin/sudo", "--validate"]:
            self.assertIsNone(kwargs["timeout"])
            self.assertFalse(kwargs["capture_output"])
            self.assertIsNone(kwargs["input"])
            return SimpleNamespace(returncode=0, stdout=b"")
        if argv[0] == "/usr/bin/sudo":
            self.assertEqual(argv[1], "--non-interactive")
            argv = [argv[0], *argv[2:]]
        if argv[:2] == ["/usr/bin/systemctl", "--system"]:
            if "--property=StatusText" in argv:
                output = self.startup_status
            else:
                output = self.model.show(AccountSystemd.PROPERTIES) if self.model else b"LoadState=not-found\nDropInPaths=\nFragmentPath=\nActiveState=inactive\nMainPID=0\nControlGroup=\n"
        elif argv[:3] == ["/usr/bin/sudo", "--", "/usr/bin/systemctl"]:
            self.assertEqual(argv[3], "--system")
            if argv[4] == "daemon-reload":
                self.assertEqual(len(argv), 5)
                self.model.reload()
            else:
                if argv[4] == "--no-block":
                    self.assertEqual(argv[5], "start")
                    argv.pop(4)
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
        elif argv[:3] == ["/usr/bin/sudo", "--", "/usr/bin/cmp"]:
            self.assertEqual(argv[3:6], ["--silent", "--", "-"])
            self.assertEqual(len(argv), 7)
            matches = self.deployment_files[Path(argv[-1])] == kwargs["input"]
            return SimpleNamespace(returncode=0 if matches else 1, stdout=b"", stderr=b"")
        elif argv[:3] == ["/usr/bin/sudo", "--", "/usr/bin/mv"]:
            self.assertEqual(argv[3:], ["-T", "--", str(self.model.stage_path), str(self.model.unit_path)])
            self.model.replace_unit()
        elif argv[:3] == ["/usr/bin/sudo", "--", "/usr/bin/sync"]:
            self.assertEqual(argv[3:], ["-f", "--", str(self.model.unit_path.parent)])
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

    def upgrade_fixture(self):
        packages = ({"name": "buzz", "version": "desktop-v1.2.3", "sha256": "ab" * 32},
                    {"name": "goose", "version": "v4.5.6", "sha256": "cd" * 32})

        def stage(directory, **kwargs):
            for name in dependencies.NAMES:
                (directory / name).write_bytes(name.encode())

        return (patch.object(dependencies, "PACKAGES", packages), patch.object(dependencies, "stage_pins", side_effect=stage))

    def test_named_worker_upgrade_reports_selection_and_rolls_back_after_failed_startup(self):
        created = self.create()
        self.call("start", "assistant")
        before_revision = self.target_doc["revision"]
        packages, staging = self.upgrade_fixture()
        original_control = self.model.control

        def control(operation):
            original_control(operation)
            if operation == "start":
                self.model.overrides.update(ActiveState="failed", MainPID="0", ExecMainCode="1", ExecMainStatus="76")

        with packages, staging as fetch, patch.object(self.model, "control", side_effect=control):
            failed = self.call("worker", "upgrade", "assistant", success=False)
            fetch.assert_called_once()
        self.assertEqual(failed["code"], "local_runtime")
        self.assertIn("worker rollback assistant", failed["message"])
        status = self.call("status", "assistant")
        self.assertEqual(status["deployment"]["selected_worker"]["tools"]["goose"]["version"], "v4.5.6")
        self.assertTrue(status["deployment"]["worker_rollback_available"])
        self.assertIsNone(status["deployment"]["running_worker"])
        self.model.overrides.clear()
        rolled_back = self.call("worker", "rollback", "assistant")
        self.assertEqual(rolled_back["identity"], created["identity"])
        self.assertEqual(rolled_back["revision"], before_revision)
        self.assertEqual(self.target_doc["desired"], "running")
        self.assertTrue(rolled_back["runtime_started"])
        self.assertIsNone(rolled_back["deployment"]["selected_worker"])
        self.assertEqual(rolled_back["deployment"]["previous_worker"], status["deployment"]["selected_worker"])
        self.assertEqual(len(self.installs), 1)
        self.assertEqual(self.private_state, b"SYNTHETIC-PRIVATE-STATE")

    def test_prompt_revisions_are_snapshots_and_survive_configure_and_rollback(self):
        created = self.create()
        self.assertEqual(self.call("prompt", "activate", "assistant", "--file", str(self.prompt), success=False)["code"],
                         "worker_upgrade_required")
        packages, staging = self.upgrade_fixture()
        with packages, staging as fetch:
            selected = self.call("worker", "upgrade", "assistant")
            self.assertEqual(self.call("worker", "upgrade", "assistant"), selected)
            fetch.assert_called_once()
        self.assertFalse(self.model.enabled)
        self.assertIsNone(selected["deployment"]["running_worker"])
        initial = b"SYNTHETIC-PRIVATE-PROMPT"
        first = self.call("prompt", "activate", "assistant", "--file", str(self.prompt))
        unit = self.model.files[self.model.unit_path]
        credential = Path(next(line.split(":", 1)[1] for line in unit.decode().splitlines() if line.startswith("LoadCredential=")))
        self.assertEqual(self.deployment_files[credential], initial)
        self.assertNotIn(initial, unit)
        self.assertNotIn(str(self.prompt).encode(), unit)
        atomic(self.prompt, b"SYNTHETIC-PRIVATE-NEXT-PROMPT")
        started = self.call("start", "assistant")
        self.assertEqual(started["deployment"]["running_worker"], selected["deployment"]["selected_worker"])
        self.assertEqual(self.deployment_files[credential], initial)
        second = self.call("prompt", "activate", "assistant", "--file", str(self.prompt))
        self.assertEqual(second["deployment"]["previous_prompt_revision"], first["deployment"]["prompt_revision"])
        self.assertTrue(second["runtime_started"])
        self.assertNotEqual(self.model.files[self.model.unit_path], unit)
        configured = self.call("configure", "assistant", "--config", str(self.config))
        self.assertEqual(configured["identity"], created["identity"])
        self.assertEqual(configured["deployment"]["prompt_revision"], second["deployment"]["prompt_revision"])
        rolled_back = self.call("prompt", "rollback", "assistant")
        self.assertEqual(rolled_back["deployment"]["prompt_revision"], first["deployment"]["prompt_revision"])
        self.assertEqual(self.model.files[self.model.unit_path], unit)
        self.assertEqual(self.deployment_files[credential], initial)
        self.assertEqual(self.call("worker", "rollback", "assistant", success=False)["code"], "prompt_active")
        self.call("prompt", "clear", "assistant")
        self.call("worker", "rollback", "assistant")
        self.call("delete", "assistant")
        self.assertEqual(self.call("list")["agents"], [])

    def test_interrupted_switch_retries_without_fetching_and_requires_stopped_manager(self):
        self.create()
        self.call("start", "assistant")
        packages, staging = self.upgrade_fixture()
        with packages, staging as fetch:
            self.model.fail = "reload"
            self.call("worker", "upgrade", "assistant", success=False)
            self.assertEqual(self.call("status", "assistant", success=False)["code"], "deployment_incomplete")
            self.model.fail = None
            self.model.overrides["ControlGroup"] = "/synthetic"
            with patch("buzz_server.account_supervisor.cgroup_empty", return_value=False):
                self.assertEqual(self.call("worker", "upgrade", "assistant", success=False)["code"], "stop_unverified")
            self.model.overrides.clear()
            finished = self.call("worker", "upgrade", "assistant")
            fetch.assert_called_once()
        self.assertTrue(finished["runtime_started"])
        self.assertEqual(self.model.effects.count("replace"), 1)

    def test_interrupted_prompt_activation_requires_matching_bytes(self):
        self.create()
        packages, staging = self.upgrade_fixture()
        with packages, staging:
            self.call("worker", "upgrade", "assistant")
        self.call("start", "assistant")
        original = self.prompt.read_bytes()
        command = ("prompt", "activate", "assistant", "--file", str(self.prompt))
        self.model.fail = "reload"
        self.call(*command, success=False)
        self.model.fail = None
        effects = self.model.effects[:]
        atomic(self.prompt, b"SYNTHETIC-PRIVATE-EDITED")
        error = self.call(*command, success=False)
        self.assertEqual(error["code"], "deployment_incomplete")
        self.assertIn("stop assistant", error["message"])
        self.assertEqual(self.model.effects, effects)
        self.assertFalse(self.model.active)
        atomic(self.prompt, original)
        finished = self.call(*command)
        self.assertTrue(finished["runtime_started"])
        self.assertIsNotNone(finished["deployment"]["prompt_revision"])
        self.assertEqual(self.model.effects.count("replace"), 2)

    def test_interrupted_worker_upgrade_refuses_different_cli_payload(self):
        self.create()
        packages, staging = self.upgrade_fixture()
        with packages, staging as fetch:
            self.model.fail = "replace"
            self.call("worker", "upgrade", "assistant", success=False)
            self.model.fail = None
            effects = self.model.effects[:]
            with patch("buzz_server.account_deployment.__version__", "2.0.0"):
                error = self.call("worker", "upgrade", "assistant", success=False)
            self.assertEqual(error["code"], "deployment_incomplete")
            self.assertEqual(self.model.effects, effects)
            self.call("worker", "upgrade", "assistant")
            fetch.assert_called_once()

    def test_explicit_stop_cancels_an_unpublished_switch(self):
        self.create()
        self.call("start", "assistant")
        unit = self.model.files[self.model.unit_path]
        packages, staging = self.upgrade_fixture()
        with packages, staging:
            self.model.fail = "replace"
            self.call("worker", "upgrade", "assistant", success=False)
        self.model.fail = None
        stopped = self.call("stop", "assistant")
        self.assertFalse(self.model.active)
        self.assertFalse(self.model.enabled)
        self.assertEqual(self.model.files, {self.model.unit_path: unit})
        self.assertIsNone(stopped["deployment"]["selected_worker"])

    def test_json_on_terminal_never_validates_and_sudo_failure_stays_structured(self):
        with patch("sys.stdin.isatty", return_value=True), \
             patch("sys.stdout.isatty", return_value=True), \
             patch("sys.stderr.isatty", return_value=True):
            self.create()
            self.assertNotIn(["/usr/bin/sudo", "--validate"],
                             [call.args[0] for call in self.run.call_args_list])
            self.run.reset_mock()
            original = self.system_run
            def denied(argv, **kwargs):
                if argv[0] == "/usr/bin/sudo":
                    self.assertEqual(argv[1], "--non-interactive")
                    return SimpleNamespace(returncode=1, stdout=b"")
                return original(argv, **kwargs)
            self.run.side_effect = denied
            error = self.call("start", "assistant", success=False)
        self.assertEqual(error["code"], "account_command_failed")
        self.assertFalse(self.model.active)

    def test_inference_checks_stopped_state_after_authentication(self):
        self.create()
        self.requests.clear()
        self.run.reset_mock()

        def started_while_waiting(argv, **kwargs):
            if argv == ["/usr/bin/sudo", "--validate"]:
                self.model.active = True
            return self.system_run(argv, **kwargs)

        self.run.side_effect = started_while_waiting
        with patch("sys.stdin.isatty", return_value=True):
            self.call("inference", "configure", "assistant", structured=False, success=False)
        self.assertEqual(self.run.call_args_list[0].args[0], ["/usr/bin/sudo", "--validate"])
        self.assertFalse(self.requests)

    def test_guided_dedicated_create_collects_without_mutation_lock_and_paths(self):
        answers = ["", "", "", "nsec1secret", OPERATOR_NPUB[:-1] + "q", OPERATOR_NPUB, "wss://relay.example.test", "SYNTHETIC-PRIVATE-PROMPT", "yes"]
        prompts = []
        def answer(prompt):
            self.assertFalse((self.store.root / ".lock").exists())
            self.assertFalse(self.bindings.path("assistant").exists())
            prompts.append(prompt)
            return answers.pop(0)
        with patch("builtins.input", side_effect=answer), patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True), patch("sys.stderr.isatty", return_value=True):
            output = self.call("create", "assistant", "--account", "fixture", structured=False)
        self.assertIn("Buzz identity: " + "cd" * 32, output)
        self.assertIn("created and left stopped", output)
        self.assertIn("inference configure assistant", output)
        self.assertEqual(prompts[3:6], [prompts[3]] * 3)
        applied = next(r for op, r in self.requests if op == "apply")
        self.assertEqual(applied["config"]["owner"], cli.operator_public_key("ab" * 32))
        self.assertEqual(applied["prompt"], "SYNTHETIC-PRIVATE-PROMPT")
        self.assertTrue(applied["config"]["harness"]["command"].startswith("/home/fixture/"))
        self.assertFalse(answers)
        self.acquire.assert_not_called()

    def test_cancel_at_operator_key_leaves_no_agent_state(self):
        with patch("builtins.input", side_effect=["", "", "", "wrong", EOFError]), patch("sys.stdin.isatty", return_value=True):
            self.call("create", "assistant", "--account", "fixture", structured=False, success=False)
        self.assertFalse(self.bindings.path("assistant").exists())
        self.run.assert_not_called()

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
            self.assertIs(started["runtime_started"], True)
            self.assertEqual(started["startup"]["profile"], "confirmed")
            self.assertIs(started["buzz_response_verified"], False)
            self.assertIn("Buzz reply untested", self.call("start", "assistant", structured=False))
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
                result = self.call("inference", "configure", "assistant", structured=False)
                self.assertIn("A clean exit can also mean setup was cancelled", result)
            self.call("delete", "assistant")
            self.assertEqual(self.private_state, b"SYNTHETIC-PRIVATE-STATE")
            self.assertTrue(self.bindings.path("assistant").exists())
        release.uninstall(prefix)
        self.assertFalse((prefix / "bin/buzz-server").exists())

    def test_dedicated_start_refusal_uses_only_safe_stage(self):
        self.create()
        self.model.overrides.update(ActiveState="failed", SubState="failed", MainPID="0",
                                    Result="exit-code", ExecMainCode="1", ExecMainStatus="79")
        self.startup_status = b"rejected: SYNTHETIC-PRIVATE-RESPONSE\n"
        error = self.call("start", "assistant", success=False)
        self.assertEqual(error["code"], "profile_refused")
        self.assertIn("Automatic retries ended", error["message"])
        status = self.call("status", "assistant", structured=False)
        self.assertIn("Profile publication unconfirmed", status)
        self.assertIn("Local runtime readiness unobserved", status)
        self.assertNotIn("SYNTHETIC-PRIVATE-RESPONSE", status)

    def test_dedicated_status_separates_profile_from_pending_runtime(self):
        self.create()
        self.assertIn("Profile publication unobserved", self.call("status", "assistant", structured=False))
        self.model.overrides.update(ActiveState="activating", SubState="start", MainPID="23")
        self.startup_status = b"buzz-server:local_runtime\n"
        pending = self.call("status", "assistant")
        self.assertEqual(pending["startup"], {"profile": "confirmed", "local_runtime": "unobserved",
                                              "failure_stage": None, "retry": "waiting"})
        self.assertFalse(pending["runtime_started"])

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
                if argv[0] == "/usr/bin/sudo" and argv[1:2] == ["--non-interactive"] and argv[2].startswith("--user="):
                    result.stdout = json.dumps(response).encode()
                return result
            self.run.side_effect = forged
            self.assertEqual(self.call("start", "assistant", success=False)["code"], "target_report")
            self.assertEqual(self.model.effects, [])
            self.assertFalse(self.model.active)

    def test_dedicated_doctor_needs_no_operator_user_manager_or_sudo(self):
        out, err = io.StringIO(), io.StringIO()

        def system_version(*args, **kwargs):
            if terminal_run:
                self.assertIn("Checking the dedicated account layout", err.getvalue())
                self.assertIn("Checking the systemd system manager", err.getvalue())
            return SimpleNamespace(returncode=0, stdout=b"fixture-systemd\n")

        terminal_run = False
        with (patch("buzz_server.account_process.run", side_effect=system_version) as query,
              patch("buzz_server.cli.Systemd.check", side_effect=AssertionError("user manager must not be checked")),
              patch("buzz_server.release.before_command") as update_offer,
              patch("buzz_server.dependencies.interactive", return_value=True)):
            result = self.call("doctor", "--account", "fixture")
            terminal_run = True
            argv = ["--state-dir", str(self.store.root), "--unit-dir", str(self.units), "doctor", "--account", "fixture"]
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(argv), 0)
        plain = out.getvalue()
        update_offer.assert_not_called()
        self.assertIn("Dedicated-account preflight passed for fixture", plain)
        self.assertIn("Account layout and system manager reachability verified", plain)
        self.assertIn("Runtime tools were not checked", plain)
        self.assertIn("relay access and Buzz replies were not checked", plain)
        self.assertIn("buzz-server create AGENT --account fixture", plain)
        self.assertEqual(query.call_args.args[0], ["/usr/bin/systemctl", "--system", "show", "--property=Version", "--value"])
        self.assertIs(result["buzz_response_verified"], False)
        self.run.assert_not_called()

    def test_dedicated_doctor_plain_failure_preserves_guidance(self):
        out, err = io.StringIO(), io.StringIO()
        argv = ["--state-dir", str(self.store.root), "--unit-dir", str(self.units), "doctor", "--account", "fixture"]
        with (contextlib.redirect_stdout(out), contextlib.redirect_stderr(err),
              patch("buzz_server.account_process.run", return_value=SimpleNamespace(returncode=1, stdout=b""))):
            self.assertEqual(cli.main(argv), 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("Dedicated-account preflight failed [supervisor_unavailable]", err.getvalue())
        self.assertIn("inspect systemctl --system locally", err.getvalue())
        self.assertIn("relay access and Buzz replies were not checked", err.getvalue())

    def test_dedicated_doctor_explains_home_prerequisite_without_repair(self):
        original = Path.lstat
        for path, mode, uid, expected in (
            (self.entry.pw_dir, stat.S_IFDIR | 0o750, self.entry.pw_uid, "has mode 0750; required mode is 0700"),
            (self.entry.pw_dir, stat.S_IFDIR | 0o700, 0, "must be owned by fixture"),
            (self.entry.pw_dir, stat.S_IFLNK | 0o777, self.entry.pw_uid, "real directory"),
            ("/home", stat.S_IFDIR | 0o775, 0, "not writable by group or others"),
        ):
            def layout(candidate):
                if candidate == Path(path):
                    return SimpleNamespace(st_mode=mode, st_uid=uid)
                return original(candidate)

            with self.subTest(path=path, mode=mode, uid=uid), patch("buzz_server.accounts.Path.lstat", layout):
                result = self.call("doctor", "--account", "fixture", success=False)
                assert isinstance(result, dict)
                self.assertEqual(result["code"], "account_binding")
                self.assertIn(expected, result["message"])
                self.assertIn("administrator", result["message"])
                self.assertIn(path, result["message"])
                if stat.S_IMODE(mode) == 0o750:
                    self.assertIn("chmod 0700 -- /home/fixture", result["message"])
                else:
                    self.assertNotIn("chmod", result["message"])
                self.assertNotIn("binding", result["message"])
                self.assertFalse(self.bindings.path("assistant").exists())
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
            if argv[:4] == ["/usr/bin/sudo", "--non-interactive", "--", "/usr/bin/ln"]:
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
