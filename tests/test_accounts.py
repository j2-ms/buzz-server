"""Synthetic account/authority tests: never invoke sudo or a host manager."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import stat
import tempfile
import unittest
from unittest.mock import patch

from buzz_server.accounts import Bindings
from buzz_server.account_commands import CLEAN_ENV, Commands
from buzz_server.errors import Error
from buzz_server.storage import Store, atomic


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="buzz-account-")
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "state")
        self.bindings = Bindings(self.store)
        self.entry = SimpleNamespace(pw_name="fixture", pw_uid=os.getuid() + 10000,
                                     pw_gid=os.getgid() + 10000, pw_dir="/home/fixture")
        self.lookup = patch("buzz_server.accounts.pwd.getpwnam", return_value=self.entry).start()
        self.reverse = patch("buzz_server.accounts.pwd.getpwuid", return_value=self.entry).start()
        self.addCleanup(patch.stopall)
        original = Path.lstat

        def layout(path):
            if str(path) in ("/home/fixture", "/home", "/"):
                return SimpleNamespace(st_mode=stat.S_IFDIR | (0o700 if str(path) == "/home/fixture" else 0o755),
                                       st_uid=self.entry.pw_uid if str(path) == "/home/fixture" else 0)
            return original(path)

        patch("buzz_server.accounts.Path.lstat", layout).start()
        self.run = patch("buzz_server.account_commands.run",
                         return_value=SimpleNamespace(returncode=0, stdout=b"synthetic")).start()

    def reserve(self):
        return self.bindings.reserve("assistant", "fixture")

    def test_private_binding_is_deterministic_and_reserves_id(self):
        binding = self.reserve()
        self.assertEqual(self.bindings.load("assistant"), binding)
        self.assertRegex(binding.service, r"^buzz-server-account-[0-9a-f]{24}-assistant.service$")
        self.assertTrue(binding.data.startswith("/home/fixture/.local/share/buzz-server-managed/"))
        self.assertEqual(self.bindings.path("assistant").stat().st_mode & 0o777, 0o600)
        with self.assertRaises(Error):
            self.reserve()
        self.run.assert_not_called()

    def test_every_command_revalidates_account_before_effect(self):
        binding = self.reserve()
        command = Commands(self.bindings, binding)
        self.entry = SimpleNamespace(**{**self.entry.__dict__, "pw_uid": self.entry.pw_uid + 1})
        self.lookup.return_value = self.reverse.return_value = self.entry
        for effect in (lambda: command.control("stop"), command.reload,
                       lambda: command.show(["User"]), lambda: command.target("pass")):
            with self.assertRaises(Error):
                effect()
        self.run.assert_not_called()

    def test_root_operator_remap_and_missing_account_refuse(self):
        for uid in (0, os.getuid()):
            self.lookup.return_value = SimpleNamespace(**{**self.entry.__dict__, "pw_uid": uid})
            with self.assertRaises(Error):
                self.reserve()
        self.lookup.side_effect = KeyError
        with self.assertRaises(Error):
            self.reserve()
        self.run.assert_not_called()

    def test_corrupt_binding_cannot_authorize_effect(self):
        binding = self.reserve()
        path = self.bindings.path(binding.id)
        original = binding.document()
        for field, value in (("service", "sshd.service"), ("data", "/home/unrelated"),
                             ("uid", True), ("home", "/root"), ("version", True)):
            atomic(path, json.dumps({**original, field: value}).encode())
            with self.assertRaises(Error):
                Commands(self.bindings, binding).control("start")
        path.unlink()
        with self.assertRaises(Error):
            Commands(self.bindings, binding).control("stop")
        self.run.assert_not_called()

    def test_untrusted_operator_state_ancestor_cannot_authorize_root_file_access(self):
        binding = self.reserve()
        original = Path.lstat
        parent = self.store.root.parent
        # Keep the bad directory on the actual binding path, regardless of
        # where TMPDIR lives. Model trusted, traversable ancestors so a private
        # host home cannot shield the deliberately writable fixture directory.
        ancestors = set(parent.parents)
        for owner, mode in ((self.entry.pw_uid, 0o700), (os.getuid(), 0o777)):
            def replaced(path):
                if path == parent:
                    return SimpleNamespace(st_uid=owner, st_mode=stat.S_IFDIR | mode)
                if path in ancestors:
                    return SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o755)
                return original(path)
            with self.subTest(owner=owner, mode=mode), patch("buzz_server.accounts.Path.lstat", replaced):
                with self.assertRaises(Error):
                    Commands(self.bindings, binding).stage_unit(self.bindings.root / "assistant.unit")
        self.run.assert_not_called()

    def test_fixed_root_argv_and_clean_environment(self):
        binding = self.reserve()
        command = Commands(self.bindings, binding)
        command.control("stop")
        args, kwargs = self.run.call_args
        self.assertEqual(args[0], ["/usr/bin/sudo", "--", "/usr/bin/systemctl", "--system", "stop", binding.service])
        self.assertEqual(kwargs["env"], CLEAN_ENV)
        self.assertEqual(kwargs["cwd"], "/")
        self.assertEqual(kwargs["umask"], 0o077)
        for operation in ("edit", "link", "start sshd", "daemon-reexec"):
            with self.assertRaises(ValueError):
                command.control(operation)
        self.assertEqual(self.run.call_count, 1)

    def test_target_python_runs_only_after_numeric_uid_transition(self):
        binding = self.reserve()
        Commands(self.bindings, binding).target("pass", input=b"synthetic")
        args, kwargs = self.run.call_args
        self.assertEqual(args[0], ["/usr/bin/sudo", "--user=#" + str(binding.uid),
                                  "--group=#" + str(binding.gid), "--", "/usr/bin/env", "-i",
                                  "PATH=/usr/bin:/bin", "LANG=C.UTF-8", "HOME=/home/fixture",
                                  "/usr/bin/python3", "-I", "-c", "pass"])
        self.assertEqual(kwargs["input"], b"synthetic")
        self.assertEqual(kwargs["env"], CLEAN_ENV)


if __name__ == "__main__":
    unittest.main()
