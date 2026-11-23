import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from buzz_server.account_supervisor import AccountSystemd
from buzz_server.errors import Error
from buzz_server.storage import read_json, write_json


class FakeCommands:
    """Deterministic files/manager model; no host file or service operations."""
    def __init__(self, root):
        self.binding = SimpleNamespace(id="assistant", uid=20001, gid=20001, home="/home/fixture",
                                       data="/home/fixture/.local/share/buzz-server-managed/test/assistant",
                                       service="buzz-server-account-test-assistant.service")
        self.bindings = SimpleNamespace(root=root, revalidate=Mock())
        self.unit_path = Path("/etc/systemd/system") / self.binding.service
        self.stage_path = self.unit_path.with_name("." + self.binding.service + ".stage")
        self.files = {}
        self.effects = []
        self.loaded = False
        self.enabled = False
        self.active = False
        self.overrides = {}
        self.fail = None

    def effect(self, operation):
        self.effects.append(operation)
        if self.fail == operation:
            raise Error("synthetic", "Synthetic interruption")

    def show(self, properties):
        if not self.loaded:
            return b"LoadState=not-found\nDropInPaths=\nFragmentPath=\nActiveState=inactive\nMainPID=0\nControlGroup=\n"
        s = {p: "" for p in properties}
        s.update(Id=self.binding.service, LoadState="loaded", ActiveState="active" if self.active else "inactive",
                 SubState="running" if self.active else "dead", MainPID="23" if self.active else "0",
                 FragmentPath=str(self.unit_path), UnitFileState="enabled" if self.enabled else "disabled",
                 Result="success", NeedDaemonReload="no", KillMode="control-group", User=str(self.binding.uid),
                 Group=str(self.binding.gid), WorkingDirectory="/", NoNewPrivileges="yes",
                 Environment="PATH=/usr/bin:/bin LANG=C.UTF-8 HOME=" + self.binding.home,
                 ExecStart="{ path=/usr/bin/python3 ; argv[]=" + " ".join(self.supervisor.argv) +
                           " ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }")
        s.update(self.overrides)
        return ("\n".join(k + "=" + v for k, v in s.items()) + "\n").encode()

    def reload(self):
        self.effect("reload")
        self.loaded = self.unit_path in self.files

    def control(self, operation):
        self.effect(operation)
        if operation == "enable":
            self.enabled = True
        if operation == "disable":
            self.enabled = False
        if operation == "start":
            self.active = True
        if operation == "stop":
            self.active = False

    def stage_unit(self, source):
        self.effect("stage")
        self.files[self.stage_path] = source.read_bytes()

    def publish_unit(self):
        self.effect("publish")
        if self.unit_path in self.files:
            raise Error("collision", "Synthetic collision")
        self.files[self.unit_path] = self.files[self.stage_path]

    def unlink_unit(self, stage=False):
        self.effect("unlink-stage" if stage else "unlink-unit")
        del self.files[self.stage_path if stage else self.unit_path]


class AccountSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="buzz-account-manager-")
        self.addCleanup(self.temp.cleanup)
        self.commands = FakeCommands(Path(self.temp.name))
        self.supervisor = AccountSystemd(self.commands)
        self.commands.supervisor = self.supervisor

        def root_file(path, exact=True, missing=False):
            if path not in self.commands.files:
                if missing:
                    return False
                raise Error("unit_conflict", "Synthetic missing root unit")
            if exact and self.commands.files[path] != self.supervisor.content():
                raise Error("unit_conflict", "Synthetic changed root unit")
            return True

        patcher = patch.object(self.supervisor, "root_file", side_effect=root_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def install(self):
        self.supervisor.install()
        self.commands.effects.clear()

    def test_generated_unit_has_only_fixed_target_execution_and_no_hooks(self):
        content = self.supervisor.content().decode()
        self.assertIn("User=20001\nGroup=20001\n", content)
        self.assertIn("ExecStart=:/usr/bin/python3 -I /home/fixture/", content)
        self.assertIn("WantedBy=multi-user.target", content)
        for forbidden in ("ExecStartPre", "ExecStartPost", "ExecStop", "sudo", "entry.py", "buzz-server _worker", "+/", "!/", "EnvironmentFile"):
            self.assertNotIn(forbidden, content)

    def test_boolean_journal_version_cannot_authorize_retry_effect(self):
        for operation, action in (("install", self.supervisor.install), ("remove", self.supervisor.remove)):
            write_json(self.supervisor.journal, {"version": True, "operation": operation})
            with self.assertRaises(Error):
                action()
        self.assertEqual(self.commands.effects, [])

    def test_missing_fragment_with_live_or_unknown_process_state_blocks_retry(self):
        self.install()
        self.supervisor.stop()
        self.commands.fail = "reload"
        with self.assertRaises(Error):
            self.supervisor.remove()
        self.commands.fail = None
        self.commands.effects.clear()
        for extra in ("ActiveState=active\nMainPID=23\nControlGroup=\n",
                      "ActiveState=inactive\nControlGroup=\n",
                      "ActiveState=inactive\nMainPID=0\nControlGroup=/synthetic\n"):
            with patch.object(self.commands, "show", return_value=("LoadState=not-found\nDropInPaths=\nFragmentPath=\n" + extra).encode()), patch("buzz_server.account_supervisor.cgroup_empty", return_value=False):
                with self.assertRaises(Error):
                    self.supervisor.remove()
            self.assertEqual(self.commands.effects, [])

    def test_publish_is_staged_and_retry_cleans_journaled_stage(self):
        self.commands.fail = "publish"
        with self.assertRaises(Error):
            self.supervisor.install()
        self.assertEqual(read_json(self.supervisor.journal), {"version": 1, "operation": "install"})
        self.assertNotIn(self.commands.unit_path, self.commands.files)
        self.assertIn(self.commands.stage_path, self.commands.files)
        self.commands.fail = None
        self.supervisor.install()
        self.assertIn("unlink-stage", self.commands.effects)
        self.assertEqual(set(self.commands.files), {self.commands.unit_path})
        self.assertFalse(self.supervisor.journal.exists())
        self.supervisor.inspect()

    def test_published_unit_retry_finishes_reload_and_stage_cleanup(self):
        self.commands.fail = "reload"
        with self.assertRaises(Error):
            self.supervisor.install()
        self.commands.fail = None
        self.supervisor.install()
        self.assertEqual(set(self.commands.files), {self.commands.unit_path})
        self.assertFalse(self.supervisor.journal.exists())

    def test_unjournaled_stage_and_wrong_unit_refuse_without_effects(self):
        for path in (self.commands.unit_path, self.commands.stage_path):
            self.commands.files = {path: b"unrelated"}
            with self.assertRaises(Error):
                self.supervisor.install()
            self.assertEqual(self.commands.effects, [])

    def test_manager_execution_identity_fragment_and_hooks_gate_all_effects(self):
        self.install()
        original = self.commands.show(self.supervisor.PROPERTIES).decode()
        execution = next(line.split("=", 1)[1] for line in original.splitlines() if line.startswith("ExecStart="))
        for key, value in (("User", "0"), ("Group", "0"), ("FragmentPath", "/unrelated"),
                           ("DropInPaths", "/etc/systemd/system/service.d/override.conf"),
                           ("NeedDaemonReload", "yes"), ("ExecStart", execution.replace(" -I ", " -E ")),
                           ("ExecStartPre", "/untrusted"), ("EnvironmentFiles", "/home/fixture/env"),
                           ("NoNewPrivileges", "no"), ("KillMode", "process"), ("RootDirectory", "/home/fixture")):
            self.commands.overrides = {key: value}
            for action in (self.supervisor.start, self.supervisor.stop, self.supervisor.remove):
                with self.assertRaises(Error, msg=key):
                    action()
            self.assertEqual(self.commands.effects, [], key)

    def test_nonempty_or_unverifiable_cgroup_blocks_removal(self):
        self.install()
        self.commands.overrides = {"ControlGroup": "/synthetic"}
        with patch("buzz_server.account_supervisor.cgroup_empty", return_value=False):
            with self.assertRaises(Error):
                self.supervisor.remove()
        self.assertEqual(self.commands.effects, [])
        self.assertIn(self.commands.unit_path, self.commands.files)

    def test_stop_then_remove_and_interrupted_removal_retry(self):
        self.install()
        self.supervisor.start()
        self.assertTrue(self.commands.active)
        with self.assertRaises(Error):
            self.supervisor.remove()
        self.supervisor.stop()
        self.assertFalse(self.commands.enabled)
        self.commands.fail = "reload"
        with self.assertRaises(Error):
            self.supervisor.remove()
        self.assertNotIn(self.commands.unit_path, self.commands.files)
        self.assertTrue(self.supervisor.journal.exists())
        self.commands.fail = None
        self.commands.effects.clear()
        for override in ({"User": "0"}, {"DropInPaths": "/synthetic/override.conf"},
                         {"ExecStartPre": "/untrusted"}):
            self.commands.overrides = override
            with self.assertRaises(Error):
                self.supervisor.remove()
            self.assertEqual(self.commands.effects, [])
        self.commands.overrides = {"NeedDaemonReload": "yes", "UnitFileState": ""}
        self.supervisor.remove()
        self.assertEqual(read_json(self.supervisor.journal), {"version": 1, "operation": "remove"})


if __name__ == "__main__":
    unittest.main()
