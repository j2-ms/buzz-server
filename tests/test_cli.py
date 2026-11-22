import argparse
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
from unittest.mock import Mock, patch

from buzz_server import cli, config
from buzz_server.errors import Error
from buzz_server.identity import generate, public
from buzz_server.runtime import event, prepare, profile_accepted, worker
from buzz_server.storage import Store, atomic, private_dir, read_private
from buzz_server.supervisor import PreexistingUnit, Systemd, quote


class FakeSystemd:
    """Unit-test double only. Never used by disposable acceptance."""
    active = False
    enabled = False
    fail_stop = False
    fail_remove = False
    name = staticmethod(Systemd.name)

    def __init__(self, store, unit_dir):
        self.store = store

    def check(self):
        pass

    def install(self, ident, new=False):
        pass

    def inspect(self, ident):
        return {"ActiveState": "active" if self.active else "inactive", "MainPID": "10" if self.active else "0", "Result": "success"}

    def stop(self, ident):
        if self.fail_stop:
            raise Error("stop_unverified", "Synthetic stop failed; definition retained.")
        type(self).active = False
        type(self).enabled = False

    def start(self, ident, ready):
        type(self).active = True
        type(self).enabled = True
        from buzz_server.storage import write_json
        d = self.store.load(ident)
        write_json(self.store.directory(ident) / "ready.json", {"worker_pid": 10, "child_pid": 11, "revision": d["revision"]})

    def remove(self, ident):
        if self.fail_remove:
            raise Error("delete_unverified", "Synthetic removal failed; definition retained.")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="buzz-unit-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "state"
        self.units = self.base / "units"
        self.prompt = self.base / "prompt"
        atomic(self.prompt, b"SYNTHETIC-PROMPT-DO-NOT-RETURN")
        self.c = {"relay_url": "ws://127.0.0.1:19000", "owner": "ab" * 32,
                  "profile": {"name": "Synthetic agent", "about": "Test", "avatar": ""},
                  "prompt_file": str(self.prompt), "harness": {"kind": "goose", "command": "/usr/bin/true"}, "provider": {"kind": "goose-managed"}}
        self.config_path = self.base / "config.json"
        self.write_config(self.c)
        FakeSystemd.active = FakeSystemd.enabled = FakeSystemd.fail_stop = FakeSystemd.fail_remove = False
        self.supervisor_patch = patch("buzz_server.cli.Systemd", FakeSystemd)
        self.supervisor_patch.start()
        self.addCleanup(self.supervisor_patch.stop)
        self.prepare_patch = patch("buzz_server.cli.prepare")
        self.prepare_patch.start()
        self.addCleanup(self.prepare_patch.stop)

    def write_config(self, c):
        atomic(self.config_path, json.dumps(c).encode())

    def call(self, *argv, success=True):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--json", "--state-dir", str(self.root), "--unit-dir", str(self.units), *argv])
        self.assertEqual(code, 0 if success else 1, err.getvalue())
        combined = out.getvalue() + err.getvalue()
        self.assertNotIn("SYNTHETIC-SECRET-DO-NOT-RETURN", combined)
        self.assertNotIn("SYNTHETIC-PROMPT-DO-NOT-RETURN", combined)
        response = json.loads(out.getvalue() if success else err.getvalue())
        self.assertEqual(response["schema_version"], 1)
        return response["data"] if success else response["error"]

    def create(self):
        return self.call("create", "agent", "--config", str(self.config_path))

    def guided(self, answers):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
             patch("sys.stdin.isatty", return_value=True), \
             patch.object(out, "isatty", return_value=True), patch.object(err, "isatty", return_value=True), \
             patch("builtins.input", side_effect=answers), patch("buzz_server.runtime.tool", return_value="/usr/bin/true"):
            code = cli.main(["--state-dir", str(self.root), "--unit-dir", str(self.units), "create", "agent"])
        return code, out.getvalue(), err.getvalue()

    def test_guided_defaults_and_private_files(self):
        old = os.umask(0)
        try:
            code, out, err = self.guided(["", "", "", "ab" * 32, "wss://relay.example", "", "yes"])
        finally:
            os.umask(old)
        self.assertEqual(code, 0, err)
        doc = Store(self.root).load("agent")
        self.assertEqual(doc["config"]["profile"], {"name": "agent", "about": "", "avatar": ""})
        prompt = Path(doc["config"]["prompt_file"])
        self.assertEqual(read_private(prompt), b"You are a helpful assistant.")
        self.assertEqual(prompt.stat().st_mode & 0o777, 0o600)
        self.assertEqual(prompt.parent.stat().st_mode & 0o777, 0o700)
        self.assertNotIn("You are a helpful assistant.", out)

    def test_guided_custom_choices_and_unit_conflict(self):
        answers = ["Custom", "Description", "https://example.org/avatar", "ab" * 32,
                   "wss://relay.example", "Synthetic custom instruction", "yes"]
        with patch.object(FakeSystemd, "install", side_effect=PreexistingUnit("unit_conflict", "conflict")):
            code, _, _ = self.guided(answers)
        self.assertEqual(code, 1)
        self.assertFalse((self.root / "agents/agent").exists())
        code, out, err = self.guided(answers)
        self.assertEqual(code, 0, err)
        doc = Store(self.root).load("agent")
        self.assertEqual(doc["config"]["profile"]["name"], "Custom")
        self.assertEqual(read_private(doc["config"]["prompt_file"]), b"Synthetic custom instruction")
        self.assertNotIn("Synthetic custom instruction", out)

    def test_guided_invalid_and_discard_leave_no_agent(self):
        for answers in (["", "", "", "invalid", "wss://relay.example", "", "yes"],
                        ["", "", "", "ab" * 32, "wss://relay.example", "", "no"],
                        ["", "", EOFError()], ["", "", KeyboardInterrupt()]):
            code, _, _ = self.guided(answers)
            self.assertEqual(code, 1)
            self.assertFalse((self.root / "agents/agent").exists())

    def test_guided_nonterminal_and_json_refused(self):
        self.assertEqual(self.call("create", "agent", success=False)["code"], "interactive_required")
        self.assertFalse(self.root.exists())
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["--state-dir", str(self.root), "create", "agent"]), 1)
        self.assertIn("interactive_required", err.getvalue())
        self.assertFalse(self.root.exists())

    def test_guided_prepare_failure_removes_agent(self):
        with patch("buzz_server.cli.prepare", side_effect=Error("synthetic", "failed")):
            code, _, _ = self.guided(["", "", "", "ab" * 32, "wss://relay.example", "", "yes"])
        self.assertEqual(code, 1)
        self.assertFalse((self.root / "agents/agent").exists())

    def test_guided_prompts_do_not_hold_lifecycle_lock(self):
        def answers():
            for value in ["", "", "", "ab" * 32, "wss://relay.example", "", "yes"]:
                self.assertFalse((self.root / ".lock").exists())
                yield value

        code, _, err = self.guided(answers())
        self.assertEqual(code, 0, err)

    def test_persistent_identity_configuration_and_intent(self):
        first = self.create()
        self.assertFalse(first["runtime_started"])
        self.call("start", "agent")
        self.c["profile"]["name"] = "Edited"
        self.write_config(self.c)
        configured = self.call("configure", "agent", "--config", str(self.config_path))
        self.assertEqual(configured["identity"], first["identity"])
        self.assertEqual(configured["revision"], 2)
        self.assertTrue(configured["runtime_started"])
        self.call("restart", "agent")
        self.call("stop", "agent")
        self.c["profile"]["about"] = "Stopped edit"
        self.write_config(self.c)
        stopped = self.call("configure", "agent", "--config", str(self.config_path))
        self.assertEqual(stopped["desired"], "stopped")
        self.assertFalse(FakeSystemd.active)
        self.assertFalse(FakeSystemd.enabled)
        disk = Store(self.root).load("agent")
        self.assertEqual(disk["config"]["profile"]["about"], "Stopped edit")
        self.assertEqual(disk["identity"], first["identity"])
        self.assertEqual(len(self.call("list")["agents"]), 1)
        deleted = self.call("delete", "agent")
        self.assertTrue(deleted["data_retained"])
        self.assertTrue((self.root / "agents/agent/identity.key").exists())
        self.assertEqual(self.call("list")["agents"], [])
        self.call("create", "agent", "--config", str(self.config_path), success=False)

    def test_failed_stop_and_delete_preserve_definition_and_data(self):
        self.create()
        self.call("start", "agent")
        FakeSystemd.fail_stop = True
        self.call("stop", "agent", success=False)
        self.call("delete", "agent", success=False)
        self.assertTrue(FakeSystemd.active)
        self.assertEqual(Store(self.root).load("agent")["desired"], "stopped")
        self.assertTrue((self.root / "agents/agent/identity.key").exists())
        FakeSystemd.fail_stop = False
        FakeSystemd.fail_remove = True
        self.call("delete", "agent", success=False)
        self.assertEqual(Store(self.root).load("agent")["id"], "agent")

    def test_failed_configure_stop_keeps_old_configuration(self):
        self.create()
        self.call("start", "agent")
        old = Store(self.root).load("agent")["config"]
        self.c["profile"]["name"] = "New"
        self.write_config(self.c)
        FakeSystemd.fail_stop = True
        self.call("configure", "agent", "--config", str(self.config_path), success=False)
        doc = Store(self.root).load("agent")
        self.assertEqual(doc["config"], old)
        self.assertEqual(doc["revision"], 1)

    def test_corrupt_state_no_partial_listing(self):
        self.create()
        atomic(self.root / "agents/agent/definition.json", b'{"secret":"SYNTHETIC-SECRET-DO-NOT-RETURN"')
        self.assertEqual(self.call("list", success=False)["code"], "unsafe_state")
        self.call("start", "agent", success=False)
        self.call("delete", "agent", success=False)

    def test_obsolete_stored_configuration_can_be_stopped_replaced_and_deleted(self):
        identity = self.create()["identity"]
        self.call("start", "agent")
        store = Store(self.root)
        old = store.load("agent")
        old["config"]["provider"] = {"kind": "anthropic", "model": "synthetic", "env_file": "/unused"}
        store.save(old)
        with patch("buzz_server.runtime.subprocess.Popen") as launch, patch("buzz_server.runtime.signal.signal"):
            self.assertEqual(worker(store, "agent"), 0)
            launch.assert_not_called()
        self.assertEqual(self.call("list")["agents"][0]["configuration"]["provider"], "obsolete")
        for operation in ("start", "restart"):
            self.assertEqual(self.call(operation, "agent", success=False)["code"], "obsolete_config")
        args = cli.parser().parse_args(["--state-dir", str(self.root), "--unit-dir", str(self.units), "inference", "configure", "agent"])
        tty = Mock()
        tty.isatty.return_value = True
        with patch.multiple(cli.sys, stdin=tty, stdout=tty, stderr=tty), self.assertRaises(Error) as error:
            cli.execute(args)
        self.assertEqual(error.exception.code, "obsolete_config")
        self.assertTrue(FakeSystemd.active)
        self.assertEqual(self.call("stop", "agent")["desired"], "stopped")
        self.assertFalse(FakeSystemd.active)
        repaired = self.call("configure", "agent", "--config", str(self.config_path))
        self.assertEqual(repaired["identity"], identity)
        self.assertEqual(repaired["configuration"]["provider"], "goose-managed")
        self.assertFalse(FakeSystemd.active)
        old["desired"] = "stopped"
        store.save(old)
        self.assertTrue(self.call("delete", "agent")["data_retained"])
        self.assertTrue((self.root / "agents/agent/identity.key").exists())

    def test_bad_schema_and_denied_file_no_changes(self):
        for mutate in (lambda c: c.update(private_key="SYNTHETIC-SECRET-DO-NOT-RETURN"),
                       lambda c: c.update(relay_url="ws://user:SYNTHETIC-SECRET-DO-NOT-RETURN@localhost"),
                       lambda c: c["provider"].update(kind="arbitrary-plugin"),
                       lambda c: c["harness"].update(command="relative"),
                       lambda c: c.update(owner="not-a-public-key")):
            c = copy.deepcopy(self.c)
            mutate(c)
            self.write_config(c)
            self.assertEqual(self.call("create", "agent", "--config", str(self.config_path), success=False)["code"], "invalid_config")
        self.write_config(self.c)
        self.config_path.chmod(0o644)
        self.assertEqual(self.call("create", "agent", "--config", str(self.config_path), success=False)["code"], "invalid_config")
        self.assertFalse((self.root / "agents/agent").exists())

    def test_insecure_relay_is_only_allowed_on_literal_loopback(self):
        for url in ("ws://relay.example.com", "ws://localhost", "ws://127.0.0.1@relay.example.com", "ws://[::1]:0", "ws://[::1%25eth0]", " ws://127.0.0.1"):
            with self.subTest(url=url):
                self.write_config(dict(self.c, relay_url=url))
                with self.assertRaises(Error) as result:
                    config.load(self.config_path)
                self.assertEqual(result.exception.code, "invalid_config")
        for url in ("ws://127.0.0.2:9000", "ws://[::1]:9000", "wss://relay.example.com"):
            with self.subTest(url=url):
                self.write_config(dict(self.c, relay_url=url))
                self.assertEqual(config.load(self.config_path)["relay_url"], url)

    def test_symlink_and_world_readable_state_denied(self):
        self.create()
        definition = self.root / "agents/agent/definition.json"
        backup = self.base / "backup"
        definition.rename(backup)
        definition.symlink_to(backup)
        self.call("status", "agent", success=False)
        definition.unlink()
        backup.rename(definition)
        self.root.chmod(0o755)
        self.call("list", success=False)

    def test_lifecycle_logs_whitelist_and_corruption(self):
        self.create()
        event(self.root / "agents/agent", "worker_starting")
        self.assertEqual(self.call("logs", "agent")["events"][0]["event"], "worker_starting")
        atomic(self.root / "agents/agent/events.jsonl", b'{"time":1,"event":"SYNTHETIC-SECRET-DO-NOT-RETURN"}\n')
        self.call("logs", "agent", success=False)

    def test_unsafe_identifiers_root_and_unknown_arguments(self):
        self.call("start", "../other", success=False)
        self.call("create", "agent", "--private-key", "SYNTHETIC-SECRET-DO-NOT-RETURN", success=False)
        with patch("buzz_server.cli.os.getuid", return_value=0):
            self.assertEqual(self.call("list", success=False)["code"], "root_denied")

    def test_unavailable_manager_is_actionable_and_does_not_create(self):
        with patch.object(FakeSystemd, "check", side_effect=Error("supervisor_unavailable", "Check the user manager.")):
            self.assertEqual(self.call("create", "agent", "--config", str(self.config_path), success=False)["code"], "supervisor_unavailable")
        self.assertFalse((self.root / "agents/agent").exists())


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="buzz-runtime-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def test_known_secp256k1_identity_and_new_identity(self):
        self.assertEqual(public("0" * 63 + "1"), "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798")
        key, identity = generate()
        self.assertEqual(public(key), identity)
        for invalid in ("0" * 64, "z" * 64, "f" * 64):
            with self.assertRaises(Error):
                public(invalid)

    def test_systemd_error_suppresses_subprocess_output(self):
        store = Store(self.base / "state")
        supervisor = Systemd(store, self.base / "units")
        p = subprocess.CompletedProcess([], 1, b"", b"SYNTHETIC-SECRET")
        with patch("buzz_server.supervisor.subprocess.run", return_value=p):
            with self.assertRaises(Error) as result:
                supervisor.check()
        self.assertNotIn("SYNTHETIC-SECRET", str(result.exception))

    def test_systemd_refuses_foreign_unit_and_unverified_stop(self):
        supervisor = Systemd(Store(self.base / "state"), self.base / "units")
        with patch.object(supervisor, "call", return_value="LoadState=loaded\nFragmentPath=/foreign\n"):
            with self.assertRaises(Error):
                supervisor.inspect("agent")
        with patch.object(supervisor, "call", return_value=""), patch.object(supervisor, "inspect", return_value={"ActiveState": "active", "MainPID": "123"}):
            atomic(supervisor.unit_dir / supervisor.name("agent"), supervisor.content("agent"))
            with self.assertRaisesRegex(Error, "has not stopped"):
                supervisor.stop("agent")

    def test_stop_refuses_to_mutate_an_unverified_unit(self):
        supervisor = Systemd(Store(self.base / "state"), self.base / "units")
        atomic(supervisor.unit_dir / supervisor.name("agent"), b"unmanaged")
        with patch.object(supervisor, "inspect"), patch.object(supervisor, "call") as call:
            with self.assertRaisesRegex(Error, "modified unit"):
                supervisor.stop("agent")
            call.assert_not_called()

    def test_owned_unit_from_another_installation_can_be_stopped_and_refreshed(self):
        supervisor = Systemd(Store(self.base / "state"), self.base / "units")
        path = supervisor.unit_dir / supervisor.name("agent")
        with patch("buzz_server.supervisor.sys.executable", "/old prefix/bin/python3"):
            old = supervisor.content("agent")
        atomic(path, old)
        supervisor.unit_dir.chmod(0o755)
        state = {"ActiveState": "inactive", "MainPID": "0", "UnitFileState": "disabled", "ControlGroup": ""}
        with patch.object(supervisor, "inspect", return_value=state), patch.object(supervisor, "call") as call:
            supervisor.stop("agent")
            self.assertEqual(read_private(path), old)
            self.assertEqual([entry.args[0] for entry in call.call_args_list], ["disable", "daemon-reload", "stop"])
            supervisor.install("agent")
            self.assertEqual(read_private(path), supervisor.content("agent"))
        self.assertEqual(supervisor.unit_dir.stat().st_mode & 0o777, 0o755)

    def test_modified_or_wrong_state_unit_cannot_be_refreshed(self):
        supervisor = Systemd(Store(self.base / "state"), self.base / "units")
        foreign = Systemd(Store(self.base / "foreign"), supervisor.unit_dir)
        path = supervisor.unit_dir / supervisor.name("agent")
        for old in (supervisor.content("agent") + b"Also=foreign.service\n", foreign.content("agent"), supervisor.content("other")):
            atomic(path, old)
            with patch.object(supervisor, "call") as call:
                with self.assertRaisesRegex(Error, "modified unit"):
                    supervisor.install("agent")
                call.assert_not_called()
            self.assertEqual(read_private(path), old)

    def test_original_draft_unit_can_be_stopped_without_old_installation(self):
        supervisor = Systemd(Store(self.base / "state"), self.base / "units")
        path = supervisor.unit_dir / supervisor.name("agent")
        old = ('[Unit]\nDescription=Buzz Server managed agent\nStartLimitIntervalSec=30\nStartLimitBurst=3\n'
               f'[Service]\nType=exec\nExecStart=:"/no-such-install/python" "-I" "/old prefix/entry.py" "_worker" "--state-dir" "{supervisor.store.root}" "agent"\n'
               'UMask=0077\nNoNewPrivileges=yes\nKillMode=control-group\nTimeoutStopSec=10\nRestart=on-failure\nRestartSec=2\n'
               'StandardOutput=null\nStandardError=null\n[Install]\nWantedBy=default.target\n').encode()
        atomic(path, old)
        state = {"ActiveState": "inactive", "MainPID": "0", "UnitFileState": "disabled", "ControlGroup": ""}
        with patch.object(supervisor, "inspect", return_value=state), patch.object(supervisor, "call") as call:
            supervisor.stop("agent")
            self.assertEqual([entry.args[0] for entry in call.call_args_list], ["disable", "daemon-reload", "stop"])
            supervisor.install("agent")
        self.assertEqual(read_private(path), supervisor.content("agent"))

    def test_refuses_unverified_manager_configuration(self):
        supervisor = Systemd(Store(self.base / "state"), self.base / "units")
        state = {"LoadState": "loaded", "FragmentPath": str(supervisor.unit_dir / supervisor.name("agent")),
                 "ActiveState": "active", "MainPID": "123", "DropInPaths": "", "NeedDaemonReload": "no",
                 "KillMode": "control-group", "ControlGroup": "/fixture"}
        for key, value in (("DropInPaths", "/fixture.conf"), ("NeedDaemonReload", "yes"), ("KillMode", "process")):
            result = dict(state, **{key: value})
            output = "\n".join(f"{k}={v}" for k, v in result.items())
            with self.subTest(key=key), patch.object(supervisor, "call", return_value=output) as call:
                with self.assertRaises(Error) as error:
                    supervisor.start("agent", lambda _: True)
                self.assertEqual(error.exception.code, "unit_conflict")
                self.assertEqual([entry.args[0] for entry in call.call_args_list], ["show"])

    def test_delete_refuses_remaining_cgroup_tasks_even_without_main_pid(self):
        supervisor = Systemd(Store(self.base / "state"), self.base / "units")
        unit = supervisor.unit_dir / supervisor.name("agent")
        atomic(unit, supervisor.content("agent"))
        root = self.base / "cgroups"
        events = root / "fixture/cgroup.events"
        atomic(events, b"populated 1\nfrozen 0\n")
        state = {"ActiveState": "inactive", "MainPID": "0", "ControlGroup": "/fixture"}
        with patch.object(supervisor, "inspect", return_value=state), patch("buzz_server.supervisor.CGROUP_ROOT", root):
            with self.assertRaises(Error) as error:
                supervisor.remove("agent")
            self.assertEqual(error.exception.code, "stop_unverified")
            self.assertTrue(unit.is_file())
            atomic(events, b"populated 0\nfrozen 0\n")
            self.assertTrue(supervisor.stopped("agent"))

    def test_worker_honors_persisted_stop_without_running_runtime(self):
        store = Store(self.base / "state")
        with patch.object(store, "load", return_value={"desired": "stopped"}), patch("buzz_server.runtime.prepare") as prep:
            self.assertEqual(worker(store, "agent"), 0)
            prep.assert_not_called()

    def test_runtime_launch_signal_stops_child_and_profile_has_no_provider_key(self):
        store = Store(self.base / "state")
        private_dir(store.directory("agent"))
        shared_env = {key: "fixture" for key in ("PATH", "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "LANG", "BUZZ_PRIVATE_KEY")}
        env = dict(shared_env, OPENAI_API_KEY="synthetic-provider-key", ANTHROPIC_API_KEY="synthetic-second-key", GOOSE_PROVIDER="openai", GOOSE_MODEL="synthetic-model")
        profiled = Mock(returncode=0)
        profiled.communicate.return_value = (b'{"accepted":true}', None)
        runtime = Mock()
        runtime.poll.return_value = None
        signals = {}

        def launch(command, **_):
            if command == ["profile"]:
                return profiled
            signals[signal.SIGTERM](signal.SIGTERM, None)
            return runtime

        doc = {"desired": "running", "revision": 1, "config": {"provider": {"kind": "goose-managed"}}}
        with patch.object(store, "load", return_value=doc), patch(
            "buzz_server.runtime.prepare", return_value=(["runtime"], ["profile"], env, self.base)
        ), patch("buzz_server.runtime.signal.signal", side_effect=signals.__setitem__), patch(
            "buzz_server.runtime.subprocess.Popen", side_effect=launch
        ) as popen:
            self.assertEqual(worker(store, "agent"), 0)
        self.assertEqual(popen.call_args_list[0].kwargs["env"], shared_env)
        self.assertEqual(popen.call_args_list[1].kwargs["env"], env)
        runtime.terminate.assert_called_once()
        self.assertFalse((store.directory("agent") / "ready.json").exists())

    def test_start_resets_only_failed_units(self):
        supervisor = Systemd(Store(self.base / "state"), self.base / "units")
        atomic(supervisor.unit_dir / supervisor.name("agent"), supervisor.content("agent"))
        ready = {"ActiveState": "active", "UnitFileState": "enabled"}
        for state in ("inactive", "failed"):
            with self.subTest(state=state), patch.object(supervisor, "call") as call, patch.object(
                supervisor, "inspect", side_effect=[{"ActiveState": state}, {"ActiveState": state}, ready]
            ):
                supervisor.start("agent", lambda status: status == ready)
                expected = (["reset-failed"] if state == "failed" else []) + ["enable", "daemon-reload", "start"]
                self.assertEqual([entry.args[0] for entry in call.call_args_list], expected)

    def test_unit_arguments_cannot_expand_specifiers_or_shell(self):
        self.assertEqual(quote('a%h"\\b'), '"a%%h\\"\\\\b"')
        supervisor = Systemd(Store(self.base / "state"), self.base / "units")
        unit = supervisor.content("agent").decode()
        for invariant in ('ExecStart=:"', "KillMode=control-group", "NoNewPrivileges=yes", "StandardOutput=null", "Restart=on-failure"):
            self.assertIn(invariant, unit)
        self.assertIn("StartLimitIntervalSec=0\n", unit)
        self.assertIn("RestartSec=5\n", unit)
        self.assertNotIn("User=root", unit)

    def test_profile_acceptance_requires_explicit_true(self):
        self.assertTrue(profile_accepted(b'{"accepted":true}'))
        for output in (b'{"accepted":false}', b'{"accepted":"true"}', b'{}', b'[]', b'not JSON'):
            self.assertFalse(profile_accepted(output))


if __name__ == "__main__":
    unittest.main()
